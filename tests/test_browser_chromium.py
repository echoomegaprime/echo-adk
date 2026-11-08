"""Opt-in REAL Chromium acceptance, using only controlled local HTTP pages.

Set ECHO_ADK_BROWSER_TEST_EXECUTABLE to a trusted Chrome/Chromium executable and
install echo-adk[browser,dev]. No production state, credentials, models or SDK
network are used. A skipped test is not browser verification.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from echo_adk.browser import BrowserConfig, BrowserError, BrowserManager

EXECUTABLE = os.environ.get("ECHO_ADK_BROWSER_TEST_EXECUTABLE")
pytestmark = pytest.mark.skipif(not EXECUTABLE, reason="Real browser test requires an explicit task-owned executable configuration")
A, B = "a" * 32, "b" * 32


@pytest.fixture
def sites():
    hits = [[], []]
    slow_started = threading.Event()
    release_slow = threading.Event()
    targets = []

    def handler(index):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                hits[index].append(self.path)
                if self.path == "/slow":
                    slow_started.set()
                    release_slow.wait(timeout=15)
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", targets[1] + "/private-target")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                cookie = self.headers.get("Cookie", "none")
                body = f"""<!doctype html><meta charset="utf-8"><title>Controlled browser test</title>
                  <h1>Browser boundary test</h1><p id="cookie">Cookie: {cookie}</p>
                  <label for="note">Note</label><input id="note" aria-label="Note">
                  <button id="save" onclick="localStorage.setItem('note',document.querySelector('#note').value);
                    document.querySelector('#stored').textContent='Stored: '+localStorage.getItem('note')">Save note</button>
                  <p id="stored"></p><input type="password" aria-label="Password" value="never-snapshot-password">
                  <input type="file" aria-label="Upload"><textarea aria-label="API token">never-snapshot-token</textarea>
                  <script>document.querySelector('#stored').textContent='Stored: '+(localStorage.getItem('note')||'empty');</script>"""
                if self.path == "/noise":
                    body = """<!doctype html><style>body{margin:0}</style><canvas width=1280 height=720></canvas>
                    <script>const c=document.querySelector('canvas').getContext('2d'),d=c.createImageData(1280,720);
                    for(let i=0;i<d.data.length;i+=4){d.data[i]=Math.random()*255;d.data[i+1]=Math.random()*255;
                    d.data[i+2]=Math.random()*255;d.data[i+3]=255} c.putImageData(d,0,0);</script>"""
                if self.path == "/cross":
                    body += f'<iframe src="{targets[1]}/frame"></iframe><img src="{targets[1]}/pixel">'
                    body += f"<script>fetch('{targets[1]}/fetch').catch(()=>{{}});new WebSocket('ws://127.0.0.1:{servers[1].server_port}/ws');</script>"
                if self.path == "/webrtc":
                    body += f"""<p id=rtc>RTC preparing</p><script>
                    const rtc=new RTCPeerConnection({{iceServers:[{{urls:'turn:127.0.0.1:{servers[1].server_port}?transport=tcp',
                    username:'controlled-test',credential:'controlled-test'}}],iceTransportPolicy:'relay'}});
                    rtc.createDataChannel('controlled-test');
                    rtc.createOffer().then(o=>rtc.setLocalDescription(o))
                    .then(()=>document.querySelector('#rtc').textContent='RTC configured')
                    .catch(()=>document.querySelector('#rtc').textContent='RTC refused');</script>"""
                data = body.encode()
                self.send_response(200)
                if self.path == "/seed":
                    self.send_header("Set-Cookie", "browser_session=synthetic-cookie-secret; HttpOnly; Max-Age=86400; SameSite=Lax")
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

            def log_message(self, *_):
                pass

        return Handler

    class CountedServer(ThreadingHTTPServer):
        def get_request(self):
            request = super().get_request()
            hits[servers.index(self)].append("tcp-accepted")
            return request

    servers = [CountedServer(("127.0.0.1", 0), handler(i)) for i in range(2)]
    targets.extend(f"http://127.0.0.1:{server.server_port}" for server in servers)
    threads = []
    for server in servers:
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        threads.append(thread)
    yield targets, hits, slow_started
    release_slow.set()
    for server, thread in zip(servers, threads, strict=True):
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


@pytest.fixture
def browser(tmp_path, sites):
    origins, _, _ = sites
    manager = BrowserManager(
        tmp_path / "browser-state",
        BrowserConfig(enabled=True, allowed_origins=(origins[0],), private_origins=(origins[0],), executable=EXECUTABLE),
    )
    try:
        yield manager
    finally:
        manager.shutdown()
        assert not manager._sessions


def element(snapshot, name, role=None):
    return next(row["ref"] for row in snapshot["elements"] if row["name"] == name and (role is None or row["role"] == role))


def test_actual_processes_profiles_cookies_refs_and_artifacts_are_separate(browser, sites, tmp_path):
    origins, _, _ = sites
    assert not browser.health()["available"] and browser.health()["verified_at"] is None
    first = browser.execute(A, {"action": "navigate", "url": origins[0] + "/seed"})
    assert first["untrusted"] is True and first["title"] == "Controlled browser test"
    assert browser.health()["available"] and browser.health()["verified_at"]
    assert browser.health()["chromium_sandbox"] is True and browser.health()["os_isolation"] is False
    assert "never-snapshot" not in json.dumps(first)
    assert all(row["name"] not in ("Password", "Upload", "API token") for row in first["elements"])
    note_ref = element(first, "Note", "textbox")
    filled = browser.execute(A, {"action": "fill", "ref": note_ref, "value": "bot-alpha-private-note"})
    assert "bot-alpha-private-note" not in filled["text"]  # Input values are excluded.
    with pytest.raises(BrowserError) as caught:
        browser.execute(A, {"action": "fill", "ref": note_ref, "value": "stale reference"})
    assert caught.value.status == 404
    saved = browser.execute(A, {"action": "click", "ref": element(filled, "Save note")})
    assert "Stored: bot-alpha-private-note" in saved["text"]
    other = browser.execute(B, {"action": "navigate", "url": origins[0] + "/"})
    assert "bot-alpha-private-note" not in other["text"] and "Stored: empty" in other["text"]
    assert browser._sessions[A].process.pid != browser._sessions[B].process.pid
    with pytest.raises(BrowserError) as caught:
        browser.execute(B, {"action": "click", "ref": element(saved, "Save note")})
    assert caught.value.status == 404
    shot = browser.execute(A, {"action": "screenshot"})["artifact"]
    image = browser.read_artifact(A, shot["artifact_id"])
    png = base64.b64decode(image["base64"])
    assert png.startswith(b"\x89PNG") and len(png) <= 512 * 1024
    assert image["sha256"] == hashlib.sha256(png).hexdigest()
    assert (image["width"], image["height"]) == (1280, 720)
    with pytest.raises(BrowserError) as caught:
        browser.read_artifact(B, shot["artifact_id"])
    assert caught.value.status == 404
    # An optional test report directory contains only this controlled page image.
    report_dir = os.environ.get("ECHO_ADK_BROWSER_TEST_REPORT_DIR")
    if report_dir:
        report_path = Path(report_dir)
        report_path.mkdir(parents=True, exist_ok=True)
        (report_path / "controlled-bot-browser.png").write_bytes(png)
    browser.close(A)
    reopened = browser.execute(A, {"action": "navigate", "url": origins[0] + "/"})
    assert "Stored: bot-alpha-private-note" in reopened["text"]
    assert "synthetic-cookie-secret" not in reopened["text"] and "[REDACTED]" in reopened["text"]
    assert "browser_session" not in other["text"]
    browser.reset(A)
    reset = browser.execute(A, {"action": "navigate", "url": origins[0] + "/"})
    assert "Stored: empty" in reset["text"] and "browser_session" not in reset["text"]
    with pytest.raises(BrowserError):
        browser.read_artifact(A, shot["artifact_id"])


def test_actual_redirect_subresource_websocket_and_private_ip_egress_refusal(browser, sites, tmp_path):
    origins, hits, _ = sites
    browser.execute(A, {"action": "navigate", "url": origins[0] + "/cross"})
    assert hits[1] == []
    with pytest.raises(BrowserError) as caught:
        browser.execute(A, {"action": "navigate", "url": origins[0] + "/redirect"})
    assert caught.value.status == 403
    assert hits[1] == []
    blocked = BrowserManager(tmp_path / "private-denied", BrowserConfig(enabled=True, allowed_origins=(origins[0],), executable=EXECUTABLE))
    before = len(hits[0])
    try:
        with pytest.raises(BrowserError) as caught:
            blocked.execute(A, {"action": "navigate", "url": origins[0] + "/"})
        assert caught.value.status == 403
        assert len(hits[0]) == before, "Chromium must not bypass the proxy for loopback"
    finally:
        blocked.shutdown()


def test_actual_cancellation_kills_only_its_bot_and_never_replays(browser, sites):
    origins, hits, slow_started = sites
    browser.execute(A, {"action": "navigate", "url": origins[0] + "/"})
    browser.execute(B, {"action": "navigate", "url": origins[0] + "/"})
    other_pid = browser._sessions[B].process.pid
    cancel = threading.Event()
    caught = []

    def action():
        try:
            browser.execute(A, {"action": "navigate", "url": origins[0] + "/slow", "timeout_ms": 20000}, cancel=cancel)
        except BrowserError as error:
            caught.append(error)

    thread = threading.Thread(target=action)
    thread.start()
    assert slow_started.wait(timeout=5)
    started = time.monotonic()
    cancel.set()
    thread.join(timeout=4)
    assert not thread.is_alive() and time.monotonic() - started < 4
    assert len(caught) == 1 and caught[0].status == 499 and caught[0].unknown_outcome
    assert hits[0].count("/slow") == 1
    assert A not in browser._sessions
    assert browser._sessions[B].process.pid == other_pid and browser._sessions[B].process.poll() is None
    assert browser.execute(B, {"action": "snapshot"})["title"] == "Controlled browser test"


def test_actual_screenshot_cap_refuses_without_truncation(browser, sites):
    origins, _, _ = sites
    browser.execute(A, {"action": "navigate", "url": origins[0] + "/noise"})
    with pytest.raises(BrowserError) as caught:
        browser.execute(A, {"action": "screenshot"})
    assert caught.value.status == 413
    folder = browser.storage.directory(A) / "artifacts"
    assert not folder.exists() or not list(folder.glob("*.png"))


def test_proxy_owns_dns_resolution_for_an_allowed_hostname(tmp_path, sites):
    origins, hits, _ = sites
    hostname_origin = origins[0].replace("127.0.0.1", "localhost")
    manager = BrowserManager(
        tmp_path,
        BrowserConfig(
            enabled=True,
            allowed_origins=(hostname_origin,),
            private_origins=(hostname_origin,),
            executable=EXECUTABLE,
        ),
    )
    try:
        result = manager.execute(A, {"action": "navigate", "url": hostname_origin + "/"})
        assert result["title"] == "Controlled browser test" and hits[0]
    finally:
        manager.shutdown()


def test_actual_operation_deadline_does_not_accept_late_navigation(browser, sites):
    origins, hits, _ = sites
    browser.execute(A, {"action": "navigate", "url": origins[0] + "/"})
    started = time.monotonic()
    with pytest.raises(BrowserError) as caught:
        browser.execute(A, {"action": "navigate", "url": origins[0] + "/slow", "timeout_ms": 500})
    assert caught.value.status == 504 and caught.value.unknown_outcome
    assert time.monotonic() - started < 3 and hits[0].count("/slow") == 1
    assert A not in browser._sessions


def test_webrtc_cannot_open_a_direct_private_tcp_socket_while_idle(browser, sites):
    origins, hits, _ = sites
    browser.execute(A, {"action": "navigate", "url": origins[0] + "/webrtc"})
    time.sleep(0.5)  # Allow the real browser's ICE gathering to attempt its transport.
    snapshot = browser.execute(A, {"action": "snapshot"})
    assert "RTC configured" in snapshot["text"] or "RTC refused" in snapshot["text"]
    assert hits[1] == [], "WebRTC must not bypass the checking proxy with a direct TCP connection"


def test_real_probe_is_cached_and_health_never_launches(browser, monkeypatch):
    status = browser.status(A)
    assert status["available"] is True and status["verified_at"]
    assert not browser._sessions  # The probe used and closed its own inert profile.
    monkeypatch.setattr("echo_adk.browser.runtime.Worker", lambda *a: pytest.fail("cached status/health must not launch"))
    assert browser.status(B)["verified_at"] == status["verified_at"]
    assert browser.health()["available"] is True


def test_sdk_to_actual_http_browser_scopes_and_cross_bot_resources(agents_copy, tmp_path, sites):
    from echo_adk.api.server import BackgroundServer
    from echo_adk.api.service import AdkService
    from echo_adk.sdk.operations import SdkOperations

    def forbidden(*a, **kw):
        raise AssertionError("This browser test must not call a model or live broker")

    class NoBroker:
        invoke = forbidden

    service = AdkService(agents_copy, tmp_path / "http-state", model_factory=forbidden, broker=NoBroker(), start_scheduler=False)
    origins, _, _ = sites
    service.workspace.browser.shutdown()
    service.workspace.browser = BrowserManager(
        service.state_dir,
        BrowserConfig(enabled=True, allowed_origins=(origins[0],), private_origins=(origins[0],), executable=EXECUTABLE),
    )
    owner = object()
    read_only = object()

    def authorize(operation, context):
        return context is owner or (context is read_only and operation.name == "bot.browser.status")

    try:
        with BackgroundServer(service) as origin:
            adapter = SdkOperations(origin, authorize=authorize, timeout_s=30)

            def call(operation, params, context=owner):
                reply = adapter.invoke(operation, params, context=context)
                assert 200 <= reply.status_code < 300 and reply.body["ok"] is True, reply
                return reply.body["data"]

            call(
                "agent.create",
                {"name": "browser-test", "role": "researcher", "capabilities": [], "lane": {"node": "forge", "model": "qwen-local"}},
            )
            bots = [call("bot.create", {"name": name, "agent": "browser-test", "lease": {"scopes": []}})["bot_id"] for name in ("A", "B")]
            denied = adapter.invoke("bot.browser.act", {"bot_id": bots[0], "action": "navigate", "url": origins[0]}, context=read_only)
            assert denied.status_code == 403 and denied.body["outcome"] == "not_started"
            assert not service.workspace.browser._sessions
            snap = call("bot.browser.act", {"bot_id": bots[0], "action": "navigate", "url": origins[0]})
            assert call("bot.browser.status", {"bot_id": bots[0]}, context=read_only)["available"]
            other = call("bot.browser.act", {"bot_id": bots[1], "action": "navigate", "url": origins[0]})
            assert other["bot_id"] == bots[1]
            crossed = adapter.invoke(
                "bot.browser.act", {"bot_id": bots[1], "action": "click", "ref": element(snap, "Save note")}, context=owner
            )
            assert crossed.status_code == 404
            artifact = call("bot.browser.act", {"bot_id": bots[0], "action": "screenshot"})["artifact"]
            own = call("bot.browser.artifact", {"bot_id": bots[0], "artifact_id": artifact["artifact_id"]})
            assert hashlib.sha256(base64.b64decode(own["base64"])).hexdigest() == artifact["sha256"]
            crossed = adapter.invoke("bot.browser.artifact", {"bot_id": bots[1], "artifact_id": artifact["artifact_id"]}, context=owner)
            assert crossed.status_code == 404
            assert call("bot.browser.close", {"bot_id": bots[0]})["closed"]
            assert call("bot.browser.reset", {"bot_id": bots[0]})["reset"]
            assert not service.store.list_runs(), "No model dispatch is needed for owner browser controls"
    finally:
        service.close()
