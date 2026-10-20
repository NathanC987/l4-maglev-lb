#!/usr/bin/env bash
# AWS as referee: fetches the VPC Flow Logs and CloudWatch EC2 metrics covering a
# measured run, and checks them with cloud/referee.py: the LB sends only GRE and no
# replies, the backends reply from the VIP straight to the client, and maglev-lb's
# own count of forwarded packets equals AWS's count of GRE packets leaving the LB.
#
#   cloud/dsr-proof.sh [WINDOW_FILE] [--wait-min N]
#
# WINDOW_FILE (default .run/window.json) is written by the run scripts with
# mark_window. Flow logs reach CloudWatch a few minutes after the traffic, so
# this polls for up to --wait-min minutes (default 12) before giving up.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

WINDOW="$RUN_DIR/window.json" WAIT_MIN=12
while [[ $# -gt 0 ]]; do
    case "$1" in
        --wait-min) WAIT_MIN=$2; shift 2 ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *) WINDOW=$1; shift ;;
    esac
done
require_tools
load_outputs
[[ -f "$WINDOW" ]] || die "no measured window at $WINDOW (run a cloud/ benchmark or the demo first)"
[[ "$(aws_q ec2 describe-flow-logs --filter "Name=log-group-name,Values=$FlowLogGroup" \
      --query 'length(FlowLogs)')" != "0" ]] \
    || die "flow logs are off for this stack (cloud/up.sh --flow-logs on)"

read -r T0 T1 < <(python3 -c 'import json,sys; w=json.load(open(sys.argv[1])); print(w["start"]["epoch"], w["end"]["epoch"])' "$WINDOW")
DIR="$RUN_DIR/referee-$T0"
mkdir -p "$DIR"
cp "$WINDOW" "$DIR/window.json"
{
    echo "{"
    first=1
    for r in $(roles); do
        [[ $first -eq 1 ]] || echo ","
        first=0
        printf '  "%s": "%s"' "$(instance_id "$r")" "$r"
    done
    echo ""
    echo "}"
} >"$DIR/roles.json"

# Records are delivered per interface, a few minutes late. The LB's interface
# always has traffic (health checks), so a record on it ending after the window
# means everything up to the window's end has arrived.
fetch_flowlogs() {
    aws logs filter-log-events --log-group-name "$FlowLogGroup" \
        --start-time $(((T0 - 300) * 1000)) --end-time $(((T1 + 300) * 1000)) \
        --output json --query '{events: events[].{message: message}}' >"$DIR/flowlogs.json"
}
delivered() {
    python3 - "$DIR/flowlogs.json" "$(instance_id lb)" "$T1" <<'PY'
import json, sys
path, lb, t1 = sys.argv[1], sys.argv[2], int(sys.argv[3])
for e in json.load(open(path)).get("events") or []:
    p = e["message"].split()
    if len(p) == 17 and p[2] == lb and p[16] == "OK" and int(p[14]) >= t1 + 60:
        sys.exit(0)
sys.exit(1)
PY
}
echo "==> Waiting for flow logs covering $(date -d "@$T0" +%T)..$(date -d "@$T1" +%T) (up to ${WAIT_MIN} min)"
deadline=$(($(date +%s) + WAIT_MIN * 60))
until fetch_flowlogs && delivered; do
    [[ $(date +%s) -lt $deadline ]] || { echo "    still incomplete; checking what has arrived"; break; }
    sleep 30
done

echo "==> CloudWatch metrics for the same window"
for r in $(roles); do
    [[ "$r" == "mon" ]] && continue
    period=300
    [[ "$r" == "lb" ]] && period=60   # detailed monitoring on the LB only
    for metric in NetworkOut NetworkPacketsOut; do
        aws cloudwatch get-metric-statistics --namespace AWS/EC2 --metric-name "$metric" \
            --dimensions "Name=InstanceId,Value=$(instance_id "$r")" \
            --start-time "$(date -u -d "@$T0" +%FT%TZ)" --end-time "$(date -u -d "@$T1" +%FT%TZ)" \
            --period "$period" --statistics Sum --output json >"$DIR/cw-$r-$metric.json" || true
    done
done

echo ""
python3 "$CLOUD_DIR/referee.py" "$DIR"
