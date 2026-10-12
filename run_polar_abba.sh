#!/usr/bin/env bash
# Uses chamber_session_abba.py: a copy of chamber_session.py with
# --replicates / --rep-start / --no-bracket added (rows 3, 5, 8).
#
# Polar H10, ABBA run order, all in ONE session directory (300 s per run):
#
#   1  row3  forced_channel_no_load                 rep1  start check
#   2  row3  forced_channel_duty75pct               rep1  A
#   3  row5  headroom_k0_duty75pct                  rep1  B
#   4  row8  headroom_k2_duty100pct x3,
#            headroom_k8_duty100pct x3              rep1-3 (shuffled)
#   5  row5  headroom_k0_duty75pct                  rep2  B
#   6  row3  forced_channel_duty75pct               rep2  A
#   7  row3  forced_channel_no_load                 rep2  end check
#
# Step 1 creates the session; every later step --resume's it, so everything
# lands in one manifest.csv/results.csv. Repeats are recorded as rep2 (via
# --rep-start) rather than skipped. Row 3's automatic no_load bracket is off
# (--no-bracket) since step 7 already is the end check.
#
# Usage:
#   ./run_polar_abba.sh [extra args, e.g. --test-mode]
#   ./run_polar_abba.sh --session 20261008_101500 [extra args]
#       resume after an abort: steps already in the manifest are skipped
#       (PASSED or FAILED -- rows 3/5/8 don't retry), so just re-run it.

set -euo pipefail
cd "$(dirname "$0")"

SESSION=""
if [[ "${1:-}" == "--session" ]]; then
    SESSION="$2"
    shift 2
fi
EXTRA=("$@")

COMMON=(--device Polar_H10 --replicates 1 --no-bracket)

step() {
    local n="$1"; shift
    echo
    echo "=================== step $n/7: $* ==================="
    if [[ -z "$SESSION" ]]; then
        python3 chamber_session_abba.py run "${COMMON[@]}" "$@" "${EXTRA[@]}"
        SESSION=$(ls captures | grep -E '^[0-9]{8}_[0-9]{6}$' | sort | tail -1)
        echo ">>> session: $SESSION"
    else
        python3 chamber_session_abba.py run --resume "$SESSION" \
            "${COMMON[@]}" "$@" "${EXTRA[@]}"
    fi
}

trap 'echo; echo "!!! stopped. Resume with: $0 --session ${SESSION:-<timestamp>} ${EXTRA[*]}"' ERR

step 1 --row 3 --condition forced_channel_no_load   --rep-start 1
step 2 --row 3 --condition forced_channel_duty75pct --rep-start 1
step 3 --row 5 --condition headroom_k0_duty75pct    --rep-start 1
step 4 --row 8 --condition headroom_k2_duty100pct --condition headroom_k8_duty100pct \
       --replicates 3 --rep-start 1
step 5 --row 5 --condition headroom_k0_duty75pct    --rep-start 2
step 6 --row 3 --condition forced_channel_duty75pct --rep-start 2
step 7 --row 3 --condition forced_channel_no_load   --rep-start 2

echo
echo "=== all 7 steps done -- session $SESSION ==="
