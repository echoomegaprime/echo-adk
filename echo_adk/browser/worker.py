"""Private JSON-lines provider process. Never import this module for a health check.

Only fixed internal DOM readers execute script. No caller-supplied JavaScript,
selectors, launch arguments, storage export or host file operations are accepted.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import uuid
from pathlib import Path

from echo_adk.core.redact import redact

from .config import MAX_ARTIFACT_BYTES, MAX_ELEMENTS, MAX_TEXT_CHARS, VIEWPORT, BrowserConfig
from .errors import BrowserError
from .policy import NetworkPolicy, safe_url
from .proxy import FilteringProxy
from .storage import ProfileLock

_MAX_MESSAGE = 1024 * 1024
_TEXT = r"""() => {
  const out = []; let size = 0;
  const walk = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
  while(walk.nextNode() && size < 24000) {
    const node=walk.currentNode, el=node.parentElement;
    if(!el || el.closest('script,style,noscript,input,textarea,select,[contenteditable],iframe,[hidden],[aria-hidden="true"]')) continue;
    const style=getComputedStyle(el);
    if(style.visibility==='hidden' || style.display==='none' || el.getClientRects().length===0) continue;
    const text=(node.textContent || '').replace(/\s+/g,' ').trim();
    if(text) { out.push(text); size+=text.length; }
  }
  return out.join('\n').slice(0,24000);
}"""
_ELEMENT = r"""el => {
  if(!el.isConnected || el.ownerDocument!==document || !el.getClientRects().length) return null;
  const style=getComputedStyle(el);
  if(style.visibility==='hidden' || style.display==='none') return null;
  const tag=el.tagName.toLowerCase(), type=(el.getAttribute('type') || '').toLowerCase();
  const fields=['name','id','autocomplete','aria-label','placeholder'].map(k=>el.getAttribute(k)||'').join(' ');
  const secretPattern=/password|passwd|secret|token|credential|api[-_ ]?key|authorization|one[-_ ]?time|otp|cc-|credit[-_ ]?card|ssn/i;
  const sensitive=type==='password' || type==='file' || secretPattern.test(fields);
  const roles={button:'button',a:'link',select:'combobox'};
  let role=el.getAttribute('role') || roles[tag] || (type==='checkbox'?'checkbox':type==='radio'?'radio':'textbox');
  let name=el.getAttribute('aria-label') || '';
  if(!name && el.labels) name=Array.from(el.labels).map(x=>x.textContent||'').join(' ');
  if(!name && !['input','textarea','select'].includes(tag) && !el.isContentEditable) name=el.innerText || '';
  if(!name) name=el.getAttribute('placeholder') || '';
  const textInput=tag==='input' && !['checkbox','radio','button','submit','reset','file','hidden'].includes(type);
  const editable=!el.disabled && !el.readOnly && (tag==='textarea' || textInput || el.isContentEditable);
  return {role,name:name.slice(0,256),sensitive,editable};
}"""


class _Session:
    def __init__(self, command: dict):
        self.refs = {}
        self.context = self.provider = self.proxy = self.lock = None
        self.page = None
        self.config = BrowserConfig(**command["config"])
        self.policy = NetworkPolicy(self.config)
        profile = Path(command["profile"])
        # The manager controls this absolute path; no model input reaches it.
        if not profile.is_absolute() or profile.is_symlink():
            raise BrowserError("invalid_profile", 409, "Browser profile is not a private regular directory")
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = ProfileLock(profile.parent / "profile.lock")
        self.proxy = FilteringProxy(self.policy)
        try:
            from playwright.sync_api import sync_playwright

            self.provider = sync_playwright().start()
            self.context = self.provider.chromium.launch_persistent_context(
                str(profile),
                executable_path=self.config.executable,
                headless=True,
                chromium_sandbox=self.config.chromium_sandbox,
                timeout=command["timeout_ms"],
                viewport=VIEWPORT,
                device_scale_factor=1,
                accept_downloads=False,
                service_workers="block",
                permissions=[],
                proxy={"server": self.proxy.url, "bypass": "<-loopback>"},
                args=[
                    "--disable-quic",
                    # Prevent HTTP/2 origin coalescing inside an allowed CONNECT
                    # tunnel from sharing it with a different HTTPS origin.
                    "--disable-http2",
                    # DNS belongs to the checking proxy, including prefetches.
                    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
                    "--auth-schemes=basic,digest",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                    "--disable-background-networking",
                    "--disable-component-update",
                    "--disable-sync",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
                # The worker environment was allowlisted before Playwright's
                # driver existed. Pass it explicitly to Chromium as well.
                env=dict(os.environ),
            )
            self.context.route("**/*", self._route)
            # Never call connect_to_server. Closing from the initial synchronous
            # route callback deadlocks Playwright 1.62 before page navigation can
            # finish. Ordinary page scripts get an explicit SecurityError; the
            # context route is an additional no-upstream boundary.
            self.context.route_web_socket("**/*", lambda route: None)
            self.context.add_init_script("""(() => {
              class DisabledWebSocket {
                constructor() { throw new DOMException('WebSockets are disabled by the bot browser policy', 'SecurityError'); }
              }
              Object.defineProperty(globalThis, 'WebSocket', {value: DisabledWebSocket, writable: false, configurable: false});
            })();""")
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            for page in self.context.pages:
                if page != self.page:
                    page.close()
            self.context.on("page", lambda page: page.close() if page != self.page else None)
            self.page.on("dialog", lambda dialog: dialog.dismiss())
            self.page.on("download", lambda download: download.cancel())
            self.page.on("framenavigated", self._navigation)
            self.version = self.context.browser.version if self.context.browser else "unknown"
        except Exception:
            self.close()
            raise

    def _navigation(self, frame):
        if self.page and frame == self.page.main_frame:
            self._clear_refs()

    def _clear_refs(self):
        refs, self.refs = self.refs, {}
        for handle in refs.values():
            try:
                handle.dispose()
            except Exception:
                pass

    def _route(self, route):
        try:
            self.policy.validate(route.request.url, navigation=route.request.is_navigation_request())
            route.continue_()
        except (BrowserError, ValueError):
            self.proxy.server.budget.deny()
            route.abort("blockedbyclient")

    def _clean(self, text: str) -> str:
        # Session cookie material remains inside this isolated provider process.
        # In particular, HttpOnly values are never injected into page JavaScript.
        for value in self._cookie_values:
            if len(value) >= 4:
                text = text.replace(value, "[REDACTED]")
        return redact(text)[0]

    def snapshot(self):
        self._clear_refs()
        self._cookie_values = [cookie.get("value", "") for cookie in self.context.cookies()]
        raw = self.page.evaluate(_TEXT)
        cleaned = self._clean(raw)
        elements = []
        # Limit handles before crossing the browser IPC boundary, even when a
        # hostile page creates millions of matching nodes.
        collection = self.page.evaluate_handle(r"""() => Array.from(document.querySelectorAll(
            'a[href],button,input:not([type="hidden"]),textarea,select,[contenteditable="true"],[role="button"],[role="link"]'
        )).slice(0,400)""")
        handles = [value.as_element() for value in collection.get_properties().values() if value.as_element() is not None]
        collection.dispose()
        for handle in handles:
            keep = False
            try:
                info = handle.evaluate(_ELEMENT)
                if info and not info["sensitive"] and len(elements) < MAX_ELEMENTS:
                    ref = uuid.uuid4().hex
                    self.refs[ref] = handle
                    elements.append({"ref": ref, "role": info["role"][:40], "name": self._clean(info["name"])[:256]})
                    keep = True
            finally:
                if not keep:
                    handle.dispose()
        return {
            "status": "ready",
            "url": self._clean(safe_url(self.page.url)),
            "title": self._clean(self.page.title())[:256],
            "text": cleaned[:MAX_TEXT_CHARS],
            "elements": elements,
            "truncated": len(cleaned) > MAX_TEXT_CHARS or len(handles) > MAX_ELEMENTS,
        }

    def execute(self, command: dict):
        action = command["action"]
        timeout = command["timeout_ms"]
        deadline = time.monotonic() + timeout / 1000
        self.page.set_default_timeout(timeout)
        self.page.set_default_navigation_timeout(timeout)
        self.proxy.begin(deadline)
        try:
            if action == "navigate":
                self.policy.validate(command["url"], navigation=True)
                self._clear_refs()
                response = self.page.goto(command["url"], wait_until="domcontentloaded", timeout=timeout)
                self.policy.validate(self.page.url, navigation=True)
                if response and response.status == 403 and self.proxy.denied:
                    raise BrowserError("network_denied", 403, "Browser navigation was refused by the host network policy")
            elif action in ("click", "fill"):
                handle = self.refs.get(command["ref"])
                if handle is None:
                    raise BrowserError("reference_missing", 404, "Browser reference is missing or belongs to another snapshot")
                info = handle.evaluate(_ELEMENT)
                if info is None:
                    raise BrowserError("reference_stale", 409, "Browser reference no longer belongs to a visible element")
                if info["sensitive"]:
                    raise BrowserError("credential_field", 403, "Credential and file controls are unavailable to browser tools")
                if action == "fill":
                    if not info["editable"]:
                        raise BrowserError("reference_not_editable", 400, "Browser reference is not an editable text field")
                    handle.fill(command["value"], timeout=timeout)
                else:
                    handle.click(timeout=timeout, no_wait_after=False)
            elif action == "screenshot":
                png = self.page.screenshot(
                    type="png",
                    full_page=False,
                    animations="disabled",
                    caret="hide",
                    mask=[self.page.locator("input,textarea,[contenteditable],iframe")],
                    timeout=timeout,
                )
                if len(png) > MAX_ARTIFACT_BYTES:
                    raise BrowserError("artifact_too_large", 413, "Browser screenshot exceeds the 512 KiB artifact limit")
                return {"status": "ready", "png_base64": base64.b64encode(png).decode("ascii"), **VIEWPORT}
            elif action != "snapshot":
                raise BrowserError("invalid_action", 400, "Unknown browser action")
            return self.snapshot()
        except BrowserError:
            raise
        except Exception as exc:
            if type(exc).__name__ == "TimeoutError":
                raise BrowserError("timeout", 504, "Browser operation exceeded its deadline", unknown_outcome=True) from None
            if self.proxy.denied:
                raise BrowserError(
                    "network_denied",
                    403,
                    "Browser network policy refused a request; action may have partly completed",
                    unknown_outcome=True,
                ) from None
            raise BrowserError(
                "browser_operation_failed", 503, "Browser operation failed; inspect current state before any retry", unknown_outcome=True
            ) from None
        finally:
            self.proxy.end()

    def close(self):
        self._clear_refs()
        for value in (self.context, self.provider):
            if value is not None:
                try:
                    value.close() if value is self.context else value.stop()
                except Exception:
                    pass
        if self.proxy is not None:
            self.proxy.close()
        if self.lock is not None:
            self.lock.close()


def _emit(value: dict):
    wire = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    if len(wire) > _MAX_MESSAGE:
        wire = json.dumps(
            {"ok": False, "error": BrowserError("result_too_large", 413, "Browser result exceeds its transport limit").as_dict()}
        )
    sys.stdout.write(wire + "\n")
    sys.stdout.flush()


def main():
    session = None
    try:
        while True:
            line = sys.stdin.buffer.readline(_MAX_MESSAGE + 1)
            if not line:
                break
            if len(line) > _MAX_MESSAGE or not line.endswith(b"\n"):
                _emit({"ok": False, "error": BrowserError("invalid_request", 413, "Browser IPC request too large").as_dict()})
                break
            try:
                command = json.loads(line)
                if session is None:
                    if command.get("action") != "init":
                        raise BrowserError("invalid_request", 400, "Browser session is not initialized")
                    session = _Session(command)
                    result = {"status": "ready", "browser_version": session.version}
                elif command["action"] == "close":
                    session.close()
                    session = None
                    _emit({"ok": True, "result": {"status": "closed"}})
                    break
                else:
                    result = session.execute(command)
                _emit({"ok": True, "result": result})
            except BrowserError as exc:
                _emit({"ok": False, "error": exc.as_dict()})
            except Exception:
                _emit(
                    {
                        "ok": False,
                        "error": BrowserError(
                            "provider_unavailable", 503, "Playwright Chromium could not start with the configured sandbox and policy"
                        ).as_dict(),
                    }
                )
                if session is None:
                    break
    finally:
        if session is not None:
            session.close()


if __name__ == "__main__":
    main()
