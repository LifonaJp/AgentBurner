from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .models import Episode, FaultSpec, Limits, OracleSpec, ReviewPoint, Rule, RunConfig, Scenario


class ConfigError(ValueError):
    pass


_FEEDBACK = ("notify_and_continue", "none", "stop_on_fail")
_ORACLE_TYPES = ("unchanged", "allowed_changes", "required_paths", "command")
_LIMIT_KEYS = (
    "max_episodes",
    "max_runtime_sec",
    "episode_timeout_sec",
    "oracle_timeout_sec",
    "max_consecutive_failures",
    "max_output_bytes_per_episode",
    "max_run_disk_bytes",
    "max_workspace_files",
)


def _schema() -> dict[str, Any]:
    positive = {"type": "integer", "minimum": 1}
    path_array = {"type": "array", "items": {"type": "string", "minLength": 1}}
    oracle = {
        "type": "object",
        "required": ["id", "type"],
        "properties": {
            "id": {"type": "string", "minLength": 1},
            "type": {"type": "string", "enum": list(_ORACLE_TYPES)},
            "paths": path_array,
            "baseline": {"type": "string", "enum": ["run_start", "episode_start"]},
            "argv": path_array,
            "rule_id": {"type": "string", "minLength": 1},
            "applicability": {"type": "string", "enum": ["always", "conditional"]},
        },
        "additionalProperties": False,
    }
    fault = {
        "type": "object",
        "required": ["type", "path"],
        "properties": {
            "type": {"type": "string", "enum": ["delete_file_before_episode"]},
            "path": {"type": "string", "minLength": 1},
        },
        "additionalProperties": False,
    }
    episode = {
        "type": "object",
        "required": ["id", "purpose", "prompt", "oracles", "review_points"],
        "properties": {
            "id": {"type": "string", "minLength": 1},
            "purpose": {"type": "string", "minLength": 1},
            "prompt": {"type": "string", "minLength": 1},
            "feedback": {"type": "string", "enum": ["inherit", *_FEEDBACK]},
            "fault": {"anyOf": [fault, {"type": "null"}]},
            "oracles": {"type": "array", "items": oracle},
            "review_points": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["id", "criterion"],
                    "properties": {
                        "id": {"type": "string", "minLength": 1},
                        "criterion": {"type": "string", "minLength": 1},
                        "rule_id": {"type": "string", "minLength": 1},
                        "applicability": {"type": "string", "enum": ["always", "conditional"]},
                        "condition": {"type": "string", "minLength": 1},
                    },
                    "additionalProperties": False,
                },
            },
        },
        "additionalProperties": False,
    }
    limits = {"type": "object", "required": list(_LIMIT_KEYS), "properties": {k: positive for k in _LIMIT_KEYS}, "additionalProperties": False}
    return {
        "config": {
            "type": "object",
            "required": ["schema_version", "name", "fixture", "scenario", "output_root", "codex", "feedback", "limits"],
            "properties": {
                "schema_version": {"type": "string", "enum": ["1.0"]},
                "name": {"type": "string", "minLength": 1},
                "fixture": {"type": "string", "minLength": 1},
                "scenario": {"type": "string", "minLength": 1},
                "output_root": {"type": "string", "minLength": 1},
                "codex": {
                    "type": "object",
                    "required": ["executable", "model"],
                    "properties": {
                        "executable": {"type": "string", "minLength": 1},
                        "model": {"anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}]},
                    },
                    "additionalProperties": False,
                },
                "feedback": {"type": "string", "enum": list(_FEEDBACK)},
                "limits": limits,
                "exclusions": path_array,
            },
            "additionalProperties": False,
        },
        "scenario": {
            "type": "object",
            "required": ["schema_version", "id", "description", "episodes"],
            "properties": {
                "schema_version": {"type": "string", "enum": ["1.0", "1.1"]},
                "id": {"type": "string", "minLength": 1},
                "description": {"type": "string", "minLength": 1},
                "episodes": {"type": "array", "minItems": 1, "items": episode},
                "rules": {"type": "array", "items": {"type": "object", "required": ["id", "family_id", "kind", "statement", "start_episode", "delivery"], "properties": {
                    "id": {"type": "string", "minLength": 1}, "family_id": {"type": "string", "minLength": 1},
                    "kind": {"type": "string", "enum": ["constraint", "decision", "purpose"]}, "statement": {"type": "string", "minLength": 1},
                    "start_episode": {"type": "string", "minLength": 1}, "end_episode_exclusive": {"anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}]},
                    "delivery": {"type": "string", "enum": ["prompt", "agents_file"]}, "supersedes": {"anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}]}
                }, "additionalProperties": False}},
            },
            "additionalProperties": False,
        },
    }


def _fallback_schema_check(value: Any, schema: dict[str, Any], where: str = "$") -> None:
    if schema.get("anyOf"):
        for option in schema["anyOf"]:
            try:
                _fallback_schema_check(value, option, where)
                return
            except ConfigError:
                pass
        raise ConfigError(f"{where}: value has an invalid type")
    kind = schema.get("type")
    checks = {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "null": lambda v: v is None,
    }
    if kind in checks and not checks[kind](value):
        raise ConfigError(f"{where}: expected {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ConfigError(f"{where}: unsupported value {value!r}")
    if isinstance(value, str) and len(value) < schema.get("minLength", 0):
        raise ConfigError(f"{where}: string is empty")
    if isinstance(value, (int, float)) and value < schema.get("minimum", float("-inf")):
        raise ConfigError(f"{where}: value must be at least {schema['minimum']}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise ConfigError(f"{where}: list must not be empty")
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                _fallback_schema_check(item, item_schema, f"{where}[{index}]")
    if isinstance(value, dict):
        missing = set(schema.get("required", ())) - value.keys()
        if missing:
            raise ConfigError(f"{where}: missing keys: {', '.join(sorted(missing))}")
        properties = schema.get("properties", {})
        extras = value.keys() - properties.keys()
        if schema.get("additionalProperties") is False and extras:
            raise ConfigError(f"{where}: unsupported keys: {', '.join(sorted(extras))}")
        for key, child in value.items():
            if key in properties:
                _fallback_schema_check(child, properties[key], f"{where}.{key}")


def _validate_schema(value: Any, schema: dict[str, Any], label: str) -> None:
    try:
        import jsonschema
    except ImportError:
        _fallback_schema_check(value, schema, label)
        return
    errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(value), key=lambda error: list(map(str, error.path)))
    if errors:
        error = errors[0]
        path = ".".join(str(part) for part in error.path)
        raise ConfigError(f"{label}{'.' + path if path else ''}: {error.message}")


def _read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read JSON file {path}: {exc}") from exc


def _relative_path(value: str, label: str) -> str:
    windows_path = PureWindowsPath(value)
    if (
        not value
        or "\\" in value
        or any(character in value for character in "*?[]")
        or PurePosixPath(value).is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
    ):
        raise ConfigError(f"{label} must be a non-empty workspace-relative POSIX path")
    parts = value.split("/")
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ConfigError(f"{label} contains an unsafe path component")
    return PurePosixPath(*parts).as_posix()


def _parse_scenario(raw: dict[str, Any], fixture: Path) -> Scenario:
    episode_order = {item["id"]: index for index, item in enumerate(raw["episodes"])}
    rules: list[Rule] = []
    rule_ids: set[str] = set()
    if raw.get("schema_version") == "1.1":
        for item in raw.get("rules", []):
            rule = Rule(**item)
            if rule.id in rule_ids:
                raise ConfigError(f"duplicate rule id: {rule.id}")
            rule_ids.add(rule.id)
            if rule.start_episode not in episode_order or (rule.end_episode_exclusive is not None and rule.end_episode_exclusive not in episode_order):
                raise ConfigError(f"rule {rule.id}: start/end episode must exist")
            if rule.end_episode_exclusive is not None and episode_order[rule.end_episode_exclusive] <= episode_order[rule.start_episode]:
                raise ConfigError(f"rule {rule.id}: end must follow start")
            if rule.delivery == "agents_file" and (rule.end_episode_exclusive is not None or rule.start_episode != raw["episodes"][0]["id"]):
                raise ConfigError(f"rule {rule.id}: agents_file rules must cover the entire run")
            rules.append(rule)
        by_id = {rule.id: rule for rule in rules}
        visiting: set[str] = set()
        visited: set[str] = set()
        def visit(rule_id: str) -> None:
            if rule_id in visiting:
                raise ConfigError("rule supersedes cycle")
            if rule_id in visited:
                return
            visiting.add(rule_id)
            rule = by_id[rule_id]
            if rule.supersedes:
                old = by_id.get(rule.supersedes)
                if old is None or old.id == rule.id:
                    raise ConfigError(f"rule {rule.id}: supersedes must reference another rule")
                if (old.kind, old.family_id, old.end_episode_exclusive) != (rule.kind, rule.family_id, rule.start_episode):
                    raise ConfigError(f"rule {rule.id}: supersedes must match kind/family and adjacent period")
                visit(old.id)
            visiting.remove(rule_id)
            visited.add(rule_id)
        for rule in rules:
            visit(rule.id)
        for i, left in enumerate(rules):
            for right in rules[i + 1:]:
                l0, r0 = episode_order[left.start_episode], episode_order[right.start_episode]
                l1 = episode_order[left.end_episode_exclusive] if left.end_episode_exclusive else len(episode_order)
                r1 = episode_order[right.end_episode_exclusive] if right.end_episode_exclusive else len(episode_order)
                if left.family_id == right.family_id and max(l0, r0) < min(l1, r1):
                    raise ConfigError(f"rules {left.id} and {right.id}: overlapping periods")
    elif raw.get("rules"):
        raise ConfigError("rules require scenario schema_version 1.1")
    seen_episode_ids: set[str] = set()
    faults: set[str] = set()
    signatures: dict[str, tuple[Any, ...]] = {}
    check_signatures: dict[str, tuple[Any, ...]] = {}
    episodes: list[Episode] = []
    for episode_raw in raw["episodes"]:
        episode_id = episode_raw["id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", episode_id):
            raise ConfigError(f"episode id must contain only letters, digits, underscore, or hyphen: {episode_id!r}")
        if episode_id in seen_episode_ids:
            raise ConfigError(f"duplicate episode id: {episode_id}")
        seen_episode_ids.add(episode_id)
        feedback = episode_raw.get("feedback", "inherit")
        if feedback not in ("inherit", *_FEEDBACK):
            raise ConfigError(f"episode {episode_id}: invalid feedback")

        fault_raw = episode_raw.get("fault")
        fault = None
        if fault_raw is not None:
            path = _relative_path(fault_raw["path"], f"episode {episode_id} fault path")
            if not path.startswith("inputs/"):
                raise ConfigError(f"episode {episode_id}: faults are limited to inputs/")
            target = fixture / path
            if not target.is_file() or target.is_symlink():
                raise ConfigError(f"episode {episode_id}: fault target must be an existing regular file: {path}")
            fault = FaultSpec(fault_raw["type"], path)
            faults.add(path)

        oracle_ids: set[str] = set()
        all_check_ids: set[str] = set()
        oracles: list[OracleSpec] = []
        for oracle_raw in episode_raw["oracles"]:
            oracle_id = oracle_raw["id"]
            if not re.fullmatch(r"[A-Za-z0-9_-]+", oracle_id):
                raise ConfigError(f"oracle id must contain only letters, digits, underscore, or hyphen: {oracle_id!r}")
            if oracle_id in oracle_ids:
                raise ConfigError(f"episode {episode_id}: duplicate oracle id: {oracle_id}")
            oracle_ids.add(oracle_id)
            all_check_ids.add(oracle_id)
            kind = oracle_raw["type"]
            paths = tuple(_relative_path(path, f"oracle {oracle_id} path") for path in oracle_raw.get("paths", ()))
            if kind in ("unchanged", "allowed_changes", "required_paths") and not paths:
                raise ConfigError(f"oracle {oracle_id}: paths must not be empty")
            baseline = oracle_raw.get("baseline")
            if kind == "unchanged" and baseline not in ("run_start", "episode_start"):
                raise ConfigError(f"oracle {oracle_id}: unchanged requires a baseline")
            if kind != "unchanged" and baseline is not None:
                raise ConfigError(f"oracle {oracle_id}: baseline is only valid for unchanged")
            argv = tuple(oracle_raw.get("argv", ()))
            if kind == "command":
                if paths:
                    raise ConfigError(f"oracle {oracle_id}: command does not accept paths")
                if not argv:
                    raise ConfigError(f"oracle {oracle_id}: command argv must not be empty")
                for arg in argv:
                    if re.search(r"\{(?!workspace\}|oracle_root\})[^}]*\}", arg):
                        raise ConfigError(f"oracle {oracle_id}: only workspace/oracle_root placeholders are supported")
            elif "argv" in oracle_raw:
                raise ConfigError(f"oracle {oracle_id}: argv is only valid for command")
            rule_id = oracle_raw.get("rule_id")
            applicability = oracle_raw.get("applicability", "always")
            if raw["schema_version"] == "1.1":
                if not rule_id or rule_id not in rule_ids:
                    raise ConfigError(f"oracle {oracle_id}: rule_id must reference a defined rule")
                rule = next(rule for rule in rules if rule.id == rule_id)
                if not (episode_order[rule.start_episode] <= episode_order[episode_id] and (rule.end_episode_exclusive is None or episode_order[episode_id] < episode_order[rule.end_episode_exclusive])):
                    raise ConfigError(f"oracle {oracle_id}: rule is not active in {episode_id}")
                if applicability != "always":
                    raise ConfigError(f"oracle {oracle_id}: automatic oracles must be always applicable")
            signature = (kind, paths, baseline, argv, rule_id, applicability)
            previous = signatures.setdefault(oracle_id, signature)
            if previous != signature:
                raise ConfigError(f"oracle {oracle_id}: rule meaning changes within the scenario")
            previous_check = check_signatures.setdefault(oracle_id, ("auto", *signature))
            if previous_check != ("auto", *signature):
                raise ConfigError(f"check {oracle_id}: definition changes within the scenario")
            oracles.append(OracleSpec(oracle_id, kind, paths, baseline, argv, rule_id, applicability))

        reviews: list[ReviewPoint] = []
        review_ids: set[str] = set()
        for review_raw in episode_raw["review_points"]:
            review_id = review_raw["id"]
            if review_id in review_ids:
                raise ConfigError(f"episode {episode_id}: duplicate review point id: {review_id}")
            review_ids.add(review_id)
            if review_id in all_check_ids:
                raise ConfigError(f"episode {episode_id}: duplicate check id: {review_id}")
            all_check_ids.add(review_id)
            rule_id = review_raw.get("rule_id")
            applicability = review_raw.get("applicability", "always")
            condition = review_raw.get("condition")
            if raw["schema_version"] == "1.1":
                if not rule_id or rule_id not in rule_ids:
                    raise ConfigError(f"review point {review_id}: rule_id must reference a defined rule")
                rule = next(rule for rule in rules if rule.id == rule_id)
                if not (episode_order[rule.start_episode] <= episode_order[episode_id] and (rule.end_episode_exclusive is None or episode_order[episode_id] < episode_order[rule.end_episode_exclusive])):
                    raise ConfigError(f"review point {review_id}: rule is not active in {episode_id}")
                if applicability == "conditional" and not condition:
                    raise ConfigError(f"review point {review_id}: conditional requires condition")
            reviews.append(ReviewPoint(review_id, review_raw["criterion"], rule_id, applicability, condition))
            review_signature = ("manual", review_raw["criterion"], rule_id, applicability, condition)
            previous_check = check_signatures.setdefault(review_id, review_signature)
            if previous_check != review_signature:
                raise ConfigError(f"check {review_id}: definition changes within the scenario")
        episodes.append(Episode(episode_id, episode_raw["purpose"], episode_raw["prompt"], feedback, fault, tuple(oracles), tuple(reviews)))

    fault_paths = {episode.fault.path for episode in episodes if episode.fault}
    for episode in episodes:
        for oracle in episode.oracles:
            if oracle.type == "unchanged" and oracle.baseline == "run_start" and fault_paths.intersection(oracle.paths):
                raise ConfigError(f"episode {episode.id}: run_start unchanged conflicts with an injected fault path")
    return Scenario(raw["schema_version"], raw["id"], raw["description"], tuple(episodes), tuple(rules))


def load_config(path: str | Path) -> tuple[RunConfig, Scenario]:
    config_path = Path(path).expanduser().resolve()
    raw = _read_json(config_path)
    schemas = _schema()
    _validate_schema(raw, schemas["config"], "config")
    base = config_path.parent
    fixture = (base / raw["fixture"]).resolve()
    scenario_path = (base / raw["scenario"]).resolve()
    output_root = (base / raw["output_root"]).resolve()
    if not fixture.is_dir():
        raise ConfigError(f"fixture directory does not exist: {fixture}")
    from .workspace import validate_fixture

    validate_fixture(fixture)
    if not scenario_path.is_file():
        raise ConfigError(f"scenario file does not exist: {scenario_path}")
    if fixture == output_root or fixture in output_root.parents or output_root in fixture.parents:
        raise ConfigError("fixture and output_root must not contain one another")

    scenario_raw = _read_json(scenario_path)
    scenario_schema = schemas["scenario"]
    if scenario_raw.get("schema_version") == "1.0":
        scenario_schema = json.loads(json.dumps(scenario_schema))
        scenario_schema["properties"].pop("rules", None)
        scenario_schema["properties"]["schema_version"]["enum"] = ["1.0"]
        episode_schema = scenario_schema["properties"]["episodes"]["items"]
        episode_schema["properties"]["oracles"]["items"]["properties"].pop("rule_id", None)
        episode_schema["properties"]["oracles"]["items"]["properties"].pop("applicability", None)
        point_schema = episode_schema["properties"]["review_points"]["items"]["properties"]
        for key in ("rule_id", "applicability", "condition"):
            point_schema.pop(key, None)
    _validate_schema(scenario_raw, scenario_schema, "scenario")
    scenario = _parse_scenario(scenario_raw, fixture)
    if scenario.schema_version == "1.1":
        from .evaluation import validate_agents_rules
        validate_agents_rules(scenario, fixture)
    limits = Limits(**{key: raw["limits"][key] for key in _LIMIT_KEYS})
    if len(scenario.episodes) > limits.max_episodes:
        raise ConfigError(f"scenario has {len(scenario.episodes)} episodes; max_episodes is {limits.max_episodes}")
    exclusions = tuple(raw.get("exclusions", ()))
    for pattern in exclusions:
        normalized = pattern[:-3] if pattern.endswith("/**") else pattern
        _relative_path(normalized, "exclusion")
    config = RunConfig(
        schema_version=raw["schema_version"],
        name=raw["name"],
        fixture=fixture,
        scenario=scenario_path,
        output_root=output_root,
        codex_executable=raw["codex"]["executable"],
        model=raw["codex"]["model"],
        feedback=raw["feedback"],
        limits=limits,
        exclusions=exclusions,
    )
    return config, scenario
