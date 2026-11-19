"""Model paths (R1 no tokens, R2 no fallback). Ported from Prime ``providers.ts`` / ``model.ts``.

Two kinds of model, both credential-free from the agent's point of view:

* :class:`LoopbackModel` - an OpenAI-compatible chat-completions server on a **loopback origin only**
  (FORGE's Qwen at ``http://127.0.0.1:11441``). No ``api_key`` is ever accepted.
* :class:`CliModel` - a vendor CLI (``claude``, ``codex``, ``grok``, ``agy``) as a subprocess. The child
  finds its own login in its own store; the child environment is allowlisted and drops anything
  matching ``KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL``.

A provider that is missing, signed out, timed out or unreadable raises :class:`ProviderUnavailable`
naming the provider. Nothing here ever retries against another provider.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from .log import get_logger
from .redact import is_secret_field, redact
from .types import MODEL_ID, ValidationError

log = get_logger("models")

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")
DEFAULT_LOOPBACK_ENDPOINT = "http://127.0.0.1:11441"
DEFAULT_LOOPBACK_MODEL = "qwen"
DEFAULT_LOOPBACK_PROVIDER = "qwen-local"
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_CONTEXT_BYTES = 180_000
MAX_CLI_OUTPUT_BYTES = 4_000_000

CLI_PROVIDERS = ("claude", "codex", "grok", "argon")
PROVIDER_ALIASES = {"gpt": "codex", "agy": "argon", "gemini": "argon"}
PROVIDER_LABEL = {
    "claude": "Claude (claude CLI)",
    "codex": "GPT (codex CLI)",
    "grok": "Grok (grok CLI)",
    "argon": "Argon (agy CLI)",
}
PROVIDER_BIN = {"claude": "claude", "codex": "codex", "grok": "grok", "argon": "agy"}
DEFAULT_CLI_MODEL = {"argon": "gemini-4-argon-medium"}
# Only what a CLI needs to find its own login and run. Nothing named like a key, token or secret passes.
ENV_ALLOW = (
    "PATH",
    "Path",
    "PATHEXT",
    "SystemRoot",
    "SYSTEMROOT",
    "windir",
    "ComSpec",
    "TEMP",
    "TMP",
    "TMPDIR",
    "HOME",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "APPDATA",
    "LOCALAPPDATA",
    "USERNAME",
    "LANG",
    "TERM",
)
_SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.IGNORECASE)


class ProviderUnavailable(Exception):
    """The named provider could not answer. The step ends here; no fallback is used (R2)."""

    def __init__(self, provider: str, detail: str) -> None:
        self.provider = provider
        self.detail = detail
        label = PROVIDER_LABEL.get(provider, provider)
        super().__init__(f"{label} is unavailable: {detail}; no fallback was used")


@dataclass(frozen=True)
class Answered:
    """What actually answered a step. ``reported`` is False when the provider does not say."""

    provider: str
    requested: Optional[str]
    model: Optional[str]
    reported: bool

    def to_dict(self) -> dict[str, Any]:
        return {"provider": self.provider, "requested": self.requested, "model": self.model, "reported": self.reported}


@dataclass(frozen=True)
class ModelReply:
    """One completion. ``cost_usd`` is the provider-reported cost when known; otherwise the runner
    falls back to the model's declared ``cost_per_call_usd`` (0 for loopback and CLI subscriptions)."""

    text: str
    answered: Answered
    cost_usd: Optional[float] = None


class Model(Protocol):
    provider: str
    cost_per_call_usd: float

    def complete(self, system: str, messages: list[dict[str, str]]) -> ModelReply: ...


def child_env(source: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """Allowlisted child environment for CLI providers (lifted from Prime ``childEnv``)."""
    env = os.environ if source is None else source
    out: dict[str, str] = {}
    for key in ENV_ALLOW:
        value = env.get(key)
        if value is not None and not _SECRET_ENV.search(key):
            out[key] = value
    return out


def validate_endpoint(endpoint: Any) -> str:
    """A model endpoint must be a credential-free loopback origin (``http://127.0.0.1:11441``)."""
    if not isinstance(endpoint, str) or len(endpoint) > 200:
        raise ValidationError("Model endpoint must be a credential-free loopback origin")
    parts = urllib.parse.urlsplit(endpoint)
    host = parts.hostname
    if (
        parts.scheme not in ("http", "https")
        or host is None
        or host.lower() not in LOOPBACK_HOSTS
        or parts.username
        or parts.password
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        raise ValidationError("Model endpoint must be a credential-free loopback origin")
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname}{port}"


def _refuse_secret_fields(value: Any, where: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if is_secret_field(str(key)):
                raise ValidationError(f"Invalid models configuration: credential-shaped field {key!r} in {where}")
            _refuse_secret_fields(item, f"{where}.{key}")
    elif isinstance(value, list):
        for item in value:
            _refuse_secret_fields(item, where)


def _check_messages(messages: Any) -> list[dict[str, str]]:
    if not isinstance(messages, list) or not messages:
        raise ValidationError("messages must be a non-empty list")
    out: list[dict[str, str]] = []
    for item in messages:
        if not isinstance(item, Mapping) or item.get("role") not in ("user", "assistant", "system"):
            raise ValidationError("each message needs role user|assistant|system")
        if not isinstance(item.get("content"), str):
            raise ValidationError("each message needs string content")
        out.append({"role": str(item["role"]), "content": str(item["content"])})
    if len(json.dumps(out, ensure_ascii=False).encode("utf-8")) > MAX_CONTEXT_BYTES:
        raise ValidationError("Context limit reached; start a focused run")
    return out


# --------------------------------------------------------------------------- Loopback (OpenAI-compatible)


class LoopbackModel:
    """OpenAI-compatible ``/v1/chat/completions`` on a loopback origin. 1 MiB response cap."""

    def __init__(
        self,
        endpoint: str = DEFAULT_LOOPBACK_ENDPOINT,
        model: str = DEFAULT_LOOPBACK_MODEL,
        *,
        provider: str = DEFAULT_LOOPBACK_PROVIDER,
        timeout_s: float = 120.0,
        max_bytes: int = MAX_RESPONSE_BYTES,
        max_tokens: int = 4096,
        opener: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.endpoint = validate_endpoint(endpoint)
        if not isinstance(model, str) or not MODEL_ID.match(model) or re.search(r"cloud", model, re.IGNORECASE):
            raise ValidationError("Cloud or invalid model names are not supported")
        if not isinstance(timeout_s, (int, float)) or timeout_s <= 0 or timeout_s > 600:
            raise ValidationError("Invalid model timeout")
        if not isinstance(max_bytes, int) or max_bytes < 1024 or max_bytes > 16 * 1024 * 1024:
            raise ValidationError("Invalid response cap")
        self.model = model
        self.provider = provider
        self.timeout_s = float(timeout_s)
        self.max_bytes = max_bytes
        self.max_tokens = max_tokens
        self.cost_per_call_usd = 0.0  # local inference: no metered cost
        self._open = opener or urllib.request.urlopen
        self.last: Optional[Answered] = None

    def _request(self, path: str, body: Optional[dict[str, Any]] = None) -> Any:
        url = f"{self.endpoint}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json")
        try:
            with self._open(request, timeout=self.timeout_s) as response:
                status = getattr(response, "status", 200)
                raw = response.read(self.max_bytes + 1)
        except urllib.error.HTTPError as exc:
            raise ProviderUnavailable(self.provider, f"local model HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            log.debug("loopback_model_error", provider=self.provider, error=type(exc).__name__)
            raise ProviderUnavailable(self.provider, "local model endpoint is unreachable or timed out") from None
        if status != 200:
            raise ProviderUnavailable(self.provider, f"local model HTTP {status}")
        if len(raw) > self.max_bytes:
            raise ProviderUnavailable(self.provider, "local model response limit exceeded")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ProviderUnavailable(self.provider, "local model returned invalid JSON") from None

    def models(self) -> list[str]:
        value = self._request("/v1/models")
        items = value.get("data") if isinstance(value, dict) else None
        if not isinstance(items, list):
            raise ProviderUnavailable(self.provider, "local model inventory unavailable")
        return [str(m.get("id")) for m in items if isinstance(m, dict) and isinstance(m.get("id"), str)]

    def complete(self, system: str, messages: list[dict[str, str]]) -> ModelReply:
        convo = _check_messages(messages)
        payload = {
            "model": self.model,
            "messages": ([{"role": "system", "content": system}] if system else []) + convo,
            "temperature": 0,
            "stream": False,
            "max_tokens": self.max_tokens,
        }
        value = self._request("/v1/chat/completions", payload)
        choices = value.get("choices") if isinstance(value, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderUnavailable(self.provider, "local model response is incomplete")
        message = choices[0].get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise ProviderUnavailable(self.provider, "local model response is incomplete")
        reported = value.get("model")
        model_name = reported if isinstance(reported, str) and 0 < len(reported) <= 150 else None
        text, _ = redact(content)
        self.last = Answered(self.provider, self.model, model_name, model_name is not None)
        return ModelReply(text, self.last)


# --------------------------------------------------------------------------- CLI providers


@dataclass(frozen=True)
class RunResult:
    code: Optional[int]
    stdout: str
    stderr: str
    timed_out: bool


Runner = Callable[..., RunResult]


class SpawnFailed(Exception):
    """The provider binary could not be started."""


def run_cli(
    cmd: str,
    args: list[str],
    *,
    cwd: str,
    env: Mapping[str, str],
    input_text: Optional[str],
    timeout_s: float,
    max_bytes: int,
) -> RunResult:
    """Spawn ``cmd`` headlessly, never through a shell, with a hard timeout and an output cap."""
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    resolved = shutil.which(cmd) or cmd
    try:
        completed = subprocess.run(
            [resolved, *args],
            cwd=cwd,
            env=dict(env),
            input=(input_text or "").encode("utf-8"),
            capture_output=True,
            timeout=timeout_s,
            shell=False,
            check=False,
            **kwargs,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or b""
        err = exc.stderr or b""
        return RunResult(None, out.decode("utf-8", "replace")[:max_bytes], err.decode("utf-8", "replace")[:8000], True)
    except (FileNotFoundError, PermissionError, OSError) as exc:
        raise SpawnFailed(str(exc)) from None
    stdout = completed.stdout or b""
    too_much = len(stdout) > max_bytes
    return RunResult(
        completed.returncode,
        stdout[:max_bytes].decode("utf-8", "replace"),
        (completed.stderr or b"")[:8000].decode("utf-8", "replace"),
        too_much,
    )


def _json_lines(stdout: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def _most_output(usage: Any) -> Optional[str]:
    if not isinstance(usage, dict):
        return None
    best, top = None, -1
    for name, value in usage.items():
        tokens = value.get("outputTokens") if isinstance(value, dict) else None
        n = tokens if isinstance(tokens, (int, float)) else 0
        if n > top:
            top, best = n, name
    return best


@dataclass(frozen=True)
class CliSpec:
    stdin: bool
    args: Callable[[Optional[str], str, str, str], list[str]]
    parse: Callable[[str], tuple[str, Optional[str]]]
    max_prompt_chars: Optional[int] = None


def _parse_claude(stdout: str) -> tuple[str, Optional[str]]:
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError:
        raise ProviderUnavailable("claude", "returned unreadable output") from None
    if not isinstance(value, dict) or value.get("is_error") or not isinstance(value.get("result"), str):
        detail = value.get("result") if isinstance(value, dict) else None
        raise ProviderUnavailable("claude", detail[:200] if isinstance(detail, str) else "returned an error")
    return value["result"], _most_output(value.get("modelUsage"))


def _parse_codex(stdout: str) -> tuple[str, Optional[str]]:
    events = _json_lines(stdout)
    failed = any(e.get("type") in ("turn.failed", "error") for e in events)
    messages = [
        e
        for e in events
        if e.get("type") == "item.completed" and isinstance(e.get("item"), dict) and e["item"].get("type") == "agent_message"
    ]
    text = messages[-1]["item"].get("text") if messages else None
    if not isinstance(text, str):
        raise ProviderUnavailable("codex", "the CLI reported a failed turn" if failed else "returned no answer")
    return text, None  # the codex CLI does not say which model answered


def _parse_grok(stdout: str) -> tuple[str, Optional[str]]:
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError:
        raise ProviderUnavailable("grok", "returned unreadable output") from None
    if not isinstance(value, dict) or not isinstance(value.get("text"), str):
        raise ProviderUnavailable("grok", "returned no answer")
    return value["text"], _most_output(value.get("modelUsage"))


def _parse_argon(stdout: str) -> tuple[str, Optional[str]]:
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError:
        raise ProviderUnavailable("argon", "returned unreadable output") from None
    if not isinstance(value, dict) or value.get("status") != "SUCCESS" or not isinstance(value.get("response"), str):
        status = value.get("status") if isinstance(value, dict) else None
        raise ProviderUnavailable("argon", f"the CLI reported status {status if isinstance(status, str) else 'unknown'}")
    return value["response"], None  # agy does not say which model answered


SPECS: dict[str, CliSpec] = {
    # Not --bare: bare skips the subscription login. Project-only settings in an empty directory keep
    # the user's hooks, memory and plugins out of the call.
    "claude": CliSpec(
        stdin=True,
        args=lambda model, _dir, _prompt, system: [
            "-p",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--tools",
            "",
            "--setting-sources",
            "project",
            "--disable-slash-commands",
            "--strict-mcp-config",
            "--system-prompt",
            system,
            *(["--model", model] if model else []),
        ],
        parse=_parse_claude,
    ),
    "codex": CliSpec(
        stdin=True,
        args=lambda model, _dir, _prompt, _system: [
            "exec",
            "--json",
            "--ephemeral",
            "--skip-git-repo-check",
            "-s",
            "read-only",
            *(["-m", model] if model else []),
        ],
        parse=_parse_codex,
    ),
    "grok": CliSpec(
        stdin=False,
        args=lambda model, dir_, _prompt, _system: [
            "--output-format",
            "json",
            "--prompt-file",
            os.path.join(dir_, "prompt.txt"),
            "--tools",
            "",
            "--max-turns",
            "1",
            *(["-m", model] if model else []),
        ],
        parse=_parse_grok,
    ),
    # agy takes the prompt only on its command line (stdin is ignored), so the prompt must fit Windows' argv limit.
    "argon": CliSpec(
        stdin=False,
        max_prompt_chars=28000,
        args=lambda model, _dir, prompt, _system: [
            "--output-format",
            "json",
            "--mode",
            "plan",
            "--model",
            model or DEFAULT_CLI_MODEL["argon"],
            "--print-timeout",
            "150s",
            f"-p={prompt}",
        ],
        parse=_parse_argon,
    ),
}


def render_prompt(system: str, messages: list[dict[str, str]], *, include_system: bool) -> str:
    head = f"{system}\n\n" if include_system and system else ""
    body = "\n\n".join(f"[{m['role']}]\n{m['content']}" for m in messages)
    return f"{head}<conversation>\n{body}\n</conversation>\n"


class CliModel:
    """A vendor CLI as a subprocess. The agent never sees a token (R1); nothing falls back (R2)."""

    def __init__(
        self,
        provider: str,
        *,
        model: Optional[str] = None,
        runner: Optional[Runner] = None,
        bin: Optional[str] = None,
        timeout_s: float = 180.0,
    ) -> None:
        provider = PROVIDER_ALIASES.get(provider, provider)
        if provider not in SPECS:
            raise ValidationError("Unknown provider")
        if model is not None and (not isinstance(model, str) or not MODEL_ID.match(model)):
            raise ValidationError("Invalid model name")
        if not isinstance(timeout_s, (int, float)) or timeout_s < 1 or timeout_s > 600:
            raise ValidationError("Invalid model timeout")
        self.provider = provider
        self.requested = model
        self.bin = bin or PROVIDER_BIN[provider]
        self.timeout_s = float(timeout_s)
        self.cost_per_call_usd = 0.0  # subscription CLIs: no per-call metering
        self._runner: Runner = runner or run_cli
        self.last: Optional[Answered] = None

    def complete(self, system: str, messages: list[dict[str, str]]) -> ModelReply:
        convo = _check_messages(messages)
        spec = SPECS[self.provider]
        # claude takes the system prompt as its own flag; every other CLI gets it inside the prompt.
        prompt = render_prompt(system, convo, include_system=self.provider != "claude")
        if spec.max_prompt_chars and len(prompt) > spec.max_prompt_chars:
            raise ProviderUnavailable(
                self.provider,
                f"the prompt is too large for this CLI's command line ({len(prompt)} > {spec.max_prompt_chars} characters); "
                "start a focused run",
            )
        work = tempfile.mkdtemp(prefix="echo-adk-provider-")
        try:
            if not spec.stdin:
                path = os.path.join(work, "prompt.txt")
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(prompt)
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
            try:
                result: RunResult = self._runner(
                    self.bin,
                    spec.args(self.requested, work, prompt, system),
                    cwd=work,
                    env=child_env(),
                    input_text=prompt if spec.stdin else None,
                    timeout_s=self.timeout_s,
                    max_bytes=MAX_CLI_OUTPUT_BYTES,
                )
            except SpawnFailed:
                raise ProviderUnavailable(self.provider, f'could not start "{self.bin}"') from None
            except ProviderUnavailable:
                raise
            except Exception:
                log.debug("cli_runner_error", provider=self.provider, exc_info=True)
                raise ProviderUnavailable(self.provider, "could not run") from None
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if result.timed_out:
            raise ProviderUnavailable(self.provider, "timed out or returned too much output")
        try:
            text, model_name = spec.parse(result.stdout)
        except ProviderUnavailable:
            raise
        except Exception:
            raise ProviderUnavailable(
                self.provider, f"exited with code {result.code}" if result.code else "returned no usable answer"
            ) from None
        if result.code:
            raise ProviderUnavailable(self.provider, f"exited with code {result.code}")
        clean, _ = redact(text)
        self.last = Answered(
            self.provider,
            self.requested or DEFAULT_CLI_MODEL.get(self.provider),
            model_name,
            model_name is not None,
        )
        return ModelReply(clean, self.last)


# --------------------------------------------------------------------------- Factory


MODEL_IDS = ("qwen-local", *CLI_PROVIDERS)
_CONFIG_KEYS = ("endpoint", "model", "timeout_s", "cli")


def parse_models_config(value: Any) -> dict[str, Any]:
    """Strict models config: loopback endpoint, model name, per-CLI model pins. Credential fields refused."""
    config: dict[str, Any] = {
        "endpoint": os.environ.get("ECHO_ADK_MODEL_ENDPOINT", DEFAULT_LOOPBACK_ENDPOINT),
        "model": os.environ.get("ECHO_ADK_MODEL_NAME", DEFAULT_LOOPBACK_MODEL),
        "timeout_s": 120.0,
        "cli": {},
    }
    if value is None:
        validate_endpoint(config["endpoint"])
        return config
    if not isinstance(value, Mapping):
        raise ValidationError("Invalid models configuration: must be an object")
    _refuse_secret_fields(value, "models")
    extra = set(value) - set(_CONFIG_KEYS)
    if extra:
        raise ValidationError(f"Invalid models configuration: unknown field {sorted(extra)[0]!r}")
    if "endpoint" in value:
        config["endpoint"] = validate_endpoint(value["endpoint"])
    else:
        validate_endpoint(config["endpoint"])
    if "model" in value:
        if not isinstance(value["model"], str) or not MODEL_ID.match(value["model"]):
            raise ValidationError("Invalid models configuration: model")
        config["model"] = value["model"]
    if "timeout_s" in value:
        timeout = value["timeout_s"]
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0 or timeout > 600:
            raise ValidationError("Invalid models configuration: timeout_s")
        config["timeout_s"] = float(timeout)
    if "cli" in value:
        cli = value["cli"]
        if not isinstance(cli, Mapping):
            raise ValidationError("Invalid models configuration: cli must be an object")
        for provider, entry in cli.items():
            canonical = PROVIDER_ALIASES.get(str(provider), str(provider))
            if canonical not in SPECS:
                raise ValidationError("Invalid models configuration: unknown cli provider field")
            if not isinstance(entry, Mapping):
                raise ValidationError(f"Invalid models configuration: cli.{canonical} must be an object")
            if set(entry) - {"model"}:
                raise ValidationError(f"Invalid models configuration: unknown cli.{canonical} field")
            model = entry.get("model")
            if model is not None and (not isinstance(model, str) or not MODEL_ID.match(model)):
                raise ValidationError(f"Invalid models configuration: cli.{canonical}.model")
            config["cli"][canonical] = {"model": model} if model else {}
    return config


def build_model(model_id: str, config: Optional[Mapping[str, Any]] = None, *, runner: Optional[Runner] = None) -> Any:
    """The one place a Model is built from a lane model id. Unknown ids fail; nothing is substituted."""
    parsed = parse_models_config(dict(config) if config is not None and not isinstance(config, dict) else config)
    canonical = PROVIDER_ALIASES.get(model_id, model_id)
    if canonical in ("qwen-local", "qwen", "loopback"):
        return LoopbackModel(parsed["endpoint"], parsed["model"], timeout_s=parsed["timeout_s"])
    if canonical in SPECS:
        pinned = parsed["cli"].get(canonical, {}).get("model")
        return CliModel(canonical, model=pinned, runner=runner)
    raise ValidationError(f"Unknown model provider {model_id!r}")
