#!/bin/bash
# run_local_stress_test.sh
#
# Convenience wrapper around stress_test.py (expected in this same
# directory), pre-tuned against THIS repo's LOCAL dev server and the
# concurrency-guard / rate-limiter values currently in its local .env:
#
#   MAX_CONCURRENT_TRANSLATE_REQUESTS=1   MAX_CONCURRENT_EXECUTE_REQUESTS=1
#   RATE_LIMIT_TRANSLATE="10 per minute"  RATE_LIMIT_EXECUTE="5 per minute"
#
# Every number below is sized against those specific values, not the
# deployed Cloud Run instance's (env.yaml has different ones - 30/20 - and
# a different --url entirely). If you change local .env, revisit the
# comments above each block here too.
#
# Usage:
#   ./run_local_stress_test.sh              # runs everything, in order
#   ./run_local_stress_test.sh whoami
#   ./run_local_stress_test.sh burst
#   ./run_local_stress_test.sh sustained
#   ./run_local_stress_test.sh fairness
#
# Override the target or model without editing this file:
#   DATALECT_URL=http://127.0.0.1:3000 DATALECT_MODEL=gemini-3.5-flash-lite ./run_local_stress_test.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STRESS_PY="$SCRIPT_DIR/stress_test.py"

if [ ! -f "$STRESS_PY" ]; then
  echo "Error: stress_test.py not found next to this script ($SCRIPT_DIR)." >&2
  exit 1
fi

# Matches CRBOT_HOSTNAME/CRBOT_PORT in .env and what run_server.sh binds to.
URL="${DATALECT_URL:-http://127.0.0.1:3000}"

# Cheapest configured model (see GOOGLE_MODELS in .env) - every translate/
# summarize call is a real LLM call even against your local server, so
# this keeps repeated runs of this script from burning real spend.
MODEL="${DATALECT_MODEL:-gemini-3.5-flash-lite}"

MODE="${1:-all}"

run_whoami() {
  echo "=== whoami (confirms the local server is up and reachable) ==="
  python3 "$STRESS_PY" --url "$URL" --mode whoami
  echo
}

run_burst() {
  # MAX_CONCURRENT_*_REQUESTS=1 locally, so n=4 simultaneous requests is
  # already 4x the limit - guarantees a clean, unambiguous split without
  # needing a large burst.
  echo "=== burst: translate, n=4 (expect 1x 200, 3x 503) ==="
  python3 "$STRESS_PY" --url "$URL" --mode burst \
    --endpoint translate --n 4 --model "$MODEL"
  echo

  echo "=== burst: execute, n=4 (expect 1x 200, 3x 503) ==="
  python3 "$STRESS_PY" --url "$URL" --mode burst \
    --endpoint execute --n 4
  echo
}

run_sustained() {
  # RATE_LIMIT_TRANSLATE=10/min: one request every 3s for 40s fires ~13-14
  # requests, comfortably past 10 within the same minute-window.
  echo "=== sustained: translate, every 3s for 40s (expect ~10x 200 then 429s) ==="
  python3 "$STRESS_PY" --url "$URL" --mode sustained \
    --endpoint translate --interval 3 --duration 40 --model "$MODEL"
  echo

  # RATE_LIMIT_EXECUTE=5/min: one request every 5s for 40s fires ~8,
  # comfortably past 5 within the same minute-window.
  echo "=== sustained: execute, every 5s for 40s (expect ~5x 200 then 429s) ==="
  python3 "$STRESS_PY" --url "$URL" --mode sustained \
    --endpoint execute --interval 5 --duration 40
  echo
}

run_fairness() {
  # 2 independent users x 6 calls x 3s apart (~18s per user) each stay
  # well under the 10/min-per-user translate budget - both should finish
  # all-200s, confirming the two users' counters never share state.
  echo "=== fairness: translate, 2 users x 6 calls (expect all 200s for both) ==="
  python3 "$STRESS_PY" --url "$URL" --mode fairness \
    --endpoint translate --users 2 --calls-per-user 6 --interval 3 --model "$MODEL"
  echo
}

case "$MODE" in
  whoami)    run_whoami ;;
  burst)     run_whoami; run_burst ;;
  sustained) run_whoami; run_sustained ;;
  fairness)  run_whoami; run_fairness ;;
  all)       run_whoami; run_burst; run_sustained; run_fairness ;;
  *)
    echo "Usage: $0 [whoami|burst|sustained|fairness|all]" >&2
    exit 1
    ;;
esac