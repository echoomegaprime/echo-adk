"""Provider-independent browser boundaries; real Chromium tests are separate."""

import base64
import hashlib
import json
import os
import struct
import threading
import time

import pytest

from echo_adk.browser import BrowserConfig, BrowserError, BrowserManager
from echo_adk.browser.config import MAX_ARTIFACT_BYTES, MAX_PROFILE_BYTES
from echo_adk.browser.process import worker_environment
from echo_adk.browser.storage import BrowserStorage, ProfileLock

A = "a" * 32
B = "b" * 32


def png_container(payload=b""):
    # Only the storage envelope is under test here; this is not browser evidence.
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", 1280, 720) + payload


def test_health_and_disabled_reset_do_not_import_or_launch_playwright(tmp_path, monkeypatch):
    def forbidden(*a, **kw):
        raise AssertionError("A health/reset operation must not launch a browser")

    monkeypatch.setattr("echo_adk.browser.runtime.Worker", forbidden)
    manager = BrowserManager(tmp_path)
    profile = manager.storage.profile(A)
    (profile / "Cookies").write_text("private-cookie")
    artifact = manager.storage.save_artifact(A, base64.b64encode(png_container()).decode())
    health = manager.health()
    assert not health["available"] and not health["enabled"]
    assert health["verified_at"] is None and health["os_isolation"] is False
    assert health["isolation"] == "process_and_profile"
    assert manager.status(A)["available"] is False
    assert manager.reset(A)["profile_removed"] is True
    assert not profile.exists()
    with pytest.raises(BrowserError) as caught:
        manager.read_artifact(A, artifact["artifact_id"])
    assert caught.value.status == 404
    manager.shutdown()


def test_enabled_health_remains_unverified_without_a_real_probe(tmp_path, monkeypatch):
    monkeypatch.setattr("echo_adk.browser.runtime.Worker", lambda *a: pytest.fail("health must not spawn"))
    monkeypatch.setattr("echo_adk.browser.runtime.importlib.metadata.version", lambda _: "1.62.0")
    manager = BrowserManager(tmp_path, BrowserConfig(enabled=True))
    status = manager.health()
    assert status["enabled"] is True and status["available"] is False
    assert "not yet been verified" in status["reason"]
    manager.shutdown()


def test_verified_health_has_a_bounded_cache(tmp_path, monkeypatch):
    manager = BrowserManager(tmp_path)
    manager._verified({"browser_version": "test-only-provider-result"})
    verified = manager.health()["verified_at"]
    manager._probe_at = time.monotonic() - 61
    assert manager.health()["available"] is False
    assert manager.health()["verification_stale"] is True
    assert manager.health()["verified_at"] == verified
    manager.shutdown()


@pytest.mark.parametrize(
    "command",
    [
        {"action": "evaluate", "javascript": "document.cookie"},
        {"action": "snapshot", "selector": "input"},
        {"action": "click", "ref": "#password"},
        {"action": "navigate", "url": "https://example.com", "proxy": "http://localhost"},
        {"action": "screenshot", "path": "/tmp/key.png"},
        {"action": "fill", "ref": A, "value": "x" * 4097},
        {"action": "snapshot", "timeout_ms": 30001},
        {"action": "snapshot", "timeout_ms": True},
        {"action": "snapshot", "timeout_ms": 0},
        {"action": "snapshot", "timeout_ms": 1.5},
        {"action": "navigate"},
    ],
)
def test_model_cannot_expand_action_schema_or_host_policy(tmp_path, command):
    manager = BrowserManager(tmp_path)
    with pytest.raises(BrowserError) as caught:
        manager.execute(A, command)
    assert caught.value.status == 400
    assert not manager._sessions
    manager.shutdown()


def test_cancelled_before_dispatch_never_spawns_or_mutates_a_profile(tmp_path):
    manager = BrowserManager(tmp_path)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(BrowserError) as caught:
        manager.execute(A, {"action": "snapshot"}, cancel=cancel)
    assert caught.value.status == 499 and caught.value.unknown_outcome is False
    assert not (manager.storage.bots / A).exists()


def test_credentials_never_enter_provider_environment(tmp_path):
    env = {
        "PATH": "/usr/bin",
        "SystemRoot": "C:\\Windows",
        "ECHO_GATE_API_KEY": "secret1",
        "ECHO_PRIME_SDK_HMAC_SECRET": "secret2",
        "OAUTH_CLIENT_SECRET": "secret3",
        "AWS_SECRET_ACCESS_KEY": "secret4",
        "PYTHONPATH": "/untrusted",
        "LD_PRELOAD": "/untrusted/lib.so",
        "DEBUG": "pw:*",
        "HOME": "/user-home",
    }
    result = worker_environment(tmp_path, env)
    assert result["PATH"] == "/usr/bin" and result["SystemRoot"] == "C:\\Windows"
    assert result["HOME"] == str(tmp_path)
    assert result["PYTHONPATH"] != "/untrusted"
    assert not any("secret" in value for value in result.values())
    assert "DEBUG" not in result and "LD_PRELOAD" not in result


def test_artifacts_are_opaque_capped_integrity_checked_and_bot_bound(tmp_path):
    storage = BrowserStorage(tmp_path)
    raw = png_container(b"unit-storage")
    first = storage.save_artifact(A, base64.b64encode(raw).decode())
    result = storage.read_artifact(A, first["artifact_id"])
    assert base64.b64decode(result["base64"]) == raw
    assert result["sha256"] == hashlib.sha256(raw).hexdigest()
    assert "path" not in result
    with pytest.raises(BrowserError) as caught:
        storage.read_artifact(B, first["artifact_id"])
    assert caught.value.status == 404
    for _ in range(8):
        storage.save_artifact(A, base64.b64encode(raw).decode())
    with pytest.raises(BrowserError) as caught:
        storage.read_artifact(A, first["artifact_id"])
    assert caught.value.status == 404
    assert len(list((storage.directory(A) / "artifacts").glob("*.png"))) == 8
    with pytest.raises(BrowserError) as caught:
        storage.save_artifact(A, base64.b64encode(png_container(b"x" * MAX_ARTIFACT_BYTES)).decode())
    assert caught.value.status == 413
    latest = storage.save_artifact(A, base64.b64encode(raw).decode())
    (storage.directory(A) / "artifacts" / (latest["artifact_id"] + ".png")).write_bytes(raw + b"tampered")
    with pytest.raises(BrowserError) as caught:
        storage.read_artifact(A, latest["artifact_id"])
    assert caught.value.status == 409


def test_storage_refuses_traversal_symlinks_and_unowned_profile_lock(tmp_path):
    storage = BrowserStorage(tmp_path)
    with pytest.raises(BrowserError):
        storage.profile("../other")
    directory = storage.directory(A)
    lock = ProfileLock(directory / "profile.lock")
    try:
        with pytest.raises(BrowserError) as caught:
            storage.reset(A)
        assert caught.value.status == 409
    finally:
        lock.close()
    if os.name != "nt":
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "keep").write_text("not browser state")
        (directory / "profile").symlink_to(outside, target_is_directory=True)
        with pytest.raises(BrowserError):
            storage.profile(A)
        storage.reset(A)
        assert (outside / "keep").read_text() == "not browser state"


def test_profile_limit_is_a_checkpoint_and_does_not_follow_chromium_symlinks(tmp_path):
    storage = BrowserStorage(tmp_path)
    profile = storage.profile(A)
    if os.name != "nt":
        (profile / "SingletonLock").symlink_to("external-process-id")
    with (profile / "oversize").open("wb") as stream:
        stream.truncate(MAX_PROFILE_BYTES + 1)
    with pytest.raises(BrowserError) as caught:
        storage.profile_size(A)
    assert caught.value.status == 413
    storage.reset(A)


def test_artifact_response_cannot_inject_additional_fields(tmp_path):
    storage = BrowserStorage(tmp_path)
    artifact = storage.save_artifact(A, base64.b64encode(png_container()).decode())
    path = storage.directory(A) / "artifacts" / (artifact["artifact_id"] + ".json")
    path.write_text(json.dumps({**artifact, "cookie": "never-return-this", "path": "/private"}))
    result = storage.read_artifact(A, artifact["artifact_id"])
    assert "cookie" not in result and "path" not in result


def test_recognizable_secret_fill_is_rejected_before_dispatch(tmp_path):
    manager = BrowserManager(tmp_path)
    with pytest.raises(BrowserError) as caught:
        manager.execute(A, {"action": "fill", "ref": B, "value": "access_token=synthetic-never-send"})
    assert caught.value.code == "credential_value" and not manager._sessions


def test_resource_inspection_never_creates_state_or_launches(tmp_path, monkeypatch):
    monkeypatch.setattr("echo_adk.browser.runtime.Worker", lambda *a: pytest.fail("resource inspection must not launch"))
    manager = BrowserManager(tmp_path)
    assert manager.has_resources(A) is False
    assert not (manager.storage.bots / A).exists()
    profile = manager.storage.profile(A)
    assert manager.has_resources(A) is False
    lock = ProfileLock(profile.parent / "profile.lock")
    try:
        assert manager.has_resources(A) is False
    finally:
        lock.close()
    (profile / "Cookies").write_text("private-browser-state")
    assert manager.has_resources(A) is True
    assert manager.has_resources(B) is False
    manager.reset(A)
    assert manager.has_resources(A) is False
    manager._starting.add(A)
    assert manager.has_resources(A) is True
    manager._starting.clear()
    if os.name != "nt":
        (manager.storage.bots / A / "profile").symlink_to(tmp_path / "unknown", target_is_directory=True)
        assert manager.has_resources(A) is True


def test_cancel_during_storage_checkpoint_never_launches_a_worker(tmp_path, monkeypatch):
    monkeypatch.setattr("echo_adk.browser.runtime.importlib.metadata.version", lambda _: "1.62.0")
    monkeypatch.setattr("echo_adk.browser.runtime.Worker", lambda *a: pytest.fail("cancelled operation launched a worker"))
    manager = BrowserManager(tmp_path, BrowserConfig(enabled=True))
    cancel = threading.Event()
    monkeypatch.setattr(manager.storage, "profile_size", lambda _: cancel.set())
    with pytest.raises(BrowserError) as caught:
        manager.execute(A, {"action": "snapshot"}, cancel=cancel)
    assert caught.value.status == 499 and caught.value.unknown_outcome is False
    assert not manager._starting and not manager._sessions


def test_orphan_pngs_cannot_evade_retention_and_partial_write_is_cleaned(tmp_path, monkeypatch):
    storage = BrowserStorage(tmp_path)
    artifact = storage.save_artifact(A, base64.b64encode(png_container()).decode())
    folder = storage.directory(A) / "artifacts"
    (folder / (artifact["artifact_id"] + ".json")).unlink()
    for _ in range(8):
        storage.save_artifact(A, base64.b64encode(png_container()).decode())
    assert len(list(folder.glob("*.png"))) == 8
    assert not (folder / (artifact["artifact_id"] + ".png")).exists()
    real_open = os.open

    def failing_open(path, flags, *args, **kwargs):
        if str(path).endswith(".json") and flags & os.O_CREAT:
            raise OSError("synthetic disk failure containing no provider data")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("echo_adk.browser.storage.os.open", failing_open)
    with pytest.raises(BrowserError) as caught:
        storage.save_artifact(A, base64.b64encode(png_container()).decode())
    assert caught.value.code == "artifact_unavailable"
    assert {p.stem for p in folder.glob("*.png")} == {p.stem for p in folder.glob("*.json")}
