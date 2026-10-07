from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path

from .models import EpisodeContext, OracleResult, OracleSpec, ProcessLimits, ProcessSpec
from .process import run_process
from .workspace import changed_paths, copy_fixture


class OracleInfrastructureError(RuntimeError):
    pass


def command_sandbox_status() -> tuple[bool, str]:
    executable = shutil.which("bwrap")
    if executable is None:
        return False, "bubblewrap (bwrap) is not installed"
    try:
        import subprocess

        result = subprocess.run(
            [executable, "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev", "--unshare-net", "--unshare-pid", "--die-with-parent", "--", "/bin/true"],
            capture_output=True,
            timeout=5,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"bubblewrap probe failed: {exc}"
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        return False, f"bubblewrap sandbox unavailable: {detail or result.returncode}"
    return True, executable


def _same_paths(paths: tuple[str, ...], baseline, current) -> tuple[bool, list[str]]:
    changed = [path for path in paths if baseline.entries.get(path) != current.entries.get(path)]
    return not changed, changed


def _command_oracle(spec: OracleSpec, context: EpisodeContext) -> OracleResult:
    available, detail = command_sandbox_status()
    if not available:
        raise OracleInfrastructureError(detail)
    bwrap = detail
    try:
        context.oracle_root.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="oracle-", dir=context.oracle_root)
    except OSError as exc:
        raise OracleInfrastructureError(f"cannot create isolated oracle workspace: {exc}") from exc
    with temporary as temp_name:
        oracle_root = Path(temp_name) / "workspace"

        def copy_regular(source: str, destination: str) -> str:
            if not stat.S_ISREG(os.lstat(source).st_mode):
                raise OracleInfrastructureError(f"workspace contains a special file: {source}")
            return shutil.copy2(source, destination)

        try:
            shutil.copytree(context.workspace, oracle_root, symlinks=True, copy_function=copy_regular)
        except (OSError, shutil.Error, OracleInfrastructureError) as exc:
            raise OracleInfrastructureError(f"cannot copy workspace for oracle: {exc}") from exc
        command = [arg.replace("{workspace}", str(oracle_root)).replace("{oracle_root}", str(oracle_root)) for arg in spec.argv]
        (oracle_root / ".oracle-home").mkdir()
        (oracle_root / ".oracle-tmp").mkdir()
        sandbox_argv = [
            bwrap,
            "--ro-bind", "/", "/",
            "--bind", str(oracle_root), str(oracle_root),
            "--proc", "/proc",
            "--dev", "/dev",
            "--unshare-net",
            "--unshare-pid",
            "--die-with-parent",
            "--chdir", str(oracle_root),
            "--",
            *command,
        ]
        stdout_path = Path(temp_name) / "stdout.txt"
        stderr_path = Path(temp_name) / "stderr.txt"
        try:
            result = run_process(
                ProcessSpec(
                    tuple(sandbox_argv),
                    context.workspace,
                    b"",
                    stdout_path,
                    stderr_path,
                    context.secrets,
                    {
                        "PATH": os.defpath,
                        "LANG": "C.UTF-8",
                        "LC_ALL": "C.UTF-8",
                        "HOME": str(oracle_root / ".oracle-home"),
                        "TMPDIR": str(oracle_root / ".oracle-tmp"),
                    },
                ),
                None,
                ProcessLimits(context.timeout_sec, 1_048_576),
                context.stop_check,
            )
        except OSError as exc:
            raise OracleInfrastructureError(f"cannot launch sandboxed oracle: {exc}") from exc
        output_parts: list[str] = []
        for path in (stdout_path, stderr_path):
            try:
                output_parts.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError as exc:
                raise OracleInfrastructureError(f"cannot read oracle output: {exc}") from exc
        output = "".join(output_parts)
        if result.error or result.returncode is None:
            raise OracleInfrastructureError(result.error or "oracle process did not return an exit code")
        if result.cleanup_incomplete:
            return OracleResult(spec.id, "unknown", "command oracle process cleanup incomplete", output, "cleanup_incomplete")
        if result.timed_out:
            return OracleResult(spec.id, "unknown", "command oracle timed out", output, "timeout")
        if result.stopped:
            error = "oracle_output_limit" if result.stop_reason == "output_limit" else result.stop_reason
            return OracleResult(spec.id, "unknown", "command oracle was stopped", output, error)
        if result.truncated:
            return OracleResult(spec.id, "unknown", "command oracle output limit reached", output, "output_limit")
        if result.returncode == 0:
            return OracleResult(spec.id, "pass", "command exited with status 0", output)
        if result.returncode == 1:
            return OracleResult(spec.id, "fail", "command exited with status 1", output)
        return OracleResult(spec.id, "unknown", f"command exited with status {result.returncode}", output)


def evaluate(spec: OracleSpec, context: EpisodeContext) -> OracleResult:
    if spec.type == "unchanged":
        baseline = context.run_start if spec.baseline == "run_start" else context.episode_start
        from .workspace import snapshot

        current = snapshot(context.workspace, context.exclusions)
        passed, changed = _same_paths(spec.paths, baseline, current)
        return OracleResult(
            spec.id,
            "pass" if passed else "fail",
            "all paths are unchanged" if passed else f"changed paths: {', '.join(changed)}",
        )
    if spec.type == "allowed_changes":
        from .workspace import snapshot

        current = snapshot(context.workspace, context.exclusions)
        changed = changed_paths(context.episode_start, current)
        unexpected = sorted(changed - set(spec.paths))
        return OracleResult(
            spec.id,
            "pass" if not unexpected else "fail",
            "changes stayed within the allowed paths" if not unexpected else f"unexpected changed paths: {', '.join(unexpected)}",
        )
    if spec.type == "required_paths":
        from .workspace import snapshot

        current = snapshot(context.workspace, context.exclusions)
        missing = [path for path in spec.paths if (entry := current.entries.get(path)) is None or entry.kind != "file"]
        return OracleResult(
            spec.id,
            "pass" if not missing else "fail",
            "all required regular files exist" if not missing else f"missing regular files: {', '.join(missing)}",
        )
    if spec.type == "command":
        return _command_oracle(spec, context)
    return OracleResult(spec.id, "unknown", "unsupported oracle type", execution_error="configuration")
