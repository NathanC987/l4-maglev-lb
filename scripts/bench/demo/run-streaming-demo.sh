#!/usr/bin/env bash
# The "Netflix-style" live demo: brings up the bench topology, starts
# maglev-lb with the FULL backend list but only some backends' streaming
# responders actually running, starts a randomized concurrent viewer load,
# and on a scripted timeline: crashes one backend, gracefully drains
# another, then scales out the still-dormant ones - reusing the M2 health
# checker unchanged for both the crash and scale-out transitions, and
# Phase A's new admin-FIFO mechanism for the drain. Meant to be watched
# live on the bench Grafana dashboard while it runs - see
# docs/benchmarking.md for what to look for and why each timing default is
# what it is. Unlike scripts/bench/controlled/*, parameters here are
# intentionally randomized/narratively paced, not fixed for exact
# cross-run comparison.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=../bench-topology.sh
source "$BENCH_DIR/bench-topology.sh"
REPO_ROOT="$(cd "$BENCH_DIR/../.." && pwd)"

BIN="${1:-${MAGLEV_LB_BIN:-$REPO_ROOT/build/maglev-lb}}"

# --- Tunables (all env-overridable) -------------------------------------
BENCH_INITIAL_LIVE="${BENCH_INITIAL_LIVE:-5}"       # of BENCH_N_BACKENDS (default 8, see bench-topology.sh)
BENCH_CONCURRENCY="${BENCH_CONCURRENCY:-200}"
BENCH_BITRATE_MIN_KBPS="${BENCH_BITRATE_MIN_KBPS:-2000}"
BENCH_BITRATE_MAX_KBPS="${BENCH_BITRATE_MAX_KBPS:-6000}"
BENCH_SESSION_MIN_S="${BENCH_SESSION_MIN_S:-8}"
BENCH_SESSION_MAX_S="${BENCH_SESSION_MAX_S:-30}"
BENCH_RAMP_S="${BENCH_RAMP_S:-20}"
BENCH_PRECRASH_LOAD_S="${BENCH_PRECRASH_LOAD_S:-90}"
BENCH_POSTCRASH_LOAD_S="${BENCH_POSTCRASH_LOAD_S:-60}"
BENCH_POSTDRAIN_LOAD_S="${BENCH_POSTDRAIN_LOAD_S:-60}"
BENCH_POSTSCALE_LOAD_S="${BENCH_POSTSCALE_LOAD_S:-90}"
BENCH_COOLDOWN_S="${BENCH_COOLDOWN_S:-150}" # > 120s TCP idle timeout + margin - see docs/benchmarking.md
BENCH_RUN_PERF="${BENCH_RUN_PERF:-1}"
KEEP_RUNNING="${KEEP_RUNNING:-0}" # 1 = skip teardown at the end (for continued dashboard watching)

LB_LOG="$PID_DIR/maglev-lb-demo.log"
RESULTS_DIR="$PID_DIR/results"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
REPORT_FILE="$RESULTS_DIR/demo-report-$TIMESTAMP.txt"

if [[ $EUID -ne 0 ]]; then
    echo "run-streaming-demo.sh must be run as root" >&2
    exit 1
fi
if [[ ! -x "$BIN" ]]; then
    echo "maglev-lb binary not found at $BIN (build it first, or pass its path)" >&2
    exit 1
fi
if [[ "$BENCH_INITIAL_LIVE" -ge "$BENCH_N_BACKENDS" ]]; then
    echo "BENCH_INITIAL_LIVE ($BENCH_INITIAL_LIVE) must be < BENCH_N_BACKENDS ($BENCH_N_BACKENDS)" >&2
    exit 1
fi

LB_PID=""
VIEWER_PID=""
PERF_PID=""
BE_PIDS=()   # index 1..BENCH_N_BACKENDS, empty string if not (yet) started

cleanup() {
    echo "==> Cleaning up"
    [[ -n "$PERF_PID" ]] && kill "$PERF_PID" 2>/dev/null
    [[ -n "$VIEWER_PID" ]] && kill "$VIEWER_PID" 2>/dev/null
    for p in "${BE_PIDS[@]:-}"; do [[ -n "$p" ]] && kill "$p" 2>/dev/null; done
    if [[ -n "$LB_PID" ]]; then
        kill "$LB_PID" 2>/dev/null || true
        wait "$LB_PID" 2>/dev/null || true
    fi
    if [[ "$KEEP_RUNNING" -eq 1 ]]; then
        echo "==> KEEP_RUNNING=1: leaving the bench topology up for continued dashboard watching."
        echo "    Tear it down later with: sudo scripts/bench/teardown-bench-netns.sh"
    else
        "$BENCH_DIR/teardown-bench-netns.sh"
    fi
}
trap cleanup EXIT

mkdir -p "$RESULTS_DIR"

echo "==> Setting up bench topology ($BENCH_N_BACKENDS backends, $BENCH_INITIAL_LIVE live initially)"
if ! "$BENCH_DIR/setup-bench-netns.sh"; then
    echo "FAIL: setup-bench-netns.sh failed" >&2
    exit 1
fi

start_backend() {
    local i=$1
    ip netns exec "$(be_ns "$i")" python3 "$BENCH_DIR/streaming_server.py" \
        0.0.0.0 "$BACKEND_PORT" "BE$i" \
        --bitrate-min-kbps "$BENCH_BITRATE_MIN_KBPS" --bitrate-max-kbps "$BENCH_BITRATE_MAX_KBPS" \
        --duration-min-s "$BENCH_SESSION_MIN_S" --duration-max-s "$BENCH_SESSION_MAX_S" \
        >"$PID_DIR/BE$i.log" 2>&1 &
    BE_PIDS[$i]=$!
}

echo "==> Starting $BENCH_INITIAL_LIVE of $BENCH_N_BACKENDS backend responders (the rest stay"
echo "    dormant - namespace/GRE/VIP already exist, just no process listening yet)"
for i in $(seq 1 "$BENCH_INITIAL_LIVE"); do
    start_backend "$i"
done
sleep 0.5

echo "==> Starting maglev-lb with the FULL $BENCH_N_BACKENDS-backend list"
BACKEND_ARGS=()
for i in $(seq 1 "$BENCH_N_BACKENDS"); do
    BACKEND_ARGS+=(--backend "$(be_ip "$i"):$BACKEND_PORT")
done
mkfifo "$ADMIN_FIFO"
: >"$LB_LOG"
ip netns exec "$NS_LB" "$BIN" \
    --iface "$VETH_LB_NS" --vip "$VIP" --director-ip "$LB_IP" \
    "${BACKEND_ARGS[@]}" \
    --hc-interval-ms 500 --hc-timeout-ms 300 --hc-rise 2 --hc-fall 2 \
    --admin-fifo "$ADMIN_FIFO" \
    --metrics-port "$METRICS_PORT" \
    >"$LB_LOG" 2>&1 &
LB_PID=$!

ready=0
for _ in $(seq 1 50); do
    kill -0 "$LB_PID" 2>/dev/null || { echo "FAIL: maglev-lb exited early"; cat "$LB_LOG" >&2; exit 1; }
    grep -q "starting datapath" "$LB_LOG" 2>/dev/null && { ready=1; break; }
    sleep 0.2
done
[[ "$ready" -eq 1 ]] || { echo "FAIL: maglev-lb not ready in time" >&2; cat "$LB_LOG" >&2; exit 1; }
echo "    maglev-lb up (PID $LB_PID), metrics on :$METRICS_PORT"

metrics() { ip netns exec "$NS_LB" curl -s "http://127.0.0.1:$METRICS_PORT/metrics"; }
backends_eligible() { metrics | grep -oP '^maglev_lb_backends_eligible \K[0-9]+'; }
table_generation() { metrics | grep -oP '^maglev_lb_table_generation \K[0-9]+'; }
backend_up() { metrics | grep -oP "maglev_lb_backend_up\{backend_id=\"$1\"[^}]*\} \K[0-9]+"; }
backend_packets() { metrics | grep -oP "maglev_lb_backend_packets_total\{backend_id=\"$1\"[^}]*\} \K[0-9]+"; }
drops_unhealthy() { metrics | grep -oP 'maglev_lb_drops_total\{reason="backend_unhealthy"\} \K[0-9]+'; }
active_flows() { metrics | grep -oP '^maglev_lb_conntrack_active_flows \K[0-9]+'; }
evictions() { metrics | grep -oP '^maglev_lb_conntrack_evictions_total \K[0-9]+'; }

# The dormant backends start "healthy=true" by default (see
# src/backend/backend_manager.h) until the health checker actually proves
# otherwise, so viewer traffic must not start until they've genuinely
# converged to unhealthy/ineligible - otherwise some early sessions would
# be routed to a backend nothing is listening on yet.
echo "==> Waiting for initial convergence ($BENCH_INITIAL_LIVE backends eligible)"
converged=0
for _ in $(seq 1 100); do
    e="$(backends_eligible)"
    [[ "$e" == "$BENCH_INITIAL_LIVE" ]] && { converged=1; break; }
    sleep 0.2
done
[[ "$converged" -eq 1 ]] || echo "    WARNING: convergence check timed out (last value: $(backends_eligible))"
echo "    converged: $(backends_eligible) backends eligible"

if [[ "$BENCH_RUN_PERF" -eq 1 ]] && command -v perf >/dev/null 2>&1; then
    perf stat -p "$LB_PID" -o "$RESULTS_DIR/demo-perf-stat-$TIMESTAMP.txt" 2>/dev/null &
    PERF_PID=$!
    echo "==> perf stat attached to maglev-lb (PID $LB_PID)"
fi

echo "==> Starting viewer load: ramping to concurrency=$BENCH_CONCURRENCY over ${BENCH_RAMP_S}s"
arrival_jitter="$(awk "BEGIN { j = $BENCH_RAMP_S / $BENCH_CONCURRENCY; print (j < 0.01) ? 0.01 : j }")"
: >"$PID_DIR/viewer-demo.log"
ip netns exec "$NS_CLIENT" python3 "$BENCH_DIR/viewer_gen.py" "$VIP" "$BACKEND_PORT" \
    --concurrency "$BENCH_CONCURRENCY" --metrics-port "$VIEWER_METRICS_PORT" \
    --arrival-jitter-s "$arrival_jitter" \
    --max-session-s "$((BENCH_SESSION_MAX_S + 15))" \
    --max-runtime-s 0 \
    >"$PID_DIR/viewer-demo.log" 2>&1 &
VIEWER_PID=$!
echo "    viewer_gen up (PID $VIEWER_PID), metrics on :$VIEWER_METRICS_PORT"

baseline_drops="$(drops_unhealthy)"

echo ""
echo "==> [T+0] Sustained load for ${BENCH_PRECRASH_LOAD_S}s before the crash event"
sleep "$BENCH_PRECRASH_LOAD_S"

# ---- Crash event --------------------------------------------------------
crash_id=1
echo ""
echo "=== CRASH event: SIGKILL backend $crash_id (id=$((crash_id - 1))) ==="
crash_gen_before="$(table_generation)"
[[ -n "${BE_PIDS[$crash_id]:-}" ]] && kill "${BE_PIDS[$crash_id]}" 2>/dev/null
BE_PIDS[$crash_id]=""
for _ in $(seq 1 50); do
    g="$(table_generation)"
    [[ -n "$g" && "$g" -gt "$crash_gen_before" ]] && break
    sleep 0.2
done
echo "    backend $crash_id down: $(backend_up "$((crash_id - 1))"), table_generation=$(table_generation),"
echo "    drops_backend_unhealthy so far: $(( $(drops_unhealthy) - baseline_drops ))"

echo ""
echo "==> Sustained load for ${BENCH_POSTCRASH_LOAD_S}s after the crash"
sleep "$BENCH_POSTCRASH_LOAD_S"

# ---- Graceful drain event ------------------------------------------------
drain_id=2
echo ""
echo "=== DRAIN event: gracefully draining backend $drain_id (id=$((drain_id - 1))) ==="
drain_gen_before="$(table_generation)"
drain_drops_before="$(drops_unhealthy)"
echo "DRAIN $((drain_id - 1))" >"$ADMIN_FIFO"
for _ in $(seq 1 50); do
    g="$(table_generation)"
    [[ -n "$g" && "$g" -gt "$drain_gen_before" ]] && break
    sleep 0.2
done
echo "    table_generation=$(table_generation), drops_backend_unhealthy delta during drain:" \
     "$(( $(drops_unhealthy) - drain_drops_before )) (expect 0 - contrast with the crash above)"

echo ""
echo "==> Sustained load for ${BENCH_POSTDRAIN_LOAD_S}s (watch backend $drain_id's traffic taper"
echo "    to zero on the dashboard while its already-open sessions keep completing normally)"
sleep "$BENCH_POSTDRAIN_LOAD_S"
kill "${BE_PIDS[$drain_id]}" 2>/dev/null
BE_PIDS[$drain_id]=""
echo "    stopped backend $drain_id's responder process (its flows had already finished draining)"

# ---- Scale-out event ------------------------------------------------------
echo ""
echo "=== SCALE-OUT event: starting the $((BENCH_N_BACKENDS - BENCH_INITIAL_LIVE)) still-dormant backend(s) ==="
scale_gen_before="$(table_generation)"
for i in $(seq "$((BENCH_INITIAL_LIVE + 1))" "$BENCH_N_BACKENDS"); do
    echo "    starting backend $i responder"
    start_backend "$i"
done
for _ in $(seq 1 50); do
    g="$(table_generation)"
    [[ -n "$g" && "$g" -gt "$scale_gen_before" ]] && break
    sleep 0.2
done
echo "    backends_eligible=$(backends_eligible), table_generation=$(table_generation)"

echo ""
echo "==> Sustained load for ${BENCH_POSTSCALE_LOAD_S}s (watch the new backends' packet counters"
echo "    climb from zero on the dashboard as they start taking real traffic)"
sleep "$BENCH_POSTSCALE_LOAD_S"

# ---- Wind-down and cool-down ---------------------------------------------
echo ""
echo "=== Wind-down: telling viewer_gen to stop launching new sessions ==="
kill -USR1 "$VIEWER_PID" 2>/dev/null
wait "$VIEWER_PID" 2>/dev/null
VIEWER_PID=""
echo "    all viewer sessions finished naturally"

echo ""
echo "=== Cool-down: ${BENCH_COOLDOWN_S}s with no new connections (> conntrack's 120s idle"
echo "    timeout + margin - watch conntrack_active_flows drain toward zero and"
echo "    conntrack_evictions_total climb on the dashboard; see docs/benchmarking.md) ==="
cooldown_start_flows="$(active_flows)"
cooldown_start_evictions="$(evictions)"
sleep "$BENCH_COOLDOWN_S"
cooldown_end_flows="$(active_flows)"
cooldown_end_evictions="$(evictions)"

if [[ -n "$PERF_PID" ]]; then
    kill -INT "$PERF_PID" 2>/dev/null
    wait "$PERF_PID" 2>/dev/null || true
fi

# ---- Final report ---------------------------------------------------------
final_metrics="$(metrics)"
viewer_summary="$(grep -oP '(?<=final summary: ).*' "$PID_DIR/viewer-demo.log" | tail -1)"

{
    echo "=== Netflix-style streaming demo report: $TIMESTAMP ==="
    echo ""
    echo "Backends: $BENCH_N_BACKENDS total, $BENCH_INITIAL_LIVE live at start"
    echo "Concurrency target: $BENCH_CONCURRENCY, bitrate ${BENCH_BITRATE_MIN_KBPS}-${BENCH_BITRATE_MAX_KBPS}kbps,"
    echo "session duration ${BENCH_SESSION_MIN_S}-${BENCH_SESSION_MAX_S}s"
    echo ""
    echo "--- LB-side final /metrics snapshot ---"
    echo "$final_metrics" | grep -E '^maglev_lb_(table_generation|table_regen_last_microseconds|rx_|tx_|conntrack_|backends_)'
    echo ""
    echo "--- Per-backend final state ---"
    echo "$final_metrics" | grep -E '^maglev_lb_backend_(up|draining|packets_total|bytes_total)\{'
    echo ""
    echo "--- Cool-down (reaper) proof ---"
    echo "active_flows: $cooldown_start_flows -> $cooldown_end_flows"
    echo "evictions:    $cooldown_start_evictions -> $cooldown_end_evictions"
    echo ""
    echo "--- Client-side (viewer_gen) final summary ---"
    echo "${viewer_summary:-not captured}"
} | tee "$REPORT_FILE"

if [[ -f "$RESULTS_DIR/demo-perf-stat-$TIMESTAMP.txt" ]]; then
    echo "" | tee -a "$REPORT_FILE"
    echo "--- perf stat ---" | tee -a "$REPORT_FILE"
    cat "$RESULTS_DIR/demo-perf-stat-$TIMESTAMP.txt" | tee -a "$REPORT_FILE"
fi

echo ""
echo "Report saved to $REPORT_FILE"
echo "DEMO COMPLETE"
