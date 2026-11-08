"""Real local HTTP traffic through the filtering proxy, without a browser."""

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from echo_adk.browser import BrowserConfig
from echo_adk.browser.policy import NetworkPolicy
from echo_adk.browser.proxy import FilteringProxy


@pytest.fixture
def site():
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            data = b"controlled origin"
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", hits
    server.shutdown()
    server.server_close()
    thread.join(timeout=1)


def request(proxy, wire):
    with socket.create_connection(("127.0.0.1", proxy.server.server_address[1]), timeout=2) as stream:
        stream.sendall(wire.encode())
        result = b""
        while True:
            part = stream.recv(65536)
            if not part:
                return result
            result += part


def test_private_origin_needs_exact_exception_and_idle_network_is_disabled(site):
    origin, hits = site
    proxy = FilteringProxy(NetworkPolicy(BrowserConfig(allowed_origins=(origin,))))
    wire = f"GET {origin}/ HTTP/1.1\r\nHost: {origin[7:]}\r\n\r\n"
    try:
        proxy.begin(time.monotonic() + 3)
        assert b"403 Forbidden" in request(proxy, wire)
        assert hits == []
    finally:
        proxy.close()
    proxy = FilteringProxy(NetworkPolicy(BrowserConfig(allowed_origins=(origin,), private_origins=(origin,))))
    try:
        assert b"403 Forbidden" in request(proxy, wire)
        assert hits == []
        proxy.begin(time.monotonic() + 3)
        assert b"controlled origin" in request(proxy, wire)
        assert hits == ["/"]
        proxy.end()
        assert b"403 Forbidden" in request(proxy, wire)
        assert hits == ["/"]
    finally:
        proxy.close()


@pytest.mark.parametrize(
    "tail",
    [
        "Host: other.example\r\n",
        "Host: {host}\r\nHost: {host}\r\n",
        "Host: {host}\r\nTransfer-Encoding: chunked\r\n",
        "Host: {host}\r\nUpgrade: websocket\r\n",
        "Host: {host}\r\nContent-Length: 9999999\r\n",
        "Host: {host}\r\nProxy-Authorization: Basic secret\r\n",
    ],
)
def test_ambiguous_headers_and_websockets_never_reach_origin(site, tail):
    origin, hits = site
    proxy = FilteringProxy(NetworkPolicy(BrowserConfig(allowed_origins=(origin,), private_origins=(origin,))))
    try:
        proxy.begin(time.monotonic() + 3)
        wire = f"GET {origin}/ HTTP/1.1\r\n" + tail.format(host=origin[7:]) + "\r\n"
        assert b"403 Forbidden" in request(proxy, wire)
        assert hits == []
    finally:
        proxy.close()
