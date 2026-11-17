#!/usr/bin/env bash
# Installs packages, ships the current working tree, builds maglev-lb on the LB,
# applies each host's network setup (cloud/hostnet/), and starts Prometheus +
# Grafana on mon. Idempotent: re-run it after changing code.
#
# The code is shipped from this checkout (tracked files plus untracked ones that
# are not ignored), so uncommitted changes deploy too and no host needs GitHub.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_tools
refresh_outputs
export AWS_DEFAULT_REGION="$Region"

ROLES=($(roles))
NBE="$(backend_count)"

# Runs "fn ROLE" for every role in parallel; fails if any of them failed.
for_all() {
    local fn=$1 pids=() failed=0
    shift
    for r in "$@"; do
        "$fn" "$r" >"$RUN_DIR/deploy-$r.log" 2>&1 &
        pids+=($!)
    done
    for i in "${!pids[@]}"; do
        if ! wait "${pids[$i]}"; then
            echo "    FAILED on ${*:i+1:1} - last lines of .run/deploy-${*:i+1:1}.log:" >&2
            tail -n 15 "$RUN_DIR/deploy-${*:i+1:1}.log" >&2
            failed=1
        fi
    done
    return "$failed"
}

echo "==> Waiting for SSH and cloud-init on ${ROLES[*]}"
for_all wait_ssh "${ROLES[@]}"

echo "==> Installing packages (first run only)"
install_pkgs() {
    local r=$1 extra=""
    case "$r" in
        lb) extra="build-essential cmake pkg-config libxxhash-dev libncurses-dev" ;;
        mon) extra="docker.io docker-compose-v2" ;;
    esac
    ssh_to "$r" "set -e; [ -f ~/.l4mlb-pkgs ] && exit 0
        sudo apt-get update -q
        sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q iproute2 ethtool python3 curl tcpdump $extra
        touch ~/.l4mlb-pkgs"
}
for_all install_pkgs "${ROLES[@]}"

echo "==> Shipping the working tree"
tarball="$RUN_DIR/src.tgz"
(cd "$REPO_ROOT" && git ls-files -co --exclude-standard -z \
    | tar --null --ignore-failed-read -T - -czf "$tarball")
ship() {
    local r=$1
    scp_to "$r" "$tarball" /tmp/l4mlb-src.tgz
    ssh_to "$r" "rm -rf ~/$REMOTE_REPO && mkdir -p ~/$REMOTE_REPO ~/$REMOTE_RUN && \
        tar -xzf /tmp/l4mlb-src.tgz -C ~/$REMOTE_REPO && rm /tmp/l4mlb-src.tgz"
}
for_all ship "${ROLES[@]}"

echo "==> Building maglev-lb (Release) on lb"
ssh_to lb "cd ~/$REMOTE_REPO && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release >/dev/null && \
    cmake --build build -j\$(nproc) >/dev/null && ls -l build/maglev-lb" \
    || die "the build on lb failed (re-run: ssh to lb and run cmake --build build)"

echo "==> Host network setup"
hostnet() {
    local r=$1
    case "$r" in
        lb) ssh_to lb "sudo ~/$REMOTE_REPO/cloud/hostnet/lb.sh" ;;
        client*) ssh_to "$r" "sudo ~/$REMOTE_REPO/cloud/hostnet/client.sh" ;;
        be*) ssh_to "$r" "sudo ~/$REMOTE_REPO/cloud/hostnet/backend.sh $LB_IP $VIP" ;;
        mon) ;;
    esac
}
for_all hostnet "${ROLES[@]}"
for r in "${ROLES[@]}"; do sed -n '$p' "$RUN_DIR/deploy-$r.log" | sed "s/^/    /"; done

if [[ " ${ROLES[*]} " == *" mon "* ]]; then
    echo "==> Prometheus + Grafana on mon"
    account="$(aws_q sts get-caller-identity --query Account)"
    ids="LB=$(instance_id lb) CLIENT=$(instance_id client) ACCOUNT=$account"
    for i in $(seq 1 "$NBE"); do ids="$ids BE$i=$(instance_id "be$i")"; done
    ssh_to mon "set -e; cd ~/$REMOTE_REPO
        [ -f ~/grafana-admin-password ] || (umask 077; head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n' >~/grafana-admin-password)
        $ids REGION=$Region LOG_GROUP=$FlowLogGroup python3 cloud/render_dashboard.py \
            monitoring/grafana-cloud/dashboards ~/$REMOTE_RUN/dashboards
        export GF_ADMIN_PASSWORD=\$(cat ~/grafana-admin-password) AWS_REGION=$Region \
               DASHBOARD_DIR=\$HOME/$REMOTE_RUN/dashboards
        sudo -E docker compose -f monitoring/docker-compose-cloud.yml up -d --force-recreate >/dev/null
        sudo docker ps --format '{{.Names}}: {{.Status}}'" | sed "s/^/    /"
    echo "    Grafana: cloud/grafana-tunnel.sh, then http://localhost:3000 (admin password: cloud/grafana-tunnel.sh prints it)"
fi

echo "==> Deployed to ${ROLES[*]}"
