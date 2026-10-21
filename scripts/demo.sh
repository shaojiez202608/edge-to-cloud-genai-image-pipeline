#!/usr/bin/env bash
# One-command demo. Default: the offline path (local simulation mode, no AWS, no Docker).
#   ./scripts/demo.sh            offline D1-D6 with a readable summary (out/demo_local.json)
#   ./scripts/demo.sh --cloud    cloud path against a deployed sandbox stack (paid: ALLOW_PAID=1):
#                                identities -> e2e (D1-D3, D6) -> rollout drills (D4) -> load test (D5)
#   ./scripts/demo.sh --reset    reset: remove local demo output (out/demo_local.json)
set -euo pipefail
cd "$(dirname "$0")/.."
PARAMS="${PARAMS:-config/sandbox.json}"
case "${1:-}" in
  --reset)
    for f in out/demo_local.json; do [ -f "$f" ] && mv "$f" "$f.prev" && echo "moved $f to $f.prev"; done
    ;;
  --cloud)
    [ "${ALLOW_PAID:-}" = "1" ] || { echo "The cloud demo uses paid services; re-run with ALLOW_PAID=1." >&2; exit 2; }
    uv run python scripts/setup_identities.py --params "$PARAMS"
    uv run python scripts/e2e_aws.py --params "$PARAMS" --injection --report out/e2e_aws.json || true
    uv run python scripts/rollout_drill_jobs.py --params "$PARAMS" --report out/rollout_jobs_aws.json || true
    uv run python scripts/rollout_drill_gg.py --params "$PARAMS" --report out/rollout_gg_aws.json || true
    uv run python scripts/loadtest_aws.py --params "$PARAMS" --report out/loadtest_aws.json || true
    echo "Reports in out/. Clean up with: make destroy && make verify-cleanup"
    ;;
  *)
    uv run python scripts/local_demo.py "$@"
    ;;
esac
