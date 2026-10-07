from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any

from .process import _Redactor


IMPORTANT_EVENTS = {
    "run_started",
    "run_finished",
    "episode_started",
    "episode_finished",
    "fault_planned",
    "fault_applied",
    "stop",
    "process_started",
    "process_finished",
    "stdin_sent",
    "rule_presented",
    "presentation_uncertain",
    "workspace_reset",
    "session_started",
    "comparison_started",
    "attempt_started",
    "attempt_finished",
    "comparison_stopped",
    "comparison_finished",
}


class JournalError(OSError):
    pass


class Journal:
    def __init__(self, run_dir: Path, run_id: str, secrets: tuple[str, ...] = ()):
        self.path = run_dir / "journal.jsonl"
        self.run_id = run_id
        self.secrets = secrets
        self.sequence = 0
        self.lock = threading.Lock()
        self.last_fsync = time.monotonic()
        try:
            self.file = self.path.open("x", encoding="utf-8", newline="\n")
        except OSError as exc:
            raise JournalError(f"cannot create journal: {exc}") from exc

    def append_event(self, event_type: str, payload: dict[str, Any], episode_id: str | None = None) -> int:
        with self.lock:
            self.sequence += 1
            def redact(value):
                if isinstance(value, str):
                    return redact_bytes(value.encode("utf-8"), self.secrets).decode("utf-8", "replace")
                if isinstance(value, dict):
                    return {redact(key): redact(child) for key, child in value.items()}
                if isinstance(value, list):
                    return [redact(child) for child in value]
                return value
            event = {
                "schema_version": "1.0",
                "run_id": self.run_id,
                "seq": self.sequence,
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "episode_id": episode_id,
                "type": event_type,
                "payload": redact(payload),
            }
            try:
                self.file.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
                self.file.flush()
                now = time.monotonic()
                if event_type in IMPORTANT_EVENTS or now - self.last_fsync >= 1:
                    os.fsync(self.file.fileno())
                    self.last_fsync = now
            except (OSError, UnicodeError) as exc:
                self.sequence -= 1
                raise JournalError(f"cannot persist journal event {event_type}: {exc}") from exc
            return self.sequence

    def close(self) -> None:
        try:
            self.file.close()
        except OSError as exc:
            raise JournalError(f"cannot close journal: {exc}") from exc


def known_secrets(environment: dict[str, str] | None = None) -> tuple[str, ...]:
    environment = os.environ if environment is None else environment
    secret_name = re.compile(r"(TOKEN|KEY|SECRET|PASSWORD|CREDENTIAL|AUTH|COOKIE)", re.IGNORECASE)
    return tuple(
        value for name, value in environment.items()
        if secret_name.search(name) and isinstance(value, str) and len(value) >= 4
    )


def redact_bytes(data: bytes, secrets: tuple[str, ...]) -> bytes:
    return _Redactor(secrets).feed(data, final=True)


def _safe_artifact_path(run_dir: Path, relative: str, create_parents: bool = True) -> Path:
    if not relative or "\\" in relative or "\x00" in relative or PureWindowsPath(relative).drive:
        raise JournalError("unsafe artifact path")
    parts = relative.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise JournalError("unsafe artifact path")
    try:
        path = run_dir.resolve(strict=True)
        if run_dir.is_symlink():
            raise JournalError("unsafe Run path")
        if not path.is_dir():
            raise JournalError("Run path is not a directory")
        for index, part in enumerate(parts):
            path /= part
            try:
                mode = path.lstat().st_mode
            except FileNotFoundError:
                if index == len(parts) - 1:
                    return path
                if not create_parents:
                    raise JournalError("artifact parent is missing")
                path.mkdir()
                mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or (index < len(parts) - 1 and not stat.S_ISDIR(mode)):
                raise JournalError("unsafe artifact path")
            if index == len(parts) - 1 and not stat.S_ISREG(mode):
                raise JournalError("unsafe artifact path")
        return path
    except OSError as exc:
        raise JournalError(f"cannot access artifact path {relative}: {exc}") from exc


def save_artifact(run_dir: Path, relative: str, data: bytes, secrets: tuple[str, ...] = ()) -> dict[str, Any]:
    destination = _safe_artifact_path(run_dir, relative)
    root = run_dir.resolve(strict=True)
    safe_data = redact_bytes(data, secrets)
    try:
        with destination.open("xb") as file:
            file.write(safe_data)
            file.flush()
            os.fsync(file.fileno())
    except OSError as exc:
        raise JournalError(f"cannot save artifact {relative}: {exc}") from exc
    return {
        "path": destination.relative_to(root).as_posix(),
        "size": len(safe_data),
        "sha256": hashlib.sha256(safe_data).hexdigest(),
    }


def artifact_ref(run_dir: Path, relative: str) -> dict[str, Any]:
    path = _safe_artifact_path(run_dir, relative, create_parents=False)
    root = run_dir.resolve(strict=True)
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as file:
            while chunk := file.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise JournalError(f"artifact is missing or unreadable: {relative}") from exc
    return {"path": path.relative_to(root).as_posix(), "size": size, "sha256": digest.hexdigest()}


def atomic_write(path: Path, data: bytes) -> None:
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as file:
            descriptor = -1
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise JournalError(f"cannot atomically write {path}: {exc}") from exc
