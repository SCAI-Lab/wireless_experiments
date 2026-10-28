#!/bin/bash
#SBATCH --job-name=medwear_setup
#SBATCH --partition=gpu
#SBATCH --nodes=1
#SBATCH --exclusive
#SBATCH --time=04:00:00
#SBATCH --output=%x_%j.out
# Prepare the SLURM node for job_e2e.sbatch, after each deploy.sh: load the
# infrastructure images shipped from the laptop, build the MedWear and replay
# images, and initialise MedWear (credentials, project, replay user).
# Safe to re-run: unchanged images come from the layer cache, and
# init_server.sh keeps existing credentials, project and users.
#
#   sbatch ~/medwear_e2e/job_setup.sbatch             # submitted by deploy.sh
set -euo pipefail
E2E=${E2E:-$HOME/medwear_e2e}
export XDG_RUNTIME_DIR=/run/user/$(id -u) PATH=$HOME/.local/bin:$PATH CONTAINERS_NO_DOCKER_MSG=1
# Keep every container (builds included) inside this job's cgroup
export CONTAINERS_CONF_OVERRIDE=$E2E/containers-slurm.conf
# The node reaches the internet only through the ETH proxy
PROXY=http://proxy.ethz.ch:3128
export http_proxy=$PROXY https_proxy=$PROXY HTTP_PROXY=$PROXY HTTPS_PROXY=$PROXY
export no_proxy=localhost,127.0.0.1,.ethz.ch NO_PROXY=localhost,127.0.0.1,.ethz.ch

echo "[$(date -Is)] loading infrastructure images"
# MinIO: not pullable any more. podman load names docker-saved images
# localhost/<repo>; compose.slurm.yml expects docker.io/<repo>.
podman load -q -i "$E2E/images/infra_images.tar.gz" | sed -n 's|^Loaded image: localhost/||p' \
    | while read -r img; do podman tag "localhost/$img" "docker.io/$img"; done
while read -r ref tag; do                                   # the rest: by digest
    podman pull -q "$ref" >/dev/null
    podman tag "$ref" "$tag"
done < "$E2E/images/infra_pull.txt"
# Preflight: a container must land in this job's cgroup and podman stats
# must work (the replay records per-container CPU from it)
echo "[$(date -Is)] preflight"
JOBCG=$(cut -d: -f3 /proc/self/cgroup | sed 's|/step_batch.*||')
podman rm -f medwear-preflight >/dev/null 2>&1 || true
podman run -d --name medwear-preflight docker.io/library/postgres:15-alpine sleep 120 >/dev/null
CG=$(cut -d: -f3 "/proc/$(podman inspect --format '{{.State.Pid}}' medwear-preflight)/cgroup")
echo "job cgroup: $JOBCG"; echo "container cgroup: $CG"
podman stats --no-stream --format "{{.Name}} {{.CPUPerc}} {{.MemUsage}}" medwear-preflight
podman rm -f medwear-preflight >/dev/null
case "$CG" in "$JOBCG"/*) echo "preflight OK" ;; *) echo "preflight FAILED: container outside the job"; exit 1 ;; esac

echo "[$(date -Is)] building images"
bash "$E2E/build_images.sh"
echo "[$(date -Is)] initialising MedWear"
bash "$E2E/init_server.sh"
echo "[$(date -Is)] setup done"
