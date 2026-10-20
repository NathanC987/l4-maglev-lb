#!/usr/bin/env bash
# Shows whether anything from this project is still running in the account's
# current region: the stack, and any instance tagged by it in any live state
# (including ones left behind by a failed delete).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_tools

echo "Region: $(aws configure get region || echo "${AWS_DEFAULT_REGION:-?}")"
echo "Stack $STACK: $(stack_status)"

instances="$(aws_q ec2 describe-instances \
    --filters Name=tag-key,Values=l4mlb-role \
              Name=instance-state-name,Values=pending,running,stopping,stopped,shutting-down \
    --query 'Reservations[].Instances[].[Tags[?Key==`l4mlb-role`]|[0].Value,InstanceId,InstanceType,State.Name,LaunchTime]')"
if [[ -z "$instances" ]]; then
    echo "Instances: none"
else
    echo "Instances:"
    echo "$instances" | while read -r role id type state launched; do
        printf '    %-6s %-20s %-15s %-10s launched %s\n' "$role" "$id" "$type" "$state" "$launched"
    done
fi
