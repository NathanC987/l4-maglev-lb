#!/usr/bin/env bash
# Draining verification: proves a backend marked DRAINING (via the
# --admin-fifo "DRAIN <id>" command) keeps serving flows already pinned to
# it, with ZERO drops - unlike a plain health-check failure, where
# drops_backend_unhealthy is expected to spike (see run-flow-regen-test.sh).
# Holds one flow open on the survivor backend, drains that SAME backend
# (not the other one - the interesting case is disrupting the backend the
# held flow actually depends on), then confirms: (1) table regenerates,
# (2) the held flow's reply still comes from the draining backend with no
# new backend_unhealthy drops, (3) a brand new flow opened during the drain
# avoids the draining backend entirely. Tears everything down on exit.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=topology.sh
source "$SCRIPT_DIR/topology.sh"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
INTEGRATION_DIR="$REPO_ROOT/test/integration"

BIN="${1:-${MAGLEV_LB_BIN:-$REPO_ROOT/build/maglev-lb}}"
LB_LOG="$PID_DIR/maglev-lb-drain.log"
ADMIN_FIFO="$PID_DIR/admin.fifo"
NFT_TABLE=l4mlbdrain

if [[ $EUID -ne 0 ]]; then
    echo "run-drain-test.sh must be run as root" >&2
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

echo "==> Creating admin FIFO at $ADMIN_FIFO"
rm -f "$ADMIN_FIFO"
mkfifo "$ADMIN_FIFO"

echo "==> Starting maglev-lb (fast health-check settings, --admin-fifo enabled)"
mkdir -p "$PID_DIR"
: >"$LB_LOG"
ip netns exec "$NS_LB" "$BIN" \
    --iface "$VETH_LB_NS" \
    --vip "$VIP" \
    --director-ip "$LB_IP" \
    --backend "$BE1_IP:$BACKEND_PORT" \
    --backend "$BE2_IP:$BACKEND_PORT" \
    --hc-interval-ms 200 --hc-timeout-ms 300 --hc-rise 2 --hc-fall 2 \
    --admin-fifo "$ADMIN_FIFO" \
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

metrics() {
    ip netns exec "$NS_LB" curl -s "http://127.0.0.1:$METRICS_PORT/metrics"
}
drops_unhealthy() {
    metrics | grep -oP 'maglev_lb_drops_total\{reason="backend_unhealthy"\} \K[0-9]+'
}
table_generation() {
    metrics | grep -oP '^maglev_lb_table_generation \K[0-9]+'
}

echo "==> Opening a long-lived flow"
coproc CPROC {
    ip netns exec "$NS_CLIENT" python3 "$INTEGRATION_DIR/open_flow.py" "$VIP" "$BACKEND_PORT"
}
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
    survivor_id=0
    other="BACKEND2"
else
    survivor_id=1
    other="BACKEND1"
fi
echo "    held flow is on $survivor (id=$survivor_id); draining $survivor itself"

baseline_drops="$(drops_unhealthy)"
baseline_gen="$(table_generation)"
echo "==> Baseline: drops_backend_unhealthy=$baseline_drops table_generation=$baseline_gen"

echo "==> Draining backend id=$survivor_id via the admin FIFO"
echo "DRAIN $survivor_id" >"$ADMIN_FIFO"

echo "==> Waiting for the table to regenerate in response to the drain"
regen_ok=0
for _ in $(seq 1 50); do
    new_gen="$(table_generation)"
    if [[ -n "$new_gen" && "$new_gen" -gt "$baseline_gen" ]]; then
        regen_ok=1
        break
    fi
    sleep 0.2
done
if [[ "$regen_ok" -ne 1 ]]; then
    echo "FAIL: table never regenerated after the DRAIN command; log follows:" >&2
    cat "$LB_LOG" >&2
    overall_rc=1
fi
sed -n '/health:/p;/table_generator:/p' "$LB_LOG" | sed 's/^/    /'

echo "==> Checking maglev_lb_backend_draining reflects the drain"
draining_val="$(metrics | grep -oP "maglev_lb_backend_draining\{backend_id=\"$survivor_id\"[^}]*\} \K[0-9]+")"
if [[ "$draining_val" == "1" ]]; then
    echo "    OK: backend id=$survivor_id reports draining=1"
else
    echo "FAIL: backend id=$survivor_id draining gauge = '$draining_val' (expected 1)"
    overall_rc=1
fi

echo "==> Continuing the held flow (must still be answered by $survivor - draining, not down)"
echo >&"${CPROC[1]}"
if ! IFS= read -r -t 5 line2 <&"${CPROC[0]}"; then
    echo "FAIL: held flow produced no second reply after the drain"
    overall_rc=1
else
    echo "    $line2"
    reply2="${line2#REPLY2 }"
    if [[ "$reply2" != "$survivor" ]]; then
        echo "FAIL: held flow moved from $survivor to $reply2 during its own drain - a draining"
        echo "      backend must keep serving flows already pinned to it"
        overall_rc=1
    else
        echo "    OK: held flow stayed pinned to $survivor while it was draining"
    fi
fi
wait "$CPROC_PID" 2>/dev/null || true
CPROC_PID=""

echo "==> Checking drops_backend_unhealthy did NOT increase (the headline property)"
final_drops="$(drops_unhealthy)"
echo "    baseline=$baseline_drops final=$final_drops"
if [[ "$final_drops" == "$baseline_drops" ]]; then
    echo "    OK: zero backend_unhealthy drops across the drain - unlike a real failure"
    echo "        (see run-flow-regen-test.sh, where this counter is expected to move)"
else
    echo "FAIL: drops_backend_unhealthy went from $baseline_drops to $final_drops during a"
    echo "      graceful drain - draining must not disrupt already-pinned flows"
    overall_rc=1
fi

echo "==> Sampling new flows opened during the drain (must all land on $other, never $survivor)"
mismatch=0
for i in $(seq 1 10); do
    got="$(ip netns exec "$NS_CLIENT" python3 "$INTEGRATION_DIR/check_backend.py" "$VIP" "$BACKEND_PORT" 2>/dev/null || echo "ERROR")"
    if [[ "$got" != "$other" ]]; then
        echo "    sample $i: got $got (expected $other)"
        mismatch=$((mismatch + 1))
    fi
done
if [[ "$mismatch" -eq 0 ]]; then
    echo "    OK: all 10 new connections avoided the draining backend"
else
    echo "FAIL: $mismatch/10 new connections landed on the draining backend $survivor"
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
    echo "ALL DRAIN TESTS PASSED"
else
    echo ""
    echo "SOME DRAIN TESTS FAILED"
fi
exit "$overall_rc"
