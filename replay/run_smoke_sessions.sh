#!/bin/bash
# Short check of a new MedWear deployment before the full replays: one chamber
# run (Polar H10, AFH free) at W = 1 s under its own tag and patient IDs, then
# a summary of the bridge's metrics (wire format, verdicts, failures).
#   E2E_CMD=./run_smoke_sessions.sh sbatch job_e2e.sbatch
set -euo pipefail
cd "$(dirname "$0")"
rm -f logs/STOP
[ -f .replay_credentials ] && source .replay_credentials
: "${PROJECT:?}" "${MEDWEAR_USERNAME:?}" "${MEDWEAR_PASSWORD:?}"
set +u
[ -f /opt/ros/jazzy/setup.bash ] && source /opt/ros/jazzy/setup.bash
set -u
TAG=${1:-smoke_sessions}
C=../captures
python3 replay_chamber.py --tag "$TAG" --project "$PROJECT" --batch-window 1 \
    --patient-suffix "__$TAG" --only '^row2_Polar_H10_duty100pct_rep1$' \
    "$C/20261007_105222_chamber_1"
python3 - "$TAG" <<'EOF'
import collections, glob, json, sys
c = collections.Counter()
for p in glob.glob(f"logs/{sys.argv[1]}/*/*.metrics.jsonl"):
    for line in open(p):
        r = json.loads(line)
        c[f"wire={r.get('wire')}"] += 1
        c[f"ok={r['ok']} status={r.get('result_status')}"] += 1
        c["session_setups"] += r.get("session_setup_s") is not None
for k, v in sorted(c.items()):
    print(f"SMOKE {k}: {v}")
EOF
grep -h "SUMMARY" logs/"$TAG"/*/*.bridge.log
grep -hc "COMMIT OK" logs/"$TAG"/*/*.api.log | sed 's/^/SMOKE api COMMIT OK lines: /'
