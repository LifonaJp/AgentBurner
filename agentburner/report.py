from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .journal import JournalError, _safe_artifact_path, atomic_write
from .models import AuditResult, Summary


EXECUTION_STATES = ("completed", "harness_error", "timeout", "stopped", "interrupted")
VERDICTS = ("pass", "fail", "unknown", "not_evaluated")


def _artifact_refs(value: Any):
    if isinstance(value, dict):
        if {"path", "size", "sha256"} <= value.keys():
            yield value
        else:
            for child in value.values():
                yield from _artifact_refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _artifact_refs(child)


def _read_events(path: Path) -> tuple[list[dict[str, Any]], bool, list[str]]:
    events: list[dict[str, Any]] = []
    errors: list[str] = []
    crash_tail = False
    try:
        data = path.read_bytes()
    except OSError as exc:
        return [], False, [f"cannot read journal: {exc}"]
    lines = data.splitlines(keepends=True)
    for index, raw in enumerate(lines):
        if not raw.strip():
            continue
        try:
            event = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"invalid number {value}")))
        except (UnicodeError, ValueError) as exc:
            if index == len(lines) - 1 and not raw.endswith((b"\n", b"\r")):
                crash_tail = True
                break
            errors.append(f"journal line {index + 1} is invalid JSON")
            continue
        if not isinstance(event, dict):
            errors.append(f"journal line {index + 1} is not an object")
            continue
        events.append(event)
    return events, crash_tail, errors


def _check_artifact(run_dir: Path, ref: dict[str, Any]) -> str | None:
    relative = ref.get("path")
    if not isinstance(relative, str):
        return "artifact reference has no path"
    try:
        path = _safe_artifact_path(run_dir, relative, create_parents=False)
        if not path.is_file():
            return f"artifact is missing or unsafe: {relative}"
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as file:
            while chunk := file.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
    except (JournalError, OSError):
        return f"artifact is missing or unreadable: {relative}"
    if size != ref.get("size") or digest.hexdigest() != ref.get("sha256"):
        return f"artifact hash or size mismatch: {relative}"
    return None


def _summary_data(run_dir: Path, events: list[dict[str, Any]], crash_tail: bool) -> dict[str, Any]:
    scenario: dict[str, Any] = {}
    try:
        scenario = json.loads((run_dir / "control" / "scenario.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    planned = scenario.get("episodes", []) if isinstance(scenario, dict) else []
    planned_count = len(planned) if isinstance(planned, list) else 0
    run_start = next((event for event in events if event.get("type") == "run_started"), {})
    run_end = next((event for event in reversed(events) if event.get("type") == "run_finished"), None)
    finished: dict[str, dict[str, Any]] = {}
    started_ids: list[str] = []
    first_violation = None
    rule_results: dict[str, list[dict[str, Any]]] = defaultdict(list)
    pass_to_fail: list[dict[str, Any]] = []
    previous_by_rule: dict[str, str] = {}
    faults: list[dict[str, Any]] = []
    cleanup_incomplete = False
    parser_errors: list[dict[str, Any]] = []
    missing_artifacts: list[str] = []
    for event in events:
        kind = event.get("type")
        episode_id = event.get("episode_id")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if kind == "episode_started" and isinstance(episode_id, str):
            started_ids.append(episode_id)
        elif kind == "episode_finished" and isinstance(episode_id, str):
            finished[episode_id] = payload
            for oracle in payload.get("oracles", []):
                if not isinstance(oracle, dict) or not isinstance(oracle.get("oracle_id", oracle.get("id")), str):
                    continue
                rule_id, verdict = oracle.get("rule_id") or oracle.get("oracle_id", oracle.get("id")), oracle.get("verdict", "unknown")
                rule_results[rule_id].append({"episode_id": episode_id, "verdict": verdict})
                if verdict == "fail" and first_violation is None:
                    first_violation = {"episode_id": episode_id, "rule_id": rule_id, "summary": oracle.get("summary")}
                if previous_by_rule.get(rule_id) == "pass" and verdict == "fail":
                    pass_to_fail.append({"episode_id": episode_id, "rule_id": rule_id})
                previous_by_rule[rule_id] = verdict
        elif kind == "fault_applied":
            faults.append(payload)
        elif kind == "process_finished" and payload.get("cleanup_incomplete"):
            cleanup_incomplete = True
        elif kind == "parser_error":
            parser_errors.append({"episode_id": episode_id, **payload})
        for ref in _artifact_refs(payload):
            if _check_artifact(run_dir, ref):
                missing_artifacts.append(ref.get("path", "<unknown>"))

    execution_counts = Counter()
    verdict_counts = Counter()
    episode_summaries: list[dict[str, Any]] = []
    latencies: list[dict[str, Any]] = []
    usage: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") != "episode_finished":
            continue
        episode_id = event.get("episode_id")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        state = payload.get("execution_status", "harness_error")
        verdict = payload.get("auto_verdict", "unknown")
        execution_counts[state] += 1
        verdict_counts[verdict] += 1
        episode_summaries.append({
            "episode_id": episode_id,
            "execution_status": state,
            "auto_verdict": verdict,
            "latency_sec": payload.get("latency_sec"),
        })
        latencies.append({"episode_id": episode_id, "seconds": payload.get("latency_sec")})
        if payload.get("usage") is not None:
            usage.append({"episode_id": episode_id, "reported": payload["usage"]})
    interrupted = len(set(started_ids) - finished.keys())
    if interrupted:
        execution_counts["interrupted"] += interrupted
    for state in EXECUTION_STATES:
        execution_counts.setdefault(state, 0)
    for verdict in VERDICTS:
        verdict_counts.setdefault(verdict, 0)
    review_points_pending = [
        {"episode_id": episode.get("id"), **point}
        for episode in planned
        if isinstance(episode, dict) and episode.get("id") in set(started_ids)
        for point in episode.get("review_points", [])
        if isinstance(point, dict)
    ]
    ended_payload = run_end.get("payload", {}) if isinstance(run_end, dict) and isinstance(run_end.get("payload"), dict) else {}
    start_payload = run_start.get("payload", {}) if isinstance(run_start.get("payload"), dict) else {}
    config = start_payload.get("config", {}) if isinstance(start_payload.get("config"), dict) else {}
    return {
        "schema_version": "1.0",
        "run_id": run_start.get("run_id"),
        "runner_version": "0.1.0",
        "codex_version": start_payload.get("codex_version"),
        "adapter": "codex-cli",
        "config_hash": start_payload.get("config_hash"),
        "scenario_hash": start_payload.get("scenario_hash"),
        "fixture_hash": start_payload.get("fixture_hash"),
        "instruction_hash": start_payload.get("instruction_hash"),
        "session_id": next(
            (event.get("payload", {}).get("session_id") for event in events
             if event.get("type") == "episode_finished" and isinstance(event.get("payload"), dict)
             and event["payload"].get("session_id")),
            None,
        ),
        "model": config.get("model"),
        "missing_telemetry": start_payload.get("missing_telemetry", []),
        "planned_episode_count": planned_count,
        "started_episode_count": len(started_ids),
        "completed_episode_count": execution_counts["completed"],
        "execution_status_counts": dict(execution_counts),
        "auto_verdict_counts": dict(verdict_counts),
        "review_pending_count": len(review_points_pending),
        "review_points_pending": review_points_pending,
        "rule_id_results": dict(rule_results),
        "first_observed_violation": first_violation,
        "pass_to_fail_transitions": pass_to_fail,
        "faults": faults,
        "end_reason": ended_payload.get("end_reason", "crash_tail" if crash_tail else "incomplete"),
        "cleanup_incomplete": cleanup_incomplete,
        "runtime_sec": ended_payload.get("runtime_sec"),
        "episode_latency_sec": latencies,
        "episodes": episode_summaries,
        "reported_usage": usage,
        "missing_artifacts": sorted(set(missing_artifacts)),
        "parser_errors": parser_errors,
        "known_limitations": [
            "Semantic intent and review_points are not evaluated automatically.",
            "File checks observe Episode boundaries and may miss temporary changes that were restored.",
            "Excluded paths are not included in file-change verdicts.",
            "Snapshot hashes do not preserve file contents for recovery.",
        ],
        "incomplete": run_end is None or crash_tail,
    }


def audit(run_dir: Path, check_summary: bool = True, check_evaluation: bool = True) -> AuditResult:
    run_dir = run_dir.resolve()
    events, crash_tail, errors = _read_events(run_dir / "journal.jsonl")
    if not events:
        errors.append("journal has no complete events")
    expected_seq = 1
    run_id = None
    started: set[str] = set()
    finished: set[str] = set()
    seen_run_started = seen_run_finished = False
    for index, event in enumerate(events, 1):
        if seen_run_finished:
            errors.append("events found after run_finished")
        if not isinstance(event.get("seq"), int) or event["seq"] != expected_seq:
            errors.append(f"event {index}: sequence is not {expected_seq}")
            expected_seq = event.get("seq", expected_seq) + 1 if isinstance(event.get("seq"), int) else expected_seq + 1
        else:
            expected_seq += 1
        if event.get("schema_version") != "1.0" or not isinstance(event.get("type"), str) or not isinstance(event.get("payload"), dict):
            errors.append(f"event {index}: invalid event envelope")
        if run_id is None:
            run_id = event.get("run_id")
        elif event.get("run_id") != run_id:
            errors.append(f"event {index}: run_id changed")
        kind = event.get("type")
        episode_id = event.get("episode_id")
        if kind == "run_started":
            if seen_run_started or index != 1:
                errors.append("run_started must occur exactly once and first")
            seen_run_started = True
        elif kind == "run_finished":
            if seen_run_finished:
                errors.append("duplicate run_finished")
            seen_run_finished = True
        if kind == "episode_started":
            if not isinstance(episode_id, str) or episode_id in started:
                errors.append(f"event {index}: missing or duplicate episode id")
            else:
                started.add(episode_id)
        elif kind == "episode_finished":
            if episode_id not in started or episode_id in finished:
                errors.append(f"event {index}: episode finish has no unique matching start")
            else:
                finished.add(episode_id)
        payload = event.get("payload")
        if isinstance(payload, dict):
            for ref in _artifact_refs(payload):
                problem = _check_artifact(run_dir, ref)
                if problem:
                    errors.append(f"event {index}: {problem}")
    if events and not seen_run_started:
        errors.append("journal has no run_started event")
    if check_summary and (run_dir / "summary.json").is_file():
        try:
            cached = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
            rebuilt = _summary_data(run_dir, events, crash_tail)
            for key in (
                "run_id",
                "planned_episode_count",
                "started_episode_count",
                "completed_episode_count",
                "execution_status_counts",
                "auto_verdict_counts",
            ):
                if cached.get(key) != rebuilt.get(key):
                    errors.append(f"summary.json does not match journal field {key}")
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
            errors.append("summary.json is invalid JSON")
    if check_evaluation and (run_dir / "control" / "scenario.json").is_file():
        try:
            scenario = json.loads((run_dir / "control" / "scenario.json").read_text(encoding="utf-8"))
            if scenario.get("schema_version") == "1.1":
                from .evaluation import audit_evaluation
                errors.extend(audit_evaluation(run_dir))
        except (OSError, ValueError) as exc:
            errors.append(f"evaluation audit failed: {exc}")
    return AuditResult(not errors, errors, crash_tail, events)


def build_report(run_dir: Path) -> Summary:
    run_dir = run_dir.resolve()
    result = audit(run_dir, check_summary=False, check_evaluation=False)
    if not result.valid:
        raise JournalError("journal audit failed: " + "; ".join(result.errors))
    data = _summary_data(run_dir, result.events, result.crash_tail)
    summary_bytes = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    lines = [
        f"# AgentBurner report: {data.get('run_id') or run_dir.name}",
        "",
        f"- Status: {'incomplete' if data['incomplete'] else 'finished'}",
        f"- End reason: {data['end_reason']}",
        f"- Episodes: {data['completed_episode_count']} completed / {data['planned_episode_count']} planned",
        f"- Execution states: {json.dumps(data['execution_status_counts'], ensure_ascii=False, sort_keys=True)}",
        f"- Automatic verdicts: {json.dumps(data['auto_verdict_counts'], ensure_ascii=False, sort_keys=True)}",
        f"- Manual review pending: {data['review_pending_count']}",
        f"- Runtime: {data['runtime_sec'] if data['runtime_sec'] is not None else 'unknown'} seconds",
        "",
        "## First observed automatic violation",
        "",
    ]
    violation = data["first_observed_violation"]
    lines.append(
        "None observed." if violation is None else
        f"- Episode {violation['episode_id']}, rule {violation['rule_id']}: {violation.get('summary', '')}"
    )
    lines.extend(["", "## Episode results", ""])
    lines.extend(
        f"- {episode['episode_id']}: execution={episode['execution_status']}, auto_verdict={episode['auto_verdict']}"
        for episode in data["episodes"]
    )
    lines.extend(["", "## Manual review pending", ""])
    if data["review_points_pending"]:
        lines.extend(
            f"- {item['episode_id']} / {item['id']}: {item['criterion']}"
            for item in data["review_points_pending"]
        )
    else:
        lines.append("None.")
    lines.extend(["", "## Known limits", ""])
    lines.extend(f"- {item}" for item in data["known_limitations"])
    if data["missing_artifacts"]:
        lines.extend(["", "## Missing or corrupt artifacts", ""])
        lines.extend(f"- {item}" for item in data["missing_artifacts"])
    report_bytes = ("\n".join(lines) + "\n").encode("utf-8")
    atomic_write(run_dir / "summary.json", summary_bytes)
    atomic_write(run_dir / "report.md", report_bytes)
    return Summary(data)
