#!/bin/bash
# Gym replay for the paper section "End to End through MEDWEAR": all 72
# recordings of the three SPZ gym sessions (Polar H10 and Corsano; AFH free,
# k = 8, 2, 0; three replicates), all bridged streams, W = 1 s as in the
# chamber's full replay (~6 h at rate 1.0). Resumable like run_e2e.sh.
# Analysis runs afterwards on a copy of logs/ (analyze_e2e.py --full-tag gym_W1).
#
# Needs: MEDWEAR_USERNAME, MEDWEAR_PASSWORD (write access to $PROJECT), from the
# environment or from .replay_credentials (see setup_replay_user.sh).
# Usage: ./run_gym.sh [tag]   (default gym_W1; on the SLURM node:
#        E2E_CMD="./run_gym.sh gym_W1" sbatch job_e2e.sbatch). A run on a shared
#        (non-exclusive) node uses its own tag, e.g. gym_W1_shared, so the
#        exclusive run does not skip its recordings.
set -euo pipefail
cd "$(dirname "$0")"
TAG=${1:-gym_W1}

# Stop cleanly between runs with: touch logs/STOP
rm -f logs/STOP
[ -f .replay_credentials ] && source .replay_credentials

: "${PROJECT:?set PROJECT to the MedWear project id, e.g. medwear_wireless_chamber}"
: "${MEDWEAR_USERNAME:?}" "${MEDWEAR_PASSWORD:?}"
C=../captures
SESSIONS="$C/gym_SPZ_20261013_133642 $C/gym_SPZ_20261013_160606 $C/gym_SPZ_20261016_103145"

# ROS 2 + healthcare_msgs (already sourced in the replay container)
set +u
[ -f /opt/ros/jazzy/setup.bash ] && source /opt/ros/jazzy/setup.bash
[ -f "${ROS_WS:-}/install/setup.bash" ] && source "$ROS_WS/install/setup.bash"
set -u

# Another tag also gets its own patient IDs, so MedWear never answers the
# main run's batches as duplicates of an earlier run's
SUFFIX=""; [ "$TAG" = gym_W1 ] || SUFFIX="__$TAG"
python3 replay_chamber.py --tag "$TAG" --project "$PROJECT" --batch-window 1 \
    --patient-suffix "$SUFFIX" $SESSIONS
echo "Done: logs/$TAG"
