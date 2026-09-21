#!/usr/bin/env bash
# Full pipeline: setup → reindex → serve → run heldout questions → results
# Usage: scripts/reproduce.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SCRIPT_DIR"

CORPUS_DIR="${CORPUS_DIR:-corpus}"
PORT="${PORT:-8000}"

echo "============================================"
echo "  Ask the Tenant Book — Reproduce Pipeline"
echo "============================================"

# 1. Setup venv + deps
echo ""
echo "=== Step 1: Setup ==="
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
source venv/bin/activate
pip install -q -r service/requirements.txt

# 2. Build OKF knowledge base
echo ""
echo "=== Step 2: Reindex (build OKF knowledge base) ==="
scripts/reindex.sh "$CORPUS_DIR"

# 3. Start service in background
echo ""
echo "=== Step 3: Starting service ==="
python3 service/ask_service.py --corpus "$CORPUS_DIR" --port "$PORT" &
SERVICE_PID=$!
trap 'kill "$SERVICE_PID" 2>/dev/null || true; wait "$SERVICE_PID" 2>/dev/null || true' EXIT
READY=false
for attempt in {1..30}; do
    if python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:$PORT/health', timeout=1)" 2>/dev/null; then
        READY=true
        break
    fi
    sleep 1
done

# Check service is up
if [ "$READY" != true ] || ! kill -0 "$SERVICE_PID" 2>/dev/null; then
    echo "ERROR: Service failed to start"
    exit 1
fi
echo "  Service running (PID $SERVICE_PID)"

# 4. Run heldout questions
echo ""
echo "=== Step 4: Running heldout questions ==="
mkdir -p results
RUN_STATUS=0
python3 service/run_questions.py \
    --questions questions/heldout.jsonl \
    --out results/answers.jsonl \
    --url "http://localhost:$PORT" || RUN_STATUS=$?

# 5. Copy manifest
cp results/answers_manifest.json results/manifest.json

echo ""
echo "============================================"
echo "  Run finished (status $RUN_STATUS). Results in results/"
echo "    results/answers.jsonl"
echo "    results/manifest.json"
echo "============================================"
exit "$RUN_STATUS"
