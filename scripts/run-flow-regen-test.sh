#!/usr/bin/env bash
# M2 verification: proves conntrack pinning survives an UNRELATED Maglev
# table regeneration triggered by the health checker detecting a dead
# backend. Holds one flow open on backend A, kills backend B's echo server
# (not A's), waits for the health checker + table_generator to react, then
# confirms: (1) the held flow on A is completely unaffected, (2) new flows
# opened after the regen land only on A. Tears everything down on exit.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=topology.sh
source "$SCRIPT_DIR/topology.sh"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
INTEGRATION_DIR="$REPO_ROOT/test/integration"

BIN="${1:-${MAGLEV_LB_BIN:-$REPO_ROOT/build/maglev-lb}}"
LB_LOG="$PID_DIR/maglev-lb.log"
NFT_TABLE=l4mlbregen

if [[ $EUID -ne 0 ]]; then
    echo "run-flow-regen-test.sh must be run as root" >&2
    exit 1
fi
if [[ ! -x "$BIN" ]]; then
    echo "maglev-lb binary not found at $BIN (build it first, or pass its path)" >&2
    exit 1
fi

LB_PID=""
CPROC_PID=""
overall_rc=0

cleanup() {
    echo "==> Cleaning up"
    if [[ -n "${CPROC_PID:-}" ]] && kill -0 "$CPROC_PID" 2>/dev/null; then
        kill "$CPROC_PID" 2>/dev/null || true
    fi
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

echo "==> Installing netdev/ingress DSR-bypass counter on $VETH_LB_NS"
ip netns exec "$NS_LB" nft add table netdev "$NFT_TABLE"
ip netns exec "$NS_LB" nft add chain netdev "$NFT_TABLE" ingress \
    "{ type filter hook ingress device $VETH_LB_NS priority 0; }"
ip netns exec "$NS_LB" nft add rule netdev "$NFT_TABLE" ingress ip saddr "$VIP" counter

echo "==> Starting maglev-lb (fast health-check settings for quick test convergence)"
mkdir -p "$PID_DIR"
: >"$LB_LOG"
ip netns exec "$NS_LB" "$BIN" \
    --iface "$VETH_LB_NS" \
    --vip "$VIP" \
    --director-ip "$LB_IP" \
    --backend "$BE1_IP:$BACKEND_PORT" \
    --backend "$BE2_IP:$BACKEND_PORT" \
    --hc-interval-ms 200 --hc-timeout-ms 300 --hc-rise 2 --hc-fall 2 \
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

echo "==> Opening a long-lived flow"
coproc CPROC {
    ip netns exec "$NS_CLIENT" python3 "$INTEGRATION_DIR/open_flow.py" "$VIP" "$BACKEND_PORT"
}
# A *named* coproc (coproc CPROC { ... }) makes bash set $CPROC_PID itself -
# the generic $COPROC_PID only exists for an unnamed `coproc { ...; }`, so
# there is nothing to assign here; CPROC_PID is already usable.

if ! IFS= read -r -t 5 line1 <&"${CPROC[0]}"; then
    echo "FAIL: no response opening the long-lived flow" >&2
    exit 1
fi
echo "    $line1"
survivor="${line1#ANSWERED_BY }"
if [[ "$survivor" != "BACKEND1" && "$survivor" != "BACKEND2" ]]; then
    echo "FAIL: unexpected response: $line1" >&2
    exit 1
fi
if [[ "$survivor" == "BACKEND1" ]]; then
    victim="BACKEND2"
else
    victim="BACKEND1"
fi
echo "    held flow is on $survivor; will kill $victim"

echo "==> Killing $victim's echo server (simulating a crash)"
victim_pidfile="$PID_DIR/$victim.pid"
if [[ ! -f "$victim_pidfile" ]]; then
    echo "FAIL: no pidfile for $victim at $victim_pidfile" >&2
    exit 1
fi
kill "$(cat "$victim_pidfile")"
rm -f "$victim_pidfile"

echo "==> Waiting for the health checker to detect the failure and regenerate the table"
regen_ok=0
for _ in $(seq 1 50); do
    if grep -q "backend.*UNHEALTHY" "$LB_LOG" 2>/dev/null &&
        grep -q "table_generator: regenerated" "$LB_LOG" 2>/dev/null; then
        regen_ok=1
        break
    fi
    sleep 0.2
done
if [[ "$regen_ok" -ne 1 ]]; then
    echo "FAIL: health checker never marked $victim unhealthy / table never regenerated; log follows:" >&2
    cat "$LB_LOG" >&2
    overall_rc=1
fi
sed -n '/health:/p;/table_generator:/p' "$LB_LOG" | sed 's/^/    /'

echo "==> Continuing the held flow"
echo >&"${CPROC[1]}"
if ! IFS= read -r -t 5 line2 <&"${CPROC[0]}"; then
    echo "FAIL: held flow produced no second reply after the regen"
    overall_rc=1
else
    echo "    $line2"
    reply2="${line2#REPLY2 }"
    if [[ "$reply2" != "$survivor" ]]; then
        echo "FAIL: held flow moved from $survivor to $reply2 across the regen - conntrack pinning broken"
        overall_rc=1
    else
        echo "    OK: held flow stayed pinned to $survivor across the regen"
    fi
fi
wait "$CPROC_PID" 2>/dev/null || true
CPROC_PID=""

echo "==> Sampling new flows after the regen (must all land on $survivor)"
mismatch=0
for i in $(seq 1 10); do
    got="$(ip netns exec "$NS_CLIENT" python3 "$INTEGRATION_DIR/check_backend.py" "$VIP" "$BACKEND_PORT" 2>/dev/null || echo "ERROR")"
    if [[ "$got" != "$survivor" ]]; then
        echo "    sample $i: got $got (expected $survivor)"
        mismatch=$((mismatch + 1))
    fi
done
if [[ "$mismatch" -eq 0 ]]; then
    echo "    OK: all 10 new connections landed on $survivor"
else
    echo "FAIL: $mismatch/10 new connections did not land on $survivor"
    overall_rc=1
fi

echo ""
echo "==> Checking the LB never saw a VIP-sourced (DSR reply) packet"
counter_line="$(ip netns exec "$NS_LB" nft list chain netdev "$NFT_TABLE" ingress)"
echo "$counter_line" | sed 's/^/    /'
packets="$(echo "$counter_line" | grep -oP 'packets \K[0-9]+' | head -1)"
if [[ "$packets" != "0" ]]; then
    echo "FAIL: LB saw $packets packet(s) sourced from the VIP - DSR bypass broken"
    overall_rc=1
else
    echo "    OK: 0 packets"
fi

if [[ "$overall_rc" -eq 0 ]]; then
    echo ""
    echo "ALL FLOW-REGEN TESTS PASSED"
else
    echo ""
    echo "SOME FLOW-REGEN TESTS FAILED"
fi
exit "$overall_rc"
