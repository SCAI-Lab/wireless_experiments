#!/bin/bash
# W = 0.1 s durable path for the five confined Polar H10 runs at 75 % load
# (row3_Polar_H10_forced_channel_duty75pct: chamber_1 rep1-3, chamber_2 rep1-2),
# so the k = 0 row of the replay table covers the same ten runs as Table II
# (k = 0 and confined use the same channel map and are pooled there). Same
# bridge config, window and patient suffix as run_e2e.sh's sweep_W0.1, into the
# same tag: the 17 runs already there are not touched (--only). ~27 min.
#
# Needs: MEDWEAR_USERNAME, MEDWEAR_PASSWORD (write access to $PROJECT), from the
# environment or from .replay_credentials (see setup_replay_user.sh).
# Usage: ./run_confined_W01.sh   (on the SLURM node:
#        E2E_CMD=./run_confined_W01.sh sbatch job_e2e.sbatch)
set -euo pipefail
cd "$(dirname "$0")"

# Stop cleanly between runs with: touch logs/STOP
rm -f logs/STOP
[ -f .replay_credentials ] && source .replay_credentials

: "${PROJECT:?set PROJECT to the MedWear project id, e.g. medwear_wireless_chamber}"
: "${MEDWEAR_USERNAME:?}" "${MEDWEAR_PASSWORD:?}"
C=../captures
SESSIONS="$C/20261007_105222_chamber_1 $C/20261008_010249_chamber_2"
CONFINED_RUNS='^row3_Polar_H10_forced_channel_duty75pct_rep[0-9]+$'

# ROS 2 + healthcare_msgs (already sourced in the replay container)
set +u
[ -f /opt/ros/jazzy/setup.bash ] && source /opt/ros/jazzy/setup.bash
[ -f "${ROS_WS:-}/install/setup.bash" ] && source "$ROS_WS/install/setup.bash"
set -u

python3 replay_chamber.py --tag sweep_W0.1 --project "$PROJECT" --batch-window 0.1 \
    --patient-suffix "__W0.1" --bridge-config sweep_ecg_config.yaml \
    --only "$CONFINED_RUNS" $SESSIONS
echo "Done: logs/sweep_W0.1 (confined runs)"
