#!/usr/bin/env bash
# Creates (or updates) the CloudFormation stack, then installs and configures
# every host with cloud/deploy.sh. Re-running it is safe: the stack update is a
# no-op when nothing changed, and deploy only re-ships the code.
#
#   cloud/up.sh [--profile minimal|full] [--instance-type TYPE]
#               [--flow-logs on|off] [--auto-terminate MINUTES] [--no-deploy]
#
# Options default to the values of the previous run (.run/params.env), then to
# full / t3.micro / on / 240. SSH is opened to your current public IP only
# (override with L4MLB_ADMIN_CIDR=a.b.c.d/32).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

PROFILE="" INSTANCE_TYPE="" FLOW_LOGS="" AUTO_TERMINATE="" DEPLOY=1
if [[ -f "$RUN_DIR/params.env" ]]; then
    # shellcheck disable=SC1091
    source "$RUN_DIR/params.env"
fi
while [[ $# -gt 0 ]]; do
    case "$1" in
        --profile) PROFILE=$2; shift 2 ;;
        --instance-type) INSTANCE_TYPE=$2; shift 2 ;;
        --flow-logs) FLOW_LOGS=$2; shift 2 ;;
        --auto-terminate) AUTO_TERMINATE=$2; shift 2 ;;
        --no-deploy) DEPLOY=0; shift ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *) die "unknown option $1 (see --help)" ;;
    esac
done
PROFILE=${PROFILE:-full}
INSTANCE_TYPE=${INSTANCE_TYPE:-t3.micro}
FLOW_LOGS=${FLOW_LOGS:-on}
AUTO_TERMINATE=${AUTO_TERMINATE:-240}

require_tools
aws_q sts get-caller-identity --query Account >/dev/null \
    || die "the AWS CLI is not signed in (run: aws configure sso, or aws configure)"
aws_q ec2 describe-key-pairs --key-names "$KEY_NAME" >/dev/null 2>&1 \
    || die "no EC2 key pair named $KEY_NAME in $(aws configure get region) (see docs/cloud.md)"

# Past the on-demand vCPU quota, CloudFormation starts some instances, fails on
# the rest and rolls everything back, so check it first. New accounts start at 5.
need_vcpus=$(( $(Profile=$PROFILE roles | wc -w) * $(aws_q ec2 describe-instance-types \
    --instance-types "$INSTANCE_TYPE" --query 'InstanceTypes[0].VCpuInfo.DefaultVCpus') ))
quota_vcpus="$(aws_q service-quotas get-service-quota --service-code ec2 --quota-code L-1216C47A \
    --query 'Quota.Value' 2>/dev/null || echo 0)"
used_vcpus="$(aws_q ec2 describe-instances \
    --filters Name=instance-state-name,Values=pending,running \
    --query "Reservations[].Instances[?!(Tags[?Key=='aws:cloudformation:stack-name' && Value=='$STACK'])][].CpuOptions.[CoreCount,ThreadsPerCore]" \
    | awk '{ s += $1 * $2 } END { print s + 0 }')"
if [[ "${quota_vcpus%.*}" -gt 0 && "$need_vcpus" -gt $(( ${quota_vcpus%.*} - used_vcpus )) ]]; then
    die "profile $PROFILE on $INSTANCE_TYPE needs $need_vcpus vCPUs, but the account's on-demand quota is" \
        "${quota_vcpus%.*} with $used_vcpus in use elsewhere. Ask for more in Service Quotas (EC2," \
        "\"Running On-Demand Standard (A, C, D, H, I, M, R, T, Z) instances\"; free) - see docs/cloud.md"
fi

ADMIN_CIDR="${L4MLB_ADMIN_CIDR:-}"
if [[ -z "$ADMIN_CIDR" ]]; then
    my_ip="$(curl -fsS --max-time 10 https://checkip.amazonaws.com | tr -d '[:space:]')" \
        || die "could not detect your public IP; set L4MLB_ADMIN_CIDR=a.b.c.d/32"
    ADMIN_CIDR="$my_ip/32"
fi

cat >"$RUN_DIR/params.env" <<EOF
PROFILE=$PROFILE
INSTANCE_TYPE=$INSTANCE_TYPE
FLOW_LOGS=$FLOW_LOGS
AUTO_TERMINATE=$AUTO_TERMINATE
EOF

echo "==> Stack $STACK: profile=$PROFILE type=$INSTANCE_TYPE flow-logs=$FLOW_LOGS" \
     "auto-terminate=${AUTO_TERMINATE}min ssh-from=$ADMIN_CIDR"
aws cloudformation deploy \
    --stack-name "$STACK" \
    --template-file "$CLOUD_DIR/stack.yaml" \
    --capabilities CAPABILITY_IAM \
    --no-fail-on-empty-changeset \
    --tags project=l4-maglev-lb \
    --parameter-overrides \
        "KeyName=$KEY_NAME" "AdminCidr=$ADMIN_CIDR" "Profile=$PROFILE" \
        "InstanceType=$INSTANCE_TYPE" "FlowLogs=$FLOW_LOGS" \
        "AutoTerminateMinutes=$AUTO_TERMINATE"

refresh_outputs
# Addresses can change when instances are replaced; drop stale host keys.
rm -f "$RUN_DIR/known_hosts"

echo "==> Hosts"
for r in $(roles); do
    printf '    %-6s %-15s public %-15s %s\n' "$r" "$(private_ip "$r")" "$(public_ip "$r")" "$(instance_id "$r")"
done
echo "    VIP    $Vip"

if [[ "$DEPLOY" -eq 1 ]]; then
    "$CLOUD_DIR/deploy.sh"
fi

echo ""
echo "Instances terminate themselves after ${AUTO_TERMINATE} minutes. Delete everything"
echo "sooner with: cloud/down.sh"
