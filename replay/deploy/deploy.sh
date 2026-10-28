#!/bin/bash
# Copy MedWear, the replay tooling, the chamber recordings and the
# infrastructure images to the SLURM node, then submit job_setup.sbatch,
# which builds the images and initialises MedWear there. Nothing runs on the
# node outside SLURM: this script only copies files and submits the job.
# Run on the laptop, from anywhere. Re-running only transfers what changed.
#
#   TARGET=user@host ./deploy.sh     # the SLURM login node
#   SKIP_SETUP=1 ./deploy.sh         # copy only, submit nothing
#
# Server layout (~/medwear_e2e):
#   medwear/     MedWear working tree (no secrets) + compose.slurm.yml
#   build/       context for the replay image
#   captures/    the two chamber sessions (bags, btsnoop, manifests)
#   images/      infra_images.tar.gz (minio, mc), infra_pull.txt (the rest, by digest)
#   replay/      logs/, results/, .replay_credentials (created by init_server.sh)
#   build_images.sh, init_server.sh, job_setup.sbatch, job_e2e.sbatch
set -euo pipefail

TARGET=${TARGET:?set TARGET=user@host (the SLURM login node)}
REMOTE=${REMOTE:-medwear_e2e}
WS=$(cd "$(dirname "$0")/../../.." && pwd)         # the workspace holding medwear/, wireless_experiments/
HCMSGS=${HCMSGS:-${ROS_WS:?set ROS_WS or HCMSGS}/src/healthcare_msgs}
# Platform versions are pinned: every run the paper reports uses these, and a
# different version means rerunning everything (see ../RUNS.md). The trees are
# exported from git, so local uncommitted or later commits never reach the node.
MEDWEAR_COMMIT=${MEDWEAR_COMMIT:-49be39f62da5f0d212b29c894054d6b8ceb5ba29}
HCMSGS_COMMIT=${HCMSGS_COMMIT:-c9de02f422e6ac22d8080350ac7a459f4dca7d1a}
SESSIONS=(20261007_105222_chamber_1 20261008_010249_chamber_2
          gym_SPZ_20261013_133642 gym_SPZ_20261013_160606 gym_SPZ_20261016_103145)
RS="rsync -az --delete --info=stats1"
DEPLOY=$WS/wireless_experiments/replay/deploy

SRC=$(mktemp -d)
trap 'rm -rf "$SRC"' EXIT
mkdir -p "$SRC/medwear" "$SRC/healthcare_msgs"
git -C "$WS/medwear" archive "$MEDWEAR_COMMIT" | tar -x -C "$SRC/medwear"
git -C "$HCMSGS" archive "$HCMSGS_COMMIT" | tar -x -C "$SRC/healthcare_msgs"
echo "== pinned: MedWear $MEDWEAR_COMMIT, healthcare_msgs $HCMSGS_COMMIT"

echo "== generating compose.slurm.yml"
python3 "$DEPLOY/make_compose.py" "$SRC/medwear" "$DEPLOY/compose.slurm.yml"

ssh "$TARGET" "mkdir -p $REMOTE/{medwear,build/medwear,build/wireless_experiments/scripts,captures,images,replay/logs,replay/results}"

echo "== MedWear (pinned commit; server-side secrets are excluded from --delete)"
$RS --exclude '.env' --exclude '.env.medwear' --exclude '.credentials.txt' --exclude 'venv/' \
    --exclude '.mypy_cache/' --exclude '__pycache__/' --exclude 'node_modules/' \
    "$SRC/medwear/" "$TARGET:$REMOTE/medwear/"
scp -q "$DEPLOY/compose.slurm.yml" "$TARGET:$REMOTE/medwear/"
git -C "$WS/medwear" log -1 --format='%H %s' "$MEDWEAR_COMMIT" > /tmp/medwear_version.txt
echo "healthcare_msgs: $(git -C "$HCMSGS" log -1 --format='%H %s' "$HCMSGS_COMMIT")" >> /tmp/medwear_version.txt
scp -q /tmp/medwear_version.txt "$TARGET:$REMOTE/medwear/DEPLOYED_VERSION.txt"

echo "== replay image context"
$RS "$SRC/healthcare_msgs/" "$TARGET:$REMOTE/build/healthcare_msgs/"
$RS --exclude '__pycache__/' --exclude '.mypy_cache/' \
    "$SRC/medwear/ros2_to_medwear/" "$TARGET:$REMOTE/build/medwear/ros2_to_medwear/"
scp -q "$WS/wireless_experiments/scripts/analyze_session.py" "$TARGET:$REMOTE/build/wireless_experiments/scripts/"
# --delete-excluded also clears anything an older exclude list let through
$RS --delete-excluded --exclude 'logs*/' --exclude 'results*/' --exclude '.replay_*' \
    --exclude '__pycache__/' --exclude '.mypy_cache/' \
    "$WS/wireless_experiments/replay/" "$TARGET:$REMOTE/build/wireless_experiments/replay/"
scp -q "$DEPLOY/"{build_images.sh,init_server.sh,job_setup.sbatch,job_e2e.sbatch,job_dnstest.sbatch,containers-slurm.conf} "$TARGET:$REMOTE/"

echo "== chamber recordings"
for s in "${SESSIONS[@]}"; do
    $RS --exclude '*.zip' "$WS/wireless_experiments/captures/$s/" "$TARGET:$REMOTE/captures/$s/"
done

# Infrastructure images, pinned to the exact versions the local runs used.
# MinIO's images are no longer pullable (Docker Hub and quay.io), so they are
# shipped from the laptop as a tarball (cached here; rsync sends it only when
# it changed). The others are pulled on the node by digest (infra_pull.txt).
# job_setup.sbatch loads and pulls them.
echo "== infrastructure images"
SHIP=(minio/minio:latest minio/mc:latest)
PULL=(influxdb:2.7 postgres:15-alpine keycloak/keycloak:23.0)
CACHE=${XDG_CACHE_HOME:-$HOME/.cache}/medwear_e2e
mkdir -p "$CACHE"
IDS=$(docker image inspect --format '{{.Id}}' "${SHIP[@]}")
if [ ! -f "$CACHE/infra_images.tar.gz" ] || [ "$IDS" != "$(cat "$CACHE/infra_images.ids" 2>/dev/null)" ]; then
    docker save "${SHIP[@]}" | gzip -1 -n > "$CACHE/infra_images.tar.gz.tmp"
    mv "$CACHE/infra_images.tar.gz.tmp" "$CACHE/infra_images.tar.gz"
    echo "$IDS" > "$CACHE/infra_images.ids"
fi
for img in "${PULL[@]}"; do      # "<repo>@<digest> <repo>:<tag>", fully qualified
    q=docker.io/$img; [[ $img == */* ]] || q=docker.io/library/$img
    echo "${q%:*}@$(docker image inspect --format '{{index .RepoDigests 0}}' "$img" | cut -d@ -f2) $q"
done > "$CACHE/infra_pull.txt"
rsync -a --info=stats1 "$CACHE/infra_images.tar.gz" "$CACHE/infra_pull.txt" "$TARGET:$REMOTE/images/"

[ "${SKIP_SETUP:-0}" = 1 ] && { echo "SKIP_SETUP=1: copied only; submit with: sbatch ~/$REMOTE/job_setup.sbatch"; exit 0; }

echo "== submitting the setup job"
JOB=$(ssh -n "$TARGET" "cd $REMOTE && sbatch --parsable job_setup.sbatch")
echo "submitted job $JOB; log: ~/$REMOTE/medwear_setup_$JOB.out"
echo "When it has finished: ssh $TARGET 'cd $REMOTE && sbatch job_e2e.sbatch'"
