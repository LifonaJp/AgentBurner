from __future__ import annotations

import hashlib
import json
import math
import os
import re
import signal
import time
import threading
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ComparisonError(ValueError):
    pass


_INPUT_HASH_PATHS = (
    "plan.json", "inputs/source-plan.json", "inputs/source-config.json", "inputs/source-scenario.json",
    "inputs/resolved-base-config.json", "resolved/source.json", "resolved/left.json", "resolved/right.json",
    "resolved/left-config.json", "resolved/right-config.json", "resolved/left-scenario.json", "resolved/right-scenario.json",
    "resolved/left-check-definitions.json", "resolved/right-check-definitions.json",
)
_LEGACY_INPUT_HASH_PATHS = (
    "plan.json", "inputs/source-plan.json", "inputs/source-config.json", "inputs/source-scenario.json",
    "resolved/source.json", "resolved/left.json", "resolved/right.json", "resolved/left-config.json",
    "resolved/right-config.json", "resolved/left-scenario.json", "resolved/right-scenario.json",
)


def _input_hash_paths(frozen: dict) -> tuple[str, ...]:
    """Accept the original frozen manifest layout without weakening its hashes."""
    if not isinstance(frozen, dict) or any(not isinstance(key, str) or not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for key, value in frozen.items()):
        raise ComparisonError("frozen input hash manifest is invalid")
    paths = tuple(frozen)
    if set(paths) == set(_INPUT_HASH_PATHS):
        return _INPUT_HASH_PATHS
    if set(paths) == set(_LEGACY_INPUT_HASH_PATHS):
        return _LEGACY_INPUT_HASH_PATHS
    raise ComparisonError("frozen input hash manifest has missing or unexpected paths")


def _frozen_base(root: Path, plan: dict, source_base: dict) -> dict:
    path = root / "inputs" / "resolved-base-config.json"
    if path.is_file() and not path.is_symlink():
        return _read(path)
    # Older completed comparisons froze each resolved condition config. Use that
    # verified snapshot as the normalized base instead of consulting external files.
    legacy = _safe_file(root, "resolved/left-config.json")
    base = _read(legacy)
    if not isinstance(base, dict) or base.get("schema_version") != source_base.get("schema_version"):
        raise ComparisonError("legacy frozen resolved config is invalid")
    return base


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash_file(path: Path) -> str:
    return _hash(path.read_bytes())


def _safe_file(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or "\\" in relative or "\x00" in relative or ":" in relative:
        raise ComparisonError(f"unsafe comparison path: {relative}")
    parts = relative.split("/")
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ComparisonError(f"unsafe comparison path: {relative}")
    path = root
    for index, part in enumerate(parts):
        path = path / part
        mode = path.lstat().st_mode
        if path.is_symlink() or (index < len(parts) - 1 and not path.is_dir()):
            raise ComparisonError(f"unsafe comparison path: {relative}")
    if not path.is_file():
        raise ComparisonError(f"comparison file is missing: {relative}")
    return path


def _write(path: Path, value: Any) -> None:
    from .journal import atomic_write

    atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8"))


def _read(path: Path) -> Any:
    try:
        def finite_float(value):
            number = float(value)
            if not math.isfinite(number): raise ValueError(f"non-finite number {value}")
            return number
        return json.loads(path.read_text(encoding="utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"invalid number {value}")), parse_float=finite_float)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ComparisonError(f"cannot read {path}: {exc}") from exc


def _resolve(base: Path, value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _dependencies(scenario: dict, plan: dict) -> list[dict]:
    episodes = scenario.get("episodes")
    if not isinstance(episodes, list) or not episodes:
        raise ComparisonError("scenario must contain episodes")
    ids = [item.get("id") for item in episodes if isinstance(item, dict)]
    if len(ids) != len(episodes) or len(ids) != len(set(ids)):
        raise ComparisonError("scenario episode IDs must be unique")
    entries = plan.get("dependencies")
    if not isinstance(entries, list) or len(entries) != len(ids):
        raise ComparisonError("dependencies must declare every scenario episode exactly once")
    by_id = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"episode_id", "depends_on"} or entry["episode_id"] in by_id:
            raise ComparisonError("invalid or duplicate dependency entry")
        by_id[entry["episode_id"]] = entry["depends_on"]
    if set(by_id) != set(ids):
        raise ComparisonError("dependencies must declare every scenario episode exactly once")
    seen: set[str] = set()
    output = []
    for episode_id in ids:
        deps = by_id[episode_id]
        if not isinstance(deps, list) or len(deps) != len(set(deps)) or any(dep not in seen for dep in deps):
            raise ComparisonError(f"{episode_id}: dependencies must reference unique earlier episodes")
        if plan["mode"] == "workspace" and deps:
            raise ComparisonError("workspace mode requires independent episodes with empty dependencies")
        output.append({"episode_id": episode_id, "depends_on": list(deps)})
        seen.add(episode_id)
    return output


def validate_plan(path: str | Path) -> list[dict]:
    path = Path(path).expanduser().resolve(strict=True)
    plan = _read(path)
    return _validate_and_compile(plan, path.parent)[0]


def _validate_and_compile(plan: dict, plan_dir: Path) -> tuple[list[dict], dict, dict]:
    required = {"schema_version", "id", "base_config", "mode", "replicates", "order", "feedback_policy", "fresh_context", "dependencies", "pair_limits", "output_root"}
    if not isinstance(plan, dict) or set(plan) != required:
        raise ComparisonError("plan must contain exactly the required fields")
    if plan["schema_version"] != "1.0" or not isinstance(plan["id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]+", plan["id"]):
        raise ComparisonError("invalid plan schema_version or id")
    if plan["mode"] not in ("fault", "session", "workspace"):
        raise ComparisonError("mode must be fault, session, or workspace")
    if isinstance(plan["replicates"], bool) or not isinstance(plan["replicates"], int) or not 1 <= plan["replicates"] <= 20:
        raise ComparisonError("replicates must be an integer from 1 to 20")
    if plan["order"] != "alternating" or plan["feedback_policy"] not in ("none", "native"):
        raise ComparisonError("order must be alternating and feedback_policy must be none or native")
    valid_fresh = (plan["mode"] == "session" and plan["fresh_context"] in ("none", "active_rules")) or (plan["mode"] == "workspace" and plan["fresh_context"] == "active_rules") or (plan["mode"] == "fault" and plan["fresh_context"] is None)
    if not valid_fresh:
        raise ComparisonError("fresh_context must match the selected mode")
    limits = plan["pair_limits"]
    if not isinstance(limits, dict) or set(limits) != {"max_total_runtime_sec", "max_run_attempts"}:
        raise ComparisonError("pair_limits must contain max_total_runtime_sec and max_run_attempts")
    runtime = limits["max_total_runtime_sec"]
    attempts = limits["max_run_attempts"]
    if isinstance(runtime, bool) or not isinstance(runtime, (int, float)) or not math.isfinite(runtime) or runtime <= 0:
        raise ComparisonError("max_total_runtime_sec must be positive and finite")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts != plan["replicates"] * 2:
        raise ComparisonError("max_run_attempts must equal 2 * replicates")
    config_path = _resolve(plan_dir, plan["base_config"])
    base = _read(config_path)
    if base.get("schema_version") != "1.0":
        raise ComparisonError("base config schema_version must be 1.0")
    scenario_path = _resolve(config_path.parent, base.get("scenario", ""))
    fixture_path = _resolve(config_path.parent, base.get("fixture", ""))
    if not scenario_path.is_file() or not fixture_path.is_dir():
        raise ComparisonError("base scenario or fixture does not exist")
    scenario = _read(scenario_path)
    if scenario.get("schema_version") != "1.1":
        raise ComparisonError("comparison requires scenario schema_version 1.1")
    dependencies = _dependencies(scenario, plan)
    faults = [episode.get("fault") for episode in scenario["episodes"] if episode.get("fault") is not None]
    if plan["mode"] == "fault" and not faults:
        raise ComparisonError("fault mode requires at least one scenario fault")
    if plan["mode"] != "fault" and faults:
        raise ComparisonError("faults are only allowed in fault mode")
    if plan["mode"] == "workspace" and (not isinstance(scenario.get("rules", []), list)):
        raise ComparisonError("invalid scenario rules")
    from .config import load_config

    try:
        load_config(config_path)
    except Exception as exc:
        raise ComparisonError(f"invalid base config or scenario: {exc}") from exc
    compiled = []
    for condition, suffix in (("left", "left"), ("right", "right")):
        cfg = json.loads(json.dumps(base))
        scn = json.loads(json.dumps(scenario))
        mode = plan["mode"]
        session = "fresh_each_episode" if mode == "workspace" or (mode == "session" and condition == "right") else "persistent"
        workspace = "reset_each_episode" if mode == "workspace" and condition == "right" else "persistent"
        fresh = ("active_rules" if mode == "workspace" else plan["fresh_context"] if mode == "session" and condition == "right" else None)
        if mode == "fault" and condition == "left":
            for episode in scn["episodes"]:
                episode["fault"] = None
        if plan["feedback_policy"] == "none":
            cfg["feedback"] = "none"
            for episode in scn["episodes"]:
                if "feedback" in episode:
                    episode["feedback"] = "none"
        cfg["scenario"] = str(scenario_path)
        cfg["fixture"] = str(fixture_path)
        output = _resolve(plan_dir, plan["output_root"])
        cfg["output_root"] = str(output / "runs")
        policy = {"schema_version": "1.0", "session_mode": session, "workspace_mode": workspace,
                  "fresh_context": fresh, "comparison_id": plan["id"], "condition_id": condition,
                  "replicate_index": None}
        from .evaluation import scenario_definitions
        definitions = scenario_definitions(scn, fixture_path)
        compiled.append({"condition_id": condition, "config": cfg, "scenario": scn, "execution_policy": policy,
                         "source_config": str(config_path), "source_scenario": str(scenario_path),
                         "output_root": str(output / "runs"), "dependencies": dependencies,
                         "check_definitions": definitions})
    return compiled, base, scenario


def compile_plan(plan: dict, base: dict, scenario: dict) -> list[dict]:
    """Compile the declarative two-condition plan into runner inputs."""
    # Public pure API: avoid filesystem reads and retain supplied input objects by copy.
    if plan.get("mode") == "workspace" and any(item.get("depends_on") for item in plan.get("dependencies", [])):
        raise ComparisonError("workspace mode requires independent episodes")
    deps = _dependencies(scenario, plan)
    faults = [ep for ep in scenario.get("episodes", []) if ep.get("fault") is not None]
    if (plan.get("mode") == "fault") != bool(faults):
        raise ComparisonError("fault presence does not match comparison mode")
    output = []
    for condition in ("left", "right"):
        cfg, scn = json.loads(json.dumps(base)), json.loads(json.dumps(scenario))
        mode = plan["mode"]
        if mode == "fault" and condition == "left":
            for ep in scn["episodes"]: ep["fault"] = None
        if plan.get("feedback_policy") == "none":
            cfg["feedback"] = "none"
            for ep in scn["episodes"]:
                if "feedback" in ep: ep["feedback"] = "none"
        policy = {"schema_version": "1.0", "session_mode": "fresh_each_episode" if mode == "workspace" or mode == "session" and condition == "right" else "persistent",
                  "workspace_mode": "reset_each_episode" if mode == "workspace" and condition == "right" else "persistent",
                  "fresh_context": "active_rules" if mode == "workspace" else plan.get("fresh_context") if mode == "session" and condition == "right" else None,
                  "comparison_id": plan["id"], "condition_id": condition, "replicate_index": None}
        output.append({"condition_id": condition, "config": cfg, "scenario": scn, "execution_policy": policy, "dependencies": deps})
    return output


def _observation_key(row: dict) -> tuple:
    return tuple(row.get(key) for key in ("replicate_index", "episode_id", "rule_id", "check_id", "definition_hash", "lane"))


def pair_observations(left: list[dict], right: list[dict]) -> list[dict]:
    lmap: dict[tuple, list[dict]] = {}
    rmap: dict[tuple, list[dict]] = {}
    for row in left: lmap.setdefault(_observation_key(row), []).append(row)
    for row in right: rmap.setdefault(_observation_key(row), []).append(row)
    output = []
    for key in sorted(lmap.keys() | rmap.keys(), key=lambda item: tuple(str(x) for x in item)):
        ls, rs = lmap.get(key, []), rmap.get(key, [])
        count = max(len(ls), len(rs))
        for index in range(count):
            l = ls[index] if index < len(ls) else None
            r = rs[index] if index < len(rs) else None
            lv, rv = (l or {}).get("verdict"), (r or {}).get("verdict")
            applicable = lambda x: x is not None and x.get("applicability_result", "applicable") == "applicable" and x.get("verdict") in ("pass", "fail")
            reason = None if applicable(l) and applicable(r) else "missing_left" if l is None else "missing_right" if r is None else "left_" + str(l.get("verdict") or l.get("applicability_result")) if not applicable(l) else "right_" + str(r.get("verdict") or r.get("applicability_result"))
            output.append({"replicate_index": key[0], "episode_id": key[1], "rule_id": key[2], "check_id": key[3],
                           "definition_hash": key[4], "lane": key[5], "left": l, "right": r,
                           "left_verdict": lv, "right_verdict": rv, "paired": reason is None, "exclusion_reason": reason,
                           "left_execution_status": (l or {}).get("execution_status", "not_started"),
                           "right_execution_status": (r or {}).get("execution_status", "not_started"),
                           "left_review_status": (l or {}).get("review_status", "not_started"),
                           "right_review_status": (r or {}).get("review_status", "not_started"),
                           "dependency_warning": bool((l or {}).get("dependency_warning") or (r or {}).get("dependency_warning"))})
    return output


def summarize_pairs(rows: list[dict]) -> dict:
    counts = Counter()
    excluded = Counter()
    per_replicate: dict[str, Counter] = {}
    for row in rows:
        if not row.get("paired"):
            excluded[row.get("exclusion_reason", "not_applicable")] += 1
            continue
        l, r = row["left_verdict"], row["right_verdict"]
        name = {("pass", "pass"): "both_pass", ("pass", "fail"): "right_only_fail", ("fail", "pass"): "left_only_fail", ("fail", "fail"): "both_fail"}.get((l, r))
        if name is None:
            excluded["not_binary"] += 1
            continue
        counts[name] += 1
        per_replicate.setdefault(str(row.get("replicate_index")), Counter())[name] += 1
    for name in ("both_pass", "right_only_fail", "left_only_fail", "both_fail"):
        counts.setdefault(name, 0)
    n = sum(counts.values())
    delta = (counts["right_only_fail"] - counts["left_only_fail"]) / n if n else None
    replicates = {}
    for replicate, values in sorted(per_replicate.items()):
        m = sum(values.values())
        replicates[replicate] = {**{key: values[key] for key in ("both_pass", "right_only_fail", "left_only_fail", "both_fail")},
                                 "n": m, "paired_fail_rate_delta": (values["right_only_fail"] - values["left_only_fail"]) / m if m else None}
    empty_replicate = {name: 0 for name in ("both_pass", "right_only_fail", "left_only_fail", "both_fail")}
    for replicate in {str(row.get("replicate_index")) for row in rows if row.get("replicate_index") is not None}:
        replicates.setdefault(replicate, {**empty_replicate, "n": 0, "paired_fail_rate_delta": None})
    per_lane = {}
    for lane in ("auto", "manual"):
        lane_rows = [row for row in rows if row.get("lane") == lane]
        lane_counts = Counter()
        lane_excluded = Counter()
        lane_replicates: dict[str, Counter] = {}
        for row in lane_rows:
            if not row.get("paired"):
                lane_excluded[row.get("exclusion_reason", "not_applicable")] += 1
                continue
            result = {("pass", "pass"): "both_pass", ("pass", "fail"): "right_only_fail",
                      ("fail", "pass"): "left_only_fail", ("fail", "fail"): "both_fail"}.get((row.get("left_verdict"), row.get("right_verdict")))
            if result is None:
                lane_excluded["not_binary"] += 1
                continue
            lane_counts[result] += 1
            lane_replicates.setdefault(str(row.get("replicate_index")), Counter())[result] += 1
        for name in ("both_pass", "right_only_fail", "left_only_fail", "both_fail"):
            lane_counts.setdefault(name, 0)
        lane_n = sum(lane_counts.values())
        lane_per_replicate = {}
        for replicate, values in sorted(lane_replicates.items()):
            count = sum(values.values())
            lane_per_replicate[replicate] = {**{name: values[name] for name in ("both_pass", "right_only_fail", "left_only_fail", "both_fail")},
                                             "n": count, "paired_fail_rate_delta": (values["right_only_fail"] - values["left_only_fail"]) / count if count else None}
        for replicate in {str(row.get("replicate_index")) for row in lane_rows if row.get("replicate_index") is not None}:
            lane_per_replicate.setdefault(replicate, {**empty_replicate, "n": 0, "paired_fail_rate_delta": None})
        per_lane[lane] = {"paired_counts": dict(lane_counts), "n": lane_n,
                          "paired_fail_rate_delta": (lane_counts["right_only_fail"] - lane_counts["left_only_fail"]) / lane_n if lane_n else None,
                          "excluded_counts": dict(lane_excluded), "per_replicate": lane_per_replicate}
    return {"paired_counts": dict(counts), "n": n, "paired_fail_rate_delta": delta,
            "excluded_counts": dict(excluded), "per_replicate": replicates, "per_lane": per_lane}


def _run_events(run_dir: Path) -> tuple[list[dict], bool, list[str]]:
    from .report import _read_events
    return _read_events(run_dir / "journal.jsonl")


def _run_observations(run_dir: Path, replicate: int) -> tuple[list[dict], dict]:
    from .report import audit
    run_audit = audit(run_dir)
    if not run_audit.valid:
        raise ComparisonError("BASE/EVAL Run audit failed: " + "; ".join(run_audit.errors))
    events, crash_tail, errors = _run_events(run_dir)
    if errors:
        raise ComparisonError("invalid Run journal: " + "; ".join(errors))
    if not any(event.get("type") == "run_started" for event in events):
        raise ComparisonError("Run journal has no run_started event")
    if any(event.get("run_id") != run_dir.name for event in events):
        raise ComparisonError("Run journal identity does not match its directory")
    scenario = _read(run_dir / "control" / "scenario.json")
    episode_states = {}
    for event in events:
        if event.get("type") != "episode_finished": continue
        episode_id = event.get("episode_id")
        payload = event.get("payload") or {}
        episode_states[episode_id] = payload
    from .evaluation import build_evaluation
    evaluation = build_evaluation(run_dir)
    if not isinstance(evaluation, dict) or not isinstance(evaluation.get("per_check"), list):
        raise ComparisonError("EVAL did not produce per-check observations")
    observations = []
    for check in evaluation["per_check"]:
        lane = check.get("lane")
        if lane not in ("auto", "manual"):
            raise ComparisonError("EVAL observation has invalid lane")
        verdict = check.get("auto_verdict") if lane == "auto" else check.get("manual_verdict")
        episode_id = check.get("episode_id")
        episode_payload = episode_states.get(episode_id, {})
        observations.append({"replicate_index": replicate, "episode_id": episode_id, "rule_id": check.get("rule_id"),
                             "check_id": check.get("check_id"), "definition_hash": check.get("definition_hash"), "lane": lane,
                             "verdict": verdict or "not_evaluated", "applicability_result": check.get("applicability_result", "applicable"),
                             "review_status": check.get("review_status", "not_reviewed"),
                             "condition_contaminated": bool(check.get("condition_contaminated")),
                             "execution_status": episode_payload.get("execution_status", "not_started"),
                             "evidence_refs": check.get("evidence_refs", [])})
    return observations, {"events": events, "crash_tail": crash_tail, "scenario": scenario, "episode_states": episode_states,
                          "evaluation": evaluation, "journal_hash": _hash_file(run_dir / "journal.jsonl"), "errors": errors}


def _run_metrics(data: dict) -> dict:
    scenario, states, events = data["scenario"], data["episode_states"], data["events"]
    planned = [episode["id"] for episode in scenario.get("episodes", [])]
    starts = {event.get("episode_id") for event in events if event.get("type") == "episode_started"}
    finished = {episode_id: payload for episode_id, payload in states.items()}
    statuses = Counter(payload.get("execution_status", "unknown") for payload in finished.values())
    auto_verdicts = Counter(payload.get("auto_verdict", "unknown") for payload in finished.values())
    evaluation = data.get("evaluation") or {}
    per_check = evaluation.get("per_check", [])
    lane_counts = {}
    for lane in ("auto", "manual"):
        verdict_key = "auto_verdict" if lane == "auto" else "manual_verdict"
        pass_count = fail_count = 0
        for check in per_check:
            verdict = check.get(verdict_key)
            if check.get("lane") == lane and check.get("applicability_result") == "applicable":
                pass_count += verdict == "pass"
                fail_count += verdict == "fail"
        denominator = pass_count + fail_count
        lane_counts[lane] = {"pass": pass_count, "fail": fail_count, "numerator": fail_count,
                             "denominator": denominator, "fail_rate": fail_count / denominator if denominator else None}
    first_failure = next((episode_id for episode_id in planned if finished.get(episode_id, {}).get("auto_verdict") == "fail"), None)
    observed = [episode_id for episode_id in planned if episode_id in finished]
    end_payload = next((event.get("payload", {}) for event in reversed(events) if event.get("type") == "run_finished"), {})
    latencies = [{"episode_id": episode_id, "seconds": finished[episode_id].get("latency_sec")}
                 for episode_id in planned if finished.get(episode_id, {}).get("execution_status") == "completed"
                 and finished[episode_id].get("latency_sec") is not None]
    usage = [{"episode_id": episode_id, "reported": finished[episode_id]["usage"]}
             for episode_id in planned if episode_id in finished and finished[episode_id].get("usage") is not None]
    contaminated = {event.get("episode_id") for event in events if event.get("type") in ("episode_started", "episode_finished") and event.get("payload", {}).get("condition_contaminated")}
    return {"planned_episode_count": len(planned), "started_episode_count": len(starts),
            "completed_episode_count": statuses["completed"], "execution_status_counts": dict(statuses),
            "auto_verdict_counts": dict(auto_verdicts), "auto_failed_episode_count": auto_verdicts["fail"],
            "unknown_count": auto_verdicts["unknown"], "not_evaluated_count": auto_verdicts["not_evaluated"],
            "review_pending_count": (evaluation.get("coverage") or {}).get("pending", 0), "oracle_fail_rates": lane_counts,
            "end_reason": end_payload.get("end_reason", "crash_tail" if data.get("crash_tail") else "incomplete"),
            "first_observed_violation": {"episode_id": first_failure, "episode_index": planned.index(first_failure) + 1} if first_failure else None,
            "observed_until_episode": observed[-1] if observed else None, "stop_reason": end_payload.get("end_reason"),
            "episode_latency_sec": latencies, "reported_usage": usage,
            "condition_contaminated_episode_ids": [episode_id for episode_id in planned if episode_id in contaminated],
            "incomplete": not bool(end_payload)}


def _condition_summary(runs: list[dict]) -> dict:
    statuses, verdicts = Counter(), Counter()
    planned = started = completed = failed = pending = unknown = not_evaluated = 0
    lane_totals = {lane: Counter() for lane in ("auto", "manual")}
    for run in runs:
        planned += run["planned_episode_count"]
        started += run["started_episode_count"]
        completed += run["completed_episode_count"]
        failed += run["auto_failed_episode_count"]
        pending += run["review_pending_count"]
        unknown += run.get("unknown_count", 0)
        not_evaluated += run.get("not_evaluated_count", 0)
        statuses.update(run["execution_status_counts"])
        verdicts.update(run["auto_verdict_counts"])
        for lane in lane_totals:
            lane_totals[lane].update({key: run.get("oracle_fail_rates", {}).get(lane, {}).get(key, 0) for key in ("pass", "fail")})
    for counts in lane_totals.values():
        denominator = counts["pass"] + counts["fail"]
        counts["numerator"] = counts["fail"]
        counts["denominator"] = denominator
        counts["fail_rate"] = counts["fail"] / denominator if denominator else None
    return {"replicates": len(runs), "planned_episode_count": planned, "started_episode_count": started,
            "completed_episode_count": completed, "auto_failed_episode_count": failed,
            "review_pending_count": pending, "unknown_count": unknown, "not_evaluated_count": not_evaluated,
            "execution_status_counts": dict(statuses), "auto_verdict_counts": dict(verdicts),
            "oracle_fail_rates": {lane: dict(counts) for lane, counts in lane_totals.items()}, "replicate_metrics": runs}


def _empty_run_metrics(planned: int, reason: str = "not_started") -> dict:
    return {"planned_episode_count": planned, "started_episode_count": 0, "completed_episode_count": 0,
            "execution_status_counts": {}, "auto_verdict_counts": {}, "auto_failed_episode_count": 0,
            "unknown_count": 0, "not_evaluated_count": 0, "review_pending_count": 0,
            "oracle_fail_rates": {lane: {"pass": 0, "fail": 0, "numerator": 0, "denominator": 0, "fail_rate": None} for lane in ("auto", "manual")},
            "end_reason": reason, "first_observed_violation": None, "observed_until_episode": None,
            "stop_reason": reason, "episode_latency_sec": [], "reported_usage": [],
            "condition_contaminated_episode_ids": [], "incomplete": True}


def _ledger_errors(plan: dict, events: list[dict], allow_prefix: bool = False) -> list[str]:
    errors, starts, finished = [], {}, set()
    expected = [(rep, condition) for rep in range(1, plan["replicates"] + 1)
                for condition in (("left", "right") if rep % 2 else ("right", "left"))]
    keys, run_ids = [], set()
    for index, event in enumerate(events, 1):
        seq = event.get("seq")
        if (isinstance(seq, bool) or not isinstance(seq, int) or seq != index or event.get("schema_version") != "1.0"
                or event.get("comparison_id") != plan["id"] or not isinstance(event.get("payload"), dict)):
            errors.append(f"invalid comparison journal envelope at event {index}")
        kind, payload = event.get("type"), event.get("payload", {})
        if kind == "attempt_started":
            attempt_id = payload.get("attempt_id")
            key = (payload.get("replicate_index"), payload.get("condition_id"))
            keys.append(key)
            if not isinstance(attempt_id, str) or not attempt_id or attempt_id in starts:
                errors.append("missing or duplicate attempt_id")
            else: starts[attempt_id] = payload
            run_id = payload.get("run_id")
            if not isinstance(run_id, str) or not run_id or run_id in run_ids:
                errors.append("missing or duplicate reserved run_id")
            else: run_ids.add(run_id)
        elif kind == "attempt_finished":
            attempt_id = payload.get("attempt_id")
            if attempt_id not in starts or attempt_id in finished:
                errors.append("attempt_finished has no unique matching attempt_started")
            else:
                finished.add(attempt_id)
                for key in ("run_id", "condition_id", "replicate_index"):
                    if payload.get(key) != starts[attempt_id].get(key): errors.append(f"attempt {key} changed between start and finish")
    if keys != expected[:len(keys)]: errors.append("attempts do not follow the planned alternating order")
    if len(keys) > plan["pair_limits"]["max_run_attempts"]: errors.append("attempt count exceeds plan limit")
    starts = [i for i, event in enumerate(events) if event.get("type") == "comparison_started"]
    terminals = [i for i, event in enumerate(events) if event.get("type") in ("comparison_stopped", "comparison_finished")]
    if starts != [0]: errors.append("comparison_started must occur exactly once and first")
    if (not allow_prefix and len(terminals) != 1) or (allow_prefix and len(terminals) > 1):
        errors.append("comparison must have exactly one terminal event")
    elif terminals and terminals[0] != len(events) - 1:
        errors.append("events found after comparison terminal event")
    for event in events:
        timestamp = event.get("timestamp")
        try:
            if not isinstance(timestamp, str) or not timestamp.endswith("Z") or datetime.fromisoformat(timestamp[:-1] + "+00:00").utcoffset() is None:
                raise ValueError("invalid timestamp")
        except ValueError:
            errors.append("comparison journal contains an invalid UTC timestamp")
            break
    return errors


def check_comparability(left: dict, right: dict, plan: dict) -> dict:
    allowed = ["mode_generated_fault_schedule", "execution_policy", "prompt_presentation", "native_feedback"]
    excluded = {"run_id", "created_at", "output_root", "run_dir", "timestamp", "session_id"}
    differences, missing = [], []
    def has_nonfinite(value):
        if isinstance(value, float): return not math.isfinite(value)
        if isinstance(value, dict): return any(has_nonfinite(item) for item in value.values())
        if isinstance(value, list): return any(has_nonfinite(item) for item in value)
        return False
    if not isinstance(left, dict) or not isinstance(right, dict) or has_nonfinite(left) or has_nonfinite(right):
        return {"status": "incompatible", "allowed_differences": allowed,
                "unexpected_differences": [{"field": "metadata", "detail": "invalid shape or non-finite number"}], "missing_metadata": []}
    required_fields = ("runner_version", "adapter_version", "codex_version", "executable_sha256", "actual_model", "actual_provider",
                       "fixture_hash", "agents_hash", "inherited_context", "task_prompt_hashes", "rule_definitions",
                       "check_definitions", "sandbox", "limits", "exclusions", "feedback", "episode_feedback", "fault_schedule", "execution_policy")
    for field in required_fields:
        if field not in left or field not in right or left[field] is None or right[field] is None:
            missing.append(field)
    def flatten(value, prefix=""):
        if isinstance(value, dict):
            return {key: item for k, v in value.items() for key, item in flatten(v, f"{prefix}.{k}" if prefix else k).items()}
        return {} if prefix in excluded else {prefix: value}
    lmeta = flatten(left.get("metadata", left))
    rmeta = flatten(right.get("metadata", right))
    for key in sorted(lmeta.keys() | rmeta.keys()):
        a, b = lmeta.get(key), rmeta.get(key)
        if key not in lmeta or key not in rmeta or a is None or b is None:
            if key not in excluded: missing.append(key)
        elif a != b:
            leaf = key.rsplit(".", 1)[-1]
            permitted = (plan.get("mode") == "fault" and leaf == "fault_schedule") or key.startswith("execution_policy.") or leaf == "execution_policy" or leaf in ("session_mode", "workspace_mode", "fresh_context") or (plan.get("feedback_policy") == "native" and leaf in ("feedback", "episode_feedback"))
            if not permitted: differences.append({"field": key, "left": a, "right": b})
    status = "incompatible" if differences else "limited" if missing or plan.get("mode") == "session" or left.get("incomplete") or right.get("incomplete") else "comparable"
    return {"status": status, "allowed_differences": allowed, "unexpected_differences": differences, "missing_metadata": missing}


def build_comparison(comparison_dir: str | Path) -> dict:
    root = Path(comparison_dir).resolve(strict=True)
    plan = _read(root / "plan.json")
    resolved = {condition: _read(root / "resolved" / f"{condition}.json") for condition in ("left", "right")}
    source_runs, observations, metadata = [], {"left": [], "right": []}, {}
    condition_metrics = {"left": [], "right": []}
    attempts = []
    events, crash_tail, journal_errors = _read_comparison_journal(root / "comparison_journal.jsonl")
    for event in events:
        if event.get("type") in ("attempt_started", "attempt_finished"):
            payload = dict(event.get("payload", {}))
            key = payload.get("attempt_id")
            existing = next((item for item in attempts if item.get("attempt_id") == key), None)
            if existing is None:
                attempts.append(payload)
            else:
                existing.update(payload)
    source_errors = []
    frozen = _read_optional(root / "resolved" / "input_hashes.json") or {}
    try:
        _input_hash_paths(frozen)
        for relative, digest in frozen.items():
            target = _safe_file(root, relative)
            if _hash_file(target) != digest:
                source_errors.append(f"frozen input hash mismatch: {relative}")
    except (ComparisonError, OSError) as exc:
        source_errors.append(str(exc))
    start_event = next((event for event in events if event.get("type") == "comparison_started"), {})
    manifest_path = root / "resolved" / "input_hashes.json"
    if (start_event.get("payload", {}).get("input_hashes") != frozen or not manifest_path.is_file()
            or start_event.get("payload", {}).get("input_hash_manifest_sha256") != _hash_file(manifest_path)):
        source_errors.append("pre-execution input hash record mismatch")
    if journal_errors:
        source_errors.extend(journal_errors)
    source_errors.extend(_ledger_errors(plan, events, allow_prefix=crash_tail))
    for attempt in attempts:
        condition, replicate = attempt.get("condition_id"), attempt.get("replicate_index")
        run_id = attempt.get("run_id")
        if condition not in observations or not run_id:
            source_runs.append({"condition_id": condition, "replicate_index": replicate, "run_id": run_id, "status": "not_started"})
            continue
        run_dir = root / "runs" / run_id
        if not run_dir.is_dir() or not (run_dir / "journal.jsonl").is_file():
            source_runs.append({"condition_id": condition, "replicate_index": replicate, "run_id": run_id, "status": "not_started"})
            condition_metrics[condition].append({"replicate_index": replicate, **_empty_run_metrics(len(resolved[condition].get("scenario", {}).get("episodes", [])))})
            continue
        actual_journal_hash = _hash_file(run_dir / "journal.jsonl")
        if attempt.get("source_journal_sha256") and actual_journal_hash != attempt["source_journal_sha256"]:
            source_errors.append(f"source journal hash mismatch: {run_id}")
            source_runs.append({"condition_id": condition, "replicate_index": replicate, "run_id": run_id, "status": "corrupt"})
            condition_metrics[condition].append({"replicate_index": replicate, **_empty_run_metrics(len(resolved[condition].get("scenario", {}).get("episodes", [])), "corrupt")})
            continue
        try:
            obs, data = _run_observations(run_dir, replicate)
        except (ComparisonError, OSError, ValueError, KeyError, TypeError) as exc:
            source_errors.append(f"{run_id}: {exc}")
            source_runs.append({"condition_id": condition, "replicate_index": replicate, "run_id": run_id, "status": "corrupt"})
            condition_metrics[condition].append({"replicate_index": replicate, **_empty_run_metrics(len(resolved[condition].get("scenario", {}).get("episodes", [])), "corrupt")})
            continue
        dependencies = {item["episode_id"]: item["depends_on"] for item in plan["dependencies"]}
        for row in obs:
            row["dependency_warning"] = any(
                (data["episode_states"].get(dep) or {}).get("auto_verdict") in (None, "fail", "unknown", "not_evaluated")
                or (data["episode_states"].get(dep) or {}).get("execution_status") != "completed"
                for dep in dependencies.get(row["episode_id"], [])
            )
        observations[condition].extend(obs)
        metadata[(condition, replicate)] = _read_optional(run_dir / "control" / "metadata.json") or {}
        metrics = _run_metrics(data)
        condition_metrics[condition].append({"replicate_index": replicate, **metrics})
        has_start = any(event.get("type") == "run_started" for event in data["events"])
        source_runs.append({"condition_id": condition, "replicate_index": replicate, "run_id": run_id,
                            "journal_sha256": data["journal_hash"], "evaluation_hash": _hash(_json_bytes(data["evaluation"])) if data["evaluation"] is not None else None,
                            "review_set_hash": (data["evaluation"] or {}).get("review_set_hash") if data["evaluation"] else None,
                            "end_reason": next((e.get("payload", {}).get("end_reason") for e in data["events"] if e.get("type") == "run_finished"), "incomplete"),
                            "completed_episode_count": sum(p.get("execution_status") == "completed" for p in data["episode_states"].values()),
                            "planned_episode_count": len(data["scenario"].get("episodes", [])), "crash_tail": data["crash_tail"],
                            "status": "incomplete" if metrics["incomplete"] or data["crash_tail"] else "completed",
                            "run_started": has_start, "metrics": metrics})
    observed_slots = {(item.get("condition_id"), item.get("replicate_index")) for item in attempts}
    for replicate in range(1, plan["replicates"] + 1):
        for condition in ("left", "right"):
            if (condition, replicate) not in observed_slots:
                source_runs.append({"condition_id": condition, "replicate_index": replicate, "run_id": None, "status": "not_started", "run_started": False})
                condition_metrics[condition].append({"replicate_index": replicate, **_empty_run_metrics(len(resolved[condition].get("scenario", {}).get("episodes", [])))})
    dependency_map = {item["episode_id"]: item["depends_on"] for item in plan["dependencies"]}
    for condition in ("left", "right"):
        defs = resolved[condition].get("check_definitions", {})
        for replicate in range(1, plan["replicates"] + 1):
            present = {_observation_key(row) for row in observations[condition] if row.get("replicate_index") == replicate}
            for episode_id, checks in defs.items():
                for check in checks:
                    key = (replicate, episode_id, check.get("rule_id"), check.get("check_id"), check.get("definition_hash"), check.get("lane"))
                    if key in present: continue
                    run_record = next((item for item in source_runs if item.get("condition_id") == condition and item.get("replicate_index") == replicate), {})
                    not_started = run_record.get("status") == "not_started"
                    observations[condition].append({"replicate_index": replicate, "episode_id": episode_id, "rule_id": check.get("rule_id"),
                        "check_id": check.get("check_id"), "definition_hash": check.get("definition_hash"), "lane": check.get("lane"),
                        "verdict": "not_started", "applicability_result": "unknown" if check.get("applicability") == "conditional" else "applicable",
                        "review_status": "not_started" if not_started else "missing_observation",
                        "execution_status": "not_started" if not_started else "not_observed",
                        "evidence_refs": [], "dependency_warning": bool(dependency_map.get(episode_id))})
    rows = pair_observations(observations["left"], observations["right"])
    summary = summarize_pairs(rows)
    empty_counts = {name: 0 for name in ("both_pass", "right_only_fail", "left_only_fail", "both_fail")}
    for replicate in range(1, plan["replicates"] + 1):
        summary["per_replicate"].setdefault(str(replicate), {**empty_counts, "n": 0, "paired_fail_rate_delta": None})
    comp_results = []
    for replicate in range(1, plan["replicates"] + 1):
        lmeta = metadata.get(("left", replicate), {})
        rmeta = metadata.get(("right", replicate), {})
        comp_results.append(check_comparability(lmeta, rmeta, plan))
    ranks = {"comparable": 0, "limited": 1, "incompatible": 2}
    worst = max(comp_results, key=lambda item: ranks[item["status"]], default={"status": "limited", "allowed_differences": [], "unexpected_differences": [], "missing_metadata": []})
    comps = {"status": worst["status"], "allowed_differences": sorted({x for item in comp_results for x in item["allowed_differences"]}),
             "unexpected_differences": [row for item in comp_results for row in item["unexpected_differences"]],
             "missing_metadata": sorted({x for item in comp_results for x in item["missing_metadata"]}), "per_replicate": comp_results}
    if source_errors:
        comps["status"] = "incompatible"
        comps["unexpected_differences"].extend({"field": "source_run", "detail": item} for item in source_errors)
    elif crash_tail:
        comps["status"] = "limited"
    if any(source.get("status") in ("incomplete", "not_started") for source in source_runs) or len(attempts) < 2 * plan["replicates"]:
        if comps["status"] == "comparable": comps["status"] = "limited"
    if comps["status"] == "incompatible":
        summary = {"paired_counts": None, "n": 0, "paired_fail_rate_delta": None, "excluded_counts": {}, "per_replicate": {}, "per_lane": None}
        for row in rows:
            row["paired"] = False
            row["exclusion_reason"] = "incompatible_conditions"
    started = sum(source.get("run_started", False) for source in source_runs)
    planned = 2 * plan["replicates"]
    completed = sum(item.get("completed_episode_count", 0) == item.get("planned_episode_count", -1) and item.get("run_started", False) for item in source_runs)
    not_started = planned - started
    timing_map = {}
    for source in source_runs:
        key = (source["condition_id"], source["replicate_index"])
        timing_map[key] = {row["episode_id"]: row for row in source.get("metrics", {}).get("episode_latency_sec", [])}
    episode_latency = []
    for replicate in range(1, plan["replicates"] + 1):
        for episode in resolved["left"].get("scenario", {}).get("episodes", []):
            episode_id = episode["id"]
            ltime = timing_map.get(("left", replicate), {}).get(episode_id, {}).get("seconds")
            rtime = timing_map.get(("right", replicate), {}).get(episode_id, {}).get("seconds")
            if ltime is not None or rtime is not None:
                episode_latency.append({"replicate_index": replicate, "episode_id": episode_id,
                                        "left_seconds": ltime, "right_seconds": rtime,
                                        "difference_seconds": rtime - ltime if ltime is not None and rtime is not None else None})
    return {"schema_version": "1.0", "comparison_id": plan["id"], "plan_sha256": _hash_file(root / "plan.json"), "input_hashes": frozen,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"), "source_runs": source_runs,
            "comparability": comps, "coverage": {"planned": planned, "attempts_started": len(attempts), "started": started, "completed": completed, "not_started": not_started,
            "unpaired": sum(not row["paired"] for row in rows), "unknown": sum(row.get("left_verdict") == "unknown" or row.get("right_verdict") == "unknown" for row in rows),
                "pending": sum(run["review_pending_count"] for runs in condition_metrics.values() for run in runs),
                "not_applicable": sum(str(row.get("exclusion_reason") or "").endswith("not_applicable") for row in rows)},
            **summary, "condition_summaries": {condition: _condition_summary(items) for condition, items in condition_metrics.items()},
            "episode_latency_sec": episode_latency, "episode_rows": rows, "termination": {"journal_crash_tail": crash_tail, "journal_errors": journal_errors,
                "stopped": any(e.get("type") == "comparison_stopped" for e in events), "attempts": attempts},
            "audit_errors": source_errors, "limitations": ["Paired checks are descriptive observations, not independent Episode or Run samples.",
                           "A completed earlier Episode does not establish that its prerequisites held."]}


def _read_optional(path: Path):
    try: return _read(path)
    except ComparisonError: return None


def _read_comparison_journal(path: Path) -> tuple[list[dict], bool, list[str]]:
    if not path.exists(): return [], False, ["comparison journal is missing"]
    events, errors, crash = [], [], False
    lines = path.read_bytes().splitlines(keepends=True)
    for index, line in enumerate(lines):
        try:
            event = json.loads(line, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"invalid number {value}")),
                               parse_float=lambda value: float(value) if math.isfinite(float(value)) else (_ for _ in ()).throw(ValueError(f"non-finite number {value}")))
            if not isinstance(event, dict): errors.append(f"comparison journal line {index + 1} is not an object")
            else: events.append(event)
        except (json.JSONDecodeError, UnicodeError, ValueError):
            if index == len(lines) - 1 and not line.endswith((b"\n", b"\r")): crash = True
            else: errors.append(f"invalid comparison journal line {index + 1}")
    return events, crash, errors


def _comparison_event(root: Path, seq: int, kind: str, payload: dict) -> None:
    path = root / "comparison_journal.jsonl"
    event = {"schema_version": "1.0", "comparison_id": root.name, "seq": seq,
             "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"), "type": kind, "payload": payload}
    with path.open("ab") as file:
        file.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
        file.flush(); os.fsync(file.fileno())


def run_comparison(path: str | Path) -> tuple[Path, int]:
    path = Path(path).expanduser().resolve(strict=True)
    plan = _read(path)
    compiled, _, _ = _validate_and_compile(plan, path.parent)
    root = _resolve(path.parent, plan["output_root"]) / plan["id"]
    if root.exists(): raise ComparisonError(f"comparison already exists; use a new id: {root}")
    root.mkdir(parents=True)
    (root / "resolved").mkdir(); (root / "inputs").mkdir(); (root / "runs").mkdir()
    _write(root / "plan.json", plan)
    base_path = _resolve(path.parent, plan["base_config"])
    base = _read(base_path)
    scenario_path = _resolve(base_path.parent, base["scenario"])
    (root / "inputs" / "source-plan.json").write_bytes(path.read_bytes())
    (root / "inputs" / "source-config.json").write_bytes(base_path.read_bytes())
    (root / "inputs" / "source-scenario.json").write_bytes(scenario_path.read_bytes())
    resolved_base = dict(base)
    resolved_base["fixture"] = str(_resolve(base_path.parent, base["fixture"]))
    resolved_base["scenario"] = str(scenario_path.resolve())
    _write(root / "inputs" / "resolved-base-config.json", resolved_base)
    source = {"source_plan_sha256": _hash_file(root / "inputs" / "source-plan.json"),
              "source_config_sha256": _hash_file(root / "inputs" / "source-config.json"),
              "source_scenario_sha256": _hash_file(root / "inputs" / "source-scenario.json"),
              "source_plan_path": str(path), "source_config_path": str(base_path), "source_scenario_path": str(scenario_path)}
    _write(root / "resolved" / "source.json", source)
    (root / "comparison_journal.jsonl").touch()
    for item in compiled:
        condition = item["condition_id"]
        cfg = dict(item["config"])
        cfg["scenario"] = str((root / "resolved" / f"{condition}-scenario.json").resolve())
        cfg["output_root"] = str((root / "runs").resolve())
        item = {**item, "config": cfg}
        _write(root / "resolved" / f"{condition}.json", item)
        _write(root / "resolved" / f"{condition}-scenario.json", item["scenario"])
        _write(root / "resolved" / f"{condition}-check-definitions.json", item["check_definitions"])
        _write(root / "resolved" / f"{condition}-config.json", cfg)
    input_hashes = {relative: _hash_file(root / relative) for relative in _INPUT_HASH_PATHS}
    _write(root / "resolved" / "input_hashes.json", input_hashes)
    seq, attempts, started_at = 0, 0, time.monotonic()
    stop_code, stop_reason = 0, None
    _comparison_event(root, seq := seq + 1, "comparison_started", {"plan_sha256": _hash_file(root / "plan.json"),
        "input_hashes": input_hashes, "input_hash_manifest_sha256": _hash_file(root / "resolved" / "input_hashes.json"),
        "planned_attempts": 2 * plan["replicates"]})
    order = [(rep, condition) for rep in range(1, plan["replicates"] + 1) for condition in (("left", "right") if rep % 2 else ("right", "left"))]
    from . import runner
    stop_event = threading.Event()
    previous_handler = None
    if threading.current_thread() is threading.main_thread():
        previous_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, lambda _sig, _frame: stop_event.set())
    for replicate, condition in order:
        if stop_event.is_set() or (root / "STOP").exists():
            stop_code, stop_reason = 130, "user_stop"
            break
        if attempts >= plan["pair_limits"]["max_run_attempts"]: stop_code, stop_reason = 3, "max_run_attempts"; break
        elapsed = time.monotonic() - started_at
        if elapsed >= plan["pair_limits"]["max_total_runtime_sec"]: stop_code, stop_reason = 3, "runtime_limit"; break
        attempts += 1
        attempt_id = str(uuid.uuid4())
        reserved_run_id = runner._run_id()
        run_id_box: list[str | None] = [None]
        run_root = root / "runs"
        cfg_path = root / "resolved" / f"{condition}-config.json"
        policy = dict(next(x["execution_policy"] for x in compiled if x["condition_id"] == condition), replicate_index=replicate)
        deadline = started_at + float(plan["pair_limits"]["max_total_runtime_sec"])
        def on_created(run_dir):
            run_id_box[0] = Path(run_dir).name
            try: Path(run_dir).resolve().relative_to(run_root.resolve())
            except ValueError as exc: raise ComparisonError("runner created Run outside comparison run root") from exc
            target = root / "runs" / run_id_box[0]
            target.parent.mkdir(exist_ok=True)
            if Path(run_dir).resolve() != target.resolve():
                os.replace(Path(run_dir), target)
        def external_stop_check():
            if stop_event.is_set(): return "user_stop"
            if (root / "STOP").exists(): return "user_stop"
            if time.monotonic() >= deadline: return "runtime_limit"
            return None
        _comparison_event(root, seq := seq + 1, "attempt_started", {"attempt_id": attempt_id, "condition_id": condition, "replicate_index": replicate, "run_id": reserved_run_id})
        try:
            run_dir, code = runner.run(cfg_path, execution_policy=policy, external_stop_check=external_stop_check, on_created=on_created, run_id=reserved_run_id)
            run_dir = Path(run_dir)
            if run_id_box[0] is None: on_created(run_dir)
            if run_dir.resolve() != (root / "runs" / run_id_box[0]).resolve():
                os.replace(run_dir, root / "runs" / run_id_box[0])
            run_dir = root / "runs" / run_id_box[0]
            events, _, _ = _run_events(run_dir)
            end = next((e.get("payload", {}) for e in reversed(events) if e.get("type") == "run_finished"), {})
            _comparison_event(root, seq := seq + 1, "attempt_finished", {"attempt_id": attempt_id, "condition_id": condition, "replicate_index": replicate,
                "run_id": run_id_box[0], "exit_code": code, "end_reason": end.get("end_reason"), "source_journal_sha256": _hash_file(run_dir / "journal.jsonl")})
            if end.get("cleanup_incomplete") or code == 2 or end.get("end_reason") in ("user_stop", "harness_error", "journal_error", "injection_error"):
                stop_code = 130 if end.get("end_reason") == "user_stop" else 2
                stop_reason = end.get("end_reason") or "infrastructure_error"
                break
            if time.monotonic() >= deadline:
                stop_code, stop_reason = 3, "runtime_limit"
                break
        except Exception as exc:
            _comparison_event(root, seq := seq + 1, "attempt_finished", {"attempt_id": attempt_id, "condition_id": condition, "replicate_index": replicate,
                "run_id": run_id_box[0] or reserved_run_id, "exit_code": 2, "error": f"{type(exc).__name__}: {exc}",
                "source_journal_sha256": _hash_file(root / "runs" / run_id_box[0] / "journal.jsonl") if run_id_box[0] and (root / "runs" / run_id_box[0] / "journal.jsonl").exists() else None})
            stop_code, stop_reason = 2, "infrastructure_error"
            break
    if previous_handler is not None:
        signal.signal(signal.SIGINT, previous_handler)
    if stop_event.is_set() and stop_reason is None:
        stop_code, stop_reason = 130, "user_stop"
    if stop_reason:
        _comparison_event(root, seq := seq + 1, "comparison_stopped", {"reason": stop_reason, "exit_code": stop_code})
    else:
        _comparison_event(root, seq := seq + 1, "comparison_finished", {"attempts_started": attempts})
    write_comparison(root)
    return root, stop_code


def write_comparison(comparison_dir: str | Path) -> dict:
    from .journal import atomic_write
    root = Path(comparison_dir).resolve()
    data = build_comparison(root)
    serialized = (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    atomic_write(root / "comparison.json", serialized)
    lines = [f"# Comparison: {data['comparison_id']}", "", f"- Comparability: {data['comparability']['status']}",
             f"- Runs: {data['coverage']['started']} started / {data['coverage']['planned']} planned; {data['coverage']['not_started']} not started",
             f"- Paired checks: {data['n']}", f"- Paired fail rate delta (right-left): {data['paired_fail_rate_delta']}", "",
             "## Condition summaries", ""]
    for condition, summary in data["condition_summaries"].items():
        lines.extend([f"### {condition}", "", f"- Episodes: {summary['completed_episode_count']} completed / {summary['started_episode_count']} started / {summary['planned_episode_count']} planned",
                      f"- Execution states: {json.dumps(summary['execution_status_counts'], ensure_ascii=False, sort_keys=True)}",
                      f"- Automatic verdicts: {json.dumps(summary['auto_verdict_counts'], ensure_ascii=False, sort_keys=True)}",
                      f"- Automatic failed Episodes: {summary['auto_failed_episode_count']}", f"- Reviews pending: {summary['review_pending_count']}"])
        lines.extend([f"- Unknown checks: {summary['unknown_count']}", f"- Not evaluated checks: {summary['not_evaluated_count']}"])
        for lane, stats in summary["oracle_fail_rates"].items():
            rate = "null" if stats["fail_rate"] is None else f"{stats['fail_rate']:.6f}"
            lines.append(f"- {lane} fail rate: {stats['fail']}/{stats['denominator']} = {rate}")
        for run in summary["replicate_metrics"]:
            violation = run["first_observed_violation"]
            lines.append(f"- Replicate {run['replicate_index']}: end={run['end_reason']}; first violation={violation}; observed until={run['observed_until_episode']}; stop={run['stop_reason']}")
            if run["condition_contaminated_episode_ids"]:
                lines.append(f"  - AGENTS.md changed during these Episodes; instruction conditions may be contaminated: {', '.join(run['condition_contaminated_episode_ids'])}")
            lines.extend(f"  - Completed Episode latency: {item['episode_id']}={item['seconds']} sec" for item in run["episode_latency_sec"])
            lines.extend(f"  - Reported usage: {item['episode_id']}={json.dumps(item['reported'], ensure_ascii=False, sort_keys=True)}" for item in run["reported_usage"])
        lines.append("")
    lines.extend(["## Paired counts", ""])
    for name, count in (data.get("paired_counts") or {}).items(): lines.append(f"- {name}: {count}")
    lines.append(f"- N: {data['n']}")
    lines.extend(["", "## Paired results by lane", ""])
    for lane, lane_data in (data.get("per_lane") or {}).items():
        lines.append(f"### {lane}")
        lines.append(f"- N: {lane_data['n']}; right-left fail-rate delta: {lane_data['paired_fail_rate_delta']}")
        for name, count in lane_data["paired_counts"].items(): lines.append(f"- {name}: {count}")
    lines.extend(["", "## Paired counts by replicate", ""])
    for replicate, counts in data["per_replicate"].items(): lines.append(f"- {replicate}: {json.dumps(counts, ensure_ascii=False, sort_keys=True)}")
    lines.extend(["", "## Excluded observations", ""])
    for reason, count in data["excluded_counts"].items(): lines.append(f"- {reason}: {count}")
    lines.extend(["", "## Paired Episode latency", ""])
    for item in data["episode_latency_sec"]:
        lines.append(f"- Replicate {item['replicate_index']} / {item['episode_id']}: left={item['left_seconds']}, right={item['right_seconds']}, right-left={item['difference_seconds']} sec")
    lines.extend(["", "## Observation rows", ""])
    for row in data["episode_rows"]:
        key = "/".join(str(row.get(k)) for k in ("replicate_index", "episode_id", "rule_id", "check_id", "definition_hash", "lane"))
        lines.append(f"- {key}: left={row['left_verdict']} ({row['left_execution_status']}, review={row['left_review_status']}), right={row['right_verdict']} ({row['right_execution_status']}, review={row['right_review_status']}), paired={row['paired']}, exclusion={row['exclusion_reason']}, dependency_warning={row['dependency_warning']}")
        for side in ("left", "right"):
            item = row.get(side)
            if item and item.get("evidence_refs"):
                lines.append(f"  - {side} evidence: {json.dumps(item['evidence_refs'], ensure_ascii=False, sort_keys=True)}")
    lines.extend(["", "## Comparability details", "", f"- Allowed differences: {', '.join(data['comparability']['allowed_differences'])}"])
    lines.extend(f"- Unexpected difference: {item}" for item in data["comparability"]["unexpected_differences"])
    lines.extend(f"- Missing metadata: {item}" for item in data["comparability"]["missing_metadata"])
    lines.extend(["", "## Limitations", "", *[f"- {item}" for item in data["limitations"]]])
    atomic_write(root / "comparison.md", ("\n".join(lines) + "\n").encode("utf-8"))
    return data


def audit_comparison(comparison_dir: str | Path) -> dict:
    root = Path(comparison_dir).resolve(strict=True)
    errors: list[str] = []
    source_errors: list[str] = []
    try:
        plan = _read(root / "plan.json")
        source = _read(root / "resolved" / "source.json")
        source_plan = _read(root / "inputs" / "source-plan.json")
        base = _read(root / "inputs" / "source-config.json")
        scenario = _read(root / "inputs" / "source-scenario.json")
        resolved_base = _frozen_base(root, plan, base)
        if source_plan != plan: errors.append("frozen source plan does not match plan.json")
        if source.get("source_plan_sha256") != _hash_file(root / "inputs" / "source-plan.json"): errors.append("source plan hash mismatch")
        if source.get("source_config_sha256") != _hash_file(root / "inputs" / "source-config.json"): errors.append("source config hash mismatch")
        if source.get("source_scenario_sha256") != _hash_file(root / "inputs" / "source-scenario.json"): errors.append("source scenario hash mismatch")
        compiled = compile_plan(plan, resolved_base, scenario)
        recorded = {condition: _read(root / "resolved" / f"{condition}.json") for condition in ("left", "right")}
        for expected in compiled:
            condition = expected["condition_id"]
            value = recorded[condition]
            expected_config = dict(expected["config"])
            expected_config["scenario"] = str((root / "resolved" / f"{condition}-scenario.json").resolve())
            expected_config["output_root"] = str((root / "runs").resolve())
            if value.get("condition_id") != condition or value.get("scenario") != expected["scenario"]:
                errors.append(f"resolved {condition} condition/scenario does not match frozen inputs")
            if value.get("execution_policy") != expected["execution_policy"]: errors.append(f"resolved {condition} execution policy does not match frozen plan")
            if value.get("dependencies") != expected["dependencies"]: errors.append(f"resolved {condition} dependencies do not match frozen plan")
            if _read(root / "resolved" / f"{condition}-scenario.json") != expected["scenario"]:
                errors.append(f"resolved {condition} scenario file does not match frozen plan")
            if _read(root / "resolved" / f"{condition}-config.json") != expected_config:
                errors.append(f"resolved {condition} config does not match frozen plan")
            definitions_path = root / "resolved" / f"{condition}-check-definitions.json"
            definitions = _read(definitions_path) if definitions_path.is_file() and not definitions_path.is_symlink() else value.get("check_definitions")
            if not isinstance(definitions, dict) or value.get("check_definitions") != definitions:
                errors.append(f"resolved {condition} check definitions do not match their snapshot")
            else:
                from .evaluation import scenario_definitions
                fixture = Path(value.get("config", {}).get("fixture", ""))
                try:
                    if definitions != scenario_definitions(expected["scenario"], fixture):
                        errors.append(f"resolved {condition} check definitions do not match frozen scenario/fixture")
                except (OSError, ValueError) as exc:
                    errors.append(f"resolved {condition} check definitions cannot be verified: {exc}")

        input_hashes = _read(root / "resolved" / "input_hashes.json")
        try:
            _input_hash_paths(input_hashes)
        except ComparisonError as exc:
            errors.append(str(exc))
        for relative, digest in input_hashes.items():
            try:
                target = _safe_file(root, relative)
            except (ComparisonError, OSError):
                errors.append(f"frozen input hash path is missing or unsafe: {relative}")
                continue
            if _hash_file(target) != digest:
                errors.append(f"frozen input hash mismatch: {relative}")
        events, crash_tail, journal_errors = _read_comparison_journal(root / "comparison_journal.jsonl")
        errors.extend(journal_errors)
        errors.extend(_ledger_errors(plan, events, allow_prefix=crash_tail))
        starts = [event for event in events if event.get("type") == "attempt_started"]
        finishes = [event for event in events if event.get("type") == "attempt_finished"]
        started_event = next((event for event in events if event.get("type") == "comparison_started"), None)
        if not started_event or started_event.get("seq") != 1: errors.append("comparison_started must be the first event")
        else:
            start_payload = started_event.get("payload", {})
            manifest_path = root / "resolved" / "input_hashes.json"
            if start_payload.get("input_hashes") != input_hashes or start_payload.get("input_hash_manifest_sha256") != _hash_file(manifest_path):
                errors.append("pre-execution frozen input hashes do not match")
        expected_order = [(rep, condition) for rep in range(1, plan["replicates"] + 1)
                          for condition in (("left", "right") if rep % 2 else ("right", "left"))]
        start_keys, run_ids, start_by_attempt = [], set(), {}
        for event in starts:
            payload = event.get("payload", {})
            key = (payload.get("replicate_index"), payload.get("condition_id"))
            start_keys.append(key)
            attempt_id, run_id = payload.get("attempt_id"), payload.get("run_id")
            if not isinstance(attempt_id, str) or not attempt_id or attempt_id in start_by_attempt: errors.append("missing or duplicate attempt_id")
            else: start_by_attempt[attempt_id] = payload
            if not isinstance(run_id, str) or not run_id or not all(c.isalnum() or c in "_-" for c in run_id): errors.append("unsafe or missing reserved run_id")
            elif run_id in run_ids: errors.append(f"duplicate run_id: {run_id}")
            else: run_ids.add(run_id)
        if start_keys != expected_order[:len(start_keys)]: errors.append("attempts do not follow the planned alternating order")
        if len(starts) > plan["pair_limits"]["max_run_attempts"]: errors.append("attempt count exceeds plan limit")
        finished_ids = set()
        for event in finishes:
            payload = event.get("payload", {})
            attempt_id = payload.get("attempt_id")
            if attempt_id not in start_by_attempt: errors.append("attempt_finished has no matching attempt_started")
            if attempt_id in finished_ids: errors.append(f"duplicate attempt_finished: {attempt_id}")
            finished_ids.add(attempt_id)
            if attempt_id in start_by_attempt:
                start = start_by_attempt[attempt_id]
                for key in ("run_id", "condition_id", "replicate_index"):
                    if payload.get(key) != start.get(key): errors.append(f"attempt {key} changed between start and finish")
            run_id = payload.get("run_id")
            if not isinstance(run_id, str) or run_id not in run_ids: continue
            run_dir = root / "runs" / run_id
            expected_hash = payload.get("source_journal_sha256")
            journal_path = run_dir / "journal.jsonl"
            if not run_dir.is_dir() or not journal_path.is_file():
                if expected_hash: source_errors.append(f"source journal missing: {run_id}")
                continue
            if expected_hash and _hash_file(journal_path) != expected_hash: source_errors.append(f"source journal hash mismatch: {run_id}")
        orphaned = {path.name for path in (root / "runs").iterdir() if path.is_dir() or path.is_symlink()} - run_ids
        if orphaned: errors.append("unplanned Run directories: " + ", ".join(sorted(orphaned)))

        from .report import audit
        from .evaluation import audit_evaluation
        expected_by_attempt = {item["condition_id"]: item for item in compiled}
        for run_id in run_ids:
            run_dir = root / "runs" / run_id
            if run_dir.is_symlink() or not run_dir.is_dir():
                source_errors.append(f"Run directory is missing or unsafe: {run_id}")
                continue
            journal_path = run_dir / "journal.jsonl"
            if not journal_path.is_file():
                # Attempt consumed before BASE Run creation; this is an incomplete prefix, not a successful Run.
                continue
            result = audit(run_dir)
            source_errors.extend(f"{run_id}: {error}" for error in result.errors)
            if (run_dir / "control" / "scenario.json").is_file():
                source_errors.extend(f"{run_id}: {error}" for error in audit_evaluation(run_dir))
            else:
                source_errors.append(f"{run_id}: source scenario is missing")
            start = start_by_attempt.get(next((event.get("payload", {}).get("attempt_id") for event in starts if event.get("payload", {}).get("run_id") == run_id), ""), {})
            condition = start.get("condition_id")
            replicate = start.get("replicate_index")
            policy_path = run_dir / "control" / "execution_policy.json"
            if not policy_path.is_file():
                source_errors.append(f"{run_id}: execution_policy.json is missing")
            else:
                policy = _read(policy_path)
                expected_policy = dict(expected_by_attempt[condition]["execution_policy"], replicate_index=replicate)
                if policy != expected_policy: source_errors.append(f"{run_id}: execution policy does not match plan")
            source_scenario = expected_by_attempt.get(condition, {}).get("scenario")
            if source_scenario is not None and (run_dir / "control" / "scenario.json").is_file():
                if _read(run_dir / "control" / "scenario.json") != source_scenario: source_errors.append(f"{run_id}: run scenario differs from compiled input")
        errors.extend(source_errors)
        if not errors:
            rebuilt = build_comparison(root)
            cached = _read(root / "comparison.json")
            for key in sorted((set(cached) | set(rebuilt)) - {"generated_at"}):
                if cached.get(key) != rebuilt.get(key): errors.append(f"comparison.json does not match reconstructed {key}")
    except (ComparisonError, OSError, KeyError, TypeError, ValueError) as exc:
        errors.append(str(exc))
    return {"valid": not errors, "errors": errors, "comparison_id": root.name, "limited_prefix": bool(locals().get("crash_tail", False))}
