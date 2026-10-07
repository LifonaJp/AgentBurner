#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${AGENTBURNER_VENV:-$ROOT_DIR/.venv}"
PYTHON_BIN="${PYTHON:-python3}"

"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit("AgentBurner requires Python 3.11 or newer.")
PY

if ! "$PYTHON_BIN" -m venv --help >/dev/null 2>&1; then
    echo "Python venv support is required." >&2
    exit 1
fi

if [[ ! -d "$VENV_DIR" ]]; then
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install "$ROOT_DIR"

cat <<MSG

AgentBurner installed successfully.

Run:
  $VENV_DIR/bin/agentburner --help

Optional activation:
  source "$VENV_DIR/bin/activate"
MSG
