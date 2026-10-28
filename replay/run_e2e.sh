#!/bin/bash
# Full end-to-end protocol for the paper section "End to End through MEDWEAR"
# (~18 h at rate 1.0). Resumable: re-run the same command after an
# interruption and finished runs are skipped.
#
#   1. full replay: all 120 chamber runs, all bridged streams, W = 1 s  (~12 h)
#   2. W sweep: Polar ECG only, Table III conditions (AFH free 100 %;
#      k = 8, 4, 2, 0 at 75 %, k = 0 pooled with the confined runs as in
#      Table II; 22 runs), W = 0.1, 0.5, 1, 5 s                        (~8 h)
#   3. analysis -> results/report.md, table3.tex, CSVs
#
# Needs: MEDWEAR_USERNAME, MEDWEAR_PASSWORD (write access to $PROJECT), from the
# environment or from .replay_credentials (see setup_replay_user.sh).
# Usage: ./run_e2e.sh [name]
#   name: a repeat of the protocol, e.g. dns -> tags full_W1_dns, sweep_W0.1_dns,
#   ... with their own patient IDs. MedWear keeps what it stored, so a repeat
#   under the original IDs would be answered as duplicates and never committed.
#   A named run skips step 3 (analyse a copy of logs/ on the laptop).
set -euo pipefail
cd "$(dirname "$0")"

# Keep the laptop awake for the whole protocol: a suspend stalls the
# real-time replay mid-run (re-exec once under systemd-inhibit).
if [ -z "${E2E_INHIBITED:-}" ] && command -v systemd-inhibit >/dev/null; then
    export E2E_INHIBITED=1
    exec systemd-inhibit --what=sleep:idle:handle-lid-switch --who="medwear replay" \
        --why="run_e2e.sh chamber replay into MedWear" --mode=block "$0" "$@"
fi
# Stop cleanly between runs with: touch logs/STOP
rm -f logs/STOP
# credentials written by setup_replay_user.sh
[ -f .replay_credentials ] && source .replay_credentials

: "${PROJECT:?set PROJECT to the MedWear project id, e.g. medwear_wireless_chamber}"
: "${MEDWEAR_USERNAME:?}" "${MEDWEAR_PASSWORD:?}"
C=../captures
SESSIONS="$C/20261007_105222_chamber_1 $C/20261008_010249_chamber_2"
TABLE_RUNS='^row(2_Polar_H10_duty100pct|5_Polar_H10_headroom_k[0248]_duty75pct|3_Polar_H10_forced_channel_duty75pct)_rep[0-9]+$'
RUN=${1:-}
T=""; [ -n "$RUN" ] && T="_$RUN"           # tag and patient-ID suffix
FULL_SUFFIX=""; [ -n "$RUN" ] && FULL_SUFFIX="__full_W1$T"

# ROS 2 + healthcare_msgs (already sourced in the replay container)
set +u
[ -f /opt/ros/jazzy/setup.bash ] && source /opt/ros/jazzy/setup.bash
[ -f "${ROS_WS:-}/install/setup.bash" ] && source "$ROS_WS/install/setup.bash"
set -u

# The API's /data/batch limit stays at the production default (300/minute per
# user): the busiest minute of the replay used 225 requests. Recreate med-api
# if an earlier experiment left a raised BATCH_RATE_LIMIT in place.
# (Skipped when the stack is managed externally, e.g. by the SLURM job.)
if [ -z "${E2E_MANAGED_STACK:-}" ] && \
   [ "$(docker exec medwear-api printenv BATCH_RATE_LIMIT 2>/dev/null)" != "300/minute" ]; then
    (cd ../../medwear && BATCH_RATE_LIMIT=300/minute \
        docker compose -f docker-compose.medwear-minimal.yml up -d --no-build med-api)
    sleep 15
fi

python3 replay_chamber.py --tag "full_W1$T" --project "$PROJECT" --batch-window 1 \
    --patient-suffix "$FULL_SUFFIX" $SESSIONS

for W in 0.1 0.5 1 5; do
    python3 replay_chamber.py --tag "sweep_W$W$T" --project "$PROJECT" --batch-window "$W" \
        --patient-suffix "__W$W$T" --bridge-config sweep_ecg_config.yaml \
        --only "$TABLE_RUNS" $SESSIONS
done

[ -n "$RUN" ] && { echo "Done: logs/*$T (analyse on the laptop)"; exit 0; }
python3 analyze_e2e.py --full-tag full_W1 \
    --sweep-tags sweep_W0.1 sweep_W0.5 sweep_W1 sweep_W5 --table-tag sweep_W0.1 \
    --project "$PROJECT" --check-stores $SESSIONS
echo "Done: results/report.md"
