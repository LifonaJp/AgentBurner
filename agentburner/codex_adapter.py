from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import ConfigError
from .models import Capabilities, RunConfig


class AdapterError(RuntimeError):
    pass


@dataclass(frozen=True)
class ParsedEvents:
    session_id: str | None
    turn_completed: bool
    turn_failed: bool
    unknown_events: int
    parser_errors: tuple[str, ...]
    usage: tuple[dict, ...]
    final_message: str | None


def build_argv(config: RunConfig, session_id: str | None) -> list[str]:
    argv = [config.codex_executable, "-a", "never", "--sandbox", "workspace-write"]
    if config.model is not None:
        argv.extend(["--model", config.model])
    argv.append("exec")
    if session_id is not None:
        if not session_id.strip():
            raise ConfigError("session id must not be empty")
        argv.extend(["resume", "--json", session_id, "-"])
    else:
        argv.extend(["--json", "-"])
    return argv


def _command(executable: str, *args: str) -> list[str]:
    resolved = shutil.which(executable)
    if resolved is None and Path(executable).is_file():
        resolved = str(Path(executable).resolve())
    if resolved is None:
        raise AdapterError(f"executable not found: {executable}")
    return [resolved, *args]


def _capture(argv: list[str], timeout: float = 10) -> str:
    try:
        result = subprocess.run(argv, capture_output=True, timeout=timeout, check=False, shell=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AdapterError(f"cannot run {Path(argv[0]).name} probe: {exc}") from exc
    output = (result.stdout + b"\n" + result.stderr).decode("utf-8", "replace").strip()
    if result.returncode != 0:
        raise AdapterError(f"Codex probe exited with status {result.returncode}")
    return output


def is_logged_in(executable: str) -> bool:
    try:
        result = subprocess.run(
            _command(executable, "login", "status"),
            capture_output=True,
            timeout=10,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AdapterError(f"cannot check Codex login status: {exc}") from exc
    output = (result.stdout + b"\n" + result.stderr).decode("utf-8", "replace").lower()
    if result.returncode == 0 and "not logged in" not in output and "logged out" not in output:
        return True
    if "not logged in" in output or "logged out" in output:
        return False
    raise AdapterError("cannot determine Codex login status")


def probe(executable: str) -> Capabilities:
    version = _capture(_command(executable, "--version"))
    global_help = _capture(_command(executable, "--help"))
    exec_help = _capture(_command(executable, "exec", "--help"))
    resume_help = _capture(_command(executable, "exec", "resume", "--help"))
    if "--sandbox" not in global_help or "workspace-write" not in global_help:
        raise AdapterError("Codex global help does not advertise workspace-write sandbox")
    if "--ask-for-approval" not in global_help or "never" not in global_help:
        raise AdapterError("Codex global help does not advertise the never approval policy")
    if "--json" not in exec_help:
        raise AdapterError("Codex exec help does not advertise --json")
    if "resume" not in exec_help.lower():
        raise AdapterError("Codex exec help does not advertise resume")
    if "--json" not in resume_help:
        raise AdapterError("Codex exec resume help does not advertise --json")
    return Capabilities(version, exec_help, resume_help)


def parse_jsonl(path: Path, expected_session_id: str | None = None) -> ParsedEvents:
    session_id: str | None = None
    turn_completed = turn_failed = False
    unknown = 0
    errors: list[str] = []
    usage: list[dict] = []
    final_message: str | None = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as file:
            for line_number, line in enumerate(file, 1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    errors.append(f"line {line_number}: invalid JSON ({exc.msg})")
                    continue
                if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                    errors.append(f"line {line_number}: event must be an object with a type")
                    continue
                kind = event["type"]
                if kind == "thread.started":
                    reported = event.get("thread_id")
                    if not isinstance(reported, str) or not reported:
                        errors.append(f"line {line_number}: thread.started has no thread_id")
                    elif session_id is not None and session_id != reported:
                        errors.append(f"line {line_number}: thread id changed within the session")
                    elif expected_session_id is not None and reported != expected_session_id:
                        errors.append(f"line {line_number}: resumed thread id does not match requested id")
                    else:
                        session_id = reported
                elif kind == "turn.completed":
                    turn_completed = True
                    reported_usage = event.get("usage")
                    if isinstance(reported_usage, dict):
                        usage.append(reported_usage)
                    message = event.get("last_agent_message")
                    if isinstance(message, str):
                        final_message = message
                elif kind == "turn.failed":
                    turn_failed = True
                elif kind not in ("error",) and not kind.startswith("item."):
                    unknown += 1
    except OSError as exc:
        errors.append(f"cannot read JSONL output: {exc}")
    if expected_session_id is None and session_id is None:
        errors.append("initial exec did not report thread.started.thread_id")
    return ParsedEvents(session_id, turn_completed, turn_failed, unknown, tuple(errors), tuple(usage), final_message)
