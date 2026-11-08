"""Loopback-only HTTP front for :class:`AdkService` (stdlib ``ThreadingHTTPServer``).

Headless workers, no popups, structured logs on stderr. Every response is JSON with
``Content-Type: application/json``; errors are ``{error, message}``.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import threading
import urllib.parse
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from .. import __version__
from ..core.log import get_logger
from ..core.redact import redact
from ..core.types import ValidationError
from .service import AdkService, ApiError

log = get_logger("http")

MAX_BODY_BYTES = 1024 * 1024
ID = r"(?P<id>[0-9a-f]{32})"
NAME = r"(?P<name>[a-z0-9][a-z0-9-]{0,63})"

Route = tuple[str, re.Pattern[str], str]
ROUTES: list[Route] = [
    ("GET", re.compile(r"^/health$"), "health"),
    ("GET", re.compile(r"^/agents$"), "agents"),
    ("GET", re.compile(rf"^/agents/{NAME}$"), "agent"),
    ("POST", re.compile(r"^/agents$"), "create_agent"),
    ("PUT", re.compile(rf"^/agents/{NAME}$"), "replace_agent"),
    ("DELETE", re.compile(rf"^/agents/{NAME}$"), "delete_agent"),
    ("POST", re.compile(r"^/runs$"), "create_run"),
    ("GET", re.compile(r"^/runs$"), "list_runs"),
    ("GET", re.compile(rf"^/runs/{ID}$"), "get_run"),
    ("POST", re.compile(rf"^/runs/{ID}/stop$"), "stop_run"),
    ("GET", re.compile(r"^/bots$"), "list_bots"),
    ("POST", re.compile(r"^/bots$"), "create_bot"),
    ("GET", re.compile(rf"^/bots/{ID}$"), "get_bot"),
    ("POST", re.compile(rf"^/bots/{ID}/resume$"), "resume_bot"),
    ("POST", re.compile(rf"^/bots/{ID}/pause$"), "pause_bot"),
    ("DELETE", re.compile(rf"^/bots/{ID}$"), "delete_bot"),
    ("POST", re.compile(rf"^/bots/{ID}/message$"), "message_bot"),
    ("GET", re.compile(rf"^/bots/{ID}/messages$"), "bot_messages"),
    ("POST", re.compile(r"^/groups$"), "create_group"),
    ("GET", re.compile(rf"^/groups/{ID}$"), "get_group"),
]
V2_ROUTES: list[Route] = [
    ("POST", re.compile(rf"^/bots/{ID}/events$"), "bot_event"),
    ("PUT", re.compile(rf"^/bots/{ID}$"), "update_bot"),
    ("POST", re.compile(rf"^/bots/{ID}/stop$"), "stop_bot"),
    ("GET", re.compile(r"^/bots/marketplace$"), "workspace_marketplace"),
    ("POST", re.compile(r"^/bots/connections/callback$"), "workspace_callback"),
    ("GET", re.compile(r"^/bots/templates$"), "workspace_team_templates"),
    ("GET", re.compile(r"^/bots/templates/get$"), "workspace_team_template"),
    ("POST", re.compile(r"^/bots/export$"), "workspace_team_export"),
    ("POST", re.compile(r"^/bots/import/preview$"), "workspace_team_preview"),
    ("POST", re.compile(r"^/bots/import$"), "workspace_team_import"),
    ("GET", re.compile(r"^/bots/imports$"), "workspace_team_imports"),
    ("POST", re.compile(r"^/bots/imports/inspect$"), "workspace_team_inspect"),
    ("POST", re.compile(r"^/bots/imports/recover$"), "workspace_team_recover"),
    ("GET", re.compile(rf"^/bots/{ID}/browser$"), "workspace_browser_status"),
    ("POST", re.compile(rf"^/bots/{ID}/browser/actions$"), "workspace_browser_act"),
    ("POST", re.compile(rf"^/bots/{ID}/browser/close$"), "workspace_browser_close"),
    ("POST", re.compile(rf"^/bots/{ID}/browser/reset$"), "workspace_browser_reset"),
    ("GET", re.compile(rf"^/bots/{ID}/browser/artifacts$"), "workspace_browser_artifact"),
    ("GET", re.compile(rf"^/bots/{ID}/connections$"), "workspace_connection_list"),
    ("POST", re.compile(rf"^/bots/{ID}/connections/get$"), "workspace_connection_get"),
    ("POST", re.compile(rf"^/bots/{ID}/connections/authorize$"), "workspace_connection_authorize"),
    ("POST", re.compile(rf"^/bots/{ID}/connections/invoke$"), "workspace_connection_invoke"),
    ("POST", re.compile(rf"^/bots/{ID}/connections/refresh$"), "workspace_connection_refresh"),
    ("POST", re.compile(rf"^/bots/{ID}/connections/disconnect$"), "workspace_connection_disconnect"),
    ("GET", re.compile(rf"^/bots/{ID}/subscriptions$"), "workspace_subscription_list"),
    ("POST", re.compile(rf"^/bots/{ID}/subscriptions$"), "workspace_subscription_create"),
    ("POST", re.compile(rf"^/bots/{ID}/subscriptions/enable$"), "workspace_subscription_enable"),
    ("POST", re.compile(rf"^/bots/{ID}/subscriptions/disable$"), "workspace_subscription_disable"),
    ("POST", re.compile(rf"^/bots/{ID}/subscriptions/delete$"), "workspace_subscription_delete"),
    ("POST", re.compile(rf"^/bots/{ID}/subscriptions/poll$"), "workspace_subscription_poll"),
    ("GET", re.compile(rf"^/bots/{ID}/memory$"), "workspace_memory_read"),
    ("PUT", re.compile(rf"^/bots/{ID}/memory$"), "workspace_memory_edit"),
    ("GET", re.compile(rf"^/bots/{ID}/memory/revisions$"), "workspace_memory_revisions"),
    ("POST", re.compile(rf"^/bots/{ID}/memory/restore$"), "workspace_memory_restore"),
]


def is_loopback_host(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = f"echo-adk-api/{__version__}"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    service: AdkService  # set on the server class

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - BaseHTTPRequestHandler signature
        log.debug("http", line=(format % args)[:300])

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_PATCH(self) -> None:
        self._send(405, {"error": "invalid", "message": "method not allowed"})

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Any:
        length_header = self.headers.get("Content-Length")
        try:
            length = int(length_header or 0)
        except ValueError:
            raise ApiError(400, "invalid", "bad Content-Length")
        if length < 0 or length > MAX_BODY_BYTES:
            raise ApiError(413, "invalid", "body larger than 1 MiB")
        if length and self.headers.get_content_type() != "application/json":
            raise ApiError(415, "invalid", "Content-Type must be application/json")
        raw = self.rfile.read(length) if length else b""
        if not raw.strip():
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(400, "invalid", "body is not valid JSON")

    def _dispatch(self, method: str) -> None:
        service: AdkService = type(self.server).service  # type: ignore[attr-defined]
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        contract_version = 2 if path.startswith("/v2/") else 1
        if contract_version == 2:
            path = path[3:]
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        matched_path = False
        try:
            # A loopback bind alone permits DNS rebinding and cross-origin HTML
            # form writes. Prime is a native server-to-server client and never
            # forwards browser Origin or Fetch Metadata to this worker.
            hosts = self.headers.get_all("Host", [])
            try:
                host = urllib.parse.urlsplit("http://" + (hosts[0] if len(hosts) == 1 else ""))
                trusted_host = (
                    len(hosts) == 1
                    and len(hosts[0]) <= 300
                    and host.hostname is not None
                    and is_loopback_host(host.hostname)
                    and not host.username
                    and not host.password
                    and not host.path
                    and not host.query
                    and not host.fragment
                )
                _ = host.port  # reject malformed ports as well
            except ValueError:
                trusted_host = False
            if not trusted_host or self.headers.get("Origin") is not None or self.headers.get("Sec-Fetch-Site") not in (None, "none"):
                raise ApiError(403, "denied", "Only native loopback clients may access this service")
            for verb, pattern, name in ROUTES + (V2_ROUTES if contract_version == 2 else []):
                match = pattern.match(path)
                if not match:
                    continue
                matched_path = True
                if verb != method:
                    continue
                body = self._read_json() if method in ("POST", "PUT", "DELETE") else None
                with service.workspace.admission():
                    status, payload = self._handle(service, name, match.groupdict(), query, body, contract_version=contract_version)
                self._send(status, payload)
                return
            if matched_path:
                raise ApiError(405, "invalid", "method not allowed")
            raise ApiError(404, "not_found", "no such route")
        except ApiError as exc:
            self.close_connection = True  # an admission failure can leave an unread request body
            self._send(exc.status, {**exc.to_dict(), "message": redact(exc.message)[0]})
        except ValidationError as exc:
            self._send(400, {"error": "invalid", "message": redact(str(exc))[0]})
        except Exception as exc:  # never leak a traceback to the client
            log.error("http_internal_error", path=path, exc_info=True)
            self._send(500, {"error": "unavailable", "message": f"internal error: {type(exc).__name__}"})

    @staticmethod
    def _one(query: dict[str, list[str]], key: str) -> Optional[str]:
        values = query.get(key)
        return values[-1] if values else None

    def _handle(
        self,
        service: AdkService,
        name: str,
        params: dict[str, str],
        query: dict[str, list[str]],
        body: Any,
        *,
        contract_version: int = 1,
    ) -> tuple[int, Any]:
        def bot_response(bot: dict[str, Any]) -> dict[str, Any]:
            if contract_version == 2:
                return {**bot, "budget": service.budget_health(bot["bot_id"])}
            schedule = bot.get("schedule")
            if isinstance(schedule, dict) and set(schedule) - {"every_s"}:
                raise ApiError(409, "invalid", "This bot schedule requires the v2 API; upgrade this client")
            return {key: value for key, value in bot.items() if key not in ("next_run_at", "last_scheduled_at")}

        if name.startswith("workspace_"):
            if any(len(values) != 1 for values in query.values()):
                raise ApiError(400, "invalid", "Duplicate query parameters are refused")
            return 200, service.workspace.dispatch(name, params.get("id"), body, {key: values[0] for key, values in query.items()})
        if name == "health":
            if contract_version == 2:
                return 200, {
                    **service.health(),
                    "contract_version": 2,
                    "features": {
                        "calendar_schedules": True,
                        "idempotent_events": True,
                        "memory_recall": True,
                        "bot_editing": True,
                        "bot_stopping": True,
                        "extended_schedules": True,
                        "bot_workspaces": True,
                        "oauth_marketplace": True,
                        "editable_memory": True,
                        "portable_teams": True,
                        "connection_events": True,
                    },
                    "budgets": service.budget_health(),
                    "connections": {
                        "memory": {"provider": "local", "recall": True, "durable": True, "shared": False},
                        "browser": service.workspace.browser.health(),
                        "oauth": {"configured": service.workspace.connections.catalog()["configured"], "per_bot": True},
                        "skills": {
                            "configured": service.skill_library is not None,
                            "provider": "installed",
                            "scripts_execute_automatically": False,
                        },
                    },
                }
            return 200, service.health()
        if name == "agents":
            return 200, service.list_agents()
        if name == "agent":
            return 200, service.agent_dict(params["name"])
        if name == "create_agent":
            return 201, service.create_agent(body)
        if name == "replace_agent":
            return 200, service.replace_agent(params["name"], body)
        if name == "delete_agent":
            return 200, service.delete_agent(params["name"])
        if name == "create_run":
            return 202, service.create_run(body)
        if name == "list_runs":
            return 200, service.list_runs(self._one(query, "agent"), self._one(query, "limit"))
        if name == "get_run":
            run = service.get_run(params["id"])
            if contract_version == 2:
                run["budget_accounting"] = service.run_budget(params["id"])
            return 200, run
        if name == "stop_run":
            return 200, service.stop_run(params["id"])
        if name == "list_bots":
            return 200, {"bots": [bot_response(bot) for bot in service.bots.list_bots()["bots"]]}
        if name == "create_bot":
            if contract_version == 1 and isinstance(body, dict) and isinstance(body.get("schedule"), dict):
                if set(body["schedule"]) - {"every_s"}:
                    raise ApiError(400, "invalid", "Calendar schedules and missed-run policies require POST /v2/bots")
            return 201, bot_response(service.create_bot(body))
        if name == "get_bot":
            return 200, bot_response(service.bots.get_bot(params["id"]))
        if name == "update_bot":
            return 200, bot_response(service.bots.update_bot(params["id"], body))
        if name == "stop_bot":
            if body != {}:
                raise ApiError(400, "invalid", "stop takes no fields")
            result = service.bots.stop_bot(params["id"])
            try:
                result["browser"] = service.workspace.browser.close(params["id"])
            except Exception:
                result["browser"] = {"status": "unknown", "message": "Browser close could not be verified"}
            return 200, result
        if name == "resume_bot":
            bot_response(service.bots.get_bot(params["id"]))  # check version before mutation
            return 200, bot_response(service.bots.resume(params["id"]))
        if name == "pause_bot":
            bot_response(service.bots.get_bot(params["id"]))
            return 200, bot_response(service.bots.pause(params["id"]))
        if name == "delete_bot":
            result = service.bots.delete(params["id"])
            return 200, result if contract_version == 2 else {key: result[key] for key in ("bot_id", "deleted")}
        if name == "message_bot":
            return 202, service.message_bot(params["id"], body)
        if name == "bot_event":
            return 202, service.bots.deliver_event(params["id"], body)
        if name == "bot_messages":
            messages = service.bot_messages(params["id"], self._one(query, "since"), self._one(query, "limit"))
            if contract_version == 1:
                messages = {
                    **messages,
                    "messages": [{key: value for key, value in item.items() if key != "event"} for item in messages["messages"]],
                }
            return 200, messages
        if name == "create_group":
            return 202, service.create_group(body)
        if name == "get_group":
            return 200, service.bots.get_group(params["id"])
        raise ApiError(404, "not_found", "no such route")


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    service: AdkService

    def server_bind(self) -> None:  # refuse a non-loopback bind even if a caller got this far
        host = self.server_address[0]
        if not is_loopback_host(str(host)):
            raise ValidationError(f"echo-adk-api binds to loopback only; refused {host!r}")
        super().server_bind()


def make_server(service: AdkService, host: str = "127.0.0.1", port: int = 8378) -> ApiServer:
    if not is_loopback_host(host):
        raise ValidationError(f"echo-adk-api binds to loopback only; refused host {host!r}")
    if not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValidationError("port must be 0..65535")
    server_class = type("BoundApiServer", (ApiServer,), {"service": service})
    if ":" in host:
        server_class.address_family = socket.AF_INET6
    server = server_class((host, port), Handler)
    return server


def serve(service: AdkService, host: str = "127.0.0.1", port: int = 8378, *, ready: Optional[Callable[[int], None]] = None) -> None:
    server = make_server(service, host, port)
    bound = server.server_address[1]
    log.info("listening", host=host, port=bound, version=__version__)
    if ready is not None:
        ready(bound)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()


class BackgroundServer:
    """Run the API on a thread (tests, embedding). ``with BackgroundServer(service) as url:``."""

    def __init__(self, service: AdkService, host: str = "127.0.0.1", port: int = 0) -> None:
        self.server = make_server(service, host, port)
        self.port = self.server.server_address[1]
        self.url = f"http://{host}:{self.port}"
        self._thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)

    def __enter__(self) -> str:
        self._thread.start()
        return self.url

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)
