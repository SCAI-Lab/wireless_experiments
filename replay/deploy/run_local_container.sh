#!/bin/bash
# Run the full end-to-end protocol on this laptop inside the deployment
# replay container (the same image job_e2e.sbatch uses on the SLURM node),
# against the local MedWear stack, with its own project (from
# .replay_credentials_ctr), logs_ctr/ and results_ctr/.
#
#   deploy/run_local_container.sh            # start (detached), resumable
#   touch logs_ctr/STOP                      # stop cleanly between runs
#   tail -f logs_ctr/run.log
set -euo pipefail
cd "$(dirname "$0")/.."
REPLAY=$(pwd)
WS=$(cd ../.. && pwd)
HCMSGS=${HCMSGS:-${ROS_WS:?set ROS_WS or HCMSGS}/src/healthcare_msgs}
CTX=$(mktemp -d)
trap 'rm -rf "$CTX"' EXIT

# Build context laid out as on the server (deploy.sh), without secrets
mkdir -p "$CTX/medwear" "$CTX/wireless_experiments/scripts" logs_ctr results_ctr
rsync -a --exclude .git --exclude KEY_gpg "$HCMSGS/" "$CTX/healthcare_msgs/"
rsync -a --exclude __pycache__ --exclude .mypy_cache "$WS/medwear/ros2_to_medwear/" "$CTX/medwear/ros2_to_medwear/"
cp "$WS/wireless_experiments/scripts/analyze_session.py" "$CTX/wireless_experiments/scripts/"
rsync -a --exclude 'logs*' --exclude 'results*' --exclude '.replay_*' --exclude __pycache__ \
    "$REPLAY/" "$CTX/wireless_experiments/replay/"
docker build --network=host -q -f "$CTX/wireless_experiments/replay/deploy/Containerfile.replay" \
    -t medwear-replay:local "$CTX" >/dev/null
echo "image medwear-replay:local built"

docker rm -f medwear-replay-ctr >/dev/null 2>&1 || true
rm -f logs_ctr/STOP
setsid nohup systemd-inhibit --what=sleep:idle:handle-lid-switch --who="medwear replay (container)" \
    --why="end-to-end replay in the deployment container" --mode=block \
    docker run --rm --name medwear-replay-ctr --network host \
        -v /var/run/docker.sock:/run/docker.sock \
        -v "$WS/wireless_experiments/captures:/app/wireless_experiments/captures:ro" \
        -v "$REPLAY/logs_ctr:/app/wireless_experiments/replay/logs" \
        -v "$REPLAY/results_ctr:/app/wireless_experiments/replay/results" \
        -v "$REPLAY/.replay_credentials_ctr:/app/wireless_experiments/replay/.replay_credentials:ro" \
        -e API_BASE_URL=https://127.0.0.1/v1 \
        medwear-replay:local ./run_e2e.sh >> logs_ctr/run.log 2>&1 < /dev/null &
echo "started; follow with: tail -f $REPLAY/logs_ctr/run.log"
