#!/usr/bin/env bash
# Controlled, quantitative sibling of scripts/run-drain-test.sh (which just
# proves draining is correct on the minimal M1-M3 topology). This runs BOTH
# a crash and a drain back-to-back, against DIFFERENT backends in the SAME
# fixed-load bench topology, and reports numbers side by side: detection/
# regen timing, and - the headline comparison - the drops_backend_unhealthy
# delta during each transition (expected: >0 for the crash, exactly 0 for
# the drain). Fixed (non-randomized) bitrate/duration/concurrency, unlike
# scripts/bench/demo/run-streaming-demo.sh, so repeated runs are comparable.
# See docs/benchmarking.md.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# Fixed for this test, regardless of the ambient default - MUST be exported
# (and set before sourcing bench-topology.sh) so the child setup/teardown
# scripts below, each of which sources bench-topology.sh in its own
# process, see the same value rather than falling back to their own default.
export BENCH_N_BACKENDS=4
# shellcheck source=../bench-topology.sh
source "$BENCH_DIR/bench-topology.sh"
REPO_ROOT="$(cd "$BENCH_DIR/../.." && pwd)"

BIN="${1:-${MAGLEV_LB_BIN:-$REPO_ROOT/build/maglev-lb}}"
LB_LOG="$PID_DIR/maglev-lb-crashvsdrain.log"
FIXED_BITRATE_KBPS=3000
FIXED_DURATION_S=25
VIEWER_CONCURRENCY=20

if [[ $EUID -ne 0 ]]; then
    echo "run-crash-vs-drain-test.sh must be run as root" >&2
    exit 1
fi
if [[ ! -x "$BIN" ]]; then
    echo "maglev-lb binary not found at $BIN (build it first, or pass its path)" >&2
    exit 1
fi

LB_PID=""
VIEWER_PID=""
BE_PIDS=()

cleanup() {
    echo "==> Cleaning up"
    [[ -n "$VIEWER_PID" ]] && kill "$VIEWER_PID" 2>/dev/null
    for p in "${BE_PIDS[@]}"; do kill "$p" 2>/dev/null; done
    [[ -n "$LB_PID" ]] && { kill "$LB_PID" 2>/dev/null; wait "$LB_PID" 2>/dev/null; }
    "$BENCH_DIR/teardown-bench-netns.sh"
}
trap cleanup EXIT

echo "==> Setting up bench topology ($BENCH_N_BACKENDS backends, all live)"
if ! "$BENCH_DIR/setup-bench-netns.sh"; then
    echo "FAIL: setup-bench-netns.sh failed" >&2
    exit 1
fi

echo "==> Starting $BENCH_N_BACKENDS streaming backends (fixed bitrate=${FIXED_BITRATE_KBPS}kbps duration=${FIXED_DURATION_S}s)"
for i in $(seq 1 "$BENCH_N_BACKENDS"); do
    ip netns exec "$(be_ns "$i")" python3 "$BENCH_DIR/streaming_server.py" \
        0.0.0.0 "$BACKEND_PORT" "BE$i" \
        --bitrate-min-kbps "$FIXED_BITRATE_KBPS" --bitrate-max-kbps "$FIXED_BITRATE_KBPS" \
        --duration-min-s "$FIXED_DURATION_S" --duration-max-s "$FIXED_DURATION_S" \
        >"$PID_DIR/BE$i.log" 2>&1 &
    BE_PIDS+=("$!")
done
sleep 0.5

echo "==> Creating admin FIFO"
mkfifo "$ADMIN_FIFO"

echo "==> Starting maglev-lb"
BACKEND_ARGS=()
for i in $(seq 1 "$BENCH_N_BACKENDS"); do
    BACKEND_ARGS+=(--backend "$(be_ip "$i"):$BACKEND_PORT")
done
: >"$LB_LOG"
ip netns exec "$NS_LB" "$BIN" \
    --iface "$VETH_LB_NS" --vip "$VIP" --director-ip "$LB_IP" \
    "${BACKEND_ARGS[@]}" \
    --hc-interval-ms 200 --hc-timeout-ms 300 --hc-rise 2 --hc-fall 2 \
    --admin-fifo "$ADMIN_FIFO" \
    >"$LB_LOG" 2>&1 &
LB_PID=$!

ready=0
for _ in $(seq 1 50); do
    kill -0 "$LB_PID" 2>/dev/null || { echo "FAIL: maglev-lb exited early"; cat "$LB_LOG" >&2; exit 1; }
    grep -q "starting datapath" "$LB_LOG" 2>/dev/null && { ready=1; break; }
    sleep 0.2
done
[[ "$ready" -eq 1 ]] || { echo "FAIL: maglev-lb not ready in time" >&2; cat "$LB_LOG" >&2; exit 1; }

metrics() { ip netns exec "$NS_LB" curl -s "http://127.0.0.1:$METRICS_PORT/metrics"; }
drops_unhealthy() { metrics | grep -oP 'maglev_lb_drops_total\{reason="backend_unhealthy"\} \K[0-9]+'; }
table_generation() { metrics | grep -oP '^maglev_lb_table_generation \K[0-9]+'; }

echo "==> Starting viewer load (concurrency=$VIEWER_CONCURRENCY, fixed request pattern)"
ip netns exec "$NS_CLIENT" python3 "$BENCH_DIR/viewer_gen.py" "$VIP" "$BACKEND_PORT" \
    --concurrency "$VIEWER_CONCURRENCY" --metrics-port 0 --max-runtime-s 120 \
    >"$PID_DIR/viewer.log" 2>&1 &
VIEWER_PID=$!

echo "==> Waiting for load to reach steady state"
sleep 5

# --- Phase 1: crash backend 1 -------------------------------------------
echo ""
echo "=== Phase 1: CRASH (SIGKILL backend 1, id=0) ==="
baseline_drops="$(drops_unhealthy)"
baseline_gen="$(table_generation)"
t0=$(date +%s.%N)
kill "${BE_PIDS[0]}" 2>/dev/null

regen_ok=0
for _ in $(seq 1 50); do
    g="$(table_generation)"
    [[ -n "$g" && "$g" -gt "$baseline_gen" ]] && { regen_ok=1; break; }
    sleep 0.1
done
t1=$(date +%s.%N)
crash_detect_s="$(awk "BEGIN { printf \"%.2f\", $t1 - $t0 }")"
crash_drops_delta="$(( $(drops_unhealthy) - baseline_drops ))"
if [[ "$regen_ok" -eq 1 ]]; then
    echo "    table regenerated ${crash_detect_s}s after SIGKILL"
else
    echo "    FAIL: table never regenerated after crash"
fi
echo "    drops_backend_unhealthy delta during crash: $crash_drops_delta"

echo "==> Letting load settle"
sleep 5

# --- Phase 2: drain backend 2 -------------------------------------------
echo ""
echo "=== Phase 2: GRACEFUL DRAIN (DRAIN backend 2, id=1) ==="
baseline_drops2="$(drops_unhealthy)"
baseline_gen2="$(table_generation)"
t2=$(date +%s.%N)
echo "DRAIN 1" >"$ADMIN_FIFO"

regen_ok2=0
for _ in $(seq 1 50); do
    g="$(table_generation)"
    [[ -n "$g" && "$g" -gt "$baseline_gen2" ]] && { regen_ok2=1; break; }
    sleep 0.1
done
t3=$(date +%s.%N)
drain_detect_s="$(awk "BEGIN { printf \"%.2f\", $t3 - $t2 }")"
drain_drops_delta="$(( $(drops_unhealthy) - baseline_drops2 ))"
if [[ "$regen_ok2" -eq 1 ]]; then
    echo "    table regenerated ${drain_detect_s}s after DRAIN command"
else
    echo "    FAIL: table never regenerated after drain"
fi
echo "    drops_backend_unhealthy delta during drain:  $drain_drops_delta"

# NOT waited for here: maglev_lb_backend_active_flows reaching zero.
# conntrack has no FIN/RST awareness at all (src/conntrack/conntrack.h) - a
# flow's slot is only reclaimed by the idle-timeout reaper, TCP_IDLE_TIMEOUT_NS
# = 120s hardcoded in src/main.c, entirely independent of draining. So
# "fully drained" in the active-flows-count sense always takes >=120s
# regardless of anything this test does - that's what
# scripts/bench/demo/run-streaming-demo.sh's much longer cool-down phase is
# for (BENCH_COOLDOWN_S > 120s), not a fast controlled comparison like this
# one. What's fast and meaningful here instead: traffic to the draining
# backend should stop growing shortly after its last session naturally
# finishes, well before that 120s floor.
echo "==> Confirming traffic to the draining backend actually stops (not waiting for the"
echo "    120s conntrack idle-timeout floor - active_flows won't reach 0 until then"
echo "    regardless of draining; see docs/draining.md)"
sleep "$((FIXED_DURATION_S + 3))" # comfortably past every session's fixed duration
pkt_a="$(metrics | grep -oP 'maglev_lb_backend_packets_total\{backend_id="1"[^}]*\} \K[0-9]+')"
sleep 3
pkt_b="$(metrics | grep -oP 'maglev_lb_backend_packets_total\{backend_id="1"[^}]*\} \K[0-9]+')"
traffic_stopped_ok=0
if [[ "$pkt_a" == "$pkt_b" ]]; then
    traffic_stopped_ok=1
    echo "    OK: backend 2's packet counter is flat ($pkt_a) - no more traffic reaching it"
else
    echo "    FAIL: backend 2's packet counter still moving ($pkt_a -> $pkt_b)"
fi

echo ""
echo "=== Summary ==="
printf "%-12s %-16s %-24s\n" "" "regen latency" "drops_backend_unhealthy"
printf "%-12s %-16s %-24s\n" "crash" "${crash_detect_s}s" "$crash_drops_delta"
printf "%-12s %-16s %-24s\n" "drain" "${drain_detect_s}s" "$drain_drops_delta (expected 0)"

overall_rc=0
[[ "$regen_ok" -eq 1 && "$regen_ok2" -eq 1 && "$traffic_stopped_ok" -eq 1 ]] || overall_rc=1
[[ "$crash_drops_delta" -gt 0 ]] || { echo "NOTE: expected >0 drops during the crash, got $crash_drops_delta"; }
[[ "$drain_drops_delta" -eq 0 ]] || { echo "FAIL: expected exactly 0 drops during the drain, got $drain_drops_delta"; overall_rc=1; }

kill "$VIEWER_PID" 2>/dev/null
wait "$VIEWER_PID" 2>/dev/null
VIEWER_PID=""

if [[ "$overall_rc" -eq 0 ]]; then
    echo ""
    echo "CRASH-VS-DRAIN CONTROLLED TEST PASSED"
else
    echo ""
    echo "CRASH-VS-DRAIN CONTROLLED TEST FAILED"
fi
exit "$overall_rc"
