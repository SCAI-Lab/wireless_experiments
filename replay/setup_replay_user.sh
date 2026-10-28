#!/bin/bash
# Create the MedWear project and the replay user (role "researcher": read +
# write within the project) through MedWear's admin API, using an existing
# global_admin login. The new user's credentials are written to
# .replay_credentials (mode 600, sourced by run_e2e.sh if present).
#
# Usage:
#   ADMIN_USERNAME=<global_admin user> ADMIN_PASSWORD=<pw> ./setup_replay_user.sh [project] [username]
set -euo pipefail
cd "$(dirname "$0")"

API=${API_BASE_URL:-https://localhost/v1}
PROJECT=${1:-medwear_wireless_chamber}
USERNAME=${2:-wireless_replay}
: "${ADMIN_USERNAME:?}" "${ADMIN_PASSWORD:?}"

TOKEN=$(curl -sk -X POST "$API/data/auth/token" -H 'Content-Type: application/json' \
    -d "$(python3 -c 'import json,os;print(json.dumps({"username":os.environ["ADMIN_USERNAME"],"password":os.environ["ADMIN_PASSWORD"]}))')" \
    | python3 -c 'import sys,json;print(json.load(sys.stdin).get("access_token",""))')
[ -n "$TOKEN" ] || { echo "Login as $ADMIN_USERNAME failed"; exit 1; }

echo "Creating project $PROJECT ..."
code=$(curl -sk -o /tmp/setup_project.$$ -w '%{http_code}' -X POST "$API/data/admin/projects" \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d "{\"project_name\":\"$PROJECT\"}")
case $code in
    201) echo "  created" ;;
    409) echo "  already exists" ;;
    *)   echo "  failed ($code): $(cat /tmp/setup_project.$$)"; rm -f /tmp/setup_project.$$; exit 1 ;;
esac
rm -f /tmp/setup_project.$$

PASSWORD=$(python3 -c 'import secrets;print(secrets.token_urlsafe(18))')
echo "Creating user $USERNAME (researcher) in $PROJECT ..."
code=$(curl -sk -o /tmp/setup_user.$$ -w '%{http_code}' -X POST "$API/data/admin/users" \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d "{\"username\":\"$USERNAME\",\"password\":\"$PASSWORD\",\"project_id\":\"$PROJECT\",\"role\":\"researcher\"}")
if [ "$code" != 201 ]; then
    echo "  failed ($code): $(cat /tmp/setup_user.$$)"; rm -f /tmp/setup_user.$$; exit 1
fi
rm -f /tmp/setup_user.$$

umask 077
cat > .replay_credentials <<EOF
export MEDWEAR_USERNAME=$USERNAME
export MEDWEAR_PASSWORD=$PASSWORD
export PROJECT=$PROJECT
EOF
echo "Saved credentials to $(pwd)/.replay_credentials"
