#!/bin/bash
# Initialisation of MedWear on the SLURM node, run by job_setup.sbatch.
# Never run directly on the node: everything there goes through SLURM.
#
#  1. fresh credentials (.env.medwear, .env, .credentials.txt) using
#     setup.sh's own generation code (setup.sh itself needs Python 3.10 and
#     starts all 14 services, so it is not run as a whole);
#  2. starts the benchmark subset of the stack (compose.slurm.yml) and
#     initialises InfluxDB and the Keycloak database;
#  3. creates a local global_admin (replay_admin) with Keycloak's admin CLI,
#     the project and the replay user (role researcher);
#  4. stops the stack again; the SLURM job starts it for each run.
# Safe to re-run: existing credentials, project and users are kept.
set -euo pipefail
E2E=${E2E:-$HOME/medwear_e2e}
PROJECT=${PROJECT:-medwear_wireless_chamber}
API=https://127.0.0.1:8443/v1
export XDG_RUNTIME_DIR=/run/user/$(id -u) PATH=$HOME/.local/bin:$PATH CONTAINERS_NO_DOCKER_MSG=1
COMPOSE="podman-compose -p medwear_e2e -f compose.slurm.yml"
# job_setup.sbatch sets the ETH proxy for pulls and builds; podman copies
# proxy variables into containers, and med-api would then send its calls to
# keycloak/minio through the proxy (HTTP 503). The stack needs no proxy.
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
cd "$E2E/medwear"

if [ ! -f .env.medwear ]; then
    echo "== generating credentials"
    { sed -n '/^ok()/p;/^run()/p' setup.sh; sed -n '/^gen() {/,/^ok "Credentials saved/p' setup.sh; } > /tmp/medwear_gen_$$.sh
    SERVER_HOST=localhost SERVER_IP=127.0.0.1 bash /tmp/medwear_gen_$$.sh
    rm -f /tmp/medwear_gen_$$.sh
    chmod 600 .env .env.medwear .credentials.txt
fi
# med-api reads only .env.medwear but needs Keycloak's admin login, which
# setup.sh's generator writes only to .env (the laptop's .env.medwear has it)
for v in KEYCLOAK_ADMIN KEYCLOAK_ADMIN_PASSWORD; do
    grep -q "^$v=" .env.medwear || grep "^$v=" .env >> .env.medwear
done
set -a; source .env.medwear; source .env; set +a
# setup.sh writes the InfluxDB admin password only to .credentials.txt
INFLUX_PASSWORD=$(sed -n 's|^InfluxDB *: admin / ||p' .credentials.txt)
[ -n "$INFLUX_PASSWORD" ] || { echo "no InfluxDB password in .credentials.txt"; exit 1; }

echo "== starting stack"
# no podman.socket here: systemd would start its containers in user.slice
# Container names (medwear-api, ...) are fixed by MedWear's compose file and
# shared with any other MedWear setup on this account (e.g. the benchmark):
# never touch another project's containers, and free the names when done.
other=$(podman ps -a --format '{{.Names}} {{index .Labels "io.podman.compose.project"}}' \
        | awk '$1 ~ /^medwear-/ && $2 != "medwear_e2e"')
if [ -n "$other" ]; then echo "MedWear container names taken by another project:"; echo "$other"; exit 1; fi
$COMPOSE down >/dev/null 2>&1 || true      # stale state of a killed job
$COMPOSE up -d
until podman exec medwear-postgres pg_isready -q -U "$POSTGRES_USER" 2>/dev/null; do sleep 2; done
# keycloak-db-setup (retrying, see make_compose.py) creates it before Keycloak starts
podman exec medwear-postgres psql -U "$POSTGRES_USER" -d postgres -Atc \
    "select 1 from pg_database where datname='keycloak'" | grep -q 1 \
    || { echo "keycloak database missing"; exit 1; }
podman exec medwear-influxdb influx setup --username admin --password "$INFLUX_PASSWORD" \
    --org medwear --bucket wearables --token "$INFLUX_TOKEN" --force >/dev/null 2>&1 || true

echo "== waiting for the API"
for i in $(seq 1 90); do
    curl -sk "$API/health" | grep -q '^{"status":"ok"' && break
    [ "$i" = 90 ] && { echo "API not healthy after 7.5 min; med-api log:"; podman logs --tail 30 medwear-api; exit 1; }
    sleep 5
done
curl -sk "$API/health"; echo

echo "== users and project"
KC="podman exec medwear-keycloak /opt/keycloak/bin/kcadm.sh"
podman exec medwear-keycloak sh -c '/opt/keycloak/bin/kcadm.sh config credentials --server http://localhost:8080 \
    --realm master --user "$KEYCLOAK_ADMIN" --password "$KEYCLOAK_ADMIN_PASSWORD"' >/dev/null
gen() { python3 -c "import secrets; print(secrets.token_urlsafe(18))"; }
cd "$E2E/replay"
if [ ! -f .replay_admin_credentials ]; then
    APW=$(gen)
    $KC create users -r medwear -s username=replay_admin -s enabled=true >/dev/null 2>&1 || true
    $KC set-password -r medwear --username replay_admin --new-password "$APW"
    $KC add-roles -r medwear --uusername replay_admin --cclientid med-api --rolename global_admin
    (umask 077; printf 'export ADMIN_USERNAME=replay_admin\nexport ADMIN_PASSWORD=%s\n' "$APW" > .replay_admin_credentials)
fi
if [ ! -f .replay_credentials ]; then
    source .replay_admin_credentials
    TOKEN=$(curl -sk -X POST "$API/data/auth/token" -H 'Content-Type: application/json' \
        -d "{\"username\":\"$ADMIN_USERNAME\",\"password\":\"$ADMIN_PASSWORD\"}" \
        | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
    curl -sk -o /dev/null -w "create project: %{http_code}\n" -X POST "$API/data/admin/projects" \
        -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d "{\"project_name\":\"$PROJECT\"}"
    PW=$(gen)
    code=$(curl -sk -o /dev/null -w '%{http_code}' -X POST "$API/data/admin/users" \
        -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
        -d "{\"username\":\"wireless_replay\",\"password\":\"$PW\",\"project_id\":\"$PROJECT\",\"role\":\"researcher\"}")
    echo "create user: $code"
    if [ "$code" != 201 ]; then
        # MedWear's create_user can fail after creating the Keycloak user
        # (timezone bug in the user_project insert): finish it by hand.
        $KC set-password -r medwear --username wireless_replay --new-password "$PW"
        $KC add-roles -r medwear --uusername wireless_replay --cclientid med-api --rolename researcher
        podman exec medwear-postgres psql -U "$POSTGRES_USER" -d "$PROJECT" -Atc \
            "INSERT INTO user_project (username, project_id, role) SELECT 'wireless_replay','$PROJECT','researcher'
             WHERE NOT EXISTS (SELECT 1 FROM user_project WHERE username='wireless_replay')"
    fi
    (umask 077; printf 'export MEDWEAR_USERNAME=wireless_replay\nexport MEDWEAR_PASSWORD=%s\nexport PROJECT=%s\n' \
        "$PW" "$PROJECT" > .replay_credentials)
fi
source .replay_credentials
curl -sk -o /dev/null -w "replay user login: %{http_code}\n" -X POST "$API/data/auth/token" \
    -H 'Content-Type: application/json' -d "{\"username\":\"$MEDWEAR_USERNAME\",\"password\":\"$MEDWEAR_PASSWORD\"}"

echo "== per-container cgroups"
for c in $(podman ps --format '{{.Names}}'); do
    printf '  %-26s %s\n' "$c" "$(podman inspect --format '{{.State.CgroupPath}}' "$c")"
done

cd "$E2E/medwear" && $COMPOSE down >/dev/null      # volumes are kept
echo "Initialised. Submit with: sbatch $E2E/job_e2e.sbatch"
