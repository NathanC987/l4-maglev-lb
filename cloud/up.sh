#!/usr/bin/env bash
# Creates (or updates) the CloudFormation stack, then installs and configures
# every host with cloud/deploy.sh. Re-running it is safe: the stack update is a
# no-op when nothing changed, and deploy only re-ships the code.
#
#   cloud/up.sh [--profile minimal|full|canvas] [--instance-type TYPE] [--measured-type TYPE]
#               [--cpu-credits standard|unlimited] [--flow-logs on|off]
#               [--auto-terminate MINUTES] [--no-deploy]
#
# --measured-type is for lb and client, --instance-type for the rest. Options
# default to the values of the previous run (.run/params.env), then to full /
# t3.micro / c7i-flex.large / standard / on / 240. SSH is opened to your current public IP
# only; some networks alternate between two, and both are opened then (override with
# L4MLB_ADMIN_CIDR=a.b.c.d/32, or two separated by a comma).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

PROFILE="" INSTANCE_TYPE="" MEASURED_TYPE="" CPU_CREDITS="" FLOW_LOGS="" AUTO_TERMINATE="" DEPLOY=1
if [[ -f "$RUN_DIR/params.env" ]]; then
    # shellcheck disable=SC1091
    source "$RUN_DIR/params.env"
fi
while [[ $# -gt 0 ]]; do
    case "$1" in
        --profile) PROFILE=$2; shift 2 ;;
        --instance-type) INSTANCE_TYPE=$2; shift 2 ;;
        --measured-type) MEASURED_TYPE=$2; shift 2 ;;
        --cpu-credits) CPU_CREDITS=$2; shift 2 ;;
        --flow-logs) FLOW_LOGS=$2; shift 2 ;;
        --auto-terminate) AUTO_TERMINATE=$2; shift 2 ;;
        --no-deploy) DEPLOY=0; shift ;;
        -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
        *) die "unknown option $1 (see --help)" ;;
    esac
done
PROFILE=${PROFILE:-full}
INSTANCE_TYPE=${INSTANCE_TYPE:-t3.micro}
MEASURED_TYPE=${MEASURED_TYPE:-c7i-flex.large}
CPU_CREDITS=${CPU_CREDITS:-standard}
FLOW_LOGS=${FLOW_LOGS:-on}
AUTO_TERMINATE=${AUTO_TERMINATE:-240}

require_tools
aws_q sts get-caller-identity --query Account >/dev/null \
    || die "the AWS CLI is not signed in (run: aws configure sso, or aws configure)"
aws_q ec2 describe-key-pairs --key-names "$KEY_NAME" >/dev/null 2>&1 \
    || die "no EC2 key pair named $KEY_NAME in $(aws configure get region) (see docs/cloud.md)"

# Past the on-demand vCPU quota, CloudFormation starts some instances, fails on
# the rest and rolls everything back, so check it first. New accounts start at 5.
vcpus_of() { aws_q ec2 describe-instance-types --instance-types "$1" --query 'InstanceTypes[0].VCpuInfo.DefaultVCpus'; }
n_measured=2   # lb and client; on the canvas profile the clients are ordinary hosts
[[ "$PROFILE" == canvas ]] && n_measured=1
need_vcpus=$(( n_measured * $(vcpus_of "$MEASURED_TYPE") + ($(Profile=$PROFILE roles | wc -w) - n_measured) * $(vcpus_of "$INSTANCE_TYPE") ))
quota_vcpus="$(aws_q service-quotas get-service-quota --service-code ec2 --quota-code L-1216C47A \
    --query 'Quota.Value' 2>/dev/null || echo 0)"
used_vcpus="$(aws_q ec2 describe-instances \
    --filters Name=instance-state-name,Values=pending,running \
    --query "Reservations[].Instances[?!(Tags[?Key=='aws:cloudformation:stack-name' && Value=='$STACK'])][].CpuOptions.[CoreCount,ThreadsPerCore]" \
    | awk '{ s += $1 * $2 } END { print s + 0 }')"
if [[ "${quota_vcpus%.*}" -gt 0 && "$need_vcpus" -gt $(( ${quota_vcpus%.*} - used_vcpus )) ]]; then
    die "profile $PROFILE ($MEASURED_TYPE lb and client, $INSTANCE_TYPE others) needs $need_vcpus vCPUs, but the account's on-demand quota is" \
        "${quota_vcpus%.*} with $used_vcpus in use elsewhere. Ask for more in Service Quotas (EC2," \
        "\"Running On-Demand Standard (A, C, D, H, I, M, R, T, Z) instances\"; free) - see docs/cloud.md"
fi

ADMIN_CIDR="${L4MLB_ADMIN_CIDR:-}"
if [[ -z "$ADMIN_CIDR" ]]; then
    # Some NATs send each connection out through one of several public IPs, so a
    # single lookup can miss the one SSH will use. Ask a few times.
    my_ips=()
    for _ in 1 2 3 4 5 6; do
        ip="$(curl -fsS --max-time 10 https://checkip.amazonaws.com | tr -d '[:space:]')" || continue
        [[ " ${my_ips[*]} " == *" $ip "* ]] || my_ips+=("$ip")
    done
    [[ ${#my_ips[@]} -gt 0 ]] || die "could not detect your public IP; set L4MLB_ADMIN_CIDR=a.b.c.d/32"
    [[ ${#my_ips[@]} -le 2 ]] \
        || die "this network uses ${#my_ips[@]} public IPs (${my_ips[*]}); set L4MLB_ADMIN_CIDR to a range covering them, or use another network"
    ADMIN_CIDR="$(printf '%s/32,' "${my_ips[@]}")"
    ADMIN_CIDR="${ADMIN_CIDR%,}"
fi
IFS=, read -r ADMIN_CIDR_1 ADMIN_CIDR_2 ADMIN_CIDR_EXTRA <<<"$ADMIN_CIDR"
[[ -z "${ADMIN_CIDR_EXTRA:-}" ]] || die "L4MLB_ADMIN_CIDR takes at most two ranges"

cat >"$RUN_DIR/params.env" <<EOF
PROFILE=$PROFILE
INSTANCE_TYPE=$INSTANCE_TYPE
MEASURED_TYPE=$MEASURED_TYPE
CPU_CREDITS=$CPU_CREDITS
FLOW_LOGS=$FLOW_LOGS
AUTO_TERMINATE=$AUTO_TERMINATE
EOF

echo "==> Stack $STACK: profile=$PROFILE type=$INSTANCE_TYPE lb+client=$MEASURED_TYPE cpu-credits=$CPU_CREDITS flow-logs=$FLOW_LOGS" \
     "auto-terminate=${AUTO_TERMINATE}min ssh-from=$ADMIN_CIDR"
aws cloudformation deploy \
    --stack-name "$STACK" \
    --template-file "$CLOUD_DIR/stack.yaml" \
    --capabilities CAPABILITY_IAM \
    --no-fail-on-empty-changeset \
    --tags project=l4-maglev-lb \
    --parameter-overrides \
        "KeyName=$KEY_NAME" "AdminCidr=$ADMIN_CIDR_1" "AdminCidr2=${ADMIN_CIDR_2:-}" "Profile=$PROFILE" \
        "InstanceType=$INSTANCE_TYPE" "MeasuredInstanceType=$MEASURED_TYPE" "CpuCredits=$CPU_CREDITS" "FlowLogs=$FLOW_LOGS" \
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
