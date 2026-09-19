#!/usr/bin/env bash
# Controlled flow-churn test: fixed concurrency, fixed SHORT session
# duration (much shorter than conntrack's 120s idle timeout - see
# src/conntrack/conntrack.h), sustained for a fixed window, generating a
# steady, repeatable rate of NEW flow opens. Exercises conntrack's
# pre-allocated pool on the INSERT side: the pass/fail criterion is zero
# maglev_lb_drops_total{reason="conntrack_full"} despite sustained churn,
# cross-checked against the viewer's own started-session count.
#
# What this deliberately does NOT test: reaper EVICTION. conntrack has no
# FIN/RST awareness (src/conntrack/conntrack.h) - a flow's slot is only
# reclaimed after 120s of idleness (TCP_IDLE_TIMEOUT_NS in src/main.c),
# regardless of how quickly the TCP connection itself actually closed. A
# fast, repeatable controlled test can't honestly wait that long every run;
# scripts/bench/demo/run-streaming-demo.sh's cool-down phase
# (BENCH_COOLDOWN_S > 120s) is where eviction is actually proven. See
# docs/benchmarking.md.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
export BENCH_N_BACKENDS=2
# shellcheck source=../bench-topology.sh
source "$BENCH_DIR/bench-topology.sh"
REPO_ROOT="$(cd "$BENCH_DIR/../.." && pwd)"

BIN="${1:-${MAGLEV_LB_BIN:-$REPO_ROOT/build/maglev-lb}}"
LB_LOG="$PID_DIR/maglev-lb-churn.log"
FIXED_BITRATE_KBPS=2000
FIXED_DURATION_S=2       # short: many sessions cycle through per backend
VIEWER_CONCURRENCY=50
LOAD_WINDOW_S=25

if [[ $EUID -ne 0 ]]; then
    echo "run-churn-test.sh must be run as root" >&2
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

echo "==> Setting up bench topology ($BENCH_N_BACKENDS backends)"
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
active_flows() { metrics | grep -oP '^maglev_lb_conntrack_active_flows \K[0-9]+'; }
drops_full() { metrics | grep -oP 'maglev_lb_drops_total\{reason="conntrack_full"\} \K[0-9]+'; }

baseline_flows="$(active_flows)"
baseline_drops="$(drops_full)"
echo "==> Baseline: active_flows=$baseline_flows drops_conntrack_full=$baseline_drops"

echo "==> Sustaining churn (concurrency=$VIEWER_CONCURRENCY, ${FIXED_DURATION_S}s sessions) for ${LOAD_WINDOW_S}s"
: >"$PID_DIR/viewer-churn.log"
ip netns exec "$NS_CLIENT" python3 "$BENCH_DIR/viewer_gen.py" "$VIP" "$BACKEND_PORT" \
    --concurrency "$VIEWER_CONCURRENCY" --metrics-port "$VIEWER_METRICS_PORT" \
    --max-runtime-s "$LOAD_WINDOW_S" --arrival-jitter-s 0.01 \
    >"$PID_DIR/viewer-churn.log" 2>&1 &
VIEWER_PID=$!

sleep "$LOAD_WINDOW_S"

echo "==> Waiting for viewer_gen to finish winding down"
wait "$VIEWER_PID" 2>/dev/null
VIEWER_PID=""

final_flows="$(active_flows)"
final_drops="$(drops_full)"
started="$(grep -oP '"sessions_started": \K[0-9]+' "$PID_DIR/viewer-churn.log" | tail -1)"
completed_content_end="$(grep -oP '"content_end": \K[0-9]+' "$PID_DIR/viewer-churn.log" | tail -1)"

echo ""
echo "=== Results ==="
echo "sessions started (viewer-side):       ${started:-unknown}"
echo "sessions completed cleanly:           ${completed_content_end:-unknown}"
echo "conntrack_active_flows: baseline=$baseline_flows -> final=$final_flows"
echo "drops_backend conntrack_full:  baseline=$baseline_drops -> final=$final_drops"
echo ""
echo "NOTE: active_flows is expected to stay HIGH (not shrink) even though each session"
echo "      only lasts ${FIXED_DURATION_S}s - conntrack has no FIN awareness, so entries are only"
echo "      reclaimed after the 120s idle timeout, independent of TCP-level churn. See"
echo "      docs/draining.md and docs/benchmarking.md."

overall_rc=0
drops_delta=$((final_drops - baseline_drops))
if [[ "$drops_delta" -eq 0 ]]; then
    echo "OK: zero conntrack_full drops despite sustained flow-open churn"
else
    echo "FAIL: $drops_delta conntrack_full drop(s) during the churn window - pool undersized"
    echo "      for this load (--max-flows), or a real regression"
    overall_rc=1
fi
if [[ -n "$final_flows" && "$final_flows" -gt "$baseline_flows" ]]; then
    echo "OK: active_flows grew ($baseline_flows -> $final_flows), confirming churn was real"
else
    echo "FAIL: active_flows did not grow - viewer load may not have reached the backends"
    overall_rc=1
fi

if [[ "$overall_rc" -eq 0 ]]; then
    echo ""
    echo "CHURN TEST PASSED"
else
    echo ""
    echo "CHURN TEST FAILED"
fi
exit "$overall_rc"
