#!/bin/bash
#SBATCH --job-name=medwear_e2e
#SBATCH --partition=gpu
#SBATCH --exclusive
#SBATCH --nodes=1
#SBATCH --time=2-00:00:00
#SBATCH --output=%x_%j.out
# End-to-end replay of the chamber recordings through MedWear on an
# exclusively allocated node, as in the platform benchmark (rootless
# Podman, minimal compose subset, no CPU limits, one cgroup per container).
#
#   sbatch ~/medwear_e2e/job_e2e.sbatch               # full protocol (~18 h)
#   E2E_CMD="./replay_chamber.py ..." sbatch ...       # any other command
#
# Resumable: finished runs are skipped. Stop cleanly between runs with
#   touch ~/medwear_e2e/replay/logs/STOP
# Results: ~/medwear_e2e/replay/results, logs: ~/medwear_e2e/replay/logs.
set -euo pipefail
E2E=${E2E:-$HOME/medwear_e2e}
export XDG_RUNTIME_DIR=/run/user/$(id -u) PATH=$HOME/.local/bin:$PATH CONTAINERS_NO_DOCKER_MSG=1
# Keep every container inside this job's cgroup (see containers-slurm.conf)
export CONTAINERS_CONF_OVERRIDE=$E2E/containers-slurm.conf
SOCK=$E2E/run/podman-$SLURM_JOB_ID.sock
COMPOSE="podman-compose -p medwear_e2e -f compose.slurm.yml"
JOBDIR="$E2E/replay/logs/slurm_${SLURM_JOB_ID:-manual}"
mkdir -p "$JOBDIR"

cd "$E2E/medwear"
# compose interpolates ${POSTGRES_PASSWORD}, ${KEYCLOAK_ADMIN}, ... (Keycloak's
# database login) from the environment, as init_server.sh does
set -a; source .env.medwear; source .env; set +a
cleanup() {
    echo "[$(date -Is)] stopping MedWear"
    podman stop -t 30 medwear-replay >/dev/null 2>&1 || true
    $COMPOSE down >/dev/null 2>&1 || true     # volumes are kept
    kill "${SVC_PID:-}" 2>/dev/null || true
    rm -f "$SOCK"
}
trap cleanup EXIT

# Environment record, as the benchmark's start_run.sh
{
    echo "job: ${SLURM_JOB_ID:-} on $(hostname), nodelist ${SLURM_JOB_NODELIST:-}, cpus ${SLURM_CPUS_ON_NODE:-}"
    date -Is; uptime; nproc; free -g
    echo "--- other jobs on the node"; squeue -h -w "$(hostname -s)" -o "%i %u %j %C %M" 2>/dev/null
    echo "--- podman"; podman version
    echo "--- MedWear"; cat DEPLOYED_VERSION.txt
    echo "--- images"; podman images --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.Created}}' | grep -E 'medwear|minio|influx|postgres|keycloak'
} > "$JOBDIR/environment.txt" 2>&1

echo "[$(date -Is)] starting MedWear"
# API socket for the replay container (docker stats/logs/exec), served from
# inside the job; the systemd podman.socket would run in user.slice
mkdir -p "$(dirname "$SOCK")"
# Container names (medwear-api, ...) are fixed by MedWear's compose file and
# shared with any other MedWear setup on this account (e.g. the benchmark):
# never touch another project's containers, and free the names when done.
other=$(podman ps -a --format '{{.Names}} {{index .Labels "io.podman.compose.project"}}' \
        | awk '$1 ~ /^medwear-/ && $2 != "medwear_e2e"')
if [ -n "$other" ]; then echo "MedWear container names taken by another project:"; echo "$other"; exit 1; fi
$COMPOSE down >/dev/null 2>&1 || true      # stale state of a killed job
podman system service --time=0 "unix://$SOCK" &
SVC_PID=$!
$COMPOSE up -d
for i in $(seq 1 120); do
    curl -sk https://127.0.0.1:8443/v1/health | grep -q '^{"status":"ok"' && break
    [ "$i" = 120 ] && { echo "MedWear not healthy after 10 min"; exit 1; }
    sleep 5
done
echo "[$(date -Is)] MedWear healthy"
for c in $(podman ps --format '{{.Names}}'); do
    printf '%-26s %s\n' "$c" "$(podman inspect --format '{{.State.CgroupPath}}' "$c")"
done > "$JOBDIR/cgroups.txt"
cat "$JOBDIR/cgroups.txt"
JOBCG=$(cut -d: -f3 /proc/self/cgroup | sed 's|/step_batch.*||')
if grep -v -F "$JOBCG/" "$JOBDIR/cgroups.txt"; then
    echo "containers above are outside this job's cgroup ($JOBCG); aborting"; exit 1
fi

# No podman stats here: all containers share the job's cgroup, so it would
# report the whole job for each. The replay samples per-container CPU from
# /proc instead (E2E_STATS=proc, needs --pid=host).

rm -f "$E2E/replay/logs/STOP"
echo "[$(date -Is)] replay: ${E2E_CMD:-./run_e2e.sh}"
podman run --rm --name medwear-replay --network host --pid host --security-opt label=disable \
    -e E2E_STATS=proc \
    -v "$SOCK:/run/docker.sock" \
    -v "$E2E/captures:/app/wireless_experiments/captures:ro" \
    -v "$E2E/replay/logs:/app/wireless_experiments/replay/logs" \
    -v "$E2E/replay/results:/app/wireless_experiments/replay/results" \
    -v "$E2E/replay/.replay_credentials:/app/wireless_experiments/replay/.replay_credentials:ro" \
    localhost/medwear-replay:latest ${E2E_CMD:-./run_e2e.sh}
echo "[$(date -Is)] done"
