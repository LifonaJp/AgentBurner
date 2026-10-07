from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from .models import ProcessLimits, ProcessResult, ProcessSpec


class ProcessError(RuntimeError):
    pass


class _Redactor:
    def __init__(self, secrets: tuple[str, ...]):
        self.secrets = tuple(sorted({s.encode("utf-8") for s in secrets if len(s) >= 4}, key=len, reverse=True))
        self.pending = b""
        self.keep = max((len(value) for value in self.secrets), default=1) - 1

    def feed(self, data: bytes, final: bool = False) -> bytes:
        combined = self.pending + data
        for secret in self.secrets:
            combined = combined.replace(secret, b"*" * len(secret))
        if final:
            self.pending = b""
            return combined
        split = max(0, len(combined) - self.keep)
        safe, self.pending = combined[:split], combined[split:]
        return safe


def _emit(sink: Callable[[str, dict], None] | None, event: str, payload: dict) -> None:
    if sink is not None:
        sink(event, payload)


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _stop_process(process: subprocess.Popen, grace: float) -> bool:
    if os.name == "posix":
        pgid = process.pid
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return False
        except OSError:
            process.terminate()
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
        deadline = time.monotonic() + grace
        while _group_exists(pgid) and time.monotonic() < deadline:
            time.sleep(0.05)
        if _group_exists(pgid):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                return True
            try:
                process.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass
            deadline = time.monotonic() + grace
            while _group_exists(pgid) and time.monotonic() < deadline:
                time.sleep(0.05)
        return _group_exists(pgid)
    else:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        return False


def run_process(
    spec: ProcessSpec,
    sink: Callable[[str, dict], None] | None,
    limits: ProcessLimits,
    stop_check: Callable[[], str | None] | None = None,
) -> ProcessResult:
    started = time.monotonic()
    for path in (spec.stdout_path, spec.stderr_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    files = {}
    try:
        for name, path in (("stdout", spec.stdout_path), ("stderr", spec.stderr_path)):
            files[name] = path.open("wb")
    except OSError as exc:
        for file in files.values():
            file.close()
        raise ProcessError(f"cannot create process logs: {exc}") from exc
    try:
        _emit(sink, "process_started", {"argv": list(spec.argv), "cwd": str(spec.cwd)})
    except Exception as exc:
        for file in files.values():
            file.close()
        raise ProcessError(f"cannot persist process start: {exc}") from exc
    try:
        process = subprocess.Popen(
            list(spec.argv),
            cwd=spec.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=(os.name == "posix"),
            env=spec.environment,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        for file in files.values():
            file.close()
        elapsed = time.monotonic() - started
        error = f"cannot start process: {exc}"
        try:
            _emit(sink, "process_finished", {
                "returncode": None,
                "elapsed_sec": elapsed,
                "received_bytes": 0,
                "saved_bytes": 0,
                "truncated": False,
                "timed_out": False,
                "stopped": False,
                "stop_reason": None,
                "error": error,
                "cleanup_incomplete": False,
            })
        except Exception as event_exc:
            error = f"{error}; cannot persist process result: {event_exc}"
        return ProcessResult(None, elapsed, 0, 0, False, error=error)

    events: queue.Queue[tuple[str, bytes | None]] = queue.Queue(maxsize=64)
    reader_errors: list[str] = []

    def read_stream(name: str, stream) -> None:
        try:
            while chunk := stream.read(65536):
                events.put((name, chunk))
        except (OSError, ValueError) as exc:
            reader_errors.append(f"{name} reader: {exc}")
        finally:
            events.put((name, None))

    writer_errors: list[str] = []
    input_sent = threading.Event()
    input_recorded = False

    def write_input() -> None:
        try:
            assert process.stdin is not None
            process.stdin.write(spec.stdin)
            process.stdin.flush()
            process.stdin.close()
            input_sent.set()
        except (BrokenPipeError, OSError, ValueError) as exc:
            writer_errors.append(f"stdin write failed: {exc}")
        finally:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass

    assert process.stdout is not None and process.stderr is not None
    readers = [
        threading.Thread(target=read_stream, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=read_stream, args=("stderr", process.stderr), daemon=True),
    ]
    writer = threading.Thread(target=write_input, daemon=True)
    for thread in readers:
        thread.start()
    writer.start()

    redactors = {"stdout": _Redactor(spec.secrets), "stderr": _Redactor(spec.secrets)}
    paths = {"stdout": spec.stdout_path, "stderr": spec.stderr_path}
    received = saved = 0
    eof: set[str] = set()
    timed_out = stopped = truncated = False
    stop_reason: str | None = None
    cleanup_incomplete = False
    fatal_error: str | None = None
    termination_sent = False
    group_cleanup_attempted = False
    drain_deadline: float | None = None
    deadline = started + limits.timeout_sec

    def write_redacted(name: str, data: bytes, final: bool = False) -> None:
        nonlocal saved, fatal_error
        output = redactors[name].feed(data, final)
        if output:
            try:
                files[name].write(output)
                saved += len(output)
            except OSError as exc:
                fatal_error = f"cannot save {name}: {exc}"

    try:
        while process.poll() is None or len(eof) < 2 or not events.empty():
            if input_sent.is_set() and not input_recorded:
                _emit(sink, "stdin_sent", {"bytes": len(spec.stdin)})
                input_recorded = True
            now = time.monotonic()
            if process.poll() is not None and len(eof) < 2:
                if drain_deadline is None:
                    drain_deadline = now + limits.terminate_grace_sec
                if os.name == "posix" and not group_cleanup_attempted and _group_exists(process.pid):
                    cleanup_incomplete |= _stop_process(process, limits.terminate_grace_sec)
                    group_cleanup_attempted = True
                    drain_deadline = time.monotonic() + limits.terminate_grace_sec
                if now >= drain_deadline:
                    cleanup_incomplete = True
                    for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                        if name not in eof:
                            try:
                                stream.close()
                            except OSError:
                                pass
                            eof.add(name)
                            write_redacted(name, b"", final=True)
                    break
            if fatal_error:
                if not termination_sent:
                    cleanup_incomplete |= _stop_process(process, limits.terminate_grace_sec)
                    termination_sent = True
            if now >= deadline and process.poll() is None:
                timed_out = True
                stop_reason = "timeout"
                if not termination_sent:
                    cleanup_incomplete |= _stop_process(process, limits.terminate_grace_sec)
                    termination_sent = True
            elif stop_check is not None and process.poll() is None:
                try:
                    stop_reason = stop_check()
                except Exception as exc:
                    fatal_error = f"stop monitor failed: {exc}"
                if stop_reason:
                    stopped = True
                    if not termination_sent:
                        cleanup_incomplete |= _stop_process(process, limits.terminate_grace_sec)
                        termination_sent = True
            try:
                name, chunk = events.get(timeout=limits.poll_interval_sec)
            except queue.Empty:
                continue
            if chunk is None:
                eof.add(name)
                write_redacted(name, b"", final=True)
                continue
            received += len(chunk)
            remaining = max(0, limits.max_output_bytes - (received - len(chunk)))
            accepted = chunk[:remaining]
            write_redacted(name, accepted)
            if len(accepted) < len(chunk):
                truncated = True
                stopped = True
                stop_reason = "output_limit"
                if not termination_sent:
                    cleanup_incomplete |= _stop_process(process, limits.terminate_grace_sec)
                    termination_sent = True
    finally:
        for file in files.values():
            try:
                file.flush()
                os.fsync(file.fileno())
            except OSError as exc:
                fatal_error = fatal_error or f"cannot flush process log: {exc}"
            file.close()
        for thread in readers:
            thread.join(timeout=1)
        writer.join(timeout=1)
        if input_sent.is_set() and not input_recorded:
            _emit(sink, "stdin_sent", {"bytes": len(spec.stdin)})
            input_recorded = True
        if process.poll() is None:
            cleanup_incomplete |= _stop_process(process, limits.terminate_grace_sec)
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            cleanup_incomplete = True
        if os.name == "posix" and process.returncode is not None and _group_exists(process.pid):
            cleanup_incomplete = True
            cleanup_incomplete |= _stop_process(process, limits.terminate_grace_sec)

    error = fatal_error or (reader_errors[0] if reader_errors else None) or (writer_errors[0] if writer_errors else None)
    elapsed = time.monotonic() - started
    try:
        _emit(sink, "process_finished", {
            "returncode": process.returncode,
            "elapsed_sec": elapsed,
            "received_bytes": received,
            "saved_bytes": saved,
            "truncated": truncated,
            "timed_out": timed_out,
            "stopped": stopped,
            "stop_reason": stop_reason,
            "error": error,
            "cleanup_incomplete": cleanup_incomplete,
        })
    except Exception as exc:
        error = error or f"cannot persist process result: {exc}"
    return ProcessResult(
        process.returncode,
        elapsed,
        received,
        saved,
        truncated,
        timed_out,
        stopped,
        error,
        cleanup_incomplete,
        stop_reason,
    )
