#!/usr/bin/env bash
# Raw packet-rate ceiling test: floods the VIP with minimum-size UDP frames
# via the kernel's pktgen module (no new code - pktgen ships in the kernel),
# from inside the CLIENT netns, and measures how many pps maglev-lb's
# datapath actually sustains (its own rx/tx counters, not just what pktgen
# claims to have sent). Deliberately small and separate from the realistic-
# traffic scenarios: this measures a different question (the raw ceiling of
# parse -> hash -> conntrack -> GRE-build -> send), not session-shaped
# realism, so it deliberately REUSES the smaller, already-verified M1-M3
# 2-backend topology (scripts/setup-netns.sh) rather than the bench one - a
# one-way flood needs no real backend responses. See docs/benchmarking.md.
#
# UDP source port is randomized per packet (flag UDPSRC_RND) so this
# genuinely exercises conntrack/Maglev's hash spread across many distinct
# flows, not just one. pkt_size is fixed at the minimum (64 bytes) so the
# test is pps/CPU-bound, not bandwidth-bound - the standard way pps-ceiling
# tests are run. clone_skb is deliberately 0 (disabled): cloning reuses one
# skb N times before pktgen re-randomizes its fields, which would undermine
# exactly the flow-diversity property above; the LB's own userspace path is
# almost certainly the binding constraint here anyway, not pktgen's own
# packet-construction rate.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$BENCH_DIR/../.." && pwd)"
# shellcheck source=../../topology.sh
source "$REPO_ROOT/scripts/topology.sh"

BIN="${1:-${MAGLEV_LB_BIN:-$REPO_ROOT/build/maglev-lb}}"
FLOOD_DURATION_S="${FLOOD_DURATION_S:-15}"
LB_LOG="$PID_DIR/maglev-lb-ppsceiling.log"
RESULTS_DIR="$REPO_ROOT/scripts/bench/.run/results"

if [[ $EUID -ne 0 ]]; then
    echo "run-pps-ceiling-test.sh must be run as root" >&2
    exit 1
fi
if [[ ! -x "$BIN" ]]; then
    echo "maglev-lb binary not found at $BIN (build it first, or pass its path)" >&2
    exit 1
fi
if ! modprobe pktgen 2>/dev/null && [[ ! -d /proc/net/pktgen ]]; then
    echo "FAIL: could not load the pktgen kernel module (CONFIG_NET_PKTGEN?)" >&2
    exit 1
fi

LB_PID=""
PERF_PID=""

cleanup() {
    echo "==> Cleaning up"
    ip netns exec "$NS_CLIENT" sh -c 'echo stop > /proc/net/pktgen/pgctrl' 2>/dev/null || true
    [[ -n "$PERF_PID" ]] && kill "$PERF_PID" 2>/dev/null
    if [[ -n "$LB_PID" ]]; then
        kill "$LB_PID" 2>/dev/null || true
        wait "$LB_PID" 2>/dev/null || true
    fi
    "$REPO_ROOT/scripts/teardown-netns.sh"
}
trap cleanup EXIT

mkdir -p "$RESULTS_DIR"

echo "==> Setting up the M1-M3 topology"
if ! "$REPO_ROOT/scripts/setup-netns.sh"; then
    echo "FAIL: setup-netns.sh failed" >&2
    exit 1
fi

echo "==> Starting maglev-lb"
mkdir -p "$PID_DIR"
: >"$LB_LOG"
ip netns exec "$NS_LB" "$BIN" \
    --iface "$VETH_LB_NS" --vip "$VIP" --director-ip "$LB_IP" \
    --backend "$BE1_IP:$BACKEND_PORT" --backend "$BE2_IP:$BACKEND_PORT" \
    >"$LB_LOG" 2>&1 &
LB_PID=$!

ready=0
for _ in $(seq 1 50); do
    kill -0 "$LB_PID" 2>/dev/null || { echo "FAIL: maglev-lb exited early"; cat "$LB_LOG" >&2; exit 1; }
    grep -q "starting datapath" "$LB_LOG" 2>/dev/null && { ready=1; break; }
    sleep 0.2
done
[[ "$ready" -eq 1 ]] || { echo "FAIL: maglev-lb not ready in time" >&2; cat "$LB_LOG" >&2; exit 1; }

LB_MAC="$(ip netns exec "$NS_LB" cat "/sys/class/net/$VETH_LB_NS/address")"
echo "==> LB MAC on $VETH_LB_NS: $LB_MAC"

echo "==> Configuring pktgen inside $NS_CLIENT (dev=$VETH_CLIENT_NS)"
ip netns exec "$NS_CLIENT" sh -c "
    set -e
    echo \"rem_device_all\" > /proc/net/pktgen/kpktgend_0 2>/dev/null || true
    echo \"add_device $VETH_CLIENT_NS\" > /proc/net/pktgen/kpktgend_0
    PGDEV=/proc/net/pktgen/$VETH_CLIENT_NS
    echo \"count 0\" > \$PGDEV
    echo \"clone_skb 0\" > \$PGDEV
    echo \"pkt_size 64\" > \$PGDEV
    echo \"delay 0\" > \$PGDEV
    echo \"dst_mac $LB_MAC\" > \$PGDEV
    echo \"dst_min $VIP\" > \$PGDEV
    echo \"dst_max $VIP\" > \$PGDEV
    echo \"udp_dst_min $BACKEND_PORT\" > \$PGDEV
    echo \"udp_dst_max $BACKEND_PORT\" > \$PGDEV
    echo \"udp_src_min 1024\" > \$PGDEV
    echo \"udp_src_max 65000\" > \$PGDEV
    echo \"flag UDPSRC_RND\" > \$PGDEV
"

metrics() { ip netns exec "$NS_LB" curl -s "http://127.0.0.1:$METRICS_PORT/metrics"; }
rx_packets() { metrics | grep -oP '^maglev_lb_rx_packets_total \K[0-9]+'; }
tx_packets() { metrics | grep -oP '^maglev_lb_tx_packets_total \K[0-9]+'; }
drops_all() { metrics | grep -oP 'maglev_lb_drops_total\{reason="[a-z_]+"\} \K[0-9]+' | awk '{s+=$1} END {print s+0}'; }

if command -v perf >/dev/null 2>&1; then
    perf stat -p "$LB_PID" -o "$RESULTS_DIR/pps-ceiling-perf-stat.txt" 2>/dev/null &
    PERF_PID=$!
    echo "==> perf stat attached to maglev-lb (PID $LB_PID)"
else
    echo "==> perf not found; skipping CPU accounting"
fi

rx_before="$(rx_packets)"
tx_before="$(tx_packets)"
drops_before="$(drops_all)"
t0=$(date +%s.%N)

echo "==> Flooding for ${FLOOD_DURATION_S}s (pkt_size=64, clone_skb=0, randomized UDP src port)"
ip netns exec "$NS_CLIENT" sh -c 'echo start > /proc/net/pktgen/pgctrl' &
FLOOD_PID=$!
sleep "$FLOOD_DURATION_S"
ip netns exec "$NS_CLIENT" sh -c 'echo stop > /proc/net/pktgen/pgctrl'
wait "$FLOOD_PID" 2>/dev/null || true

t1=$(date +%s.%N)
elapsed="$(awk "BEGIN { printf \"%.3f\", $t1 - $t0 }")"

if [[ -n "$PERF_PID" ]]; then
    kill -INT "$PERF_PID" 2>/dev/null
    wait "$PERF_PID" 2>/dev/null || true
fi

rx_after="$(rx_packets)"
tx_after="$(tx_packets)"
drops_after="$(drops_all)"

pktgen_result="$(ip netns exec "$NS_CLIENT" cat "/proc/net/pktgen/$VETH_CLIENT_NS")"

still_alive=0
kill -0 "$LB_PID" 2>/dev/null && [[ -n "$(metrics)" ]] && still_alive=1

rx_delta=$((rx_after - rx_before))
tx_delta=$((tx_after - tx_before))
drops_delta=$((drops_after - drops_before))
rx_pps="$(awk "BEGIN { printf \"%.0f\", $rx_delta / $elapsed }")"
tx_pps="$(awk "BEGIN { printf \"%.0f\", $tx_delta / $elapsed }")"

{
    echo "=== pps ceiling: $(date -Iseconds) ==="
    echo "flood duration: ${elapsed}s"
    echo "LB rx: $rx_delta packets (${rx_pps} pps)"
    echo "LB tx: $tx_delta packets (${tx_pps} pps)"
    echo "LB drops (all reasons) delta: $drops_delta"
    echo ""
    echo "--- pktgen's own report ($VETH_CLIENT_NS) ---"
    echo "$pktgen_result"
} | tee "$RESULTS_DIR/pps-ceiling-$(date +%Y%m%d-%H%M%S).txt"

if [[ -f "$RESULTS_DIR/pps-ceiling-perf-stat.txt" ]]; then
    echo ""
    echo "--- perf stat ---"
    cat "$RESULTS_DIR/pps-ceiling-perf-stat.txt"
fi

echo ""
if [[ "$still_alive" -eq 1 ]]; then
    echo "OK: maglev-lb survived the flood and /metrics is still responsive"
    exit 0
else
    echo "FAIL: maglev-lb is not alive/responsive after the flood"
    exit 1
fi
