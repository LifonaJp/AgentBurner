from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path, PurePosixPath
from typing import Iterable

from .config import ConfigError
from .models import Manifest, ManifestEntry


DEFAULT_EXCLUSIONS = (".git/**", ".venv/**", "__pycache__/**", ".pytest_cache/**")
WINDOWS_FILESYSTEMS = {"9p", "drvfs", "ntfs", "ntfs3", "fuseblk", "exfat", "vfat", "cifs", "smb3"}


def filesystem_type(path: Path | PurePosixPath, mountinfo: str | None = None) -> str | None:
    try:
        if mountinfo is None:
            target = PurePosixPath(Path(path).resolve().as_posix())
            mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
        else:
            target = PurePosixPath(path)
    except OSError:
        return None
    selected: tuple[int, str] | None = None
    for line in mountinfo.splitlines():
        fields = line.split()
        try:
            separator = fields.index("-")
            mountpoint = Path(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), fields[4]))
            fstype = fields[separator + 1]
            target.relative_to(mountpoint)
        except (ValueError, IndexError):
            continue
        candidate = (len(mountpoint.parts), fstype)
        if selected is None or candidate[0] > selected[0]:
            selected = candidate
    return selected[1] if selected else None


def is_linux_filesystem_type(fstype: str | None) -> bool:
    return fstype is not None and fstype.lower() not in WINDOWS_FILESYSTEMS


def _excluded(relative: str, exclusions: Iterable[str]) -> bool:
    for pattern in exclusions:
        prefix = pattern[:-3] if pattern.endswith("/**") else pattern
        if relative == prefix or relative.startswith(prefix.rstrip("/") + "/"):
            return True
    return False


def validate_fixture(root: Path) -> None:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ConfigError(f"fixture is not a directory: {root}")
    if (root / ".git").is_file() or (root / ".gitmodules").exists():
        raise ConfigError("worktree and submodule fixtures are not supported")
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in (*dirs, *files):
            path = Path(current, name)
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise ConfigError(f"fixture contains a symbolic link: {path.relative_to(root)}")
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ConfigError(f"fixture contains a special file: {path.relative_to(root)}")
            if name == ".git" and stat.S_ISDIR(mode):
                gitmodules = root / ".gitmodules"
                if gitmodules.exists():
                    raise ConfigError("submodule fixtures are not supported")


def copy_fixture(source: Path, destination: Path) -> None:
    validate_fixture(source)
    if destination.exists():
        raise FileExistsError(destination)
    shutil.copytree(source, destination, symlinks=False)


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as file:
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
            raise OSError(f"not a regular file: {path}")
        while chunk := file.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def snapshot(root: Path, exclusions: list[str] | tuple[str, ...] = ()) -> Manifest:
    root = root.resolve(strict=True)
    entries: dict[str, ManifestEntry] = {}
    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        relative_dir = current_path.relative_to(root).as_posix()
        if relative_dir != "." and _excluded(relative_dir, exclusions):
            dirs[:] = []
            continue
        dirs[:] = sorted(name for name in dirs if not _excluded((current_path / name).relative_to(root).as_posix(), exclusions))
        for name in sorted(dirs + files):
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if _excluded(relative, exclusions):
                continue
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                entry = ManifestEntry(relative, "directory", 0, hashlib.sha256(b"").hexdigest())
            elif stat.S_ISREG(mode):
                size, digest = _hash_file(path)
                entry = ManifestEntry(relative, "file", size, digest)
            elif stat.S_ISLNK(mode):
                target = os.readlink(path).encode("utf-8", "surrogateescape")
                entry = ManifestEntry(relative, "symlink", len(target), hashlib.sha256(target).hexdigest())
            else:
                entry = ManifestEntry(relative, "special", 0, "")
            entries[relative] = entry
    return Manifest(entries)


def changed_paths(before: Manifest, after: Manifest) -> set[str]:
    return {
        path
        for path in before.entries.keys() | after.entries.keys()
        if before.entries.get(path) != after.entries.get(path)
    }


def manifest_json(manifest: Manifest) -> bytes:
    return (json.dumps(manifest.to_list(), ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def apply_delete_fault(root: Path, relative: str) -> None:
    parts = Path(relative).parts
    if not parts or parts[0] != "inputs" or any(part in ("", ".", "..") for part in parts):
        raise ConfigError("fault path is outside inputs/")
    root = root.resolve(strict=True)
    target = root.joinpath(*parts)
    parent = root
    for part in parts[:-1]:
        parent = parent / part
        if parent.is_symlink() or not parent.is_dir():
            raise ConfigError(f"fault parent is not a normal directory: {relative}")
    if target.is_symlink() or not target.is_file():
        raise ConfigError(f"fault target is not an existing regular file: {relative}")
    target.unlink()


def git_metadata(root: Path) -> dict[str, str | None]:
    def run(*args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(root), *args],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip()

    return {
        "commit": run("rev-parse", "HEAD"),
        "status": run("status", "--porcelain=v1", "--untracked-files=all"),
    }


def diff_artifact(root: Path, before: Manifest, after: Manifest) -> bytes:
    chunks: list[str] = []
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "-c", "core.fsmonitor=false", "-c", "core.pager=cat", "diff", "--no-ext-diff", "--no-textconv", "--binary"],
            check=False,
            capture_output=True,
            timeout=10,
            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
        )
        if result.returncode == 0 and result.stdout:
            chunks.append(result.stdout.decode("utf-8", "replace"))
    except (OSError, subprocess.SubprocessError):
        pass
    changed = changed_paths(before, after)
    if changed:
        chunks.append("\n# Manifest changes (all filesystem entries, including untracked)\n")
        for path in sorted(changed):
            old = before.entries.get(path)
            new = after.entries.get(path)
            chunks.append(
                f"{path}: "
                f"{old.kind + ' ' + old.sha256 if old else 'absent'} -> "
                f"{new.kind + ' ' + new.sha256 if new else 'absent'}\n"
            )
    return "".join(chunks).encode("utf-8")


def tree_usage(root: Path) -> tuple[int, int]:
    total = count = 0
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(current, name)
            try:
                mode = path.lstat().st_mode
                if stat.S_ISREG(mode):
                    total += path.stat(follow_symlinks=False).st_size
                    count += 1
                elif stat.S_ISLNK(mode):
                    total += len(os.readlink(path).encode("utf-8", "surrogateescape"))
                    count += 1
                elif not stat.S_ISDIR(mode):
                    count += 1
            except OSError:
                continue
    return total, count
