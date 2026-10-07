from __future__ import annotations

import hashlib
import json
import os
import signal
import shutil
import sys
import threading
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .codex_adapter import AdapterError, build_argv, is_logged_in, parse_jsonl, probe
from .config import ConfigError, load_config
from .journal import Journal, JournalError, artifact_ref, known_secrets, save_artifact
from .models import Episode, EpisodeContext, OracleResult, ProcessLimits, ProcessSpec, RunConfig, Scenario
from .oracle import OracleInfrastructureError, command_sandbox_status, evaluate
from .process import ProcessError, run_process
from .report import build_report
from .workspace import (
    DEFAULT_EXCLUSIONS,
    apply_delete_fault,
    copy_fixture,
    diff_artifact,
    filesystem_type,
    git_metadata,
    is_linux_filesystem_type,
    manifest_json,
    snapshot,
    tree_usage,
)


class RunError(RuntimeError):
    def __init__(self, message: str, exit_code: int = 2, run_dir: Path | None = None):
        super().__init__(message)
        self.exit_code = exit_code
        self.run_dir = run_dir


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _artifact_json(run_dir: Path, path: str, value: Any, secrets: tuple[str, ...]) -> dict:
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return save_artifact(run_dir, path, data, secrets)


def _auto_verdict(results: list[OracleResult]) -> str:
    verdicts = {result.verdict for result in results}
    if "fail" in verdicts:
        return "fail"
    if "unknown" in verdicts:
        return "unknown"
    return "pass" if results else "not_evaluated"


def _notice(previous_verdict: str, previous_results: list[OracleResult]) -> str:
    if previous_verdict == "fail":
        details = "; ".join(
            f"{result.oracle_id}=fail ({result.summary})"
            for result in previous_results
            if result.verdict == "fail"
        )
        return f"前の作業の検査結果: {details}。\nこの通知は前の作業の修正依頼ではありません。次の作業に進んでください。"
    if previous_verdict == "unknown":
        return "前の作業の検査結果: 判定不能。\nこの通知は前の作業の修正依頼ではありません。次の作業に進んでください。"
    return ""


def _effective_feedback(config: RunConfig, episode: Episode) -> str:
    return config.feedback if episode.feedback == "inherit" else episode.feedback


def _verify_command_oracles(
    scenario: Scenario,
    allow_command_oracles: bool = False,
) -> None:
    has_command_oracles = any(
        oracle.type == "command"
        for episode in scenario.episodes
        for oracle in episode.oracles
    )

    if not has_command_oracles:
        return

    if not allow_command_oracles:
        raise RunError(
            "scenario contains command oracles; "
            "re-run with --allow-command-oracles only if you trust the scenario"
        )

    available, detail = command_sandbox_status()
    if not available:
        raise RunError(
            f"command Oracle requires an isolated bubblewrap sandbox: {detail}"
        )


def _run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}_{uuid.uuid4().hex}"


def _reason_to_exit(reason: str, failed: bool) -> int:
    if reason == "user_stop":
        return 130
    if reason in ("runtime_limit", "disk_limit", "workspace_file_limit", "episode_timeout", "output_limit"):
        return 3
    if reason in ("harness_error", "injection_error", "journal_error"):
        return 2
    return 1 if failed else 0


def _validate_policy(policy: dict | None) -> dict | None:
    if policy is None:
        return None
    keys = {"schema_version", "session_mode", "workspace_mode", "fresh_context", "comparison_id", "condition_id", "replicate_index"}
    if not isinstance(policy, dict) or set(policy) != keys:
        raise ConfigError("invalid ExecutionPolicy fields")
    if policy["schema_version"] != "1.0" or policy["session_mode"] not in ("persistent", "fresh_each_episode") or policy["workspace_mode"] not in ("persistent", "reset_each_episode"):
        raise ConfigError("invalid ExecutionPolicy mode")
    if policy["fresh_context"] not in (None, "none", "active_rules"):
        raise ConfigError("invalid fresh_context")
    if policy["session_mode"] == "persistent" and (policy["fresh_context"] is not None or policy["workspace_mode"] != "persistent"):
        raise ConfigError("persistent sessions require persistent workspace and null fresh_context")
    if policy["session_mode"] == "fresh_each_episode" and policy["fresh_context"] is None:
        raise ConfigError("fresh sessions require a context policy")
    if policy["workspace_mode"] == "reset_each_episode" and policy["fresh_context"] != "active_rules":
        raise ConfigError("reset requires active_rules")
    if not isinstance(policy["replicate_index"], int) or isinstance(policy["replicate_index"], bool) or policy["replicate_index"] < 1:
        raise ConfigError("invalid replicate index")
    for key in ("comparison_id", "condition_id"):
        if not isinstance(policy[key], str) or not policy[key] or not all(c.isalnum() or c in "_-" for c in policy[key]):
            raise ConfigError(f"invalid {key}")
    return dict(policy)


def _render_prompt(scenario: Scenario, episode: Episode, notification: str, policy: dict | None) -> tuple[str, list[dict]]:
    if scenario.schema_version == "1.0":
        return (episode.prompt if not notification else f"{notification}\n\n次の作業:\n{episode.prompt}"), []
    from .evaluation import expand_rules, render_rule_updates

    raw = asdict(scenario)
    fresh = policy and policy["session_mode"] == "fresh_each_episode" and policy["fresh_context"] == "active_rules"
    if fresh:
        presented = [rule for rule in expand_rules(raw).get(episode.id, []) if rule["delivery"] == "prompt"]
        updates = "有効な指示:\n" + "\n".join(f"[有効 {rule['id']}] {rule['statement']}" for rule in sorted(presented, key=lambda r: r["id"])) if presented else ""
    else:
        updates = render_rule_updates(raw, episode.id)
        presented = [rule for rule in raw["rules"] if rule["delivery"] == "prompt" and rule["start_episode"] == episode.id]
    return "\n\n".join(part for part in (notification, updates, episode.prompt) if part), presented


def _runtime_metadata(config, scenario, capabilities, baseline, instructions, definitions, policy):
    from .cli import _inherited_context
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    inherited = _inherited_context(config.output_root, codex_home)
    paths = [*inherited["agents"], *inherited["project_settings"], *([inherited["settings"]] if inherited["settings"] else [])]
    inherited["file_hashes"] = {path: _digest(Path(path).read_bytes()) for path in paths}
    executable = shutil.which(config.codex_executable)
    if not executable and Path(config.codex_executable).is_file():
        executable = config.codex_executable
    return {
        "runner_version": "0.1.0", "adapter_version": "0.1.0", "codex_version": capabilities.version,
        "executable_sha256": _digest(Path(executable).read_bytes()) if executable else None,
        "actual_model": None, "actual_provider": None,
        "fixture_hash": _digest(manifest_json(baseline)),
        "agents_hash": _digest(instructions) if instructions is not None else None,
        "inherited_context": inherited,
        "task_prompt_hashes": [{"episode_id": ep.id, "sha256": _digest(ep.prompt.encode("utf-8"))} for ep in scenario.episodes],
        "rule_definitions": [asdict(rule) for rule in getattr(scenario, "rules", ())],
        "check_definitions": definitions,
        "sandbox": {"mode": "workspace-write", "approval": "never"},
        "limits": asdict(config.limits), "exclusions": list((*DEFAULT_EXCLUSIONS, *config.exclusions)),
        "feedback": config.feedback, "episode_feedback": [{"episode_id": ep.id, "feedback": ep.feedback} for ep in scenario.episodes],
        "fault_schedule": [{"episode_id": ep.id, "fault": asdict(ep.fault) if ep.fault else None} for ep in scenario.episodes],
        "execution_policy": policy,
    }


def run(
    config_path: str | Path,
    execution_policy: dict | None = None,
    external_stop_check: Callable[[], str | None] | None = None,
    on_created: Callable[[Path], None] | None = None,
    run_id: str | None = None,
    allow_command_oracles: bool = False,
) -> tuple[Path, int]:
    config_path = Path(config_path).expanduser().resolve(strict=True)

    try:
        config, scenario = load_config(config_path)
        policy = _validate_policy(execution_policy)

        if policy is not None and scenario.schema_version != "1.1":
            raise ConfigError("comparison execution requires scenario 1.1")

        if sys.platform != "linux":
            raise RunError("Run is supported only on Linux/WSL2")

        for label, path in (
            ("fixture", config.fixture),
            ("output_root", config.output_root),
        ):
            fstype = filesystem_type(path)
            if not is_linux_filesystem_type(fstype):
                raise RunError(
                    f"{label} must reside on a Linux filesystem; "
                    f"detected {fstype or 'unknown'}"
                )

        capabilities = probe(config.codex_executable)

        if not is_logged_in(config.codex_executable):
            raise RunError(
                "Codex CLI is not logged in; authenticate with "
                "`codex login` before starting a Run"
            )

        _verify_command_oracles(
            scenario,
            allow_command_oracles=allow_command_oracles,
        )

    except RunError:
        raise
    except (ConfigError, AdapterError, OSError) as exc:
        raise RunError(str(exc)) from exc

    secrets = known_secrets()
    exclusions = (*DEFAULT_EXCLUSIONS, *config.exclusions)
    config_bytes = config_path.read_bytes()
    scenario_bytes = config.scenario.read_bytes()
    output_root = config.output_root
    try:
        output_root.mkdir(parents=True, exist_ok=True)
        run_id = run_id or _run_id()
        if not run_id or not all(c.isalnum() or c in "_-" for c in run_id):
            raise ConfigError("unsafe run id")
        run_dir = output_root / run_id
        run_dir.mkdir()
        (run_dir / "control").mkdir()
        (run_dir / "artifacts").mkdir()
        workspace = run_dir / "workspace"
        copy_fixture(config.fixture, workspace)
        if on_created:
            on_created(run_dir)
    except OSError as exc:
        raise RunError(f"cannot prepare Run directory: {exc}") from exc

    journal: Journal | None = None
    started_at = time.monotonic()
    stop_event = threading.Event()
    previous_handler = None
    if threading.current_thread() is threading.main_thread():
        previous_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, lambda _sig, _frame: stop_event.set())
    reason = "completed"
    runner_error: str | None = None
    failed = False
    cleanup_incomplete = False
    session_id: str | None = None
    prior_verdict = "pass"
    prior_results: list[OracleResult] = []
    consecutive_failures = 0
    last_disk_check = 0.0
    pending_limit_reason: str | None = None
    started_episodes = 0
    run_lock = None
    seen_sessions: set[str] = set()
    rule_presentation_refs: dict[str, dict] = {}

    try:
        import fcntl
        run_lock = (run_dir / "control" / "run.lock").open("a+b")
        fcntl.flock(run_lock, fcntl.LOCK_EX)
        # Source snapshots are stored before AUT execution and never placed in its workspace.
        config_ref = save_artifact(run_dir, "control/config.json", config_bytes, secrets)
        scenario_ref = save_artifact(run_dir, "control/scenario.json", scenario_bytes, secrets)
        agents_path = workspace / "AGENTS.md"
        instruction_bytes = agents_path.read_bytes() if agents_path.is_file() else None
        instruction_ref = save_artifact(run_dir, "control/AGENTS.md", instruction_bytes, secrets) if instruction_bytes is not None else None
        run_start = snapshot(workspace, exclusions)
        baseline_ref = save_artifact(run_dir, "artifacts/run_start_manifest.json", manifest_json(run_start), secrets)
        definitions = {}
        extra_refs = []
        if scenario.schema_version == "1.1":
            from .evaluation import scenario_definitions
            definitions = scenario_definitions(scenario, config.fixture)
            extra_refs.append(_artifact_json(run_dir, "control/check_definitions.json", definitions, secrets))
        if policy:
            extra_refs.append(_artifact_json(run_dir, "control/execution_policy.json", policy, secrets))
        metadata = _runtime_metadata(config, scenario, capabilities, run_start, instruction_bytes, definitions, policy)
        extra_refs.append(_artifact_json(run_dir, "control/metadata.json", metadata, secrets))
        git_info = git_metadata(workspace)
        missing_telemetry = []
        if config.model is None:
            missing_telemetry.append("model")
        if git_info["commit"] is None:
            missing_telemetry.append("git_commit")
        if instruction_ref is None:
            missing_telemetry.append("AGENTS.md")
        journal = Journal(run_dir, run_id, secrets)
        journal.append_event("run_started", {
            "name": config.name,
            "codex_version": capabilities.version,
            "model": config.model,
            "config": {"model": config.model},
            "config_hash": _digest(config_bytes),
            "scenario_hash": _digest(scenario_bytes),
            "fixture_hash": _digest(manifest_json(run_start)),
            "instruction_hash": _digest(instruction_bytes) if instruction_bytes is not None else None,
            "missing_telemetry": missing_telemetry,
            "git": git_info,
            "exclusions": list(exclusions),
            "metadata": metadata,
            "execution_policy": policy,
            "artifacts": [config_ref, scenario_ref, baseline_ref, *([instruction_ref] if instruction_ref else []), *extra_refs],
        })

        def stop_check() -> str | None:
            nonlocal last_disk_check, pending_limit_reason
            if stop_event.is_set() or (run_dir / "STOP").exists():
                return "user_stop"
            if external_stop_check is not None:
                external_reason = external_stop_check()
                if external_reason:
                    return external_reason
            if time.monotonic() - started_at >= config.limits.max_runtime_sec:
                return "runtime_limit"
            now = time.monotonic()
            if not last_disk_check or now - last_disk_check >= 5 or pending_limit_reason:
                last_disk_check = now
                disk_bytes, workspace_files = tree_usage(run_dir), tree_usage(workspace)
                if disk_bytes[0] > config.limits.max_run_disk_bytes:
                    pending_limit_reason = "disk_limit"
                elif workspace_files[1] > config.limits.max_workspace_files:
                    pending_limit_reason = "workspace_file_limit"
            return pending_limit_reason

        for index, episode in enumerate(scenario.episodes, 1):
            stop_reason = stop_check()
            if stop_reason:
                reason = stop_reason
                journal.append_event("stop", {"reason": reason, "before_episode": episode.id})
                break
            if policy and policy["workspace_mode"] == "reset_each_episode":
                if index > 1:
                    previous_workspace = workspace
                    workspace = run_dir / "workspaces" / episode.id
                    copy_fixture(config.fixture, workspace)
                else:
                    previous_workspace = None
                journal.append_event("workspace_reset", {
                    "workspace": workspace.relative_to(run_dir).as_posix(),
                    "previous_workspace": previous_workspace.relative_to(run_dir).as_posix() if previous_workspace else None,
                    "fixture_hash": metadata["fixture_hash"], "previous_snapshot_retained": True,
                }, episode.id)
            if episode.fault:
                journal.append_event("fault_planned", {"type": episode.fault.type, "path": episode.fault.path}, episode.id)
                try:
                    apply_delete_fault(workspace, episode.fault.path)
                except (OSError, ConfigError) as exc:
                    journal.append_event("injection_error", {"path": episode.fault.path, "error": str(exc)}, episode.id)
                    reason = "injection_error"
                    break
                journal.append_event("fault_applied", {"type": episode.fault.type, "path": episode.fault.path}, episode.id)

            episode_start = snapshot(workspace, exclusions)
            episode_dir = f"artifacts/episodes/{episode.id}"
            run_dir.joinpath(episode_dir).mkdir(parents=True, exist_ok=False)
            notification = _notice(prior_verdict, prior_results) if index > 1 and _effective_feedback(config, episode) == "notify_and_continue" else ""
            prompt, presented_rules = _render_prompt(scenario, episode, notification, policy)
            sent_bytes = (prompt + "\n").encode("utf-8")
            prompt_ref = save_artifact(run_dir, f"{episode_dir}/prompt.txt", sent_bytes if scenario.schema_version == "1.1" else prompt.encode("utf-8"), secrets)
            task_ref = save_artifact(run_dir, f"{episode_dir}/task_prompt.txt", episode.prompt.encode("utf-8"), secrets)
            notification_ref = save_artifact(run_dir, f"{episode_dir}/notification.txt", notification.encode("utf-8"), secrets)
            extension_payload = {"task_prompt_artifact": task_ref, "task_prompt_hash": task_ref["sha256"], "notification_artifact": notification_ref,
                                 "workspace": workspace.relative_to(run_dir).as_posix()}
            if scenario.schema_version == "1.1":
                from .evaluation import snapshot_rules
                active_snapshot = snapshot_rules(scenario, episode.id)
                for rule in active_snapshot["rules"]:
                    if rule["delivery"] == "agents_file":
                        rule["artifact"] = instruction_ref
                        rule["presentation"] = "file_available_read_unknown"
                    elif rule["id"] in {item["id"] for item in presented_rules}:
                        rule["artifact"] = prompt_ref
                        rule["presentation"] = "planned_prompt"
                    else:
                        rule["artifact"] = rule_presentation_refs.get(rule["id"])
                        rule["presentation"] = "prior_prompt" if rule["artifact"] else "not_presented"
                    rule["history_available"] = not policy or policy["session_mode"] == "persistent"
                active_ref = _artifact_json(run_dir, f"active_rules/{episode.id}.json", active_snapshot, secrets)
                extension_payload["active_rules"] = active_ref
                current_agents = workspace / "AGENTS.md"
                current_instruction_hash = _digest(current_agents.read_bytes()) if current_agents.is_file() else None
                extension_payload["instruction_hash"] = current_instruction_hash
                extension_payload["condition_contaminated"] = current_instruction_hash != metadata["agents_hash"]
            before_ref = save_artifact(run_dir, f"{episode_dir}/before_manifest.json", manifest_json(episode_start), secrets)
            journal.append_event("episode_started", {
                "purpose": episode.purpose,
                "prompt_artifact": prompt_ref,
                "before_manifest": before_ref,
                "fault_path": episode.fault.path if episode.fault else None,
                **extension_payload,
            }, episode.id)
            started_episodes += 1

            stdout_rel = f"{episode_dir}/stdout.jsonl"
            stderr_rel = f"{episode_dir}/stderr.txt"
            stdout_path, stderr_path = run_dir / stdout_rel, run_dir / stderr_rel

            presentation_sent = False
            def process_sink(event_type: str, payload: dict) -> None:
                nonlocal presentation_sent
                journal.append_event(event_type, payload, episode.id)
                if event_type == "stdin_sent":
                    presentation_sent = True
                    for rule in presented_rules:
                        rule_presentation_refs[rule["id"]] = prompt_ref
                        journal.append_event("rule_presented", {"rule_id": rule["id"], "delivery": "prompt", "artifact": prompt_ref,
                                                                "action": "active_context" if policy and policy["fresh_context"] == "active_rules" else "added"}, episode.id)
                    for rule in getattr(scenario, "rules", ()):
                        if rule.delivery == "agents_file" and (index == 1 or policy and policy["session_mode"] == "fresh_each_episode"):
                            journal.append_event("rule_presented", {"rule_id": rule.id, "delivery": "agents_file", "artifact": instruction_ref, "read_status": "unknown"}, episode.id)
                        if rule.end_episode_exclusive == episode.id and rule.delivery == "prompt":
                            journal.append_event("rule_expired", {"rule_id": rule.id, "artifact": prompt_ref}, episode.id)

            episode_started = time.monotonic()
            execution_status = "harness_error"
            process_result = None
            parsed = None
            try:
                if policy and policy["session_mode"] == "fresh_each_episode":
                    session_id = None
                argv = build_argv(config, session_id)
                process_result = run_process(
                    ProcessSpec(tuple(argv), workspace, sent_bytes, stdout_path, stderr_path, secrets),
                    process_sink,
                    ProcessLimits(config.limits.episode_timeout_sec, config.limits.max_output_bytes_per_episode),
                    lambda: stop_check(),
                )
                cleanup_incomplete |= process_result.cleanup_incomplete
                parsed = parse_jsonl(stdout_path, session_id)
                for parser_error in parsed.parser_errors:
                    journal.append_event("parser_error", {"error": parser_error}, episode.id)
                if process_result.timed_out:
                    execution_status = "timeout"
                    reason = "episode_timeout"
                elif process_result.stopped:
                    execution_status = "stopped"
                    reason = process_result.stop_reason or stop_check() or "output_limit"
                elif process_result.error or process_result.cleanup_incomplete or process_result.returncode != 0 or parsed.parser_errors or parsed.turn_failed or not parsed.turn_completed:
                    execution_status = "harness_error"
                    reason = "harness_error"
                else:
                    execution_status = "completed"
                    if session_id is None:
                        session_id = parsed.session_id
                        if policy and policy["session_mode"] == "fresh_each_episode" and session_id in seen_sessions:
                            execution_status = "harness_error"
                            reason = "harness_error"
                        if session_id:
                            seen_sessions.add(session_id)
                            journal.append_event("session_started", {"session_id": session_id, "session_mode": policy["session_mode"] if policy else "persistent"}, episode.id)
                    elif parsed.session_id is not None and parsed.session_id != session_id:
                        execution_status = "harness_error"
                        reason = "harness_error"
                if execution_status == "completed" and (boundary_reason := stop_check()):
                    execution_status = "stopped"
                    reason = boundary_reason
            except (ProcessError, OSError, AdapterError) as exc:
                journal.append_event("harness_error", {"error": str(exc)}, episode.id)
                execution_status = "harness_error"
                reason = "harness_error"

            if scenario.schema_version == "1.1" and not presentation_sent:
                journal.append_event("presentation_uncertain", {"rule_ids": [rule["id"] for rule in presented_rules], "artifact": prompt_ref}, episode.id)

            after = snapshot(workspace, exclusions)
            after_ref = save_artifact(run_dir, f"{episode_dir}/after_manifest.json", manifest_json(after), secrets)
            diff_ref = save_artifact(run_dir, f"{episode_dir}/diff.txt", diff_artifact(workspace, episode_start, after), secrets)
            artifacts = [prompt_ref, before_ref, after_ref, diff_ref]
            if stdout_path.is_file():
                artifacts.append(artifact_ref(run_dir, stdout_rel))
            if stderr_path.is_file():
                artifacts.append(artifact_ref(run_dir, stderr_rel))
            if parsed is not None and parsed.final_message is not None:
                artifacts.append(save_artifact(
                    run_dir,
                    f"{episode_dir}/final_message.txt",
                    parsed.final_message.encode("utf-8"),
                    secrets,
                ))
            results: list[OracleResult] = []
            if execution_status == "completed":
                context = EpisodeContext(
                    workspace,
                    episode_start,
                    run_start,
                    run_dir / "artifacts" / "oracle",
                    config.limits.oracle_timeout_sec,
                    secrets,
                    exclusions,
                    lambda: stop_check(),
                )
                for oracle in episode.oracles:
                    try:
                        result = evaluate(oracle, context)
                    except OracleInfrastructureError as exc:
                        result = OracleResult(oracle.id, "unknown", "oracle infrastructure error", execution_error=str(exc))
                        results.append(result)
                        if result.execution_error in ("user_stop", "runtime_limit", "disk_limit", "workspace_file_limit"):
                            execution_status = "stopped"
                            reason = result.execution_error
                            break
                        journal.append_event("oracle_error", {"oracle_id": oracle.id, "error": str(exc)}, episode.id)
                        execution_status = "harness_error"
                        reason = "harness_error"
                        break
                    results.append(result)
                    if result.execution_error in ("user_stop", "runtime_limit", "disk_limit", "workspace_file_limit"):
                        execution_status = "stopped"
                        reason = result.execution_error
                        break
                    if result.execution_error == "cleanup_incomplete":
                        journal.append_event("oracle_error", {"oracle_id": oracle.id, "error": result.execution_error}, episode.id)
                        execution_status = "harness_error"
                        reason = "harness_error"
                        break
                    if result.output:
                        ref = save_artifact(
                            run_dir,
                            f"{episode_dir}/oracle-{oracle.id}.txt",
                            result.output.encode("utf-8"),
                            secrets,
                        )
                        artifacts.append(ref)
            verdict = _auto_verdict(results)
            if execution_status != "completed" and verdict == "not_evaluated":
                verdict = "unknown"
            serialized_results = [asdict(result) for result in results]
            if scenario.schema_version == "1.1":
                episode_definitions = definitions.get(episode.id, [])
                for item in serialized_results:
                    check = next((check for check in episode_definitions if check["check_id"] == item["oracle_id"] and check.get("lane") == "auto"), None)
                    spec = next(spec for spec in episode.oracles if spec.id == item["oracle_id"])
                    item.update({"check_id": spec.id, "rule_id": spec.rule_id, "applicability_result": "applicable",
                                 "definition_hash": check["definition_hash"] if check else None})
            oracle_ref = _artifact_json(run_dir, f"{episode_dir}/oracle-results.json", serialized_results, secrets)
            artifacts.append(oracle_ref)
            latency = time.monotonic() - episode_started
            payload = {
                "execution_status": execution_status,
                "auto_verdict": verdict,
                "latency_sec": round(latency, 3),
                "session_id": session_id,
                "model": config.model,
                "unknown_event_count": parsed.unknown_events if parsed else 0,
                "parser_error_count": len(parsed.parser_errors) if parsed else 0,
                "usage": list(parsed.usage) if parsed else None,
                "oracles": serialized_results,
                "artifacts": artifacts,
                "received_bytes": process_result.received_bytes if process_result else 0,
                "saved_bytes": process_result.saved_bytes if process_result else 0,
                "truncated": process_result.truncated if process_result else False,
                "cleanup_incomplete": process_result.cleanup_incomplete if process_result else False,
                "presentation_status": "sent" if presentation_sent else "uncertain",
                "workspace": workspace.relative_to(run_dir).as_posix(),
                "instruction_hash": after.entries.get("AGENTS.md").sha256 if after.entries.get("AGENTS.md") else None,
                "condition_contaminated": (after.entries.get("AGENTS.md").sha256 if after.entries.get("AGENTS.md") else None) != metadata["agents_hash"],
            }
            journal.append_event("episode_finished", payload, episode.id)
            failed |= verdict == "fail"
            if verdict == "fail":
                consecutive_failures += 1
            else:
                consecutive_failures = 0
            prior_verdict, prior_results = verdict, results

            if execution_status != "completed":
                break
            if boundary_reason := stop_check():
                reason = boundary_reason
                journal.append_event("stop", {"reason": reason, "after_episode": episode.id})
                break
            effective_feedback = _effective_feedback(config, episode)
            if verdict == "fail" and effective_feedback == "stop_on_fail":
                reason = "stop_on_fail"
                journal.append_event("stop", {"reason": reason, "after_episode": episode.id})
                break
            if verdict == "fail" and consecutive_failures >= config.limits.max_consecutive_failures:
                reason = "consecutive_failures"
                journal.append_event("stop", {"reason": reason, "after_episode": episode.id})
                break
        else:
            reason = "completed"
    except (JournalError, OSError, ConfigError, OracleInfrastructureError) as exc:
        runner_error = str(exc)
        reason = "journal_error" if isinstance(exc, JournalError) else "harness_error"
    except Exception as exc:
        runner_error = f"unexpected harness error: {type(exc).__name__}: {exc}"
        reason = "harness_error"
    finally:
        if journal is not None:
            try:
                journal.append_event("run_finished", {
                    "end_reason": reason,
                    "runtime_sec": round(time.monotonic() - started_at, 3),
                    "started_episodes": started_episodes,
                    "runner_error": runner_error,
                    "cleanup_incomplete": cleanup_incomplete,
                })
            except JournalError:
                reason = "journal_error"
            try:
                journal.close()
            except JournalError:
                reason = "journal_error"
        if previous_handler is not None:
            signal.signal(signal.SIGINT, previous_handler)

    try:
        build_report(run_dir)
        if scenario.schema_version == "1.1":
            from .evaluation import write_evaluation
            write_evaluation(run_dir)
    except (JournalError, OSError) as exc:
        runner_error = runner_error or str(exc)
        reason = "journal_error"
    finally:
        if run_lock is not None:
            run_lock.close()
    if runner_error:
        raise RunError(f"Run stopped: {runner_error}. Artifacts: {run_dir}", _reason_to_exit(reason, failed), run_dir)
    return run_dir, _reason_to_exit(reason, failed)
