"""Loopback-only HTTP front for :class:`AdkService` (stdlib ``ThreadingHTTPServer``).

Headless: no browser, no popups, structured logs on stderr. Every response is JSON with
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
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        matched_path = False
        try:
            for verb, pattern, name in ROUTES:
                match = pattern.match(path)
                if not match:
                    continue
                matched_path = True
                if verb != method:
                    continue
                body = self._read_json() if method in ("POST", "PUT", "DELETE") else None
                status, payload = self._handle(service, name, match.groupdict(), query, body)
                self._send(status, payload)
                return
            if matched_path:
                raise ApiError(405, "invalid", "method not allowed")
            raise ApiError(404, "not_found", "no such route")
        except ApiError as exc:
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
    ) -> tuple[int, Any]:
        if name == "health":
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
            return 200, service.get_run(params["id"])
        if name == "stop_run":
            return 200, service.stop_run(params["id"])
        if name == "list_bots":
            return 200, service.bots.list_bots()
        if name == "create_bot":
            return 201, service.create_bot(body)
        if name == "get_bot":
            return 200, service.bots.get_bot(params["id"])
        if name == "resume_bot":
            return 200, service.bots.resume(params["id"])
        if name == "pause_bot":
            return 200, service.bots.pause(params["id"])
        if name == "delete_bot":
            return 200, service.bots.delete(params["id"])
        if name == "message_bot":
            return 202, service.message_bot(params["id"], body)
        if name == "bot_messages":
            return 200, service.bot_messages(params["id"], self._one(query, "since"), self._one(query, "limit"))
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
