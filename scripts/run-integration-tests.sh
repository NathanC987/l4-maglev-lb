#!/usr/bin/env bash
# End-to-end M1 verification: brings up the netns topology, starts
# maglev-lb, runs the TCP/UDP integration tests from the client netns, and
# checks (via an nftables netdev/ingress counter on the LB's own interface)
# that no VIP-sourced packet - i.e. a backend's DSR reply - ever reaches the
# LB. Tears everything down on exit regardless of pass/fail.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=topology.sh
source "$SCRIPT_DIR/topology.sh"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
INTEGRATION_DIR="$REPO_ROOT/test/integration"

# Positional arg preferred over $MAGLEV_LB_BIN since sudo strips
# non-allowlisted environment variables by default.
BIN="${1:-${MAGLEV_LB_BIN:-$REPO_ROOT/build/maglev-lb}}"
LB_LOG="$PID_DIR/maglev-lb.log"
NFT_TABLE=l4mlbtest

if [[ $EUID -ne 0 ]]; then
    echo "run-integration-tests.sh must be run as root" >&2
    exit 1
fi
if [[ ! -x "$BIN" ]]; then
    echo "maglev-lb binary not found at $BIN (build it first, or set MAGLEV_LB_BIN)" >&2
    exit 1
fi

LB_PID=""
overall_rc=0

cleanup() {
    echo "==> Cleaning up"
    if [[ -n "$LB_PID" ]]; then
        kill "$LB_PID" 2>/dev/null || true
        wait "$LB_PID" 2>/dev/null || true
    fi
    ip netns exec "$NS_LB" nft delete table netdev "$NFT_TABLE" 2>/dev/null || true
    "$SCRIPT_DIR/teardown-netns.sh"
}
trap cleanup EXIT

echo "==> Setting up topology"
if ! "$SCRIPT_DIR/setup-netns.sh"; then
    echo "FAIL: setup-netns.sh failed; aborting" >&2
    exit 1
fi

echo "==> Installing netdev/ingress counter on $VETH_LB_NS (catches ANY frame"
echo "    with ip saddr == VIP arriving at the LB, regardless of destination -"
echo "    this is the proof that DSR replies never transit the LB)"
ip netns exec "$NS_LB" nft add table netdev "$NFT_TABLE"
ip netns exec "$NS_LB" nft add chain netdev "$NFT_TABLE" ingress \
    "{ type filter hook ingress device $VETH_LB_NS priority 0; }"
ip netns exec "$NS_LB" nft add rule netdev "$NFT_TABLE" ingress ip saddr "$VIP" counter

echo "==> Starting maglev-lb"
mkdir -p "$PID_DIR"
: >"$LB_LOG"
ip netns exec "$NS_LB" "$BIN" \
    --iface "$VETH_LB_NS" \
    --vip "$VIP" \
    --director-ip "$LB_IP" \
    --backend "$BE1_IP:$BACKEND_PORT" \
    --backend "$BE2_IP:$BACKEND_PORT" \
    >"$LB_LOG" 2>&1 &
LB_PID=$!

echo "==> Waiting for maglev-lb to be ready (PID $LB_PID)"
ready=0
for _ in $(seq 1 50); do
    if ! kill -0 "$LB_PID" 2>/dev/null; then
        echo "FAIL: maglev-lb exited early; log follows:" >&2
        cat "$LB_LOG" >&2
        exit 1
    fi
    if grep -q "starting datapath" "$LB_LOG" 2>/dev/null; then
        ready=1
        break
    fi
    sleep 0.2
done
if [[ "$ready" -ne 1 ]]; then
    echo "FAIL: maglev-lb did not report readiness in time; log follows:" >&2
    cat "$LB_LOG" >&2
    exit 1
fi
echo "    ready:"
sed 's/^/    /' "$LB_LOG"

run_test() {
    local name=$1
    shift
    echo ""
    echo "==> Running $name"
    if ip netns exec "$NS_CLIENT" python3 "$@"; then
        echo "    $name: OK"
    else
        echo "    $name: FAILED"
        overall_rc=1
    fi
}

run_test test_tcp_stickiness "$INTEGRATION_DIR/test_tcp_stickiness.py" "$VIP" "$BACKEND_PORT"
run_test test_udp_dsr "$INTEGRATION_DIR/test_udp_dsr.py" "$VIP" "$BACKEND_PORT"

echo ""
echo "==> Checking the LB never saw a VIP-sourced (DSR reply) packet"
counter_line="$(ip netns exec "$NS_LB" nft list chain netdev "$NFT_TABLE" ingress)"
echo "$counter_line" | sed 's/^/    /'
packets="$(echo "$counter_line" | grep -oP 'packets \K[0-9]+' | head -1)"
if [[ "$packets" != "0" ]]; then
    echo "FAIL: LB saw $packets packet(s) sourced from the VIP - DSR bypass broken"
    overall_rc=1
else
    echo "    OK: 0 packets - the LB structurally never saw the return path"
fi

echo ""
echo "==> maglev-lb stats at exit:"
sed -n '/^maglev-lb:/p' "$LB_LOG" | sed 's/^/    /'

if [[ "$overall_rc" -eq 0 ]]; then
    echo ""
    echo "ALL INTEGRATION TESTS PASSED"
else
    echo ""
    echo "SOME INTEGRATION TESTS FAILED"
fi
exit "$overall_rc"
