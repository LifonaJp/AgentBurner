from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any

from .evaluation import _canonical, _effective, _journal, audit_evaluation, build_evaluation, scenario_definitions
from .journal import atomic_write


REASONS = {"confirmed_violation", "satisfied", "insufficient_evidence", "ambiguous_instruction", "conflicting_instruction", "condition_not_met"}
HEX = re.compile(r"^[0-9a-f]{64}$")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_evidence(run: Path, relative: Any, digest: Any) -> dict[str, str]:
    if not isinstance(relative, str) or not relative or "\\" in relative or "\x00" in relative:
        raise ValueError("unsafe evidence artifact path")
    parts = relative.split("/")
    if any(part in ("", ".", "..") for part in parts) or Path(relative).is_absolute() or PureWindowsPath(relative).is_absolute() or PureWindowsPath(relative).drive:
        raise ValueError("unsafe evidence artifact path")
    path = run
    for part in parts:
        path = path / part
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            raise ValueError(f"evidence artifact missing: {relative}") from exc
        if stat.S_ISLNK(mode):
            raise ValueError("symlink evidence artifact path")
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("evidence artifact must be a regular file")
    if not isinstance(digest, str) or not HEX.fullmatch(digest) or _sha(path.read_bytes()) != digest:
        raise ValueError(f"evidence artifact hash mismatch: {relative}")
    return {"artifact": "/".join(parts), "sha256": digest}


def _run_id(events: list[dict]) -> str | None:
    return events[0].get("run_id") if events else None


def _definitions(run: Path) -> tuple[dict, list[dict]]:
    scenario = json.loads((run / "control" / "scenario.json").read_text(encoding="utf-8"))
    from .evaluation import _saved_definitions
    definitions = _saved_definitions(run, scenario)
    return scenario, definitions


def export_review(run: str | Path, out: str | Path) -> dict:
    run = Path(run)
    events, journal_bytes = _journal(run)
    scenario, definitions = _definitions(run)
    items = []
    for episode in scenario.get("episodes", []):
        for check in definitions.get(episode["id"], []):
            if check.get("lane") != "manual":
                continue
            item = {"episode_id": episode["id"], "rule_id": check.get("rule_id"), "check_id": check["check_id"], "review_status": "pending", "applicability_result": "unknown" if check.get("applicability") == "conditional" else "applicable", "verdict": "not_evaluated", "reason_code": None, "rationale": "", "evidence": [], "supersedes_review_id": None}
            items.append(item)
    value = {"schema_version": "1.0", "run_id": _run_id(events), "source_journal_sha256": _sha(journal_bytes), "reviewer": "user", "items": items}
    Path(out).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return value


def _valid_status(item: dict) -> None:
    result, verdict, reason = item.get("applicability_result"), item.get("verdict"), item.get("reason_code")
    if item.get("review_status") != "reviewed":
        raise ValueError("only reviewed items may be imported")
    if not isinstance(item.get("rationale"), str) or not item["rationale"].strip():
        raise ValueError("reviewed item rationale is required")
    if not isinstance(item.get("evidence"), list) or not item["evidence"]:
        raise ValueError("reviewed item requires evidence")
    if result == "applicable":
        if verdict not in ("pass", "fail", "unknown"):
            raise ValueError("invalid verdict for applicable item")
        if verdict == "pass" and reason != "satisfied":
            raise ValueError("pass requires reason_code satisfied")
        if verdict == "fail" and reason != "confirmed_violation":
            raise ValueError("fail requires reason_code confirmed_violation")
        if verdict == "unknown" and reason not in {"insufficient_evidence", "ambiguous_instruction", "conflicting_instruction"}:
            raise ValueError("unknown requires an uncertainty reason_code")
    elif result == "not_applicable":
        if verdict != "not_evaluated" or reason != "condition_not_met":
            raise ValueError("not_applicable requires not_evaluated and condition_not_met")
    elif result == "unknown":
        if verdict != "unknown" or reason not in {"insufficient_evidence", "ambiguous_instruction", "conflicting_instruction"}:
            raise ValueError("unknown applicability requires unknown verdict")
    else:
        raise ValueError("invalid applicability_result")


def validate_review_batch(run: Path, batch: dict) -> list[dict]:
    if not isinstance(batch, dict) or set(batch) != {"schema_version", "run_id", "source_journal_sha256", "reviewer", "items"} or batch.get("schema_version") != "1.0":
        raise ValueError("review schema_version must be 1.0")
    events, journal_bytes = _journal(run)
    if batch.get("run_id") != _run_id(events):
        raise ValueError("review run_id does not match")
    if batch.get("source_journal_sha256") != _sha(journal_bytes):
        raise ValueError("source journal hash changed; export a fresh review")
    if not isinstance(batch.get("reviewer"), str) or not batch["reviewer"].strip():
        raise ValueError("reviewer is required")
    scenario, definitions = _definitions(run)
    valid_keys = {(episode_id, row.get("rule_id"), row.get("check_id")): row for episode_id, rows in definitions.items() for row in rows if row.get("lane") == "manual"}
    existing, errors = _effective(_review_files(run))
    if errors:
        raise ValueError("existing review audit failed: " + "; ".join(errors))
    if not isinstance(batch.get("items"), list):
        raise ValueError("review items must be a list")
    seen = set()
    accepted = []
    for source in batch["items"]:
        if not isinstance(source, dict):
            raise ValueError("review item must be an object")
        if set(source) != {"episode_id", "rule_id", "check_id", "review_status", "applicability_result", "verdict", "reason_code", "rationale", "evidence", "supersedes_review_id"}:
            raise ValueError("review item has missing or unexpected fields")
        key = (source.get("episode_id"), source.get("rule_id"), source.get("check_id"))
        if key not in valid_keys:
            raise ValueError(f"unknown review check: {key}")
        if key in seen:
            raise ValueError(f"duplicate review key: {key}")
        seen.add(key)
        if source.get("review_status") == "pending":
            check = valid_keys[key]
            expected_applicability = "unknown" if check.get("applicability") == "conditional" else "applicable"
            if source.get("applicability_result") != expected_applicability or source.get("verdict") != "not_evaluated" or source.get("reason_code") is not None or source.get("evidence") != [] or source.get("supersedes_review_id") is not None or not isinstance(source.get("rationale"), str):
                raise ValueError("invalid pending review state")
            continue
        _valid_status(source)
        check = valid_keys[key]
        if check.get("applicability") == "always" and source["applicability_result"] != "applicable":
            raise ValueError("always-applicable check must be applicable")
        if check.get("applicability") == "conditional" and source["applicability_result"] == "not_applicable" and source["verdict"] != "not_evaluated":
            raise ValueError("conditional not_applicable must not be evaluated")
        evidence = []
        for item in source["evidence"]:
            if not isinstance(item, dict) or set(item) != {"artifact", "sha256", "locator"} or not isinstance(item.get("locator"), str) or not item["locator"].strip():
                raise ValueError("evidence requires a human-readable locator")
            evidence.append({**_safe_evidence(run, item.get("artifact"), item.get("sha256")), "locator": item["locator"]})
        previous = existing.get(key)
        supersedes = source.get("supersedes_review_id")
        if previous:
            if supersedes != previous["review_id"]:
                raise ValueError(f"review correction must supersede current review {previous['review_id']}")
        elif supersedes is not None:
            raise ValueError("supersedes_review_id supplied without an existing review")
        accepted.append({"episode_id": key[0], "rule_id": key[1], "check_id": key[2], "review_status": "reviewed", "applicability_result": source["applicability_result"], "verdict": source["verdict"], "reason_code": source["reason_code"], "rationale": source["rationale"], "evidence": evidence, "supersedes_review_id": supersedes})
    return accepted


def _review_files(run: Path) -> list[tuple[dict, str]]:
    from .evaluation import _batch_files
    return _batch_files(run)


def _writer_lock(run: Path):
    path = run / "control" / "review.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            stream.seek(0)
            if stream.read(1) == b"":
                stream.seek(0); stream.write(b"0"); stream.flush()
            stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        return stream
    except Exception:
        stream.close()
        raise


def _unlock(stream) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        stream.close()


def _run_is_locked(run: Path) -> bool:
    path = run / "control" / "run.lock"
    if not path.is_file():
        return False
    with path.open("a+b") as stream:
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            return False
        except (OSError, BlockingIOError):
            return True


def import_review(run: str | Path, file: str | Path) -> dict:
    run = Path(run)
    incoming = json.loads(Path(file).read_text(encoding="utf-8"))
    lock = _writer_lock(run)
    try:
        if _run_is_locked(run):
            raise ValueError("cannot import review while Run is active")
        accepted = validate_review_batch(run, incoming)
        if not accepted:
            return {"items": 0, "batch_id": None}
        # Validate frozen definitions and presentation provenance before any
        # review batch is persisted. Existing evaluation output may be stale
        # after this import, so deliberately skip its cache comparison.
        eval_errors = audit_evaluation(run, check_cache=False)
        raw_scenario = json.loads((run / "control" / "scenario.json").read_text(encoding="utf-8"))
        if raw_scenario.get("schema_version") == "1.1" and eval_errors:
            raise ValueError("EVAL audit failed: " + "; ".join(eval_errors))
        events, journal_bytes = _journal(run)
        batch_id = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        value = {"schema_version": "1.0", "batch_id": batch_id, "imported_at": timestamp, "run_id": _run_id(events), "source_journal_sha256": _sha(journal_bytes), "reviewer": incoming["reviewer"], "items": [{**item, "review_id": str(uuid.uuid4())} for item in accepted]}
        directory = run / "reviews"
        directory.mkdir(exist_ok=True)
        data = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        atomic_write(directory / f"{batch_id}.json", data)
        return {"items": len(accepted), "batch_id": batch_id}
    finally:
        _unlock(lock)


def review_report(run: str | Path) -> dict:
    from .evaluation import write_evaluation
    data = write_evaluation(Path(run))
    reviews, errors = _effective(_review_files(Path(run)))
    return {"reviews": reviews, "audit_errors": errors, "evaluation": data}
