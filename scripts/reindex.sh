#!/usr/bin/env bash
# Usage: scripts/reindex.sh [corpus_dir]. Dependencies must already be installed.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
if [ -f "$SCRIPT_DIR/venv/bin/activate" ]; then
    source "$SCRIPT_DIR/venv/bin/activate"
fi
exec python3 "$SCRIPT_DIR/service/okf_builder.py" "$@"
