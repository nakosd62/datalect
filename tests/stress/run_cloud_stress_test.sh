#!/bin/bash
# run_cloud_stress_test.sh
#
# Wrapper around stress_test.py (expected in this same directory),
# pre-tuned against the DEPLOYED Cloud Run instance's actual settings:
#
#   env.yaml:
#     MAX_CONCURRENT_TRANSLATE_REQUESTS=3   MAX_CONCURRENT_EXECUTE_REQUESTS=3
#     RATE_LIMIT_TRANSLATE="30 per minute"  RATE_LIMIT_EXECUTE="20 per minute"
#   gcp_deploy.sh:
#     --concurrency=5 --min-instances=1 --max-instances=4 --session-affinity
#
# THIS HITS YOUR REAL, LIVE SERVICE - not local. Every translate/summarize
# call is a real, billed LLM call; every execute call opens a real
# connection to your Cloud SQL instance; and if anyone else is actually
# using this deployment right now, this traffic competes with theirs and
# can trip guards/limits for them too. There's no default URL baked in -
# you have to supply the real one, and the script asks for an explicit
# confirmation before firing anything (skip it with CONFIRM=1).
#
# Usage:
#   ./run_cloud_stress_test.sh <service-url> [whoami|burst|sustained|fairness|all]
#
# Find your service URL with:
#   gcloud run services describe ydyl --region=<your-region> --format='value(status.url)'
#
# Override the model, or skip the confirmation prompt, without editing
# this file:
#   DATALECT_MODEL=gemini-3.5-flash-lite ./run_cloud_stress_test.sh <url> burst
#   CONFIRM=1 ./run_cloud_stress_test.sh <url> all

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STRESS_PY="$SCRIPT_DIR/stress_test.py"

if [ ! -f "$STRESS_PY" ]; then
  echo "Error: stress_test.py not found next to this script ($SCRIPT_DIR)." >&2
  exit 1
fi

URL="${1:-}"
MODE="${2:-all}"

if [ -z "$URL" ]; then
  echo "Usage: $0 <cloud-run-service-url> [whoami|burst|sustained|fairness|all]" >&2
  echo "Find your URL with: gcloud run services describe ydyl --region=<region> --format='value(status.url)'" >&2
  exit 1
fi

MODEL="${DATALECT_MODEL:-gemini-3.5-flash-lite}"

cat >&2 <<EOF

=====================================================================
 About to run a "$MODE" stress test against your DEPLOYED Cloud Run
 service:

     $URL

 This makes REAL requests against a real service: every translate/
 summarize call is a real, billed LLM call, and every execute call
 opens a real connection to your Cloud SQL instance. If real users
 might be hitting this service right now, this competes with their
 traffic and could trip guards/limits for them too.
=====================================================================

EOF

if [ "${CONFIRM:-0}" != "1" ]; then
  read -r -p "Type 'yes' to continue: " reply
  if [ "$reply" != "yes" ]; then
    echo "Aborted."
    exit 1
  fi
fi

run_whoami() {
  echo "=== whoami (confirms the deployed service is up and reachable) ==="
  python3 "$STRESS_PY" --url "$URL" --mode whoami
  echo
}

run_burst() {
  # Guard limit is 3 (MAX_CONCURRENT_*_REQUESTS=3). n=5 is deliberately
  # kept AT OR BELOW Cloud Run's own --concurrency=5, so all 5 requests
  # are guaranteed eligible to land on the SAME instance rather than
  # possibly spilling onto a second one due to Cloud Run's own per-
  # instance cap - that would let more than 3 through and muddy the
  # result, since the app-level guard is per-instance, not fleet-wide.
  # All 5 reuse ONE session cookie, so --session-affinity keeps them
  # together on that one instance too.
  echo "=== burst: translate, n=5 (expect ~3x 200, ~2x 503) ==="
  python3 "$STRESS_PY" --url "$URL" --mode burst \
    --endpoint translate --n 5 --model "$MODEL"
  echo

  echo "=== burst: execute, n=5 (expect ~3x 200, ~2x 503) ==="
  python3 "$STRESS_PY" --url "$URL" --mode burst \
    --endpoint execute --n 5
  echo
}

run_sustained() {
  # RATE_LIMIT_TRANSLATE=30/min: to actually SEE this cap trip, at least
  # ~31 real calls have to land inside the same rolling minute - there's
  # no way around spending that much to prove the exact configured number.
  # interval is kept short (1s) since real per-call latency (translation,
  # plus any server-side retries) already adds real time on top of it;
  # duration is generous (110s) to leave margin if latency runs high that
  # minute. If you don't see 429s appear, rerun with a longer --duration
  # (edit the call below) - a slow LLM response that particular minute is
  # the likely reason, not a bug in the limiter.
  echo "=== sustained: translate, every 1s for 110s (need ~31 calls within a minute to trip; expect 200s then 429s) ==="
  python3 "$STRESS_PY" --url "$URL" --mode sustained \
    --endpoint translate --interval 1 --duration 110 --model "$MODEL"
  echo

  # RATE_LIMIT_EXECUTE=20/min: same reasoning, ~21 calls needed in-window.
  echo "=== sustained: execute, every 1s for 80s (need ~21 calls within a minute to trip; expect 200s then 429s) ==="
  python3 "$STRESS_PY" --url "$URL" --mode sustained \
    --endpoint execute --interval 1 --duration 80
  echo
}

run_fairness() {
  # 2 independent users x 8 calls x 2s apart (~16s per user) stay well
  # under the real 30/min-per-user translate budget - both should finish
  # all/mostly-200s (an occasional 503 is still possible from the shared,
  # process-local concurrency guard if two requests land in the exact
  # same instant - that's expected, unrelated to per-user fairness).
  # Unlike local testing, no GOOGLE_CLIENT_ID workaround is needed here -
  # Cloud Run already gives each anonymous session its own real identity.
  echo "=== fairness: translate, 2 users x 8 calls (expect all/mostly 200s for both) ==="
  python3 "$STRESS_PY" --url "$URL" --mode fairness \
    --endpoint translate --users 2 --calls-per-user 8 --interval 2 --model "$MODEL"
  echo
}

case "$MODE" in
  whoami)    run_whoami ;;
  burst)     run_whoami; run_burst ;;
  sustained) run_whoami; run_sustained ;;
  fairness)  run_whoami; run_fairness ;;
  all)       run_whoami; run_burst; run_sustained; run_fairness ;;
  *)
    echo "Usage: $0 <cloud-run-service-url> [whoami|burst|sustained|fairness|all]" >&2
    exit 1
    ;;
esac