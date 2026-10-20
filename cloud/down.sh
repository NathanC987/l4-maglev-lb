#!/usr/bin/env bash
# Deletes the whole stack (instances, network, IAM roles, the flow-log group) and
# waits until it is gone. Nothing billable is left behind afterwards; confirm with
# cloud/status.sh.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_tools

status="$(stack_status)"
if [[ "$status" == "NONE" ]]; then
    echo "Stack $STACK does not exist; nothing to delete."
else
    echo "==> Deleting stack $STACK (was $status)"
    aws cloudformation delete-stack --stack-name "$STACK"
    aws cloudformation wait stack-delete-complete --stack-name "$STACK"
    echo "    deleted"
fi

for cm in "$RUN_DIR"/cm-*; do
    [[ -S "$cm" ]] && ssh -o "ControlPath=$cm" -O exit dummy 2>/dev/null || true
done
rm -f "$RUN_DIR/outputs.env" "$RUN_DIR/known_hosts" "$RUN_DIR"/cm-*
"$CLOUD_DIR/status.sh"
