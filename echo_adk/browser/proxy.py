"""Task-owned filtering proxy for Chromium, including each redirect connection.

No traffic or headers are logged. Outbound sockets use checked numeric addresses.
Every operation has one finite request/byte/deadline budget; idle sessions cannot
continue using proxy connections. This is not a kernel egress firewall.
"""

from __future__ import annotations

import select
import socket
import socketserver
import threading
import time

from .errors import BrowserError
from .policy import NetworkPolicy

_MAX_HEADER = 16 * 1024
_MAX_REQUEST_BODY = 256 * 1024
_MAX_TRANSFER = 16 * 1024 * 1024
_MAX_REQUESTS = 64


class _Budget:
    def __init__(self):
        self.lock = threading.Lock()
        self.deadline = 0.0
        self.generation = 0
        self.requests = 0
        self.bytes = 0
        self.denied = 0
        self.sockets: set[socket.socket] = set()

    def begin(self, deadline: float):
        self.end()
        with self.lock:
            self.generation += 1
            self.deadline = deadline
            self.requests = self.bytes = self.denied = 0

    def end(self):
        with self.lock:
            self.deadline = 0
            sockets = tuple(self.sockets)
            self.sockets.clear()
        for stream in sockets:
            try:
                stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            stream.close()

    def admit(self, stream: socket.socket) -> int:
        with self.lock:
            if self.deadline <= time.monotonic() or self.requests >= _MAX_REQUESTS:
                raise BrowserError("network_limit", 413, "Browser network operation budget exhausted")
            self.requests += 1
            self.sockets.add(stream)
            return self.generation

    def remaining(self, generation: int, transferred: int = 0) -> float:
        with self.lock:
            self.bytes += transferred
            remaining = self.deadline - time.monotonic()
            if generation != self.generation or remaining <= 0 or self.bytes > _MAX_TRANSFER:
                raise BrowserError("network_limit", 413, "Browser network operation budget exhausted")
            return remaining

    def attach(self, stream: socket.socket, generation: int):
        self.remaining(generation)
        with self.lock:
            self.sockets.add(stream)

    def forget(self, *streams):
        with self.lock:
            for stream in streams:
                self.sockets.discard(stream)

    def deny(self):
        with self.lock:
            self.denied += 1


class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False
    request_queue_size = 16

    def __init__(self, policy: NetworkPolicy):
        self.policy = policy
        self.budget = _Budget()
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(("127.0.0.1", 0), _Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # Raw request/provider errors may contain private URLs; never print them.
        pass


class _Handler(socketserver.BaseRequestHandler):
    server: _Server

    def handle(self):
        upstream = None
        try:
            generation = self.server.budget.admit(self.request)
            self.request.settimeout(min(3.0, self.server.budget.remaining(generation)))
            header, body = self._header()
            request_line, *lines = header.decode("iso-8859-1").split("\r\n")
            pieces = request_line.split(" ")
            if len(pieces) != 3 or pieces[2] not in ("HTTP/1.0", "HTTP/1.1"):
                raise BrowserError("network_denied", 403, "Malformed browser proxy request")
            method, address, _ = pieces
            headers = []
            seen = set()
            for line in lines:
                if not line:
                    continue
                if ":" not in line or line[0] in " \t":
                    raise BrowserError("network_denied", 403, "Malformed browser proxy headers")
                key, value = line.split(":", 1)
                lowered = key.lower()
                if lowered in seen and lowered in ("host", "content-length", "transfer-encoding"):
                    raise BrowserError("network_denied", 403, "Ambiguous browser proxy headers")
                if not key or any(not (c.isalnum() or c in "!#$%&'*+-.^_`|~") for c in key):
                    raise BrowserError("network_denied", 403, "Malformed browser proxy headers")
                if any(ord(c) < 32 and c != "\t" for c in value) or "\x7f" in value:
                    raise BrowserError("network_denied", 403, "Malformed browser proxy headers")
                seen.add(lowered)
                headers.append((key, value.strip()))
            values = {k.lower(): v for k, v in headers}
            if "upgrade" in values or "transfer-encoding" in values or "proxy-authorization" in values:
                raise BrowserError("network_denied", 403, "Browser upgrades and proxy credentials are disabled")
            if method == "CONNECT":
                target = self.server.policy.validate("https://" + address)
                if target.path != "/" or body:
                    raise BrowserError("network_denied", 403, "Malformed browser tunnel request")
            else:
                if method not in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
                    raise BrowserError("network_denied", 403, "Browser HTTP method is disabled")
                target = self.server.policy.validate(address)
                if target.scheme != "http":
                    raise BrowserError("network_denied", 403, "HTTPS must use a checked browser tunnel")
                supplied_host = values.get("host", "")
                supplied = self.server.policy.validate("http://" + supplied_host)
                if supplied.origin != target.origin:
                    raise BrowserError("network_denied", 403, "Browser proxy origin and Host disagree")
            # Resolve once and connect to the checked numeric address. Reject ALL
            # answers when any address is private without the exact exception.
            for family, address_ip in self.server.policy.addresses(target):
                self.server.budget.remaining(generation)
                candidate = socket.socket(family, socket.SOCK_STREAM)
                candidate.settimeout(min(3.0, self.server.budget.remaining(generation)))
                try:
                    candidate.connect((address_ip, target.port))
                    upstream = candidate
                    break
                except OSError:
                    candidate.close()
            if upstream is None:
                raise BrowserError("network_unavailable", 503, "Allowed browser origin did not accept a connection")
            self.server.budget.attach(upstream, generation)
            if method == "CONNECT":
                self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self._tunnel(upstream, generation)
            else:
                try:
                    length = int(values.get("content-length", "0"))
                except ValueError:
                    raise BrowserError("network_denied", 403, "Malformed browser request length") from None
                if not 0 <= length <= _MAX_REQUEST_BODY or len(body) > length:
                    raise BrowserError("network_limit", 413, "Browser request body limit exceeded")
                while len(body) < length:
                    self.request.settimeout(min(3.0, self.server.budget.remaining(generation)))
                    part = self.request.recv(min(65536, length - len(body)))
                    if not part:
                        raise BrowserError("network_denied", 403, "Incomplete browser request body")
                    body += part
                forwarded = [(k, v) for k, v in headers if k.lower() not in ("connection", "proxy-connection", "keep-alive", "expect")]
                wire = f"{method} {target.path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in forwarded) + "Connection: close\r\n\r\n"
                upstream.settimeout(min(3.0, self.server.budget.remaining(generation)))
                upstream.sendall(wire.encode("iso-8859-1") + body)
                self.server.budget.remaining(generation, len(wire) + len(body))
                while True:
                    upstream.settimeout(min(1.0, self.server.budget.remaining(generation)))
                    try:
                        data = upstream.recv(65536)
                    except TimeoutError:
                        continue
                    if not data:
                        break
                    self.server.budget.remaining(generation, len(data))
                    self.request.sendall(data)
        except (BrowserError, OSError, UnicodeError, ValueError):
            self.server.budget.deny()
            try:
                self.request.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                # Refusing an idle connection can happen before reading its
                # request. A close with unread bytes becomes a TCP reset on
                # Windows and can discard the refusal response. Half-close and
                # drain a small, timed bound; never forward or log these bytes.
                self.request.shutdown(socket.SHUT_WR)
                self.request.settimeout(0.05)
                remaining = _MAX_HEADER
                while remaining > 0:
                    data = self.request.recv(min(4096, remaining))
                    if not data:
                        break
                    remaining -= len(data)
            except OSError:
                pass
        finally:
            self.server.budget.forget(self.request, upstream)
            if upstream:
                upstream.close()

    def _header(self):
        data = b""
        while b"\r\n\r\n" not in data:
            if len(data) >= _MAX_HEADER:
                raise BrowserError("network_limit", 413, "Browser request headers are too large")
            part = self.request.recv(min(4096, _MAX_HEADER - len(data)))
            if not part:
                raise BrowserError("network_denied", 403, "Incomplete browser proxy request")
            data += part
        return data.split(b"\r\n\r\n", 1)

    def _tunnel(self, upstream: socket.socket, generation: int):
        streams = (self.request, upstream)
        for stream in streams:
            stream.settimeout(1.0)
        while self.server.budget.remaining(generation) > 0:
            readable, _, _ = select.select(streams, [], [], min(0.2, self.server.budget.remaining(generation)))
            for source in readable:
                data = source.recv(65536)
                if not data:
                    return
                self.server.budget.remaining(generation, len(data))
                (upstream if source is self.request else self.request).sendall(data)


class FilteringProxy:
    def __init__(self, policy: NetworkPolicy):
        self.server = _Server(policy)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    @property
    def denied(self) -> int:
        return self.server.budget.denied

    def begin(self, deadline: float):
        self.server.budget.begin(deadline)

    def end(self):
        self.server.budget.end()

    def close(self):
        self.end()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
