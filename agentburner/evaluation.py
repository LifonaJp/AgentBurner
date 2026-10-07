from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from .config import ConfigError
from .models import Scenario


def _dict(value: Any) -> Any:
    return asdict(value) if is_dataclass(value) else value


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _normalized_rule(rule: dict | None) -> dict | None:
    if rule is None:
        return None
    return {"id": rule.get("id"), "family_id": rule.get("family_id"), "kind": rule.get("kind"), "statement": rule.get("statement"), "start_episode": rule.get("start_episode"), "end_episode_exclusive": rule.get("end_episode_exclusive"), "delivery": rule.get("delivery", "prompt"), "supersedes": rule.get("supersedes")}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def expand_rules(scenario: dict | Scenario) -> dict[str, list[dict]]:
    """Expand rule validity in episode array order (never by ID or timestamp)."""
    raw = _dict(scenario)
    episodes = raw.get("episodes", [])
    ids = [episode["id"] for episode in episodes]
    position = {episode_id: index for index, episode_id in enumerate(ids)}
    result: dict[str, list[dict]] = {episode_id: [] for episode_id in ids}
    for rule in raw.get("rules", []):
        if rule.get("start_episode") not in position:
            raise ConfigError(f"rule {rule.get('id')}: unknown start episode")
        start = position[rule["start_episode"]]
        end_id = rule.get("end_episode_exclusive")
        if end_id is not None and end_id not in position:
            raise ConfigError(f"rule {rule.get('id')}: unknown end episode")
        end = position[end_id] if end_id is not None else len(ids)
        if end <= start:
            raise ConfigError(f"rule {rule.get('id')}: end must follow start")
        for episode_id in ids[start:end]:
            result.setdefault(episode_id, []).append(dict(rule))
    for active in result.values():
        active.sort(key=lambda rule: rule["id"])
    return result


def render_rule_updates(scenario: dict | Scenario, episode_id: str) -> str:
    raw = _dict(scenario)
    episodes = [episode["id"] for episode in raw.get("episodes", [])]
    if episode_id not in episodes:
        raise ConfigError(f"unknown episode: {episode_id}")
    index = episodes.index(episode_id)
    rules = raw.get("rules", [])
    expired = sorted(rule["id"] for rule in rules if rule.get("end_episode_exclusive") == episode_id and rule.get("delivery") == "prompt")
    added = sorted((rule for rule in rules if rule.get("start_episode") == episode_id and rule.get("delivery") == "prompt"), key=lambda rule: rule["id"])
    lines = ["指示の更新:"]
    lines.extend(f"[失効 {rule_id}] 旧指示は以後適用しません。" for rule_id in expired)
    lines.extend(f"[追加 {rule['id']}] {rule['statement']}" for rule in added)
    return "\n".join(lines) if expired or added else ""


def snapshot_rules(scenario: dict | Scenario, episode_id: str) -> dict[str, Any]:
    raw = _dict(scenario)
    expanded = expand_rules(raw)
    if episode_id not in {episode["id"] for episode in raw.get("episodes", [])}:
        raise ConfigError(f"unknown episode: {episode_id}")
    rules = []
    for rule in expanded.get(episode_id, []):
        rule = _normalized_rule(rule)
        rules.append({**rule, "statement_sha256": _sha(rule["statement"].encode("utf-8")), "presentation": "added" if rule["start_episode"] == episode_id else "active_context", "artifact": None})
    return {"schema_version": "1.0", "episode_id": episode_id, "rules": rules}


def validate_agents_rules(scenario: dict | Scenario, fixture: str | Path) -> None:
    raw = _dict(scenario)
    path = Path(fixture) / "AGENTS.md"
    agents_rules = [rule for rule in raw.get("rules", []) if rule.get("delivery") == "agents_file"]
    if not agents_rules:
        return
    if path.is_symlink():
        raise ConfigError("AGENTS.md must be a regular non-symlink file")
    try:
        content = path.read_bytes().decode("utf-8").replace("\r\n", "\n")
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"agents_file rules require readable UTF-8 AGENTS.md: {exc}") from exc
    for rule in agents_rules:
        statement = rule["statement"].replace("\r\n", "\n")
        if statement not in content:
            raise ConfigError(f"rule {rule['id']}: statement not found in AGENTS.md")


def _script_refs(check: dict, fixture: Path | None) -> list[dict[str, str]]:
    if fixture is None or check.get("type") != "command":
        return []
    refs = []
    root = fixture.resolve(strict=True)
    for arg in check.get("argv", []):
        value = arg.replace("{workspace}/", "").replace("{oracle_root}/", "")
        if value in ("{workspace}", "{oracle_root}") or value.startswith("-"):
            continue
        candidate = Path(value)
        script_like = candidate.suffix.lower() in {".py", ".sh", ".js", ".mjs", ".cjs", ".rb", ".pl", ".ps1", ".lua", ".r"} or "/" in value or "\\" in value
        if not candidate.is_absolute():
            candidate = root / candidate
        if not script_like and not candidate.is_file():
            continue
        try:
            resolved = candidate.resolve(strict=True)
            if not resolved.is_file():
                continue
            ref_path = resolved.relative_to(root).as_posix() if resolved == root or root in resolved.parents else f"external:{resolved}"
            ref = {"path": ref_path, "sha256": _sha(resolved.read_bytes())}
            if ref not in refs:
                refs.append(ref)
        except (OSError, ValueError):
            # A declared script-like argument must not silently produce an unhashed definition.
            if script_like:
                raise ConfigError(f"command oracle script is missing or unsafe: {arg}")
    return refs


def _normalized_check(check: dict) -> dict:
    if "criterion" in check:
        return {"id": check.get("id"), "criterion": check.get("criterion"), "rule_id": check.get("rule_id"), "applicability": check.get("applicability", "always"), "condition": check.get("condition")}
    return {"id": check.get("id"), "type": check.get("type"), "paths": list(check.get("paths", ())), "baseline": check.get("baseline"), "argv": list(check.get("argv", ())), "rule_id": check.get("rule_id"), "applicability": check.get("applicability", "always")}


def scenario_definitions(scenario: dict | Scenario, fixture: str | Path | None = None) -> dict[str, list[dict[str, Any]]]:
    raw = _dict(scenario)
    active = expand_rules(raw)
    fixture_path = Path(fixture) if fixture is not None else None
    rules = {rule["id"]: _normalized_rule(rule) for rule in raw.get("rules", [])}
    definitions: dict[str, list[dict[str, Any]]] = {}
    for episode in raw.get("episodes", []):
        episode_id = episode["id"]
        checks = [(item, "auto") for item in episode.get("oracles", [])]
        checks += [(item, "manual") for item in episode.get("review_points", [])]
        rows = []
        check_ids: set[str] = set()
        for check, lane in checks:
            check_id = check["id"]
            if check_id in check_ids:
                raise ConfigError(f"episode {episode_id}: duplicate check id {check_id}")
            check_ids.add(check_id)
            rule_id = check.get("rule_id")
            rule = rules.get(rule_id) if rule_id else None
            applicable = rule_id is not None and rule_id in {item["id"] for item in active.get(episode_id, [])}
            if raw.get("schema_version") == "1.1" and not applicable:
                raise ConfigError(f"check {check_id}: rule is not active in {episode_id}")
            definition = {"rule": rule, "check": _normalized_check(check), "lane": lane, "script_refs": _script_refs(check, fixture_path)}
            rows.append({"episode_id": episode_id, "check_id": check_id, "rule_id": rule_id, "lane": lane, "applicability": check.get("applicability", "always"), "definition": definition, "definition_hash": _sha(_canonical(definition)), "script_refs": definition["script_refs"]})
        definitions[episode_id] = rows
    return definitions


def definition_hash(scenario: dict | Scenario, check: dict, fixture: str | Path | None = None) -> str:
    raw = _dict(scenario)
    for episode_rows in scenario_definitions(raw, fixture).values():
        for row in episode_rows:
            if row["check_id"] == check.get("id"):
                return row["definition_hash"]
    rule_id = check.get("rule_id")
    rule = next((item for item in raw.get("rules", []) if item["id"] == rule_id), None)
    value = {"rule": rule, "check": check, "lane": "manual" if "criterion" in check else "auto", "script_refs": _script_refs(check, Path(fixture) if fixture else None)}
    return _sha(_canonical(value))


def _journal(run: Path) -> tuple[list[dict], bytes]:
    data = (run / "journal.jsonl").read_bytes()
    from .report import _read_events, audit
    events, _crash_tail, read_errors = _read_events(run / "journal.jsonl")
    if read_errors:
        raise ValueError("journal is corrupt: " + "; ".join(read_errors))
    result = audit(run, check_summary=False, check_evaluation=False)
    if not result.valid:
        raise ValueError("BASE audit failed: " + "; ".join(result.errors))
    return events, data


def _batch_files(run: Path) -> list[tuple[dict, str]]:
    result = []
    root = run / "reviews"
    if not root.exists():
        return result
    if root.is_symlink() or not root.is_dir():
        raise ValueError("reviews path is unsafe")
    seen_ids: set[str] = set()
    events, journal_bytes = _journal(run)
    run_id = events[0].get("run_id") if events else None
    scenario = json.loads((run / "control" / "scenario.json").read_text(encoding="utf-8"))
    manual_keys = {(episode_id, row.get("rule_id"), row.get("check_id")) for episode_id, rows in _saved_definitions(run, scenario).items() for row in rows if row.get("lane") == "manual"}
    for path in sorted(root.iterdir()):
        if path.suffix != ".json":
            if path.is_symlink():
                raise ValueError(f"review batch path is unsafe: {path.name}")
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"review batch path is unsafe: {path.name}")
        batch = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(batch, dict) or set(batch) != {"schema_version", "batch_id", "imported_at", "run_id", "source_journal_sha256", "reviewer", "items"} or batch.get("schema_version") != "1.0" or batch.get("run_id") != run_id or batch.get("source_journal_sha256") != _sha(journal_bytes):
            raise ValueError(f"invalid review batch metadata: {path.name}")
        try:
            batch_id = str(uuid.UUID(batch.get("batch_id", "")))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError(f"invalid review batch id: {path.name}") from exc
        if path.name != f"{batch_id}.json":
            raise ValueError(f"review batch filename does not match id: {path.name}")
        try:
            imported_at = datetime.fromisoformat(batch.get("imported_at", "").replace("Z", "+00:00"))
            if imported_at.tzinfo is None:
                raise ValueError
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError(f"invalid review import timestamp: {path.name}") from exc
        if not isinstance(batch.get("reviewer"), str) or not batch["reviewer"].strip() or not isinstance(batch.get("items"), list):
            raise ValueError(f"invalid review batch contents: {path.name}")
        item_keys: set[tuple] = set()
        for item in batch["items"]:
            if not isinstance(item, dict) or set(item) != {"episode_id", "rule_id", "check_id", "review_status", "applicability_result", "verdict", "reason_code", "rationale", "evidence", "supersedes_review_id", "review_id"}:
                raise ValueError(f"invalid saved review item: {path.name}")
            try:
                review_id = str(uuid.UUID(item.get("review_id", "")))
            except (ValueError, AttributeError, TypeError) as exc:
                raise ValueError(f"invalid review id: {path.name}") from exc
            if review_id in seen_ids:
                raise ValueError(f"duplicate review id: {review_id}")
            seen_ids.add(review_id)
            key = (item["episode_id"], item["rule_id"], item["check_id"])
            if key not in manual_keys or key in item_keys:
                raise ValueError(f"invalid or duplicate saved review key: {key}")
            item_keys.add(key)
            if item.get("review_status") != "reviewed":
                raise ValueError(f"saved review {review_id} is not reviewed")
            from .review import _valid_status
            _valid_status(item)
            key = (item.get("episode_id"), item.get("rule_id"), item.get("check_id"))
            if not all(isinstance(part, str) and part for part in key):
                raise ValueError(f"invalid review key in {path.name}")
            parent = item.get("supersedes_review_id")
            if parent is not None:
                try:
                    if str(uuid.UUID(parent)) != parent:
                        raise ValueError
                except (ValueError, TypeError, AttributeError) as exc:
                    raise ValueError(f"invalid review correction reference in {path.name}") from exc
            _valid_status(item)
            if not isinstance(item.get("evidence"), list) or not item["evidence"]:
                raise ValueError(f"saved review {review_id} has no evidence")
            for evidence in item["evidence"]:
                if not isinstance(evidence, dict) or set(evidence) != {"artifact", "sha256", "locator"} or not isinstance(evidence.get("locator"), str) or not evidence["locator"].strip():
                    raise ValueError(f"invalid saved evidence for review {review_id}")
                from .review import _safe_evidence
                _safe_evidence(run, evidence.get("artifact"), evidence.get("sha256"))
        result.append((batch, _sha(path.read_bytes())))
    return result


def _saved_definitions(run: Path, scenario: dict) -> dict[str, list[dict[str, Any]]]:
    expected = scenario_definitions(scenario)
    path = run / "control" / "check_definitions.json"
    if scenario.get("schema_version") != "1.1" and not path.exists():
        return expected
    if path.is_symlink() or not path.is_file():
        raise ValueError("control/check_definitions.json is missing or unsafe")
    stored = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(stored, dict) or set(stored) != set(expected):
        raise ValueError("saved check definitions do not match scenario episodes")
    for episode_id, expected_rows in expected.items():
        rows = stored.get(episode_id)
        if not isinstance(rows, list) or len(rows) != len(expected_rows):
            raise ValueError(f"saved check definitions do not match {episode_id}")
        by_id = {row.get("check_id"): row for row in rows if isinstance(row, dict)}
        if len(by_id) != len(rows):
            raise ValueError(f"duplicate or invalid saved check definition in {episode_id}")
        for want in expected_rows:
            row = by_id.get(want["check_id"])
            if row is None or any(row.get(key) != want.get(key) for key in ("episode_id", "check_id", "rule_id", "lane", "applicability")):
                raise ValueError(f"saved check definition mismatch: {episode_id}/{want['check_id']}")
            refs = row.get("script_refs")
            if not isinstance(refs, list) or any(not isinstance(ref, dict) or not isinstance(ref.get("path"), str) or not isinstance(ref.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", ref["sha256"]) for ref in refs):
                raise ValueError(f"invalid saved command script hashes: {episode_id}/{want['check_id']}")
            script_paths = {ref["path"] for ref in refs}
            for arg in want["definition"]["check"].get("argv", []):
                value = arg.replace("{workspace}/", "").replace("{oracle_root}/", "")
                if Path(value).suffix.lower() in {".py", ".sh", ".js", ".mjs", ".cjs", ".rb", ".pl", ".ps1", ".lua", ".r"}:
                    if Path(value).is_absolute():
                        found = any(path.startswith("external:") and Path(path.removeprefix("external:")).name == Path(value).name for path in script_paths)
                    else:
                        found = Path(value).as_posix() in script_paths
                    if not found:
                        raise ValueError(f"missing saved script hash for {episode_id}/{want['check_id']}: {arg}")
            definition = {"rule": want["definition"]["rule"], "check": want["definition"]["check"], "lane": want["lane"], "script_refs": refs}
            if row.get("definition") != definition or row.get("definition_hash") != _sha(_canonical(definition)):
                raise ValueError(f"saved check definition hash mismatch: {episode_id}/{want['check_id']}")
    return stored


def _effective(batches: list[tuple[dict, str]]) -> tuple[dict[tuple, dict], list[str]]:
    entries = {}
    errors = []
    for batch, batch_hash in batches:
        for item in batch.get("items", []):
            key = (item.get("episode_id"), item.get("rule_id"), item.get("check_id"))
            rid = item.get("review_id")
            if not isinstance(rid, str):
                errors.append("review item missing review_id")
                continue
            if rid in entries:
                errors.append(f"duplicate review id: {rid}")
                continue
            entries[rid] = (key, item, batch_hash)
    children: dict[str, list[str]] = {rid: [] for rid in entries}
    for rid, (key, item, _) in entries.items():
        parent = item.get("supersedes_review_id")
        if parent:
            if parent not in entries or entries[parent][0] != key:
                errors.append(f"review {rid}: invalid supersedes reference")
            else:
                children[parent].append(rid)
    for rid, child_ids in children.items():
        if len(child_ids) > 1:
            errors.append(f"review {rid}: correction chain branches")
    effective = {}
    for rid, (key, item, batch_hash) in entries.items():
        seen = set()
        cursor = rid
        while cursor and cursor not in seen:
            seen.add(cursor)
            parent = entries[cursor][1].get("supersedes_review_id")
            cursor = parent if parent in entries else None
        if cursor:
            errors.append("review correction cycle")
        if not children[rid]:
            if key in effective:
                errors.append(f"multiple effective reviews for {key}")
            effective[key] = {**item, "batch_sha256": batch_hash}
    return effective, errors


def effective_reviews(batches: list[dict]) -> dict[tuple, dict]:
    paired = [(batch, _sha(_canonical(batch))) for batch in batches]
    effective, errors = _effective(paired)
    if errors:
        raise ValueError("; ".join(errors))
    return effective


def build_evaluation(run: str | Path) -> dict[str, Any]:
    run = Path(run)
    events, journal_bytes = _journal(run)
    scenario = json.loads((run / "control" / "scenario.json").read_text(encoding="utf-8"))
    definitions = _saved_definitions(run, scenario)
    starts = {event.get("episode_id"): event for event in events if event.get("type") == "episode_started"}
    finishes = {event.get("episode_id"): event.get("payload", {}) for event in events if event.get("type") == "episode_finished"}
    episode_order = {episode["id"]: index for index, episode in enumerate(scenario.get("episodes", []))}
    presented_at = {}
    for event in events:
        if event.get("type") == "rule_presented":
            rule_id = event.get("payload", {}).get("rule_id")
            if isinstance(rule_id, str) and event.get("episode_id") in episode_order:
                presented_at[rule_id] = min(presented_at.get(rule_id, len(episode_order)), episode_order[event["episode_id"]])
    batches = _batch_files(run)
    reviews, review_errors = _effective(batches)
    per_check = []
    for episode in scenario.get("episodes", []):
        episode_id = episode["id"]
        rows = definitions.get(episode_id, []) if isinstance(definitions, dict) else []
        result_by_id = {item.get("oracle_id", item.get("id")): item for item in finishes.get(episode_id, {}).get("oracles", []) if isinstance(item, dict)}
        for row in rows:
            key = (episode_id, row["rule_id"], row["check_id"])
            review = reviews.get(key)
            result = result_by_id.get(row["check_id"], {}) if row["lane"] == "auto" else {}
            auto_verdict = result.get("verdict", "not_evaluated")
            applicability = row.get("applicability", "always")
            applicability_result = "applicable" if applicability == "always" else "unknown"
            if review:
                applicability_result = review["applicability_result"]
            presented_flag = row["rule_id"] is None or presented_at.get(row["rule_id"], len(episode_order)) <= episode_order[episode_id]
            if row["rule_id"] and row["lane"] == "auto" and not presented_flag and episode_id in starts and episode_id in finishes:
                auto_verdict = "unknown"
            auto_evidence = finishes.get(episode_id, {}).get("artifacts", []) if row["lane"] == "auto" else []
            started_payload = starts.get(episode_id, {}).get("payload", {})
            finished_payload = finishes.get(episode_id, {})
            contaminated = bool(started_payload.get("condition_contaminated") or finished_payload.get("condition_contaminated"))
            per_check.append({"episode_id": episode_id, "rule_id": row["rule_id"], "check_id": row["check_id"], "definition_hash": row["definition_hash"], "applicability_result": applicability_result, "auto_verdict": auto_verdict, "manual_verdict": review.get("verdict") if review else ("not_evaluated" if row["lane"] == "manual" else None), "review_status": "reviewed" if review else ("pending" if row["lane"] == "manual" else "not_reviewed"), "effective_review_id": review.get("review_id") if review else None, "evidence_refs": review.get("evidence", []) if review else auto_evidence, "lane": row["lane"], "observed": episode_id in finishes, "condition_contaminated": contaminated, "definition": row.get("definition")})
    assigned = {(row["episode_id"], row["rule_id"]) for row in per_check}
    rules_by_id = {rule["id"]: rule for rule in scenario.get("rules", [])}
    active = expand_rules(scenario)
    unassigned = [{"episode_id": episode_id, "rule_id": rule["id"]} for episode_id, items in active.items() for rule in items if (episode_id, rule["id"]) not in assigned]
    coverage = {"active_rule_count": len(rules_by_id), "active_rule_episode_count": sum(len(items) for items in active.values()), "assigned_rule_episode_count": len(assigned), "unassigned_rule_episode_count": len(unassigned), "unassigned": unassigned, "pending": sum(row["review_status"] == "pending" for row in per_check), "unknown": sum(row["applicability_result"] == "unknown" or row["auto_verdict"] == "unknown" or row["manual_verdict"] == "unknown" for row in per_check), "not_applicable": sum(row["applicability_result"] == "not_applicable" for row in per_check)}
    by_episode_rule: dict[tuple[str, str], list[dict]] = {}
    for row in per_check:
        by_episode_rule.setdefault((row["episode_id"], row["rule_id"]), []).append(row)
    conflicts = []
    for (episode_id, rule_id), rows in by_episode_rule.items():
        automatic = [row for row in rows if row["lane"] == "auto" and row["auto_verdict"] in ("pass", "fail")]
        manual = [row for row in rows if row["lane"] == "manual" and row["manual_verdict"] in ("pass", "fail")]
        if automatic and manual and any(a["auto_verdict"] != m["manual_verdict"] for a in automatic for m in manual):
            conflict = {"episode_id": episode_id, "rule_id": rule_id, "automatic": [{"check_id": row["check_id"], "verdict": row["auto_verdict"]} for row in automatic], "manual": [{"check_id": row["check_id"], "verdict": row["manual_verdict"]} for row in manual]}
            conflicts.append(conflict)
    transitions = []
    for lane in ("auto", "manual"):
        groups: dict[tuple, list[dict]] = {}
        for row in per_check:
            if row["lane"] == lane:
                groups.setdefault((row["rule_id"], row["check_id"], row["definition_hash"]), []).append(row)
        for (rule_id, check_id, digest), series in groups.items():
            prior = None
            gaps = []
            row_by_episode = {row["episode_id"]: row for row in series}
            first = min(episode_order[row["episode_id"]] for row in series)
            last = max(episode_order[row["episode_id"]] for row in series)
            for episode_id in [episode["id"] for episode in scenario.get("episodes", [])[first:last + 1]]:
                row = row_by_episode.get(episode_id)
                if row is None:
                    if rule_id and rule_id in {item["id"] for item in active.get(episode_id, [])}:
                        gaps.append(episode_id)
                    continue
                value = row["auto_verdict"] if lane == "auto" else row["manual_verdict"]
                if not row["observed"]:
                    if prior:
                        gaps.append(row["episode_id"])
                    continue
                if value in ("unknown", "not_evaluated") or row["applicability_result"] == "unknown":
                    prior = None
                    gaps = []
                    continue
                if prior and prior[1] == "pass" and value == "fail":
                    transitions.append({"lane": lane, "rule_id": rule_id, "check_id": check_id, "definition_hash": digest, "from_episode": prior[0], "to_episode": row["episode_id"], "gap_episode_ids": gaps})
                prior = (row["episode_id"], value)
                gaps = []
    active_reviews = [{"review_id": row["effective_review_id"], "batch_sha256": next((review["batch_sha256"] for review in reviews.values() if review.get("review_id") == row["effective_review_id"]), None)} for row in per_check if row["effective_review_id"]]
    review_set_hash = _sha(_canonical(sorted(active_reviews, key=lambda x: x["review_id"])))
    limitations = ["Meaning and conditional applicability require human review.", "Only persisted episode boundary observations are included.", "Instruction presentation does not prove understanding or retention."]
    if any(row["condition_contaminated"] for row in per_check):
        limitations.append("AGENTS.md changed during the Run; affected instruction conditions may be contaminated.")
    return {"schema_version": "1.0", "run_id": events[0].get("run_id") if events else None, "source_journal_sha256": _sha(journal_bytes), "review_set_hash": review_set_hash, "per_check": per_check, "coverage": coverage, "transitions": transitions, "conflicts": conflicts, "limitations": limitations, "audit_errors": review_errors}


def write_evaluation(run: str | Path) -> dict[str, Any]:
    from .journal import atomic_write
    run = Path(run)
    errors = audit_evaluation(run, check_cache=False)
    raw_scenario = json.loads((run / "control" / "scenario.json").read_text(encoding="utf-8"))
    if raw_scenario.get("schema_version") == "1.1" and errors:
        raise ValueError("EVAL audit failed: " + "; ".join(errors))
    data = build_evaluation(run)
    atomic_write(run / "evaluation.json", (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8"))
    lines = [f"# Evaluation: {data['run_id'] or run.name}", "", f"- Checks: {len(data['per_check'])}", f"- Pending: {data['coverage']['pending']}", f"- Unknown: {data['coverage']['unknown']}", f"- Unassigned active rules: {data['coverage']['unassigned_rule_episode_count']}", "", "## Checks", ""]
    lines.extend(f"- {x['episode_id']} / {x['rule_id']} / {x['check_id']}: auto={x['auto_verdict']}, manual={x['manual_verdict'] or '—'}, applicability={x['applicability_result']}" for x in data["per_check"])
    lines += ["", "## Transitions", ""]
    lines.extend(f"- {x['lane']} {x['rule_id']}/{x['check_id']}: pass at {x['from_episode']} → fail at {x['to_episode']}" for x in data["transitions"])
    lines += ["", "## Limitations", "", *[f"- {x}" for x in data["limitations"]], ""]
    atomic_write(run / "evaluation_report.md", "\n".join(lines).encode("utf-8"))
    return data


def audit_evaluation(run: str | Path, check_cache: bool = True) -> list[str]:
    run = Path(run)
    errors = []
    try:
        from .report import _artifact_refs, _check_artifact
        events, journal_bytes = _journal(run)
        scenario = json.loads((run / "control" / "scenario.json").read_text(encoding="utf-8"))
        definitions = _saved_definitions(run, scenario)
        run_start = next((event for event in events if event.get("type") == "run_started"), {})
        start_payload = run_start.get("payload", {})
        initial_instruction_hash = start_payload.get("instruction_hash")
        run_agents_ref = next((ref for ref in _artifact_refs(start_payload) if ref.get("path") == "control/AGENTS.md"), None)
        scenario_hash = start_payload.get("scenario_hash")
        if scenario_hash and scenario_hash != _sha((run / "control" / "scenario.json").read_bytes()):
            errors.append("control/scenario.json does not match run_started hash")
        definition_refs = [ref for ref in _artifact_refs(start_payload) if ref.get("path") == "control/check_definitions.json"]
        if scenario.get("schema_version") == "1.1" and len(definition_refs) != 1:
            errors.append("run_started must reference control/check_definitions.json exactly once")
        for ref in definition_refs:
            if problem := _check_artifact(run, ref):
                errors.append(problem)
        active_snapshots = {}
        prompt_refs = {}
        started_events = {}
        stdin_events = {}
        presentation_events: dict[tuple[str, str], dict] = {}
        event_positions = {id(event): index for index, event in enumerate(events)}
        episode_order = {episode["id"]: index for index, episode in enumerate(scenario.get("episodes", []))}
        for seq, event in enumerate(events):
            payload = event.get("payload", {})
            if event.get("type") == "episode_started":
                started_events[event.get("episode_id")] = event
                ref = payload.get("active_rules")
                if isinstance(ref, dict):
                    active_snapshots[event.get("episode_id")] = (ref, payload)
                prompt_refs[event.get("episode_id")] = payload.get("prompt_artifact")
            if event.get("type") == "stdin_sent":
                if event.get("episode_id") in stdin_events:
                    errors.append(f"duplicate stdin_sent event for {event.get('episode_id')}")
                stdin_events[event.get("episode_id")] = event
                episode_start_payload = started_events.get(event.get("episode_id"), {}).get("payload", {})
                prompt_ref = episode_start_payload.get("prompt_artifact")
                if scenario.get("schema_version") == "1.1" and (not isinstance(prompt_ref, dict) or payload.get("bytes") != prompt_ref.get("size")):
                    errors.append(f"stdin_sent byte count does not match prompt artifact for {event.get('episode_id')}")
            if event.get("type") in ("rule_presented", "rule_expired"):
                rule_id = payload.get("rule_id")
                episode_id = event.get("episode_id")
                presentation_events[(episode_id, rule_id)] = event
                if not isinstance(rule_id, str):
                    errors.append(f"{event['type']} missing rule_id")
                ref = payload.get("artifact")
                if not isinstance(ref, dict):
                    errors.append(f"{event['type']} missing artifact reference")
                else:
                    from .report import _check_artifact
                    if problem := _check_artifact(run, ref):
                        errors.append(f"{event['type']}: {problem}")
                sent = next((prior for prior in reversed(events[:seq]) if prior.get("episode_id") == episode_id and prior.get("type") == "stdin_sent"), None)
                if sent is None:
                    errors.append(f"{event['type']} occurred before stdin_sent in {episode_id}")
                rule = next((rule for rule in scenario.get("rules", []) if rule.get("id") == rule_id), None)
                if rule is None or episode_id not in episode_order:
                    errors.append(f"{event['type']} references an unknown rule or episode")
                elif event["type"] == "rule_expired":
                    if rule.get("delivery") != "prompt" or rule.get("end_episode_exclusive") != episode_id or ref != prompt_refs.get(episode_id):
                        errors.append(f"invalid rule expiration provenance for {episode_id}/{rule_id}")
                else:
                    active_here = rule["start_episode"] in episode_order and episode_order[rule["start_episode"]] <= episode_order[episode_id] and (rule.get("end_episode_exclusive") is None or episode_order[episode_id] < episode_order[rule["end_episode_exclusive"]])
                    if not active_here or payload.get("delivery") != rule.get("delivery"):
                        errors.append(f"invalid presentation assignment for {episode_id}/{rule_id}")
                    elif rule["delivery"] == "prompt":
                        fresh = start_payload.get("execution_policy") or {}
                        allowed_actions = {"added"} if episode_id == rule.get("start_episode") else {"active_context"}
                        if fresh.get("session_mode") == "fresh_each_episode" and fresh.get("fresh_context") == "active_rules":
                            allowed_actions = {"active_context"}
                        if payload.get("action") not in allowed_actions or ref != prompt_refs.get(episode_id):
                            errors.append(f"invalid prompt presentation for {episode_id}/{rule_id}")
                    elif payload.get("read_status") != "unknown" or ref != run_agents_ref:
                        errors.append(f"invalid agents_file presentation for {episode_id}/{rule_id}")
            if event.get("type") == "presentation_uncertain":
                ref = payload.get("artifact")
                if not isinstance(ref, dict) or (problem := _check_artifact(run, ref)):
                    errors.append(f"presentation_uncertain has invalid prompt reference: {problem if isinstance(ref, dict) else 'missing reference'}")
                elif ref != prompt_refs.get(event.get("episode_id")) or event.get("episode_id") in stdin_events:
                    errors.append(f"invalid presentation_uncertain timing for {event.get('episode_id')}")
                uncertain_episode = event.get("episode_id")
                rule_ids = payload.get("rule_ids")
                if not isinstance(rule_ids, list) or len(set(rule_ids)) != len(rule_ids):
                    errors.append(f"invalid presentation_uncertain rule list for {uncertain_episode}")
                else:
                    policy = start_payload.get("execution_policy") or {}
                    fresh = policy.get("session_mode") == "fresh_each_episode" and policy.get("fresh_context") == "active_rules"
                    expected_ids = {rule["id"] for rule in (expand_rules(scenario).get(uncertain_episode, []) if fresh else scenario.get("rules", [])) if rule.get("delivery") == "prompt" and (fresh or rule.get("start_episode") == uncertain_episode)}
                    if set(rule_ids) != expected_ids:
                        errors.append(f"presentation_uncertain rule list mismatch for {uncertain_episode}")
                    for rule_id in rule_ids:
                        rule = next((item for item in scenario.get("rules", []) if item.get("id") == rule_id), None)
                        if rule is None or rule.get("delivery") != "prompt" or uncertain_episode not in episode_order:
                            errors.append(f"presentation_uncertain references invalid rule {rule_id}")
            if event.get("type") == "episode_started" and scenario.get("schema_version") == "1.1":
                expected_contamination = payload.get("instruction_hash") != initial_instruction_hash
                if payload.get("condition_contaminated") is not expected_contamination:
                    errors.append(f"condition contamination marker mismatch for {event.get('episode_id')}")
            if event.get("type") == "episode_finished" and scenario.get("schema_version") == "1.1":
                expected_contamination = payload.get("instruction_hash") != initial_instruction_hash
                if payload.get("condition_contaminated") is not expected_contamination:
                    errors.append(f"condition contamination marker mismatch for finished {event.get('episode_id')}")
        expected = expand_rules(scenario)
        rule_by_id = {rule["id"]: rule for rule in scenario.get("rules", [])}
        finished_ids = {event.get("episode_id") for event in events if event.get("type") == "episode_finished"}
        for episode_id, (ref, episode_payload) in active_snapshots.items():
            try:
                from .report import _check_artifact
                if problem := _check_artifact(run, ref):
                    errors.append(f"active rules snapshot {episode_id}: {problem}")
                    continue
                from .journal import _safe_artifact_path
                path = _safe_artifact_path(run, ref["path"])
                actual = json.loads(path.read_text(encoding="utf-8"))
                want = snapshot_rules(scenario, episode_id)
                if actual.get("schema_version") != want["schema_version"] or actual.get("episode_id") != episode_id or not isinstance(actual.get("rules"), list):
                    errors.append(f"active rules snapshot mismatch for {episode_id}")
                    continue
                actual_by_id = {item.get("id"): item for item in actual["rules"] if isinstance(item, dict)}
                if len(actual_by_id) != len(actual["rules"]) or set(actual_by_id) != {item["id"] for item in want["rules"]}:
                    errors.append(f"active rules snapshot IDs mismatch for {episode_id}")
                    continue
                for base in want["rules"]:
                    item = actual_by_id[base["id"]]
                    if any(item.get(key) != base.get(key) for key in ("id", "family_id", "kind", "statement", "statement_sha256", "start_episode", "end_episode_exclusive", "delivery", "supersedes")):
                        errors.append(f"active rules snapshot definition mismatch for {episode_id}/{base['id']}")
                        continue
                    presentation, provenance = item.get("presentation"), item.get("artifact")
                    if not isinstance(item.get("history_available"), bool):
                        errors.append(f"active rules snapshot history flag invalid for {episode_id}/{base['id']}")
                    if base["delivery"] == "agents_file":
                        if presentation != "file_available_read_unknown" or not run_agents_ref or provenance != run_agents_ref:
                            errors.append(f"agents_file provenance mismatch for {episode_id}/{base['id']}")
                    elif presentation == "planned_prompt":
                        fresh = start_payload.get("execution_policy") or {}
                        active_context = fresh.get("session_mode") == "fresh_each_episode" and fresh.get("fresh_context") == "active_rules"
                        if (base["start_episode"] != episode_id and not active_context) or provenance != episode_payload.get("prompt_artifact"):
                            errors.append(f"planned prompt provenance mismatch for {episode_id}/{base['id']}")
                    elif presentation == "prior_prompt":
                        start_pos = event_positions.get(id(started_events.get(episode_id)), len(events))
                        prior = next((event for event in events[:start_pos] if event.get("type") == "rule_presented" and event.get("payload", {}).get("rule_id") == base["id"]), None)
                        if prior is None or provenance != prior.get("payload", {}).get("artifact"):
                            errors.append(f"prior prompt provenance mismatch for {episode_id}/{base['id']}")
                    elif presentation == "not_presented":
                        earlier = any(event.get("type") == "rule_presented" and event.get("payload", {}).get("rule_id") == base["id"] and episode_order.get(event.get("episode_id"), len(episode_order)) <= episode_order.get(episode_id, -1) for event in events)
                        if provenance is not None or earlier:
                            errors.append(f"unpresented rule has artifact for {episode_id}/{base['id']}")
                    else:
                        errors.append(f"invalid presentation state for {episode_id}/{base['id']}")
                    if provenance is not None:
                        if not isinstance(provenance, dict):
                            errors.append(f"invalid provenance reference for {episode_id}/{base['id']}")
                        elif problem := _check_artifact(run, provenance):
                            errors.append(f"provenance reference: {problem}")
                    presented = presentation_events.get((episode_id, base["id"]))
                    if presented and presented.get("type") == "rule_presented":
                        event_payload = presented.get("payload", {})
                        expected_delivery = base["delivery"]
                        if event_payload.get("delivery") != expected_delivery:
                            errors.append(f"presentation delivery mismatch for {episode_id}/{base['id']}")
                        if expected_delivery == "prompt" and event_payload.get("artifact") != prompt_refs.get(episode_id):
                            errors.append(f"presentation prompt reference mismatch for {episode_id}/{base['id']}")
                    if base["delivery"] == "prompt" and presentation == "planned_prompt":
                        sent = episode_id in stdin_events
                        uncertain = any(event.get("type") == "presentation_uncertain" and event.get("episode_id") == episode_id and base["id"] in event.get("payload", {}).get("rule_ids", []) for event in events)
                        if sent and (episode_id, base["id"]) not in presentation_events:
                            errors.append(f"missing rule_presented event for {episode_id}/{base['id']}")
                        if not sent and episode_id in finished_ids and not uncertain:
                            errors.append(f"missing presentation uncertainty for {episode_id}/{base['id']}")
                for rule in rule_by_id.values():
                    if rule.get("end_episode_exclusive") == episode_id and rule.get("delivery") == "prompt" and (episode_id, rule["id"]) not in presentation_events:
                        errors.append(f"missing rule_expired event for {episode_id}/{rule['id']}")
            except (OSError, ValueError, KeyError):
                errors.append(f"active rules snapshot unreadable for {episode_id}")
        for event in events:
            if event.get("type") == "episode_started" and event.get("episode_id") not in active_snapshots and scenario.get("schema_version") == "1.1":
                errors.append(f"missing active rules snapshot for {event.get('episode_id')}")
        batches = _batch_files(run)
        _, review_errors = _effective(batches)
        errors.extend(review_errors)
        stored = run / "evaluation.json"
        if check_cache and stored.is_file():
            recorded = json.loads(stored.read_text(encoding="utf-8"))
            current = build_evaluation(run)
            if recorded != current:
                errors.append("evaluation.json does not match recomputed evaluation")
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        errors.append(f"evaluation audit failed: {exc}")
    return errors
