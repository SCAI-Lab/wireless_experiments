#!/bin/bash
# Build the MedWear and replay images on the SLURM node. Called by
# job_setup.sbatch (never run directly on the node: everything there goes
# through SLURM). Infrastructure images are not pulled here; deploy.sh ships
# them from the laptop and job_setup.sbatch loads them.
set -euo pipefail
E2E=${E2E:-$HOME/medwear_e2e}
PROXY=http://proxy.ethz.ch:3128
export http_proxy=$PROXY https_proxy=$PROXY HTTP_PROXY=$PROXY HTTPS_PROXY=$PROXY
export no_proxy=localhost,127.0.0.1,.ethz.ch NO_PROXY=localhost,127.0.0.1,.ethz.ch
export CONTAINERS_NO_DOCKER_MSG=1

cd "$E2E/medwear"
podman build -q -f docker/medwear-api.Dockerfile -t localhost/medwear_med-api:unpinned .
# MedWear's requirements are unpinned; sqlmodel >= 0.0.48 breaks every insert.
# Pin the versions the platform was developed with (as in the local stack).
printf 'FROM localhost/medwear_med-api:unpinned\nRUN pip install --no-cache-dir "sqlmodel==0.0.38" "pydantic==2.13.4" "SQLAlchemy==2.0.51"\n' \
    | podman build -q -t localhost/medwear_med-api:latest -f - .
podman build -q --build-arg SERVER_HOST=localhost --build-arg SERVER_IP=127.0.0.1 \
    -t localhost/medwear_medwear-nginx:latest docker/nginx
podman build -q -t localhost/medwear_frontend:latest frontend
# DNS for the stack (see make_compose.py): dnsmasq on a digest-pinned Alpine,
# so name service runs as a container inside the job
printf 'FROM docker.io/library/alpine@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc\nRUN apk add --no-cache dnsmasq\n' \
    | podman build -q -t localhost/medwear_dns:latest -f - .

cd "$E2E/build"
podman build -q -f wireless_experiments/replay/deploy/Containerfile.replay \
    -t localhost/medwear-replay:latest .
podman images --format '{{.Repository}}:{{.Tag}} {{.Size}}' | grep -E 'medwear|minio|influxdb|postgres|keycloak'
