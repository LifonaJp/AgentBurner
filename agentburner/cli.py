from __future__ import annotations

import argparse
import os
import shutil
import sys
import tomllib
from pathlib import Path

from .codex_adapter import AdapterError, is_logged_in, probe
from .config import ConfigError, load_config
from .oracle import command_sandbox_status
from .report import audit, build_report
from .runner import RunError, run
from .workspace import filesystem_type, git_metadata, is_linux_filesystem_type


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentburner")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "validate"):
        subparser = subparsers.add_parser(name)
        subparser.add_argument("--config", required=True, type=Path)
        run_parser = subparsers.add_parser("run")
        run_parser.add_argument("--config", required=True, type=Path)
        run_parser.add_argument(
            "--allow-command-oracles",
            action="store_true",
            help="Allow execution of command-type oracles. Disabled by default for safety.",
        )

    for name in ("audit", "report"):
        subparser = subparsers.add_parser(name)
        subparser.add_argument("run_dir", type=Path)
    review_parser = subparsers.add_parser("review")
    review_commands = review_parser.add_subparsers(dest="review_command", required=True)
    for name in ("export", "import", "report"):
        command = review_commands.add_parser(name)
        command.add_argument("run_dir", type=Path)
        if name == "export":
            command.add_argument("--out", type=Path, required=True)
        elif name == "import":
            command.add_argument("--file", type=Path, required=True)
    compare_parser = subparsers.add_parser("compare")
    compare_commands = compare_parser.add_subparsers(dest="compare_command", required=True)
    for name in ("validate", "run", "report", "audit"):
        command = compare_commands.add_parser(name)
        if name in ("validate", "run"):
            command.add_argument("--plan", type=Path, required=True)
        else:
            command.add_argument("comparison_dir", type=Path)
    return parser


def _inherited_context(output_root: Path, codex_home: Path) -> dict[str, object]:
    directories = {output_root.resolve(), *output_root.resolve().parents}
    agents = sorted(str(path / "AGENTS.md") for path in directories if (path / "AGENTS.md").is_file())
    if (codex_home / "AGENTS.md").is_file():
        agents.append(str(codex_home / "AGENTS.md"))

    settings_path = codex_home / "config.toml"
    settings: dict[str, object] = {}
    settings_error = None
    if settings_path.is_file():
        try:
            settings = tomllib.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            settings_error = str(exc)
    mcp_servers = settings.get("mcp_servers", {})
    mcp = {
        str(name): bool(value.get("enabled", True)) if isinstance(value, dict) else True
        for name, value in mcp_servers.items()
    } if isinstance(mcp_servers, dict) else {}
    skills_root = codex_home / "skills"
    skills = sorted(path.name for path in skills_root.iterdir() if path.is_dir()) if skills_root.is_dir() else []
    user_settings = settings_path.resolve() if settings_path.is_file() else None
    project_settings = sorted(
        str(candidate)
        for path in directories
        if (candidate := path / ".codex" / "config.toml").is_file() and candidate.resolve() != user_settings
    )
    return {
        "agents": agents,
        "settings": str(settings_path) if settings_path.is_file() else None,
        "settings_error": settings_error,
        "mcp": mcp,
        "skills": skills,
        "project_settings": project_settings,
    }


def _doctor(config_path: Path) -> int:
    errors: list[str] = []
    try:
        config, scenario = load_config(config_path)
        print(f"Python: {sys.version.split()[0]} (required: 3.11+)")
        if sys.version_info < (3, 11):
            errors.append("Python 3.11 or newer is required")
        print(f"Platform: {sys.platform}")
        if os.name != "posix":
            errors.append("Run target is Linux/WSL2; native Windows is unsupported")
        git = shutil.which("git")
        print(f"Git: {git or 'not found'}")
        if not git:
            errors.append("Git executable was not found")
        metadata = git_metadata(config.fixture)
        print(f"Fixture Git commit: {metadata['commit'] or 'unavailable'}")
        for label, path in (("Fixture", config.fixture), ("Output", config.output_root)):
            fstype = filesystem_type(path)
            print(f"{label} filesystem: {fstype or 'unknown'}")
            if not is_linux_filesystem_type(fstype):
                errors.append(f"{label.lower()} must reside on a Linux filesystem; detected {fstype or 'unknown'}")
        existing = config.output_root
        while not existing.exists() and existing != existing.parent:
            existing = existing.parent
        writable = existing.is_dir() and os.access(existing, os.W_OK)
        print(f"Output parent writable: {writable} ({existing})")
        if not writable:
            errors.append("output_root has no writable existing parent")
        try:
            capabilities = probe(config.codex_executable)
            print(f"Codex: {capabilities.version}")
            logged_in = is_logged_in(config.codex_executable)
            print(f"Codex login: {'authenticated' if logged_in else 'not authenticated'}")
            if not logged_in:
                errors.append("Codex CLI is not logged in; authenticate with `codex login`")
        except AdapterError as exc:
            errors.append(str(exc))
        sandbox_ok, sandbox_detail = command_sandbox_status()
        print(f"Oracle sandbox: {sandbox_detail}")
        needs_sandbox = any(oracle.type == "command" for episode in scenario.episodes for oracle in episode.oracles)
        if needs_sandbox and not sandbox_ok:
            errors.append(f"scenario contains command oracles: {sandbox_detail}")
        codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
        inherited = _inherited_context(config.output_root, codex_home)
        print(f"Inherited AGENTS: {', '.join(inherited['agents']) or 'none found'}")
        print(f"Codex user settings: {inherited['settings'] or 'not found'}")
        print(f"Project Codex settings: {', '.join(inherited['project_settings']) or 'none found'}")
        mcp = inherited["mcp"]
        print(f"Codex MCP servers: {', '.join(name + (' (enabled)' if enabled else ' (disabled)') for name, enabled in mcp.items()) or 'none configured'}")
        print(f"Codex skill directories: {', '.join(inherited['skills']) or 'none found'}")
        if inherited["settings_error"]:
            errors.append(f"cannot parse Codex user settings: {inherited['settings_error']}")
        if errors:
            for error in errors:
                print(f"ERROR: {error}", file=sys.stderr)
            return 2
        print(f"Ready: scenario={scenario.id}, episodes={len(scenario.episodes)}")
        if config.model is None:
            print("Model telemetry: unknown (Codex default is inherited)")
        return 0
    except (ConfigError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "review":
            from .review import export_review, import_review, review_report
            if args.review_command == "export":
                export_review(args.run_dir, args.out)
                print(f"Review draft: {args.out}")
            elif args.review_command == "import":
                result = import_review(args.run_dir, args.file)
                print(f"Review imported: {result}")
            else:
                review_report(args.run_dir)
                print(f"Evaluation report: {args.run_dir / 'evaluation_report.md'}")
            return 0
        if args.command == "compare":
            from .comparison import validate_plan, run_comparison, write_comparison, audit_comparison
            if args.compare_command == "validate":
                validate_plan(args.plan)
                print(f"Valid comparison plan: {args.plan}")
            elif args.compare_command == "run":
                directory, code = run_comparison(args.plan)
                print(f"Comparison directory: {directory}")
                return code
            elif args.compare_command == "report":
                result = write_comparison(args.comparison_dir)
                print(f"Comparison report: {args.comparison_dir / 'comparison.md'}")
                if result.get("audit_errors"):
                    for error in result["audit_errors"]:
                        print(f"ERROR: {error}", file=sys.stderr)
                    return 2
            else:
                result = audit_comparison(args.comparison_dir)
                errors = result.get("errors", []) if isinstance(result, dict) else result.errors if hasattr(result, "errors") else result
                if errors:
                    for error in errors:
                        print(f"ERROR: {error}", file=sys.stderr)
                    return 2
                print("Comparison audit passed")
            return 0
        if args.command == "doctor":
            return _doctor(args.config)
        if args.command == "validate":
            config, scenario = load_config(args.config)
            print(f"Valid: {config.name} / {scenario.id} ({len(scenario.episodes)} episodes)")
            return 0
        if args.command == "run":
            run_dir, code = run(
                args.config,
                allow_command_oracles=args.allow_command_oracles,
            )

            print(f"Run directory: {run_dir}")
            if code == 0:
                print("Run completed without automatic failures; manual review may remain.")
            elif code == 1:
                print("Run completed with automatic failures.")
            elif code == 3:
                print("Run stopped at a configured limit.")
            return code
        if args.command == "audit":
            result = audit(args.run_dir)
            if result.valid:
                print(f"Audit passed: {len(result.events)} events")
                if result.crash_tail:
                    print("Incomplete final JSONL line recorded as crash_tail; journal was not changed.")
                return 0
            for error in result.errors:
                print(f"ERROR: {error}", file=sys.stderr)
            return 2
        if args.command == "report":
            summary = build_report(args.run_dir)
            print(f"Report regenerated: {args.run_dir / 'report.md'}")
            print(f"Summary regenerated: {args.run_dir / 'summary.json'}")
            if summary.data["incomplete"]:
                print("Run is incomplete.")
            return 0
    except (ConfigError, AdapterError, RunError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return exc.exit_code if isinstance(exc, RunError) else 2
    return 2
