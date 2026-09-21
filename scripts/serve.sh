#!/usr/bin/env bash
# Start against CORPUS_DIR; Python validates the bundle and loads .env defaults.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
if [ -f "$SCRIPT_DIR/venv/bin/activate" ]; then
    source "$SCRIPT_DIR/venv/bin/activate"
fi
exec python3 "$SCRIPT_DIR/service/ask_service.py" "$@"
