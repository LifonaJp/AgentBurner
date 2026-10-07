# AgentBurner

AgentBurner is an **experimental long-horizon reliability testing framework for Codex agents**.

It repeatedly runs an agent against controlled scenarios, records trajectories and workspace changes, evaluates configured checks, and produces run/comparison artifacts for later analysis.

The project is intended for reliability and failure-analysis experiments. It is not a security exploit framework and should be used only in workspaces and environments you are authorized to test.

## Status

**Experimental / v0.1.0**

The implementation is usable, but interfaces, scenario formats, and evaluation behavior may still change.

## Requirements

- Linux or WSL2
- Python 3.11+
- Git
- OpenAI Codex CLI available as `codex`
- Authenticated Codex CLI (`codex login`)
- `bubblewrap` (`bwrap`) when using command oracles

AgentBurner expects its fixture and output directories to be on a Linux filesystem. Under WSL2, keep the repository and run data inside the Linux filesystem rather than `/mnt/c/...`.

## Installation

### Option 1: installer

```bash
git clone <YOUR_GITHUB_REPOSITORY_URL>
cd AgentBurner
./install.sh
```

The installer creates `.venv`, installs AgentBurner, and exposes the CLI at:

```bash
.venv/bin/agentburner
```

You can optionally activate the environment:

```bash
source .venv/bin/activate
agentburner --help
```

### Option 2: standard Python install

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
agentburner --help
```

## Quick start

The repository includes example fixtures, scenarios, configs, and comparison plans.

First check the environment:

```bash
agentburner doctor --config configs/basic.json
```

Validate the example configuration:

```bash
agentburner validate --config configs/basic.json
```

Run it:

```bash
agentburner run --config configs/basic.json
```

By default, the example config writes run artifacts below `results/`.

## Main commands

```text
agentburner doctor   --config CONFIG
agentburner validate --config CONFIG
agentburner run      --config CONFIG

agentburner audit  RUN_DIR
agentburner report RUN_DIR

agentburner review export RUN_DIR --out FILE
agentburner review import RUN_DIR --file FILE
agentburner review report RUN_DIR

agentburner compare validate --plan PLAN
agentburner compare run      --plan PLAN
agentburner compare report   COMPARISON_DIR
agentburner compare audit    COMPARISON_DIR
```

## Repository layout

```text
agentburner/   Core implementation
configs/       Example run configurations
scenarios/     Example long-horizon scenarios
plans/         Example controlled-comparison plans
fixtures/      Example workspaces used by scenarios
```

## Safety notes

AgentBurner launches Codex with approval disabled and the Codex `workspace-write` sandbox for the test workspace. Treat test scenarios as executable workloads.

- Use disposable or dedicated fixtures.
- Do not point fixtures at important source trees without backups.
- Do not place secrets or production credentials in test workspaces.
- Review scenarios and command oracles before running them.
- Keep destructive or high-impact experiments inside an isolated environment.

## Example files

A minimal starting point is:

```text
configs/basic.json
scenarios/instruction_scope.json
fixtures/sample_repo/
```

Edit or copy these files to create your own burn-in experiment. Paths in configuration files are resolved relative to the configuration file.

## License

This project is licensed under the Apache License 2.0.
See the `LICENSE` file for details
