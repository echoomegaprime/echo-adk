"""Owned subprocess lifecycle and bounded JSON IPC; no provider environment secrets."""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .errors import BrowserError

_MAX_MESSAGE = 1024 * 1024
_RUNTIME_ENV = {
    "PATH",
    "SYSTEMROOT",
    "WINDIR",
    "TEMP",
    "TMP",
    "COMSPEC",
    "PATHEXT",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMW6432",
    "PROGRAMDATA",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_IDENTIFIER",
    "PROCESSOR_LEVEL",
    "PROCESSOR_REVISION",
    "APPDATA",
    "LOCALAPPDATA",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LC_ALL",
    "TZ",
}


def worker_environment(private_home: Path, env: dict | None = None) -> dict[str, str]:
    source = os.environ if env is None else env
    result = {key: value for key, value in source.items() if key.upper() in _RUNTIME_ENV}
    # Moving HOME must not silently change Playwright's installed browser lookup.
    # This is a trusted host package-cache path, never a caller-selected profile.
    browser_cache = source.get("PLAYWRIGHT_BROWSERS_PATH")
    if browser_cache is not None:
        if browser_cache != "0" and not Path(browser_cache).is_absolute():
            raise BrowserError("invalid_config", 400, "PLAYWRIGHT_BROWSERS_PATH must be an absolute host path or 0")
    elif os.name == "nt":
        browser_cache = str(Path(source.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "ms-playwright")
    else:
        browser_cache = str(
            Path(source.get("XDG_CACHE_HOME", str(Path(source.get("HOME", str(Path.home()))) / ".cache"))) / "ms-playwright"
        )
    result["PLAYWRIGHT_BROWSERS_PATH"] = browser_cache
    result["HOME"] = str(private_home)
    result["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    result["PYTHONIOENCODING"] = "utf-8"
    result["PYTHONUNBUFFERED"] = "1"
    result["PYTHONDONTWRITEBYTECODE"] = "1"
    return result


class _WindowsJob:
    """The entire owned worker tree dies when this handle is closed."""

    def __init__(self, pid: int):
        import ctypes
        from ctypes import wintypes

        class _Basic(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]

        class _Extended(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _Basic),
                ("IoInfo", _IO),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel = kernel
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise BrowserError("cleanup_unavailable", 503, "An owned browser process cleanup boundary could not be established")
        limits = _Extended()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        process = kernel.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
        try:
            if not process or not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise OSError
            if not kernel.AssignProcessToJobObject(self.handle, process):
                raise OSError
        except OSError:
            self.close()
            raise BrowserError("cleanup_unavailable", 503, "An owned browser process cleanup boundary could not be established") from None
        finally:
            if process:
                kernel.CloseHandle(process)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _linux_identity(pid: int):
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        return text[text.rfind(")") + 2 :].split()[19]
    except (OSError, IndexError):
        return None


def _kill_linux_tree(pid: int, identity: str | None):
    """Freeze only verified descendants before killing, including detached Chrome.

    A process group alone misses browsers launched in another session. Freezing
    each owned ancestor before inspecting its children prevents further spawning.
    PID start times prevent signaling a reused, unrelated PID.
    """
    pending, frozen = [(pid, identity)], []
    seen = set()
    while pending:
        current, expected = pending.pop()
        if current in seen or expected is None or _linux_identity(current) != expected:
            continue
        seen.add(current)
        try:
            os.kill(current, signal.SIGSTOP)
        except ProcessLookupError:
            continue
        frozen.append((current, expected))
        try:
            for thread in Path(f"/proc/{current}/task").iterdir():
                for child in (thread / "children").read_text().split():
                    child_pid = int(child)
                    pending.append((child_pid, _linux_identity(child_pid)))
        except (OSError, ValueError):
            pass
    for current, expected in reversed(frozen):
        if _linux_identity(current) == expected:
            try:
                os.kill(current, signal.SIGKILL)
            except ProcessLookupError:
                pass


class Worker:
    def __init__(self, private_home: Path):
        if os.name != "nt" and not sys.platform.startswith("linux"):
            raise BrowserError("provider_unavailable", 503, "Browser process cleanup is supported only on Linux and Windows")
        self.queue: queue.Queue = queue.Queue(maxsize=2)
        self.job = None
        self.closed = False
        options = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NO_WINDOW}
        self.process = subprocess.Popen(
            [sys.executable, "-m", "echo_adk.browser.worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=str(Path(__file__).resolve().parents[2]),
            env=worker_environment(private_home),
            bufsize=0,
            **options,
        )
        self.identity = _linux_identity(self.process.pid) if os.name != "nt" else None
        try:
            if os.name == "nt":
                self.job = _WindowsJob(self.process.pid)
            elif self.identity is None:
                raise BrowserError("cleanup_unavailable", 503, "Owned browser process identity could not be verified")
        except Exception:
            self.process.kill()
            self.process.wait(timeout=3)
            raise
        self.reader = threading.Thread(target=self._read, name="echo-browser-replies", daemon=True)
        self.reader.start()

    def _read(self):
        try:
            while not self.closed:
                line = self.process.stdout.readline(_MAX_MESSAGE + 1)
                if not line or len(line) > _MAX_MESSAGE or not line.endswith(b"\n"):
                    break
                try:
                    reply = json.loads(line)
                except (ValueError, UnicodeError):
                    break
                try:
                    self.queue.put_nowait(reply)
                except queue.Full:
                    break
        finally:
            try:
                self.queue.put_nowait(None)
            except queue.Full:
                pass

    def request(self, command: dict, deadline: float, cancel: threading.Event | None = None):
        if self.closed:
            raise BrowserError("session_closed", 409, "Browser session is closed")
        if cancel is not None and cancel.is_set():
            raise BrowserError("cancelled", 499, "Browser operation was cancelled before dispatch")
        if time.monotonic() >= deadline:
            raise BrowserError("timeout", 504, "Browser operation expired before dispatch")
        wire = json.dumps(command, ensure_ascii=True, separators=(",", ":")).encode() + b"\n"
        if len(wire) > _MAX_MESSAGE:
            raise BrowserError("request_too_large", 413, "Browser request exceeds its transport limit")

        def write():
            try:
                pending = memoryview(wire)
                while pending:
                    written = self.process.stdin.write(pending)
                    if not written:
                        raise OSError
                    pending = pending[written:]
                self.process.stdin.flush()
            except (OSError, ValueError):
                try:
                    self.queue.put_nowait(None)
                except queue.Full:
                    pass

        writer = threading.Thread(target=write, name="echo-browser-request", daemon=True)
        writer.start()
        while True:
            cancelled = cancel is not None and cancel.is_set()
            remaining = deadline - time.monotonic()
            if cancelled or remaining <= 0:
                self.kill()
                raise BrowserError(
                    "cancelled" if cancelled else "timeout",
                    499 if cancelled else 504,
                    "Browser operation cancelled; its outcome is unknown"
                    if cancelled
                    else "Browser operation timed out; its outcome is unknown",
                    unknown_outcome=True,
                )
            try:
                reply = self.queue.get(timeout=min(0.05, remaining))
            except queue.Empty:
                continue
            if reply is None or not isinstance(reply, dict):
                self.kill()
                raise BrowserError("provider_unavailable", 503, "Browser provider exited without a complete result", unknown_outcome=True)
            if reply.get("ok") is True and isinstance(reply.get("result"), dict):
                return reply["result"]
            error = reply.get("error", {})
            if reply.get("ok") is not False or not isinstance(error, dict):
                self.kill()
                raise BrowserError("provider_unavailable", 503, "Browser provider returned an invalid result", unknown_outcome=True)
            code, status, message = error.get("code"), error.get("status"), error.get("message")
            if (
                not isinstance(code, str)
                or not isinstance(message, str)
                or len(message) > 300
                or status not in (400, 403, 404, 409, 413, 499, 503, 504)
            ):
                self.kill()
                raise BrowserError("provider_unavailable", 503, "Browser provider returned an invalid error", unknown_outcome=True)
            raise BrowserError(code, status, message, unknown_outcome=error.get("unknown_outcome") is True)

    def kill(self):
        if self.closed:
            return
        self.closed = True
        if self.job:
            self.job.close()
        elif self.process.poll() is None:
            _kill_linux_tree(self.process.pid, self.identity)
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=1)
        for stream in (self.process.stdin, self.process.stdout):
            try:
                stream.close()
            except OSError:
                pass
        self.reader.join(timeout=0.2)

    def close(self, deadline: float):
        if not self.closed and self.process.poll() is None:
            try:
                self.request({"action": "close"}, deadline)
            except BrowserError:
                pass
        self.kill()
