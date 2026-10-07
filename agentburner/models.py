from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Limits:
    max_episodes: int
    max_runtime_sec: int
    episode_timeout_sec: int
    oracle_timeout_sec: int
    max_consecutive_failures: int
    max_output_bytes_per_episode: int
    max_run_disk_bytes: int
    max_workspace_files: int


@dataclass(frozen=True)
class RunConfig:
    schema_version: str
    name: str
    fixture: Path
    scenario: Path
    output_root: Path
    codex_executable: str
    model: str | None
    feedback: str
    limits: Limits
    exclusions: tuple[str, ...] = ()


@dataclass(frozen=True)
class OracleSpec:
    id: str
    type: str
    paths: tuple[str, ...] = ()
    baseline: str | None = None
    argv: tuple[str, ...] = ()
    rule_id: str | None = None
    applicability: str = "always"


@dataclass(frozen=True)
class ReviewPoint:
    id: str
    criterion: str
    rule_id: str | None = None
    applicability: str = "always"
    condition: str | None = None


@dataclass(frozen=True)
class Rule:
    id: str
    family_id: str
    kind: str
    statement: str
    start_episode: str
    end_episode_exclusive: str | None = None
    delivery: str = "prompt"
    supersedes: str | None = None


@dataclass(frozen=True)
class FaultSpec:
    type: str
    path: str


@dataclass(frozen=True)
class Episode:
    id: str
    purpose: str
    prompt: str
    feedback: str
    fault: FaultSpec | None
    oracles: tuple[OracleSpec, ...]
    review_points: tuple[ReviewPoint, ...]


@dataclass(frozen=True)
class Scenario:
    schema_version: str
    id: str
    description: str
    episodes: tuple[Episode, ...]
    rules: tuple[Rule, ...] = ()


@dataclass(frozen=True)
class ManifestEntry:
    path: str
    kind: str
    size: int
    sha256: str


@dataclass(frozen=True)
class Manifest:
    entries: dict[str, ManifestEntry]

    def to_list(self) -> list[dict[str, Any]]:
        return [
            {"path": entry.path, "kind": entry.kind, "size": entry.size, "sha256": entry.sha256}
            for _, entry in sorted(self.entries.items())
        ]


@dataclass(frozen=True)
class Capabilities:
    version: str
    exec_help: str
    resume_help: str


@dataclass(frozen=True)
class ProcessLimits:
    timeout_sec: int
    max_output_bytes: int
    poll_interval_sec: float = 0.2
    terminate_grace_sec: float = 5.0


@dataclass(frozen=True)
class ProcessSpec:
    argv: tuple[str, ...]
    cwd: Path
    stdin: bytes
    stdout_path: Path
    stderr_path: Path
    secrets: tuple[str, ...] = ()
    environment: dict[str, str] | None = None


@dataclass(frozen=True)
class ProcessResult:
    returncode: int | None
    elapsed_sec: float
    received_bytes: int
    saved_bytes: int
    truncated: bool
    timed_out: bool = False
    stopped: bool = False
    error: str | None = None
    cleanup_incomplete: bool = False
    stop_reason: str | None = None


@dataclass(frozen=True)
class OracleResult:
    oracle_id: str
    verdict: str
    summary: str
    output: str = ""
    execution_error: str | None = None


@dataclass(frozen=True)
class EpisodeContext:
    workspace: Path
    episode_start: Manifest
    run_start: Manifest
    oracle_root: Path
    timeout_sec: int
    secrets: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    stop_check: Any = None


@dataclass
class AuditResult:
    valid: bool
    errors: list[str] = field(default_factory=list)
    crash_tail: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Summary:
    data: dict[str, Any]
