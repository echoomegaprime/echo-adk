"""OpenClaw channel adapter for echoBots, plus approval notifications.

Decision (Commander asked "Should OpenClaw be integrated anywhere?", 2026-11-05): OpenClaw is a
**transport, not a runtime**. echoBots keep running inside echo-adk with their own Lease, memory
scope and daily cap; OpenClaw only carries chat messages in and out.

* A bot maps to chat handles: ``channels: [{kind: "openclaw", target: "<channel>:<dest>"}]``
  (``telegram:123456789``, ``telegram:-1001234567890:topic:7``, ``discord:channel:1556...``).
* Inbound chat messages (``POST /channels/openclaw/inbound``, forwarded by the gateway-side bridge
  in ``deploy/openclaw-hook``) become bot turns in-process, on the ``POST /bots/{id}/message`` path.
* Bot replies go out through ``openclaw message send``. The OpenClaw CLI resolves its own channel
  credentials; echo-adk never reads, holds or forwards an OpenClaw token. The gateway's documented
  local APIs (WebSocket RPC ``send``, HTTP ``/tools/invoke``) both need the gateway bearer token, so
  the CLI is the token-free path.
* Health-gated: the gateway is probed over loopback HTTP (``/healthz``, ``/readyz``, unauthenticated)
  and its log is scanned for the three known wedge modes, which keep both probes at 200. The CLI is
  never used as a probe (each call opens the gateway state DB and contends with a stalled gateway).
  A wedged or unreachable gateway never stops a bot: the channel reports ``unavailable: <reason>``
  on ``GET /bots/{id}`` and the reply is dropped visibly (``last_error``), with no retry loop.
* Approvals: gated runs, bot errors, daily-cap hits and finished group chats push one notification
  (Telegram first). Gated runs and errors carry Approve/Deny callback buttons; Approve mints a Lease
  for **one** re-run of that turn. Default deny (no answer before the TTL = denied), single use, no
  always-allow, the bot's Lease template is never widened, tier >= 2 grants are never minted from
  chat, and the message carries only an approval reference (no token).
"""

from __future__ import annotations

import ipaddress
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Protocol

from ..core.log import get_logger
from ..core.redact import redact
from ..core.types import BUDGET_CAPS, MAX_PROMPT_CHARS, ValidationError, is_id, new_id, now_iso, parse_iso

if TYPE_CHECKING:  # pragma: no cover
    from ..api.service import AdkService

log = get_logger("openclaw")

KIND = "openclaw"
# Channels `openclaw message send` supports (OpenClaw 2026.9.6 docs/cli/message.md).
CHANNELS = ("telegram", "discord", "slack", "signal", "whatsapp", "matrix", "msteams", "googlechat", "imessage", "mattermost")
PREFERENCE = ("telegram",)  # "Telegram first" for notifications when no notify target is configured
MAX_CHANNELS = 4
MAX_ALLOW = 16
TEXT_LIMITS = {"telegram": 4000, "discord": 1900}
DEFAULT_TEXT_LIMIT = 3500
DEFAULT_GATEWAY_URL = "http://127.0.0.1:18789"
DEFAULT_APPROVAL_TTL_S = 1800
MIN_APPROVAL_TTL_S, MAX_APPROVAL_TTL_S = 60, 86400
DEFAULT_WEDGE_WINDOW_S = 900
OUTBOUND_QUEUE = 256
MAX_LIST_APPROVALS = 200
APPROVAL_STATUSES = ("pending", "approved", "denied", "expired", "notice")
APPROVAL_KINDS = ("gated", "error", "daily_cap", "group_done")

# A destination is one argv token: no whitespace, quotes, backslashes or shell metacharacters.
# Telegram group ids start with "-"; every value is passed as ``--flag=value`` so that is safe.
_DEST = re.compile(r"^[A-Za-z0-9@:_.+#!=/-]{1,160}$")
_SENDER = re.compile(r"^[A-Za-z0-9@:_.+=/-]{1,128}$")
_MESSAGE_ID = re.compile(r"^[A-Za-z0-9:_.+=/-]{1,128}$")
# Callback data is "echoadk:<approve|deny>:<approval_id>" (48 bytes; Telegram allows 64). Unclaimed
# Telegram callbacks reach OpenClaw as the text "callback_data: <value>", so both forms parse, and a
# plain "approve <id>" / "deny <id>" reply works on channels without buttons.
_DECISION = re.compile(r"^\s*(?:callback_data:\s*)?echoadk:(approve|deny):([0-9a-f]{32})\s*$")
_DECISION_TEXT = re.compile(r"^\s*/?(approve|deny)\s+([0-9a-f]{32})\s*$", re.IGNORECASE)


class ChannelError(ValidationError):
    """Invalid channel spec or inbound body (HTTP 400 ``invalid``)."""


# --------------------------------------------------------------------------- channel specs


def parse_target(value: Any) -> tuple[str, str]:
    """``"telegram:123"`` -> ``("telegram", "123")``. The channel prefix is required."""
    if not isinstance(value, str) or ":" not in value:
        raise ChannelError("target must be '<channel>:<destination>', e.g. telegram:123456789")
    channel, dest = value.split(":", 1)
    channel = channel.strip().lower()
    if channel not in CHANNELS:
        raise ChannelError(f"target channel {channel!r} is not one of {list(CHANNELS)}")
    if not _DEST.match(dest):
        raise ChannelError("target destination must be 1..160 characters from [A-Za-z0-9@:_.+#!=/-] (no spaces or quotes)")
    return channel, dest


def _id_list(value: Any, what: str) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_ALLOW:
        raise ChannelError(f"{what} must be a list of at most {MAX_ALLOW} sender ids")
    out = []
    for item in value:
        if not isinstance(item, str) or not _SENDER.match(item):
            raise ChannelError(f"{what} entries must be sender ids from [A-Za-z0-9@:_.+=/-]")
        out.append(item)
    return list(dict.fromkeys(out))


def validate_channels(value: Any) -> list[dict[str, Any]]:
    """Validate a bot's ``channels`` list; returns normalized specs (target lower-cased channel prefix)."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_CHANNELS:
        raise ChannelError(f"channels must be a list of at most {MAX_CHANNELS} channel handles")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise ChannelError("each channel must be an object {kind, target, approvers?, allow_from?}")
        extra = set(item) - {"kind", "target", "approvers", "allow_from"}
        if extra:
            raise ChannelError(f"channel: unknown field(s) {sorted(extra)}")
        if item.get("kind") != KIND:
            raise ChannelError(f"channel kind must be {KIND!r}")
        channel, dest = parse_target(item.get("target"))
        target = f"{channel}:{dest}"
        if target in seen:
            raise ChannelError(f"channel target {target} is listed twice")
        seen.add(target)
        spec: dict[str, Any] = {"kind": KIND, "target": target}
        if "approvers" in item:
            spec["approvers"] = _id_list(item["approvers"], "approvers")
        if "allow_from" in item:
            spec["allow_from"] = _id_list(item["allow_from"], "allow_from")
        out.append(spec)
    return out


def _preferred(targets: Iterable[str]) -> list[str]:
    """Order targets Telegram first, keeping the given order otherwise."""
    items = list(targets)
    rank = {name: i for i, name in enumerate(PREFERENCE)}
    return sorted(items, key=lambda t: (rank.get(t.split(":", 1)[0], len(PREFERENCE)), items.index(t)))


# --------------------------------------------------------------------------- config


def validate_gateway_url(url: str) -> str:
    """Loopback ``http://`` origin only: OpenClaw binds loopback and echo-adk never crosses a network to reach it."""
    try:
        parsed = urllib.parse.urlsplit(url or "")
        parsed.port  # noqa: B018 - raises ValueError for a malformed port
    except ValueError:
        raise ChannelError("ECHO_ADK_OPENCLAW_URL is not a valid URL") from None
    if parsed.scheme != "http" or not parsed.hostname:
        raise ChannelError("ECHO_ADK_OPENCLAW_URL must be an http:// origin")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ChannelError("ECHO_ADK_OPENCLAW_URL must be a bare origin (no credentials, path or query)")
    host = parsed.hostname
    loopback = host == "localhost"
    if not loopback:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
    if not loopback:
        raise ChannelError("ECHO_ADK_OPENCLAW_URL must be a loopback origin (127.0.0.1, ::1 or localhost)")
    return f"http://{parsed.netloc}"


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def default_log_dir() -> Path:
    """The gateway writes ``openclaw-YYYY-MM-DD.log`` (local date) under ``<tmp>/openclaw``."""
    return Path(os.environ.get("TEMP") or os.environ.get("TMPDIR") or tempfile.gettempdir()) / "openclaw"


@dataclass(frozen=True)
class OpenClawConfig:
    enabled: bool = False
    cli: Optional[str] = None
    node: Optional[str] = None
    gateway_url: str = DEFAULT_GATEWAY_URL
    log_dir: Optional[str] = None
    notify_target: Optional[str] = None
    approvers: tuple[str, ...] = ()  # channel-qualified sender ids: "telegram:123456789"
    approval_ttl_s: int = DEFAULT_APPROVAL_TTL_S
    require_ready: bool = True
    health_ttl_s: float = 30.0
    wedge_window_s: int = DEFAULT_WEDGE_WINDOW_S
    send_timeout_s: float = 45.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "gateway_url", validate_gateway_url(self.gateway_url))
        if self.notify_target is not None:
            channel, dest = parse_target(self.notify_target)
            object.__setattr__(self, "notify_target", f"{channel}:{dest}")
        approvers = []
        for entry in self.approvers:
            channel, sender = parse_target(entry)
            if not _SENDER.match(sender):
                raise ChannelError("approver ids must be '<channel>:<sender id>'")
            approvers.append(f"{channel}:{sender}")
        object.__setattr__(self, "approvers", tuple(dict.fromkeys(approvers)))
        ttl = self.approval_ttl_s
        if isinstance(ttl, bool) or not isinstance(ttl, int) or not MIN_APPROVAL_TTL_S <= ttl <= MAX_APPROVAL_TTL_S:
            raise ChannelError(f"approval TTL must be an integer between {MIN_APPROVAL_TTL_S} and {MAX_APPROVAL_TTL_S} seconds")

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> OpenClawConfig:
        """``ECHO_ADK_OPENCLAW=1`` turns the adapter on (off by default, e.g. on FORGE where no gateway runs)."""
        source = os.environ if env is None else env
        ttl_raw = (source.get("ECHO_ADK_APPROVAL_TTL_S") or "").strip()
        try:
            ttl = int(ttl_raw) if ttl_raw else DEFAULT_APPROVAL_TTL_S
        except ValueError:
            raise ChannelError("ECHO_ADK_APPROVAL_TTL_S must be an integer") from None
        approvers = tuple(a.strip() for a in (source.get("ECHO_ADK_OPENCLAW_APPROVERS") or "").split(",") if a.strip())
        return cls(
            enabled=_truthy(source.get("ECHO_ADK_OPENCLAW")),
            cli=(source.get("ECHO_ADK_OPENCLAW_CLI") or "").strip() or None,
            node=(source.get("ECHO_ADK_OPENCLAW_NODE") or "").strip() or None,
            gateway_url=(source.get("ECHO_ADK_OPENCLAW_URL") or "").strip() or DEFAULT_GATEWAY_URL,
            log_dir=(source.get("ECHO_ADK_OPENCLAW_LOG_DIR") or "").strip() or None,
            notify_target=(source.get("ECHO_ADK_OPENCLAW_NOTIFY") or "").strip() or None,
            approvers=approvers,
            approval_ttl_s=ttl,
            require_ready=not _truthy(source.get("ECHO_ADK_OPENCLAW_SKIP_READYZ")),
        )


# --------------------------------------------------------------------------- health


@dataclass(frozen=True)
class Health:
    ok: bool
    reason: str = ""
    note: Optional[str] = None
    checked_at: float = 0.0

    def state(self) -> str:
        return "ready" if self.ok else f"unavailable: {self.reason}"


# Wedge signatures, from SYSTEMS/quench_resource_guard/guard_core.py in the QUENCH workspace
# (fleet #49594, #49596, #49650; OpenClaw 2026.9.6). In all three modes /healthz and /readyz stay 200.
NOT_PUBLISHED_KINDS = (
    "reply dispatch runtime owner was not published",
    "model runtime owner was not published",
    "model runtime plugin generation was superseded",
    "model runtime owner was not committed after replacement",
    "model runtime lease admission made no publication progress",
    "read-only model runtime owner was not published",
    "inbound plugin registry was not published",
)
_NOT_PUBLISHED = re.compile(r"prepared (" + "|".join(re.escape(k) for k in NOT_PUBLISHED_KINDS) + r") for ")
_TURN_CLAIM = re.compile(r"Session ([0-9A-Za-z-]{8,64}) already has an active turn claim")
_LIFECYCLE_OWNED = "another OpenClaw process owns state-lifecycle"
_LIFECYCLE_RUN = re.compile(
    r"agent participant persistence failed: StateDatabaseCoordinatorContentionError: "
    + re.escape(_LIFECYCLE_OWNED)
    + r"|lane task error: lane=(?:main|session:agent:[A-Za-z0-9_.:-]+) durationMs=\d+ error=\\?\""
    + re.escape(_LIFECYCLE_OWNED)
    + r"|res \S+ agent errorCode=UNAVAILABLE errorMessage="
    + re.escape(_LIFECYCLE_OWNED)
)


def _line_epoch(line: str) -> Optional[float]:
    """The instant of one OpenClaw JSON log line; lines without a UTC offset are skipped, never guessed."""
    try:
        record = json.loads(line)
        stamp = str(record.get("time") or (record.get("_meta") or {}).get("date"))
        if stamp.endswith("Z"):
            stamp = stamp[:-1] + "+00:00"
        moment = datetime.fromisoformat(stamp)
    except (ValueError, TypeError, AttributeError):
        return None
    return moment.timestamp() if moment.tzinfo is not None else None


def _bursts(stamps: Iterable[float], gap_s: float) -> int:
    """Count events, folding lines closer than ``gap_s`` to the last kept one (one failure logs several lines)."""
    count, last = 0, None
    for ts in sorted(stamps):
        if last is not None and ts - last < gap_s:
            continue
        count, last = count + 1, ts
    return count


def scan_wedges(lines: Iterable[str], now: float, window_s: float = DEFAULT_WEDGE_WINDOW_S) -> Optional[str]:
    """The wedge reason when the window holds one, else ``None``.

    Thresholds follow the QUENCH guard's warning levels: one "not published" failure, two refused turns
    on one session (one conflict can be a genuine concurrent turn), two runs refused by a held
    state-lifecycle lock (background maintenance contention does not match the run patterns).
    """
    not_published: list[float] = []
    last_kind = ""
    claims: dict[str, list[float]] = {}
    lifecycle: list[float] = []
    for line in lines:
        published = _NOT_PUBLISHED.search(line)
        claim = _TURN_CLAIM.search(line) if not published else None
        held = _LIFECYCLE_OWNED in line and bool(_LIFECYCLE_RUN.search(line)) if not (published or claim) else False
        if not (published or claim or held):
            continue
        ts = _line_epoch(line)
        if ts is None or now - ts > window_s or ts - now > 300:
            continue
        if published:
            not_published.append(ts)
            last_kind = published.group(1)
        elif claim:
            claims.setdefault(claim.group(1), []).append(ts)
        else:
            lifecycle.append(ts)
    minutes = max(1, int(window_s // 60))
    count = _bursts(not_published, 2.0)
    if count >= 1:
        return f"gateway wedged: {last_kind} ({count} in the last {minutes} min)"
    if claims:
        session, stamps = max(claims.items(), key=lambda kv: _bursts(kv[1], 5.0))
        refused = _bursts(stamps, 5.0)
        if refused >= 2:
            return f"gateway wedged: leaked turn claim on session {session[:8]} ({refused} refused turns in {minutes} min)"
    held_count = _bursts(lifecycle, 10.0)
    if held_count >= 2:
        return f"gateway wedged: state-lifecycle lock held ({held_count} refused runs in {minutes} min)"
    return None


def tail_lines(path: Path, max_bytes: int = 1_000_000) -> list[str]:
    """Complete lines in the last ``max_bytes`` of a file (read-only; the partial first line is dropped)."""
    lines, _ = _tail(path, max_bytes)
    return lines


def _tail(path: Path, max_bytes: int) -> tuple[list[str], bool]:
    """``(lines, truncated)``: truncated when the file is longer than ``max_bytes``."""
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        start = max(0, size - max_bytes)
        handle.seek(start)
        raw = handle.read(max_bytes)
    lines = raw.decode("utf-8", "replace").splitlines()
    return (lines[1:] if start > 0 and lines else lines), start > 0


def window_lines(path: Path, since: float, *, first_bytes: int = 1_000_000, max_bytes: int = 8_000_000) -> tuple[list[str], bool]:
    """Lines covering ``since`` .. now, doubling the tail until its first dated line is older than ``since``.

    Returns ``(lines, covered)``; ``covered`` is False when even ``max_bytes`` did not reach back to ``since``.
    """
    size = first_bytes
    while True:
        lines, truncated = _tail(path, size)
        if not truncated:
            return lines, True
        first = next((ts for ts in (_line_epoch(line) for line in lines[:50]) if ts is not None), None)
        if first is not None and first <= since:
            return lines, True
        if size >= max_bytes:
            return lines, False
        size = min(size * 2, max_bytes)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # a probe never follows a redirect
        return None


class GatewayProbe:
    """Read-only gateway health: loopback HTTP probes plus the wedge scan of the gateway log."""

    def __init__(
        self,
        base_url: str = DEFAULT_GATEWAY_URL,
        *,
        log_dir: Optional[str | Path] = None,
        require_ready: bool = True,
        window_s: float = DEFAULT_WEDGE_WINDOW_S,
        timeout_s: float = 1.5,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.base_url = validate_gateway_url(base_url)
        self.log_dir = Path(log_dir) if log_dir else default_log_dir()
        self.require_ready = require_ready
        self.window_s = window_s
        self.timeout_s = timeout_s
        self._clock = clock
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def _get(self, path: str) -> tuple[int, Any]:
        request = urllib.request.Request(f"{self.base_url}{path}", method="GET", headers={"Accept": "application/json"})
        try:
            with self._opener.open(request, timeout=self.timeout_s) as response:
                status, raw = int(response.status), response.read(65536)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            try:
                raw = exc.read(65536)
            except Exception:
                raw = b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = None
        return status, body

    def check(self) -> Health:
        now = self._clock()
        for path, required in (("/healthz", True), ("/readyz", self.require_ready)):
            if not required:
                continue
            try:
                status, body = self._get(path)
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                timed_out = isinstance(exc, TimeoutError) or "timed out" in str(exc).lower()
                what = "timed out" if timed_out else "refused or unreachable"
                return Health(False, f"gateway {path} {what}", checked_at=now)
            if status != 200:
                reason = f"gateway {path} HTTP {status}"
                failing = body.get("failing") if isinstance(body, dict) else None
                if isinstance(failing, list) and failing:
                    names = ", ".join(redact(str(f))[0][:60] for f in failing[:3])
                    reason += f" (failing: {names})"
                return Health(False, reason, checked_at=now)
        wedge, note = self._scan(now)
        if wedge:
            return Health(False, wedge, checked_at=now)
        return Health(True, "", note=note, checked_at=now)

    def _scan(self, now: float) -> tuple[Optional[str], Optional[str]]:
        """``(wedge_or_failure, note)``. Without the log the wedge modes are invisible, so that is a failure too."""
        if not self.log_dir.is_dir():
            return f"wedge check impossible: gateway log dir {self.log_dir} not found (set ECHO_ADK_OPENCLAW_LOG_DIR)", None
        files = []
        for moment in (now - self.window_s, now):
            path = self.log_dir / f"openclaw-{datetime.fromtimestamp(moment):%Y-%m-%d}.log"
            if path.is_file() and path not in files:
                files.append(path)
        lines: list[str] = []
        covered = True
        try:
            for path in files:
                chunk, whole = window_lines(path, now - self.window_s)
                lines.extend(chunk)
                covered = covered and whole
        except OSError as exc:
            return f"wedge check impossible: gateway log unreadable ({type(exc).__name__})", None
        note = None
        if not files:
            note = "no gateway log for today yet"
        elif not covered:
            note = f"gateway log tail is shorter than the {int(self.window_s // 60)} min wedge window"
        return scan_wedges(lines, now, self.window_s), note


# --------------------------------------------------------------------------- transport


@dataclass(frozen=True)
class SendResult:
    ok: bool
    error: str = ""
    message_id: Optional[str] = None


class Transport(Protocol):
    def health(self) -> Health: ...

    def send(self, channel: str, dest: str, text: str, buttons: Optional[Sequence[Mapping[str, Any]]] = None) -> SendResult: ...


_SCRIPT_SUFFIXES = (".cmd", ".bat", ".ps1", ".vbs", ".wsf", ".com")

# CLI diagnostics can echo a provider URL; Telegram puts the bot token in the path (/bot<id>:<secret>/...),
# Discord bot tokens are three dot-joined base64 segments. Neither shape is in the core redactor.
_TELEGRAM_TOKEN = re.compile(r"\b(?:bot)?\d{5,12}:[A-Za-z0-9_-]{30,}")
_DISCORD_TOKEN = re.compile(r"\b[MNO][A-Za-z\d_-]{23,27}\.[A-Za-z\d_-]{6}\.[A-Za-z\d_-]{27,}")
_URL = re.compile(r"\bhttps?://[^\s\"'<>]+")


def scrub(text: str) -> str:
    """Redaction for CLI diagnostics: core patterns, Telegram/Discord bot tokens, and URLs reduced to scheme+host."""
    out = _TELEGRAM_TOKEN.sub("[REDACTED]", str(text))
    out = _DISCORD_TOKEN.sub("[REDACTED]", out)
    out = _URL.sub(_url_host_only, out)
    return redact(out)[0]


def _url_host_only(match: re.Match[str]) -> str:
    try:
        parts = urllib.parse.urlsplit(match.group(0))
        return f"{parts.scheme}://{parts.hostname or '?'}/[path]"
    except ValueError:
        return "[url]"


def resolve_cli(cli: Optional[str] = None, node: Optional[str] = None) -> list[str]:
    """argv prefix for the OpenClaw CLI.

    A Windows ``openclaw.cmd`` shim would run under ``cmd.exe``, which re-reads ``% ^ & | "`` in
    arguments, and bot text is attacker-influenced. The shim is therefore resolved to
    ``node <npm>/node_modules/openclaw/openclaw.mjs`` and executed directly; a ``.cmd``/``.bat``/``.ps1``
    that cannot be resolved that way is refused.
    """
    path = cli or shutil.which("openclaw")
    if not path:
        raise ChannelError("openclaw CLI not found (set ECHO_ADK_OPENCLAW_CLI)")
    entry = Path(path)
    suffix = entry.suffix.lower()
    if suffix in _SCRIPT_SUFFIXES:
        candidate = entry.parent / "node_modules" / "openclaw" / "openclaw.mjs"
        if not candidate.is_file():
            raise ChannelError(f"refusing to run the {suffix} shim {entry.name}; set ECHO_ADK_OPENCLAW_CLI to openclaw.mjs")
        entry, suffix = candidate, ".mjs"
    if not entry.is_file():
        raise ChannelError(f"openclaw CLI {entry} does not exist")
    if suffix in (".mjs", ".js", ".cjs"):
        runtime = node or shutil.which("node")
        if not runtime or not Path(runtime).is_file():
            raise ChannelError("node not found for the openclaw CLI (set ECHO_ADK_OPENCLAW_NODE)")
        if Path(runtime).suffix.lower() in _SCRIPT_SUFFIXES:  # a node.cmd wrapper would bring cmd.exe back
            raise ChannelError(f"refusing the {Path(runtime).suffix} node wrapper; set ECHO_ADK_OPENCLAW_NODE to node.exe")
        return [str(runtime), str(entry)]
    return [str(entry)]


def _parse_cli_json(stdout: str) -> Optional[dict[str, Any]]:
    text = stdout.strip()
    if not text:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def _message_id(payload: Mapping[str, Any]) -> Optional[str]:
    for holder in (payload, payload.get("result") if isinstance(payload.get("result"), Mapping) else {}):
        for key in ("messageId", "message_id", "id"):
            value = holder.get(key) if isinstance(holder, Mapping) else None
            if isinstance(value, (str, int)) and not isinstance(value, bool) and len(str(value)) <= 128:
                return str(value)
    return None


class CliTransport:
    """``openclaw message send --channel=.. --target=.. --message=.. --json [--presentation=..]``, no shell."""

    def __init__(
        self,
        argv_prefix: Sequence[str],
        probe: GatewayProbe,
        *,
        timeout_s: float = 45.0,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        if not argv_prefix:
            raise ChannelError("argv prefix must not be empty")
        self.argv_prefix = [str(a) for a in argv_prefix]
        self.probe = probe
        self.timeout_s = timeout_s
        self._run = runner

    def health(self) -> Health:
        return self.probe.check()

    def argv(self, channel: str, dest: str, text: str, buttons: Optional[Sequence[Mapping[str, Any]]] = None) -> list[str]:
        if channel not in CHANNELS or not _DEST.match(dest):
            raise ChannelError("refusing an invalid channel target")
        out = [*self.argv_prefix, "message", "send", f"--channel={channel}", f"--target={dest}", f"--message={text}", "--json"]
        if buttons:
            presentation = {"blocks": [{"type": "buttons", "buttons": [dict(b) for b in buttons]}]}
            out.append("--presentation=" + json.dumps(presentation, ensure_ascii=False, separators=(",", ":")))
        return out

    def send(self, channel: str, dest: str, text: str, buttons: Optional[Sequence[Mapping[str, Any]]] = None) -> SendResult:
        argv = self.argv(channel, dest, text, buttons)
        kwargs: dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": self.timeout_s,
            "stdin": subprocess.DEVNULL,
            "shell": False,
        }
        if sys.platform == "win32":  # headless: no console window per send
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            done = self._run(argv, **kwargs)
        except subprocess.TimeoutExpired:
            return SendResult(False, f"openclaw message send timed out after {int(self.timeout_s)} s")
        except OSError as exc:
            return SendResult(False, f"openclaw CLI could not start: {type(exc).__name__}")
        payload = _parse_cli_json(done.stdout or "") or {}
        if done.returncode == 0 and payload.get("ok", True) is not False:
            return SendResult(True, message_id=_message_id(payload))
        detail = payload.get("error") or payload.get("deliveryStatus") or (done.stderr or "").strip().splitlines()[-1:] or ""
        if isinstance(detail, list):
            detail = detail[0] if detail else ""
        if isinstance(detail, Mapping):
            detail = detail.get("message") or json.dumps(detail)[:200]
        message = scrub(str(detail))[:300] or "no detail"
        return SendResult(False, f"openclaw message send failed (exit {done.returncode}): {message}")


# --------------------------------------------------------------------------- adapter


@dataclass
class _ChannelRuntime:
    last_sent_at: Optional[str] = None
    last_error: Optional[str] = None
    dropped: int = 0


@dataclass
class _Job:
    kind: str  # reply | notify | ack
    target: str
    text: str
    buttons: list[dict[str, Any]] = field(default_factory=list)
    approval_id: Optional[str] = None


def _api_error(status: int, message: str) -> Exception:
    from ..api.service import ApiError

    return ApiError(status, "not_found" if status == 404 else "invalid", message)


def _clip(text: str, target: str) -> str:
    limit = TEXT_LIMITS.get(target.split(":", 1)[0], DEFAULT_TEXT_LIMIT)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class OpenClawChannels:
    """Owns channel state, inbound routing, outbound delivery and approvals for one :class:`AdkService`."""

    def __init__(
        self,
        service: AdkService,
        config: Optional[OpenClawConfig] = None,
        *,
        transport: Optional[Transport] = None,
        config_error: Optional[str] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.service = service
        self.store = service.store
        self.config = config or OpenClawConfig()
        self._clock = clock
        self._lock = threading.RLock()
        self._health_lock = threading.Lock()
        self._health: Optional[Health] = None
        self._runtime: dict[str, _ChannelRuntime] = {}
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._queue: queue.Queue[Optional[_Job]] = queue.Queue(maxsize=OUTBOUND_QUEUE)
        self._worker: Optional[threading.Thread] = None
        self._stopped = False
        self.off_reason: Optional[str] = None
        self.transport: Optional[Transport] = transport
        if config_error:
            self.off_reason = f"openclaw adapter off: invalid configuration: {config_error}"
        elif transport is None:
            if not self.config.enabled:
                self.off_reason = "openclaw adapter off (ECHO_ADK_OPENCLAW is not set on this host)"
            else:
                try:
                    probe = GatewayProbe(
                        self.config.gateway_url,
                        log_dir=self.config.log_dir,
                        require_ready=self.config.require_ready,
                        window_s=self.config.wedge_window_s,
                    )
                    self.transport = CliTransport(
                        resolve_cli(self.config.cli, self.config.node), probe, timeout_s=self.config.send_timeout_s
                    )
                except ChannelError as exc:
                    self.off_reason = f"openclaw adapter off: {exc}"
        if self.off_reason:
            log.info("openclaw_off", reason=self.off_reason)
        else:
            log.info("openclaw_on", gateway=self.config.gateway_url, notify=bool(self.config.notify_target))

    # ------------------------------------------------------------------ lifecycle
    def stop(self) -> None:
        self._stopped = True
        if self._worker is not None:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
            self._worker.join(timeout=5)
            self._worker = None

    def flush(self, timeout: float = 10.0) -> bool:
        """Wait until every queued send has been attempted (tests, shutdown)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return False

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is None and not self._stopped:
                self._worker = threading.Thread(target=self._drain, name="echo-adk-openclaw", daemon=True)
                self._worker.start()

    def _drain(self) -> None:
        while True:
            job = self._queue.get()
            try:
                if job is None:
                    return
                self._deliver(job)
            except Exception:  # the worker must outlive one bad job; the error is logged and recorded
                log.error("openclaw_job_failed", kind=getattr(job, "kind", None), exc_info=True)
                if job is not None:
                    self._record(job, ok=False, error="internal error while sending")
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------------ health and state
    def health(self) -> Health:
        if self.off_reason or self.transport is None:
            return Health(False, self.off_reason or "openclaw adapter off")
        with self._health_lock:
            cached = self._health
            if cached is not None and self._clock() - cached.checked_at < self.config.health_ttl_s:
                return cached
            try:
                fresh = self.transport.health()
            except Exception as exc:  # a broken probe is a reason, never a crash
                fresh = Health(False, f"gateway health probe failed: {type(exc).__name__}")
            if not fresh.checked_at:
                fresh = Health(fresh.ok, fresh.reason, fresh.note, self._clock())
            if not fresh.ok and (cached is None or cached.ok or cached.reason != fresh.reason):
                log.warning("openclaw_unavailable", reason=fresh.reason)
            self._health = fresh
            return fresh

    def describe(self, bot: Mapping[str, Any]) -> list[dict[str, Any]]:
        """``channels`` for the public Bot shape: spec + live ``state`` (``ready`` / ``unavailable: <reason>``)."""
        specs = bot.get("channels") or []
        if not specs:
            return []
        health = self.health()
        out = []
        for spec in specs:
            entry: dict[str, Any] = {"kind": spec["kind"], "target": spec["target"], "state": health.state()}
            for key in ("approvers", "allow_from"):
                if key in spec:
                    entry[key] = list(spec[key])
            runtime = self._runtime.get(spec["target"])
            if runtime is not None:
                if runtime.last_sent_at:
                    entry["last_sent_at"] = runtime.last_sent_at
                if runtime.last_error:
                    entry["last_error"] = runtime.last_error
                if runtime.dropped:
                    entry["dropped"] = runtime.dropped
            if health.ok and health.note:
                entry["note"] = health.note
            out.append(entry)
        return out

    def check_unique(self, specs: Sequence[Mapping[str, Any]], bot_id: Optional[str]) -> None:
        """A chat handle routes to exactly one bot."""
        wanted = {s["target"] for s in specs}
        if not wanted:
            return
        for other in self.store.list_bots():
            if other["bot_id"] == bot_id:
                continue
            clash = wanted & {s["target"] for s in other.get("channels") or []}
            if clash:
                raise _api_error(409, f"channel target {sorted(clash)[0]} is already mapped to bot {other['bot_id']}")

    def _bot_for(self, target: str) -> Optional[dict[str, Any]]:
        for bot in self.store.list_bots():
            for spec in bot.get("channels") or []:
                if spec["target"] == target:
                    return bot
        return None

    # ------------------------------------------------------------------ outbound
    def _enqueue(self, job: _Job) -> None:
        if self._stopped:
            return
        self._ensure_worker()
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            self._record(job, ok=False, error="outbound queue full; message dropped")

    def _record(self, job: _Job, *, ok: bool, error: str = "") -> None:
        with self._lock:
            runtime = self._runtime.setdefault(job.target, _ChannelRuntime())
            if ok:
                runtime.last_sent_at = now_iso()
                runtime.last_error = None
            else:
                runtime.last_error = error[:300]
                runtime.dropped += 1
        if job.approval_id:
            with self._lock:  # read-modify-write under the decision lock so a decision is never overwritten
                doc = self.store.get_approval(job.approval_id)
                if doc is not None:
                    doc["notified"] = {"target": job.target, "state": "sent" if ok else error[:300], "at": now_iso()}
                    self.store.put_approval(doc)

    def _deliver(self, job: _Job) -> None:
        health = self.health()
        if not health.ok:
            self._record(job, ok=False, error=health.state())
            log.warning("openclaw_send_dropped", kind=job.kind, reason=health.reason)
            return
        assert self.transport is not None
        channel, dest = parse_target(job.target)
        result = self.transport.send(channel, dest, _clip(job.text, job.target), job.buttons or None)
        self._record(job, ok=result.ok, error=result.error)
        if result.ok:
            log.info("openclaw_sent", kind=job.kind, channel=channel)
        else:
            log.warning("openclaw_send_failed", kind=job.kind, channel=channel, error=result.error)

    def on_reply(self, bot: Mapping[str, Any], text: str, origin: Optional[str]) -> None:
        """A bot turn finished with a final answer: send it to the chat it came from, else to every channel."""
        targets = [s["target"] for s in bot.get("channels") or []]
        if not targets:
            return
        if origin is not None:
            if origin not in targets:  # the chat was detached mid-turn: never hand its answer to another chat
                log.info("openclaw_reply_dropped", bot_id=bot.get("bot_id"), why="origin chat no longer mapped")
                return
            targets = [origin]
        clean = redact(f"[{bot['name']}] {text}")[0]
        for target in targets:
            self._enqueue(_Job("reply", target, clean))

    # ------------------------------------------------------------------ inbound
    def inbound(self, body: Any) -> tuple[int, dict[str, Any]]:
        """One chat event from the gateway bridge: a decision (approve/deny) or a message for a bot."""
        if not isinstance(body, Mapping):
            raise ChannelError("body must be an object")
        extra = set(body) - {"channel", "target", "sender", "text", "callback", "message_id"}
        if extra:
            raise ChannelError(f"unknown field(s) {sorted(extra)}")
        channel = body.get("channel")
        if not isinstance(channel, str) or channel.lower() not in CHANNELS:
            raise ChannelError(f"channel must be one of {list(CHANNELS)}")
        channel = channel.lower()
        dest = body.get("target")
        if not isinstance(dest, str) or not _DEST.match(dest):
            raise ChannelError("target must be the chat destination (1..160 characters, no spaces or quotes)")
        sender = body.get("sender")
        if not isinstance(sender, str) or not _SENDER.match(sender):
            raise ChannelError("sender must be the sender id")
        message_id = body.get("message_id")
        if message_id is not None and (not isinstance(message_id, str) or not _MESSAGE_ID.match(message_id)):
            raise ChannelError("message_id must be 1..128 characters from [A-Za-z0-9:_.+=/-]")
        text, callback = body.get("text"), body.get("callback")
        if (text is None) == (callback is None):
            raise ChannelError("exactly one of text or callback is required")
        value = text if text is not None else callback
        if not isinstance(value, str) or not value.strip():
            raise ChannelError("text/callback must be a non-empty string")
        if len(value) > MAX_PROMPT_CHARS:
            raise ChannelError(f"text is longer than {MAX_PROMPT_CHARS} characters")
        target = f"{channel}:{dest}"
        match = _DECISION.match(value) or _DECISION_TEXT.match(value)
        if match:
            approval = self.decide(match.group(2), match.group(1).lower(), via=(target, sender))
            return 200, {"kind": "decision", "approval": approval}
        if callback is not None:
            raise ChannelError("unknown callback value")
        key = f"{target}|{message_id}" if message_id is not None else None
        if key is not None:  # reserve the id; released again when the message is not accepted
            with self._lock:
                if key in self._seen:
                    return 200, {"kind": "duplicate", "message_id": message_id}
                self._seen[key] = None
                while len(self._seen) > 2000:
                    self._seen.popitem(last=False)
        try:
            bot = self._bot_for(target)
            if bot is None:
                raise _api_error(404, f"no bot is mapped to {target}")
            spec = next(s for s in bot.get("channels") or [] if s["target"] == target)
            allow = spec.get("allow_from")
            if allow is not None and sender not in allow:
                raise _api_error(403, "sender is not in this channel's allow_from list")
            queued = self.service.bots.message(bot["bot_id"], {"text": value}, origin=target)
        except Exception:
            if key is not None:
                with self._lock:
                    self._seen.pop(key, None)
            raise
        return 202, {"kind": "message", "bot_id": bot["bot_id"], **queued}

    # ------------------------------------------------------------------ approvals
    def _expire(self, doc: dict[str, Any]) -> dict[str, Any]:
        """Default deny: a pending approval past ``expires_at`` becomes ``expired`` (re-read under the lock)."""
        if doc["status"] != "pending" or not doc.get("expires_at") or parse_iso(doc["expires_at"]) > datetime.now(timezone.utc):
            return doc
        with self._lock:
            fresh = self.store.get_approval(doc["approval_id"]) or doc
            if fresh["status"] == "pending":
                fresh.update(status="expired", decided_at=now_iso(), decided_by="default-deny")
                self.store.put_approval(fresh)
            return fresh

    @staticmethod
    def public(doc: Mapping[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {
            "approval_id": doc["approval_id"],
            "kind": doc["kind"],
            "status": doc["status"],
            "actions": list(doc.get("actions", [])),
            "summary": doc["summary"],
            "created_at": doc["created_at"],
            "notified": dict(doc["notified"]) if doc.get("notified") else None,
        }
        for key in ("bot_id", "group_id", "run_id", "needs", "how", "capability", "tier", "expires_at", "decided_at", "decided_by"):
            if doc.get(key) is not None:
                out[key] = doc[key]
        mint = doc.get("mint")
        if mint:
            out["lease"] = {"scopes": list(mint["scopes"]), "max_uses": mint["max_uses"], "ttl_s": mint["ttl_s"]}
        if doc.get("result_run_id"):
            out["result_run_id"] = doc["result_run_id"]
        if doc.get("reason"):
            out["reason"] = doc["reason"]
        return out

    def list_approvals(self, status: Optional[str], bot_id: Optional[str], limit: int) -> dict[str, Any]:
        if status is not None and status not in APPROVAL_STATUSES:
            raise ChannelError(f"status must be one of {list(APPROVAL_STATUSES)}")
        if bot_id is not None and not is_id(bot_id):
            raise ChannelError("bot_id must be a bot id")
        rows = [self._expire(d) for d in self.store.list_approvals(bot_id=bot_id, limit=MAX_LIST_APPROVALS)]
        if status is not None:
            rows = [d for d in rows if d["status"] == status]
        return {"approvals": [self.public(d) for d in rows[:limit]]}

    def get_approval(self, approval_id: str) -> dict[str, Any]:
        doc = self.store.get_approval(approval_id) if is_id(approval_id) else None
        if doc is None:
            raise _api_error(404, f"approval {approval_id} not found")
        return self.public(self._expire(doc))

    def _approvers_for(self, target: str, bot_id: Optional[str]) -> set[str]:
        channel = target.split(":", 1)[0]
        allowed = {a.split(":", 1)[1] for a in self.config.approvers if a.split(":", 1)[0] == channel}
        bot = self.store.get_bot(bot_id) if bot_id else None
        for spec in (bot or {}).get("channels") or []:
            if spec["target"] == target:
                allowed.update(spec.get("approvers") or [])
        return allowed

    def decide(self, approval_id: str, decision: str, *, via: Optional[tuple[str, str]] = None) -> dict[str, Any]:
        """Approve or deny once. ``via=(target, sender)`` for chat decisions; ``None`` = owner API."""
        if decision not in ("approve", "deny"):
            raise ChannelError("decision must be approve or deny")
        with self._lock:
            doc = self.store.get_approval(approval_id) if is_id(approval_id) else None
            if doc is None:
                raise _api_error(404, f"approval {approval_id} not found")
            doc = self._expire(doc)
            if doc["status"] == "notice":
                raise _api_error(409, "this is a notice; it has nothing to approve")
            if doc["status"] != "pending":
                raise _api_error(409, f"approval is already {doc['status']}")
            if via is not None:
                target, sender = via
                notified = doc.get("notified") or {}
                if target != notified.get("target") or notified.get("state") != "sent":
                    log.warning("openclaw_decision_refused", approval_id=approval_id, why="not the delivered chat")
                    raise _api_error(403, "decisions are accepted only from the chat the approval was delivered to")
                if sender not in self._approvers_for(target, doc.get("bot_id")):
                    log.warning("openclaw_decision_refused", approval_id=approval_id, why="not an approver")
                    raise _api_error(403, "sender is not an approver for this chat")
            who = "api" if via is None else f"openclaw:{via[0].split(':', 1)[0]}:{via[1]}"
            doc.update(status="approved" if decision == "approve" else "denied", decided_at=now_iso(), decided_by=who)
            # Consumed before anything is minted: a crash between the two can never replay the approval.
            self.store.put_approval(doc)
            if decision == "approve":
                try:
                    doc["result_run_id"] = self._mint(doc)
                except Exception as exc:
                    doc.update(status="denied", reason=str(exc)[:300])
                    self.store.put_approval(doc)
                    raise _api_error(409, f"approval could not be applied: {exc}")
                self.store.put_approval(doc)
        log.info("approval_decided", approval_id=approval_id, status=doc["status"], by=who.split(":")[0])
        notified = doc.get("notified") or {}
        if notified.get("target") and (via is not None or notified.get("state") == "sent"):
            ack = f"approved: running once as run {doc['result_run_id'][:8]}" if doc["status"] == "approved" else "denied"
            by = "" if via is not None else " in Echo Prime"
            self._enqueue(_Job("ack", notified["target"], f"echo-adk · {ack}{by} (ref {approval_id})"))
        return self.public(doc)

    def _mint(self, doc: dict[str, Any]) -> str:
        """Approve = one Lease for one re-run of the turn, queued through the normal daily-cap check."""
        mint = doc.get("mint")
        bot = self.store.get_bot(doc.get("bot_id") or "")
        if not mint or bot is None:
            raise ValidationError("the bot no longer exists")
        agent = self.service.bots._agent_for(bot)
        outside = [s for s in mint["scopes"] if s not in agent.capabilities]
        if outside:
            raise ValidationError(f"scopes {outside} are no longer in the agent allowlist")
        if doc["kind"] == "error" and bot["status"] == "error":
            bot["status"] = "idle"
            bot["last_error"] = None
            self.store.put_bot(bot)
        message_id = new_id()
        lease = {"scopes": list(mint["scopes"]), "max_uses": int(mint["max_uses"]), "ttl_s": int(mint["ttl_s"])}
        run_id = self.service.queue_bot_run(bot, agent, mint["prompt"], message_id=message_id, lease_override=lease)
        self.store.put_message(
            {
                "message_id": message_id,
                "bot_id": bot["bot_id"],
                "role": "user",
                "text": f"[approved re-run] {mint['text']}"[:MAX_PROMPT_CHARS],
                "created_at": now_iso(),
                "run_id": run_id,
                "state": "pending",
                "origin": mint.get("origin"),
                "approval_id": doc["approval_id"],
            }
        )
        return run_id

    def _notify_target(self, bots: Sequence[Mapping[str, Any]]) -> Optional[str]:
        if self.config.notify_target:
            return self.config.notify_target
        targets = [s["target"] for bot in bots for s in bot.get("channels") or []]
        ordered = _preferred(dict.fromkeys(targets))
        return ordered[0] if ordered else None

    def _create(
        self,
        kind: str,
        summary: str,
        *,
        bots: Sequence[Mapping[str, Any]],
        run: Optional[Mapping[str, Any]] = None,
        group_id: Optional[str] = None,
        mint: Optional[dict[str, Any]] = None,
        gated: Optional[Mapping[str, Any]] = None,
        reason: Optional[str] = None,
    ) -> dict[str, Any]:
        approval_id = new_id()
        actionable = mint is not None
        created = datetime.now(timezone.utc)
        doc: dict[str, Any] = {
            "approval_id": approval_id,
            "kind": kind,
            "status": "pending" if actionable else "notice",
            "actions": ["approve", "deny"] if actionable else [],
            "bot_id": bots[0]["bot_id"] if len(bots) == 1 else None,
            "group_id": group_id,
            "run_id": run["run_id"] if run else None,
            "summary": redact(summary)[0][:1000],
            "created_at": now_iso(created),
            "expires_at": now_iso(created + timedelta(seconds=self.config.approval_ttl_s)) if actionable else None,
            "mint": mint,
            "reason": reason,
            "notified": None,
        }
        if gated:
            for key in ("needs", "how", "capability", "tier"):
                if gated.get(key) is not None:
                    doc[key] = redact(str(gated[key]))[0][:300] if key != "tier" else gated[key]
        target = self._notify_target(bots)
        if target is None:
            doc["notified"] = {"target": None, "state": "no channel to notify", "at": now_iso()}
        else:
            doc["notified"] = {"target": target, "state": "queued", "at": now_iso()}
        self.store.put_approval(doc)
        log.info("approval_created", approval_id=approval_id, kind=kind, actionable=actionable, notify=bool(target))
        if target is not None:
            text, buttons = self._render(doc)
            self._enqueue(_Job("notify", target, text, buttons, approval_id))
        return doc

    def _render(self, doc: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        """Notification text and buttons. Only the approval reference travels; never a token or key."""
        titles = {
            "gated": "approval needed: gated turn",
            "error": "bot error: approve a retry?",
            "daily_cap": "daily cost cap reached",
            "group_done": "group chat finished",
        }
        lines = [f"echo-adk · {titles[doc['kind']]}", doc["summary"]]
        if doc.get("needs"):
            lines.append(f"needs: {doc['needs']}")
        if doc.get("how"):
            lines.append(f"how: {doc['how']}")
        if doc.get("reason"):
            lines.append(f"note: {doc['reason']}")
        buttons: list[dict[str, Any]] = []
        if doc["status"] == "pending":
            mint = doc["mint"]
            minutes = max(1, self.config.approval_ttl_s // 60)
            lines.append(
                f"Approve runs this turn once with a fresh Lease (scopes: {', '.join(mint['scopes'])}; max_uses {mint['max_uses']}). "
                f"Deny, or no answer within {minutes} min, means denied."
            )
            lines.append(f"Reply 'approve {doc['approval_id']}' or 'deny {doc['approval_id']}' if the buttons do not show.")
            buttons = [
                {
                    "label": "Approve once",
                    "action": {"type": "callback", "value": f"echoadk:approve:{doc['approval_id']}"},
                    "style": "success",
                },
                {"label": "Deny", "action": {"type": "callback", "value": f"echoadk:deny:{doc['approval_id']}"}, "style": "danger"},
            ]
        lines.append(f"ref {doc['approval_id']}")
        return redact("\n".join(lines))[0], buttons

    # ------------------------------------------------------------------ events from BotManager
    @staticmethod
    def _gated_step(run: Mapping[str, Any]) -> Mapping[str, Any]:
        for step in reversed(run.get("steps") or []):
            if step.get("verdict") == "gated":
                return step
        return {}

    def _original(self, run: Mapping[str, Any]) -> tuple[str, Optional[str]]:
        """The user-visible text and chat origin of the turn behind ``run``."""
        message_id = run.get("message_id")
        if message_id:
            message = self.store.get_message(message_id)
            if message is not None:
                return str(message.get("text") or run.get("prompt") or ""), message.get("origin")
        return str(run.get("prompt") or ""), None

    def on_gated(self, bot: Mapping[str, Any], run: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        step = self._gated_step(run)
        needs = str(step.get("needs") or "")
        for other in self.store.list_approvals(bot_id=bot["bot_id"], limit=50):
            if other["kind"] == "gated" and other.get("needs") == needs and self._expire(other)["status"] == "pending":
                return None  # one open approval per need: a scheduled bot does not flood the chat
        template = bot["lease"]
        agent = self.service.bots._agent_for(bot)
        scopes, max_uses, reason = list(template["scopes"]), int(template["max_uses"]), None
        actionable = True
        if needs.startswith("lease scope "):
            cap = needs[len("lease scope ") :].strip()
            tier = self.service.registry.tier_of(cap)
            if cap not in agent.capabilities:
                actionable, reason = False, f"{cap} is outside the agent allowlist; chat approval cannot add it"
            elif tier is None or tier >= 2:
                actionable, reason = False, f"{cap} is tier {tier}; it needs a signed ESH/SOL grant, which chat approval cannot mint"
            elif cap not in scopes:
                scopes.append(cap)
        elif needs in ("lease", "lease renewal"):
            pass
        elif needs == "lease with more max_uses":
            max_uses = min(int(BUDGET_CAPS["max_calls"]), max(max_uses * 2, max_uses + 1))
        elif needs.startswith("tier-"):
            actionable, reason = False, "a tier >= 2 capability needs a signed ESH/SOL grant; chat approval cannot mint one"
        else:
            actionable, reason = False, "this gate cannot be satisfied by minting a Lease"
        text, origin = self._original(run)
        mint = None
        if actionable:
            mint = {
                "scopes": scopes,
                "max_uses": max_uses,
                "ttl_s": int(template["ttl_s"]),
                "prompt": run["prompt"],
                "text": text,
                "origin": origin,
            }
        summary = f"echoBot '{bot['name']}' run {run['run_id'][:8]} stopped at a gate."
        return self._create("gated", summary, bots=[bot], run=run, mint=mint, gated=step, reason=reason)

    def on_failed(self, bot: Mapping[str, Any], run: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        error = run.get("error") or {}
        code, message = str(error.get("code") or "failed"), str(error.get("message") or "")
        if code == "budget_exceeded" and message.startswith("daily_cost_cap_usd"):
            summary = (
                f"echoBot '{bot['name']}' turn {run['run_id'][:8]} was refused: {message}. "
                "The cap is never raised from chat; the bot is in error until resumed after the cap resets (UTC midnight)."
            )
            return self._create("daily_cap", summary, bots=[bot], run=run)
        template = bot["lease"]
        text, origin = self._original(run)
        mint = {
            "scopes": list(template["scopes"]),
            "max_uses": int(template["max_uses"]),
            "ttl_s": int(template["ttl_s"]),
            "prompt": run["prompt"],
            "text": text,
            "origin": origin,
        }
        provider = f" (provider {error['provider']})" if error.get("provider") else ""
        summary = (
            f"echoBot '{bot['name']}' run {run['run_id'][:8]} failed: {code}: {message[:300]}{provider}. "
            "Approve resumes the bot and retries this turn once."
        )
        return self._create("error", summary, bots=[bot], run=run, mint=mint)

    def on_group_finished(self, group: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        bots = [b for b in (self.store.get_bot(i) for i in group.get("bots") or []) if b is not None]
        summary = str(group.get("summary") or f"group '{group.get('name')}' finished ({group.get('status')})")
        return self._create("group_done", summary, bots=bots, group_id=group.get("group_id"))


__all__ = [
    "CHANNELS",
    "ChannelError",
    "CliTransport",
    "GatewayProbe",
    "Health",
    "OpenClawChannels",
    "OpenClawConfig",
    "SendResult",
    "Transport",
    "parse_target",
    "resolve_cli",
    "scan_wedges",
    "tail_lines",
    "validate_channels",
    "validate_gateway_url",
]
