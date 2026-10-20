# Shared helpers for cloud/*.sh: stack outputs, SSH to each host, and starting /
# stopping remote processes. Sourced, not executed. Runs on the operator's laptop
# and needs only the AWS CLI v2, ssh/scp, tar and git. See docs/cloud.md.

CLOUD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$CLOUD_DIR/.." && pwd)"
RUN_DIR="$CLOUD_DIR/.run"

STACK="${L4MLB_STACK:-l4mlb}"
KEY_NAME="${L4MLB_KEY_NAME:-l4mlb}"
SSH_KEY="${L4MLB_SSH_KEY:-$HOME/.ssh/$KEY_NAME.pem}"
REMOTE_REPO="l4-maglev-lb"   # relative to ubuntu's home on every host
REMOTE_RUN="l4mlb-run"       # logs and pid files on every host

# Must match cloud/stack.yaml.
VIP=10.200.0.10
CLIENT_IP=10.99.1.10
LB_IP=10.99.1.20
MON_IP=10.99.1.40
BACKEND_PORT=9000
METRICS_PORT=9105
VIEWER_METRICS_PORT=9106
ADMIN_FIFO="/home/ubuntu/$REMOTE_RUN/admin.fifo"
be_ip() { echo "10.99.1.$((30 + $1))"; }   # $1 = 1-based backend index

mkdir -p "$RUN_DIR"

die() { echo "error: $*" >&2; exit 1; }

require_tools() {
    command -v aws >/dev/null 2>&1 || die "the AWS CLI v2 is not installed (see docs/cloud.md, 'Before you start')"
    command -v ssh >/dev/null 2>&1 || die "ssh is not installed"
    [[ -f "$SSH_KEY" ]] || die "SSH key $SSH_KEY not found (set L4MLB_SSH_KEY, or L4MLB_KEY_NAME if your key pair has another name)"
}

aws_q() { aws --output text "$@"; }

stack_status() {
    aws_q cloudformation describe-stacks --stack-name "$STACK" \
        --query 'Stacks[0].StackStatus' 2>/dev/null || echo "NONE"
}

# Writes the stack's outputs to .run/outputs.env as shell assignments, then sources
# it: ClientPublicIp, LbInstanceId, FlowLogGroup, Region, Profile, ...
refresh_outputs() {
    aws_q cloudformation describe-stacks --stack-name "$STACK" \
        --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' \
        | awk -F'\t' '{ printf "%s=%s\n", $1, $2 }' >"$RUN_DIR/outputs.env.tmp" \
        || die "could not read the outputs of stack $STACK (is it up? cloud/status.sh)"
    mv "$RUN_DIR/outputs.env.tmp" "$RUN_DIR/outputs.env"
    # shellcheck disable=SC1091
    source "$RUN_DIR/outputs.env"
}

load_outputs() {
    if [[ -f "$RUN_DIR/outputs.env" ]]; then
        # shellcheck disable=SC1091
        source "$RUN_DIR/outputs.env"
    else
        refresh_outputs
    fi
    export AWS_DEFAULT_REGION="${Region:-${AWS_DEFAULT_REGION:-}}"
}

# Hosts that exist in the current profile, in a stable order.
roles() {
    if [[ "${Profile:-full}" == "full" ]]; then
        echo client lb be1 be2 be3 be4 mon
    else
        echo client lb be1
    fi
}
backend_count() { [[ "${Profile:-full}" == "full" ]] && echo 4 || echo 1; }

# client -> Client, be3 -> Be3: the prefix of the stack output names.
_out_prefix() { echo "${1^}"; }
public_ip() { local v; v="$(_out_prefix "$1")PublicIp"; echo "${!v:?no public IP for $1}"; }
instance_id() { local v; v="$(_out_prefix "$1")InstanceId"; echo "${!v:?no instance id for $1}"; }
private_ip() {
    case "$1" in
        client) echo "$CLIENT_IP" ;;
        lb) echo "$LB_IP" ;;
        mon) echo "$MON_IP" ;;
        be*) be_ip "${1#be}" ;;
        *) die "unknown role $1" ;;
    esac
}

SSH_OPTS=(-i "$SSH_KEY" -o StrictHostKeyChecking=accept-new
          -o "UserKnownHostsFile=$RUN_DIR/known_hosts" -o ConnectTimeout=10
          -o ControlMaster=auto -o "ControlPath=$RUN_DIR/cm-%C" -o ControlPersist=600
          -o ServerAliveInterval=15 -o LogLevel=ERROR -o BatchMode=yes)

# ssh_to ROLE 'command string' - the command runs in ubuntu's login shell.
ssh_to() { local r=$1; shift; ssh "${SSH_OPTS[@]}" "ubuntu@$(public_ip "$r")" "$@"; }
scp_to() { local r=$1 src=$2 dst=$3; scp -q "${SSH_OPTS[@]}" "$src" "ubuntu@$(public_ip "$r"):$dst"; }
scp_from() { local r=$1 src=$2 dst=$3; scp -q "${SSH_OPTS[@]}" "ubuntu@$(public_ip "$r"):$src" "$dst"; }

wait_ssh() { # ROLE: waits up to ~5 min for SSH and for cloud-init to finish
    local r=$1
    for _ in $(seq 1 60); do
        ssh_to "$r" true 2>/dev/null && break
        sleep 5
    done
    ssh_to "$r" 'cloud-init status --wait >/dev/null 2>&1; true' \
        || die "$r ($(public_ip "$r")) is not reachable over SSH"
}

# rstart ROLE NAME 'command' - starts a long-running process detached, in the
# repo checkout, logging to ~/l4mlb-run/NAME.log, with its pid in NAME.pid.
# A command that starts with "sudo " runs as root (sudo relays TERM/INT to it).
rstart() {
    local r=$1 name=$2 cmd=$3
    ssh_to "$r" "mkdir -p ~/$REMOTE_RUN && cd ~/$REMOTE_REPO && \
        nohup $cmd >~/$REMOTE_RUN/$name.log 2>&1 </dev/null & echo \$! >~/$REMOTE_RUN/$name.pid"
}

# rstop ROLE NAME [SIGNAL] - signals the process started by rstart (TERM default).
rstop() {
    local r=$1 name=$2 sig=${3:-TERM}
    ssh_to "$r" "f=~/$REMOTE_RUN/$name.pid; [ -f \$f ] || exit 0; p=\$(cat \$f); \
        sudo pkill -$sig -P \$p 2>/dev/null; sudo kill -$sig \$p 2>/dev/null; rm -f \$f; true"
}

rlog() { ssh_to "$1" "cat ~/$REMOTE_RUN/$2.log 2>/dev/null"; }

lb_metrics() { ssh_to lb "curl -s http://$LB_IP:$METRICS_PORT/metrics"; }

# metric_value 'regex anchored on the metric line' <<<"$metrics"
metric_value() { grep -oP "^$1 \K[0-9.e+]+" | head -1; }

# ---- The system's processes on the hosts --------------------------------------

lb_iface() { ssh_to lb "ip -o route show default | awk '{ print \$5; exit }'"; }

# start_lb N [extra maglev-lb args...]: maglev-lb with backends be1..beN, the same
# health-check timing as the local streaming demo, the admin FIFO and metrics.
start_lb() {
    local n=$1; shift
    local backends="" i
    for i in $(seq 1 "$n"); do backends="$backends --backend $(be_ip "$i"):$BACKEND_PORT"; done
    rstop lb maglev-lb
    ssh_to lb "mkdir -p ~/$REMOTE_RUN && rm -f $ADMIN_FIFO && mkfifo $ADMIN_FIFO"
    rstart lb maglev-lb "sudo build/maglev-lb --iface $(lb_iface) --vip $VIP --director-ip $LB_IP \
        $backends --hc-interval-ms 500 --hc-timeout-ms 300 --hc-rise 2 --hc-fall 2 \
        --admin-fifo $ADMIN_FIFO --metrics-port $METRICS_PORT $*"
    for _ in $(seq 1 50); do
        rlog lb maglev-lb | grep -q "starting datapath" && return 0
        sleep 0.4
    done
    rlog lb maglev-lb >&2
    die "maglev-lb did not start on lb"
}

backends_eligible() { lb_metrics | metric_value 'maglev_lb_backends_eligible'; }
table_generation() { lb_metrics | metric_value 'maglev_lb_table_generation'; }

wait_eligible() { # N [timeout_s]
    local want=$1 t=${2:-30} e=""
    for _ in $(seq 1 $((t * 2))); do
        e="$(backends_eligible)"
        [[ "$e" == "$want" ]] && return 0
        sleep 0.5
    done
    echo "    backends eligible: ${e:-?} (wanted $want)" >&2
    return 1
}

lb_drain() { ssh_to lb "echo 'DRAIN $1' >$ADMIN_FIFO"; }     # $1 = backend id (index - 1)
lb_undrain() { ssh_to lb "echo 'UNDRAIN $1' >$ADMIN_FIFO"; }

# Request/response backend (bridge/service_server.py): be$1 with capacity $2
# slots, mean service time $3 ms, queue limit $4.
start_rr_backend() {
    local i=$1 cap=${2:-32} svc=${3:-5} q=${4:-256}
    rstop "be$i" svc
    rstart "be$i" svc "python3 -u bridge/service_server.py --slot $((i - 1)) --lb-ip $LB_IP \
        --capacity $cap --service-ms $svc --service-cv 0.5 --queue-limit $q --no-stdin-control"
}

# Streaming backend (scripts/bench/streaming_server.py), as in the local demo.
start_stream_backend() {
    local i=$1
    rstop "be$i" stream
    rstart "be$i" stream "python3 -u scripts/bench/streaming_server.py 0.0.0.0 $BACKEND_PORT BE$i \
        --bitrate-min-kbps ${BENCH_BITRATE_MIN_KBPS:-2000} --bitrate-max-kbps ${BENCH_BITRATE_MAX_KBPS:-6000} \
        --duration-min-s ${BENCH_SESSION_MIN_S:-8} --duration-max-s ${BENCH_SESSION_MAX_S:-30}"
}

# Request/response client (bridge/load_client.py) at $2 req/s, run in the
# foreground for $3 seconds; its JSON stats lines go to local file $1.
run_rr_client() {
    local out=$1 rps=$2 secs=$3; shift 3
    ssh_to client "cd ~/$REMOTE_REPO && timeout -s INT $secs python3 -u bridge/load_client.py \
        --vip $VIP --src-ip $CLIENT_IP --rps $rps --timeout-ms 1000 --retries 0 \
        --no-stdin-control $* 2>&1; true" >"$out"
}

stop_all_processes() {
    local r
    for r in $(roles); do
        ssh_to "$r" "for f in ~/$REMOTE_RUN/*.pid; do [ -f \$f ] || continue; p=\$(cat \$f); \
            sudo pkill -TERM -P \$p 2>/dev/null; sudo kill -TERM \$p 2>/dev/null; rm -f \$f; done; true" &
    done
    wait
}

# ---- Measurement windows, for comparing our numbers with AWS's ----------------
# CloudWatch sums traffic per whole clock minute, so a window that starts and ends
# on minute boundaries can be compared with it exactly.
sleep_to_minute() { local s; s=$(date +%s); sleep $((60 - s % 60)); }

# mark_window FILE start|end: records the time and maglev-lb's own counters.
mark_window() {
    local file=$1 which=$2 m
    m="$(lb_metrics)"
    python3 - "$file" "$which" "$(date +%s)" \
        "$(metric_value 'maglev_lb_rx_packets_total' <<<"$m")" \
        "$(metric_value 'maglev_lb_tx_packets_total' <<<"$m")" \
        "$(metric_value 'maglev_lb_tx_bytes_total' <<<"$m")" <<'PY'
import json, os, sys
path, which, t, rx, tx, txb = sys.argv[1:]
d = json.load(open(path)) if os.path.exists(path) else {}
d[which] = {"epoch": int(t), "rxPackets": float(rx or 0), "txPackets": float(tx or 0),
            "txBytes": float(txb or 0)}
json.dump(d, open(path, "w"), indent=1)
PY
}

# ---- Is the benchmark environment itself fit to measure? ----------------------
# A burstable (t2/t3) instance out of CPU credits runs at its baseline (10-20% of a
# vCPU), and anything measured then is AWS's throttle, not maglev-lb. Prints the
# latest CloudWatch CPUCreditBalance of ROLE, or nothing (not burstable, or no
# datapoint yet: they arrive every 5 minutes).
cpu_credits() {
    aws_q cloudwatch get-metric-statistics --namespace AWS/EC2 --metric-name CPUCreditBalance \
        --dimensions "Name=InstanceId,Value=$(instance_id "$1")" \
        --start-time "$(date -u -d '-20 min' +%FT%TZ)" --end-time "$(date -u +%FT%TZ)" \
        --period 300 --statistics Average \
        --query 'sort_by(Datapoints,&Timestamp)[-1].Average' 2>/dev/null | grep -v None || true
}

# require_credits MIN ROLE...: refuses to go on when a role's balance is known and
# below MIN credits (1 credit = one vCPU at 100% for a minute). FORCE=1 overrides.
require_credits() {
    local min=$1 r c low=0; shift
    for r in "$@"; do
        c="$(cpu_credits "$r")"
        if [[ -z "$c" ]]; then
            echo "    $r: CPU credits not reported (yet, or not a burstable type)"
        elif awk "BEGIN { exit !($c < $min) }"; then
            echo "    $r: only $c CPU credits (< $min): it would be throttled" >&2
            low=1
        else
            echo "    $r: $c CPU credits"
        fi
    done
    if [[ "$low" -eq 1 && "${FORCE:-0}" != 1 ]]; then
        die "not enough CPU credits for a meaningful run; wait (instances earn them while idle) or set FORCE=1"
    fi
}

# Every *_allowance_exceeded counter of ROLE's ENA interface ("name value" lines):
# packets the hypervisor dropped or queued because the instance went over its
# bandwidth / packets-per-second / connection-tracking allowance. Empty on t2 (Xen).
ena_allowances() {
    ssh_to "$1" "ethtool -S \$(ip -o route show default | awk '{print \$5; exit}') 2>/dev/null \
        | awk '/allowance_exceeded/ { gsub(\":\", \"\", \$1); print \$1, \$2 }'"
}
