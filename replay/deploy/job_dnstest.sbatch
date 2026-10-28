#!/bin/bash
#SBATCH --job-name=medwear_dnstest
#SBATCH --partition=gpu
#SBATCH --nodes=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=8G
#SBATCH --time=00:20:00
#SBATCH --output=%x_%j.out
# Check that the MedWear stack runs entirely inside the SLURM job: start it
# with the static-name compose file (no container DNS, see make_compose.py),
# list every process of this account with its cgroup and flag those outside
# the job, check name resolution inside the containers and MedWear's health,
# then tear the stack down (volumes kept).
#
#   sbatch ~/medwear_e2e/job_dnstest.sbatch
set -uo pipefail
E2E=${E2E:-$HOME/medwear_e2e}
export XDG_RUNTIME_DIR=/run/user/$(id -u) PATH=$HOME/.local/bin:$PATH CONTAINERS_NO_DOCKER_MSG=1
export CONTAINERS_CONF_OVERRIDE=$E2E/containers-slurm.conf
COMPOSE="podman-compose -p medwear_e2e -f compose.slurm.yml"
echo "[$(date -Is)] building images (cached)"
( PROXY=http://proxy.ethz.ch:3128; export http_proxy=$PROXY https_proxy=$PROXY HTTP_PROXY=$PROXY HTTPS_PROXY=$PROXY
  export no_proxy=localhost,127.0.0.1,.ethz.ch NO_PROXY=localhost,127.0.0.1,.ethz.ch
  bash "$E2E/build_images.sh" >/dev/null ) || { echo "image build failed"; exit 1; }
cd "$E2E/medwear"
set -a; source .env.medwear; source .env; set +a
JOBCG=$(cut -d: -f3 /proc/self/cgroup | sed 's|/step_batch.*||')
trap '$COMPOSE down >/dev/null 2>&1' EXIT

other=$(podman ps -a --format '{{.Names}} {{index .Labels "io.podman.compose.project"}}' \
        | awk '$1 ~ /^medwear-/ && $2 != "medwear_e2e"')
if [ -n "$other" ]; then echo "MedWear container names taken by another project:"; echo "$other"; exit 1; fi
$COMPOSE down >/dev/null 2>&1 || true
echo "[$(date -Is)] starting MedWear (static names, DNS disabled)"
$COMPOSE up -d 2>&1 | tail -3
ok=0
for i in $(seq 1 60); do
    curl -sk https://127.0.0.1:8443/v1/health | grep -q '^{"status":"ok"' && { ok=1; break; }
    sleep 5
done
echo "health: $(curl -sk https://127.0.0.1:8443/v1/health)"

echo "=== network"
podman network inspect medwear_e2e_medwear-static --format '{{.Name}} dns_enabled={{.DNSEnabled}} subnets={{range .Subnets}}{{.Subnet}} {{end}}'

echo "=== processes of $(id -un) outside this job ($JOBCG)"
n_out=0
for p in $(pgrep -u "$(id -u)"); do
    cg=$(cut -d: -f3 "/proc/$p/cgroup" 2>/dev/null) || continue
    comm=$(ps -o comm= -p "$p" 2>/dev/null) || continue
    case "$cg" in "$JOBCG"/*) ;; *)
        case "$comm" in systemd|"(sd-pam)"|dbus-broker*|catatonit|sshd|bash|sleep|ps|pgrep|cut|awk) continue ;; esac
        printf '  %-8s %-16s %s\n' "$p" "$comm" "$cg"; n_out=$((n_out + 1)) ;;
    esac
done
echo "  ($n_out outside; aardvark-dns running: $(pgrep -u "$(id -u)" -c aardvark-dns))"
echo "=== helpers inside the job"
for c in conmon pasta rootlessport slirp4netns; do echo "  $c: $(pgrep -u "$(id -u)" -c $c)"; done

echo "=== name resolution inside the containers"
for pair in "medwear-nginx medwear-api" "medwear-nginx medwear-frontend" "medwear-api medwear-postgres" \
            "medwear-api medwear-influxdb" "medwear-api medwear-keycloak" "medwear-api medwear-minio"; do
    set -- $pair
    printf '  %-14s -> %-18s %s\n' "$1" "$2" "$(podman exec "$1" getent hosts "$2" 2>&1 | head -1)"
done
for n in medwear-api medwear-grafana; do
    printf '  nginx resolver (dnsmasq) %-16s %s\n' "$n" "$(podman exec medwear-nginx nslookup "$n" 2>&1 | grep -E 'Address|NXDOMAIN|REFUSED|can.t' | tail -1)"
done
echo "  medwear-nginx resolv.conf: $(podman exec medwear-nginx cat /etc/resolv.conf 2>&1 | grep -v '^#' | tr '\n' ' ')"
echo "  medwear-api resolv.conf: $(podman exec medwear-api cat /etc/resolv.conf 2>&1 | grep -v '^#' | tr '\n' ' ')"
echo "=== nginx log (resolve errors)"
podman logs medwear-nginx 2>&1 | grep -iE 'resolv|error' | head -5
echo "[$(date -Is)] result: health ok=$ok, processes outside the job=$n_out"
