"""Private per-bot profiles and capped opaque screenshot artifacts."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import uuid
from pathlib import Path

from .config import MAX_ARTIFACT_BYTES, MAX_ARTIFACTS, MAX_PROFILE_BYTES, VIEWPORT
from .errors import BrowserError

_ID = re.compile(r"^[a-f0-9]{32}$")


def identifier(value: str, kind: str = "bot") -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise BrowserError("invalid_id", 400, f"Browser {kind} ID must be 32 lowercase hexadecimal characters")
    return value


def private_directory(path: Path) -> Path:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise BrowserError("unsafe_storage", 409, "Browser storage is not a private regular directory")
    path.mkdir(mode=0o700, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)
    return path


class ProfileLock:
    def __init__(self, path: Path):
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise BrowserError("unsafe_storage", 409, "Browser profile lock is not a regular file")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        self.stream = os.fdopen(fd, "r+b")
        try:
            info = os.fstat(self.stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OSError
            if os.name == "nt":
                import msvcrt

                if not self.stream.read(1):
                    self.stream.write(b"0")
                    self.stream.flush()
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise BrowserError("profile_busy", 409, "This bot browser profile is already in use") from None

    def close(self):
        self.stream.close()


class BrowserStorage:
    def __init__(self, state_dir: Path):
        # The host supplies state_dir. Below this root, no symlink is accepted.
        state_dir = Path(state_dir).absolute()
        state_dir.mkdir(parents=True, exist_ok=True)
        self.root = private_directory(state_dir / "browser")
        self.bots = private_directory(self.root / "bots")
        self.probes = private_directory(self.root / "probes")

    def directory(self, bot_id: str) -> Path:
        return private_directory(self.bots / identifier(bot_id))

    def profile(self, bot_id: str) -> Path:
        return private_directory(self.directory(bot_id) / "profile")

    def profile_size(self, bot_id: str) -> int:
        profile = self.profile(bot_id)
        total = count = 0
        for directory, dirs, files in os.walk(profile, followlinks=False):
            # Chromium's own singleton symlinks are not followed or counted as
            # host files. The profile root itself may never be a symlink.
            dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
            for name in files:
                count += 1
                try:
                    info = (Path(directory) / name).lstat()
                except FileNotFoundError:
                    continue  # Browser caches can rotate during this checkpoint.
                if stat.S_ISREG(info.st_mode):
                    total += info.st_size
                if total > MAX_PROFILE_BYTES or count > 20_000:
                    raise BrowserError("profile_limit", 413, "Browser profile exceeds its checkpoint storage limit; reset is required")
        return total

    def save_artifact(self, bot_id: str, encoded: str) -> dict:
        try:
            if not isinstance(encoded, str) or len(encoded) > ((MAX_ARTIFACT_BYTES + 2) // 3) * 4:
                raise ValueError
            png = base64.b64decode(encoded, validate=True)
            if not 24 <= len(png) <= MAX_ARTIFACT_BYTES or png[:8] != b"\x89PNG\r\n\x1a\n" or png[12:16] != b"IHDR":
                raise ValueError
            width, height = struct.unpack(">II", png[16:24])
            if (width, height) != (VIEWPORT["width"], VIEWPORT["height"]):
                raise ValueError
        except (ValueError, TypeError):
            raise BrowserError(
                "artifact_invalid", 413, "Browser screenshot is invalid or exceeds its fixed viewport/512 KiB limit"
            ) from None
        folder = private_directory(self.directory(bot_id) / "artifacts")
        artifact_id = uuid.uuid4().hex
        metadata = {
            "artifact_id": artifact_id,
            "mime_type": "image/png",
            "sha256": hashlib.sha256(png).hexdigest(),
            "bytes": len(png),
            "width": width,
            "height": height,
        }
        # Count PNGs themselves, including a file left by a process crash before
        # its metadata was committed. Orphan images must not evade the cap.
        records = sorted(
            (path for path in folder.glob("*.png") if _ID.fullmatch(path.stem)),
            key=lambda path: path.stat(follow_symlinks=False).st_mtime_ns,
        )
        for record in records[: max(0, len(records) - MAX_ARTIFACTS + 1)]:
            record.unlink(missing_ok=True)
            (folder / f"{record.stem}.json").unlink(missing_ok=True)
        written = []
        try:
            for extension, value in (("png", png), ("json", json.dumps(metadata).encode())):
                path = folder / f"{artifact_id}.{extension}"
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                written.append(path)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(value)
        except OSError:
            for path in written:
                path.unlink(missing_ok=True)
            raise BrowserError("artifact_unavailable", 503, "Browser screenshot could not be stored") from None
        return metadata

    def read_artifact(self, bot_id: str, artifact_id: str) -> dict:
        identifier(artifact_id, "artifact")
        folder = private_directory(self.directory(bot_id) / "artifacts")
        try:
            values = []
            for extension, bound in (("json", 2048), ("png", MAX_ARTIFACT_BYTES)):
                path = folder / f"{artifact_id}.{extension}"
                if path.is_symlink() or not path.is_file():
                    raise FileNotFoundError
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(fd, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > bound:
                        raise ValueError
                    values.append(stream.read(bound + 1))
            metadata, png = json.loads(values[0]), values[1]
            if (
                metadata.get("artifact_id") != artifact_id
                or metadata.get("mime_type") != "image/png"
                or len(png) != metadata.get("bytes")
                or len(png) > MAX_ARTIFACT_BYTES
                or hashlib.sha256(png).hexdigest() != metadata.get("sha256")
                or metadata.get("width") != VIEWPORT["width"]
                or metadata.get("height") != VIEWPORT["height"]
            ):
                raise ValueError
            # Never return arbitrary fields injected into a metadata file.
            return {k: metadata[k] for k in ("artifact_id", "mime_type", "sha256", "bytes", "width", "height")} | {
                "base64": base64.b64encode(png).decode("ascii")
            }
        except FileNotFoundError:
            raise BrowserError("artifact_missing", 404, "Screenshot artifact is missing or belongs to another bot") from None
        except (ValueError, KeyError, TypeError, OSError):
            raise BrowserError("artifact_invalid", 409, "Screenshot artifact failed its integrity check") from None

    def reset(self, bot_id: str):
        folder = self.directory(bot_id)
        lock = ProfileLock(folder / "profile.lock")
        try:
            # Keep the held lock path stable. Never remove another manager's
            # lock inode while it may be acquiring the same bot profile.
            for name in ("profile", "artifacts"):
                path = folder / name
                if path.is_symlink():
                    path.unlink()
                elif path.exists():
                    if not path.is_dir():
                        raise BrowserError("unsafe_storage", 409, "Browser stored state is not a regular directory")
                    shutil.rmtree(path)
        finally:
            lock.close()
