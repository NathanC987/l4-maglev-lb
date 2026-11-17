#!/usr/bin/env bash
# The live canvas driving real EC2 instances (bridge/cloud_bridge.py). Adding a backend
# on the canvas starts an instance; removing it stops one. See docs/cloud.md.
#
#   cloud/canvas.sh up      create the stack (2 clients, lb, 5 backends), install and build
#                           on every host, then stop them all. Re-run to ship new code.
#   cloud/canvas.sh run [--budget-usd N] [--max-minutes N] [--idle-stop-min N]
#                           start the bridge and print the link. Nothing on AWS starts
#                           until that page is open, and everything stops when it exits.
#   cloud/canvas.sh stop    stop every instance (the bridge normally does this itself)
#   cloud/canvas.sh down    delete the whole stack, volumes included
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_tools

CANVAS_KNOWN_HOSTS="$RUN_DIR/known_hosts_canvas"

instance_ids() {
    aws_q ec2 describe-instances \
        --filters "Name=tag:l4mlb-canvas,Values=$STACK" "Name=instance-state-name,Values=$1" \
        --query 'Reservations[].Instances[].InstanceId'
}

start_all() {
    local ids
    ids="$(instance_ids stopped)"
    # shellcheck disable=SC2086
    [[ -n "$ids" ]] && aws_q ec2 start-instances --instance-ids $ids >/dev/null
    ids="$(instance_ids pending,running)"
    # shellcheck disable=SC2086
    aws ec2 wait instance-running --instance-ids $ids
}

stop_all() {
    local ids
    ids="$(instance_ids pending,running,stopping)"
    [[ -n "$ids" ]] || return 0
    # shellcheck disable=SC2086
    aws_q ec2 wait instance-running --instance-ids $(instance_ids pending) 2>/dev/null || true
    # shellcheck disable=SC2086
    aws_q ec2 stop-instances --instance-ids $ids >/dev/null
    # shellcheck disable=SC2086
    aws ec2 wait instance-stopped --instance-ids $ids
}

# The bridge verifies host keys by instance id, since a stop and start changes each
# host's public address. This is the one moment they are taken on trust.
pin_host_keys() {
    local r
    : >"$CANVAS_KNOWN_HOSTS"
    chmod 600 "$CANVAS_KNOWN_HOSTS"
    for r in $(roles); do
        ssh -n -F /dev/null -i "$SSH_KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=20 \
            -o StrictHostKeyChecking=accept-new -o "UserKnownHostsFile=$CANVAS_KNOWN_HOSTS" \
            -o "HostKeyAlias=$(instance_id "$r")" -o LogLevel=ERROR \
            "ubuntu@$(public_ip "$r")" true || die "could not reach $r to record its host key"
    done
}

cmd_up() {
    "$CLOUD_DIR/up.sh" --profile canvas --flow-logs off --cpu-credits unlimited --auto-terminate 90 --no-deploy
    echo "==> Starting every instance to install on it"
    start_all
    refresh_outputs
    export AWS_DEFAULT_REGION="$Region"
    rm -f "$RUN_DIR/known_hosts"
    "$CLOUD_DIR/deploy.sh"
    refresh_outputs
    echo "==> Recording host keys"
    pin_host_keys
    echo "==> Stopping every instance (only their disks cost anything now)"
    stop_all
    echo "Ready. Next: cloud/canvas.sh run"
}

cmd_run() {
    local dist="${L4MLB_CANVAS_DIST:-$REPO_ROOT/../breakscale/dist-live}"
    [[ -f "$dist/index.html" ]] \
        || die "no frontend at $dist: (cd ../breakscale && npm ci && npm run build:live), or set L4MLB_CANVAS_DIST"
    [[ "$(stack_status)" == *_COMPLETE && "$(stack_status)" != DELETE_* ]] \
        || die "stack $STACK is not up: cloud/canvas.sh up"
    # Your public IP may have changed since the last run; this is a no-op if it hasn't.
    "$CLOUD_DIR/up.sh" --no-deploy >/dev/null
    refresh_outputs
    [[ "${Profile:-}" == "canvas" ]] \
        || die "stack $STACK has profile ${Profile:-?}, not canvas: cloud/canvas.sh up"
    [[ -s "$CANVAS_KNOWN_HOSTS" ]] || die "no recorded host keys: cloud/canvas.sh up"
    exec python3 "$REPO_ROOT/bridge/cloud_bridge.py" --stack "$STACK" --region "$Region" \
        --role-arn "$CanvasOperatorRoleArn" --ssh-key "$SSH_KEY" --cloud-run-dir "$RUN_DIR" \
        --static-dir "$dist" "$@"
}

cmd="${1:-}"
[[ $# -gt 0 ]] && shift
case "$cmd" in
    up) cmd_up ;;
    run) cmd_run "$@" ;;
    stop) stop_all; "$CLOUD_DIR/status.sh" ;;
    down) "$CLOUD_DIR/down.sh"; rm -f "$CANVAS_KNOWN_HOSTS" ;;
    *) sed -n '2,12p' "$0"; exit 2 ;;
esac
