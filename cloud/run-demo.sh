#!/usr/bin/env bash
# The streaming demo on real hosts: the cloud version of
# scripts/bench/demo/run-streaming-demo.sh, with the same scenes in the same
# order (steady load, crash, graceful drain, scale-out, wind-down). Watch it on
# the cloud Grafana dashboards (cloud/grafana-tunnel.sh). Afterwards AWS checks
# the run independently (cloud/dsr-proof.sh, run automatically).
#
#   cloud/run-demo.sh        (needs the full profile: be1..be4 and mon)
#
# be1..be3 start live and be4 starts dormant, its host up but with nothing
# listening, so maglev-lb's health checker keeps it out until the scale-out
# scene starts its responder. The load is sized for t3.micro: about 40 Mbit/s of
# video in total, under the instances' baseline network bandwidth.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_tools
load_outputs
[[ "${Profile:-}" == "full" ]] || die "the demo needs the full profile (cloud/up.sh --profile full)"

CONCURRENCY="${BENCH_CONCURRENCY:-40}"
export BENCH_BITRATE_MIN_KBPS="${BENCH_BITRATE_MIN_KBPS:-500}"
export BENCH_BITRATE_MAX_KBPS="${BENCH_BITRATE_MAX_KBPS:-1500}"
export BENCH_SESSION_MIN_S="${BENCH_SESSION_MIN_S:-8}"
export BENCH_SESSION_MAX_S="${BENCH_SESSION_MAX_S:-30}"
RAMP_S="${BENCH_RAMP_S:-20}"
PRECRASH_S="${BENCH_PRECRASH_LOAD_S:-60}"
POSTCRASH_S="${BENCH_POSTCRASH_LOAD_S:-45}"
POSTDRAIN_S="${BENCH_POSTDRAIN_LOAD_S:-45}"
POSTSCALE_S="${BENCH_POSTSCALE_LOAD_S:-60}"
NO_REFEREE="${NO_REFEREE:-0}"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$CLOUD_DIR/results/demo-$TS"
mkdir -p "$OUT"
WINDOW="$OUT/window.json"
trap 'echo "==> Stopping the demo processes"; stop_all_processes' EXIT

echo "==> Is the environment fit to measure?"
require_credits 10 lb client

echo "==> Streaming responders on be1..be3 (be4 stays dormant)"
for i in 1 2 3; do start_stream_backend "$i"; done
for i in 4; do rstop "be$i" stream; rstop "be$i" svc; done
sleep 1

echo "==> maglev-lb with all 4 backends configured"
start_lb 4
# Backends start out healthy until the health checker proves otherwise, so no
# load until the dormant be4 has been ruled out (same as the local demo).
wait_eligible 3 30 || die "never converged to 3 eligible backends"
echo "    converged: 3 of 4 backends eligible, table generation $(table_generation)"

m() { lb_metrics; }
backend_up() { m | grep -oP "maglev_lb_backend_up\{backend_id=\"$1\"[^}]*\} \K[0-9]+"; }
drops_unhealthy() { m | metric_value 'maglev_lb_drops_total\{reason="backend_unhealthy"\}'; }
wait_generation_past() { # GEN
    for _ in $(seq 1 50); do
        local g; g="$(table_generation)"
        [[ -n "$g" && "$g" -gt "$1" ]] && return 0
        sleep 0.2
    done
    return 1
}

echo "==> ENA allowance counters before the run"
for r in lb client be1 be2 be3 be4; do ena_allowances "$r" | sed "s/^/    $r: /"; done >"$OUT/ena-before.txt"
cat "$OUT/ena-before.txt"

mark_window "$WINDOW" start
echo "==> Viewer load on the client: $CONCURRENCY concurrent sessions, ramping over ${RAMP_S}s"
jitter="$(awk "BEGIN { j = $RAMP_S / $CONCURRENCY; print (j < 0.01) ? 0.01 : j }")"
rstart client viewer "python3 -u scripts/bench/viewer_gen.py $VIP $BACKEND_PORT \
    --concurrency $CONCURRENCY --metrics-port $VIEWER_METRICS_PORT --arrival-jitter-s $jitter \
    --max-session-s $((BENCH_SESSION_MAX_S + 15)) --max-runtime-s 0"
baseline_drops="$(drops_unhealthy)"

echo ""
echo "==> [T+0] Steady load for ${PRECRASH_S}s"
sleep "$PRECRASH_S"

echo ""
echo "=== CRASH: SIGKILL be1's responder (backend id 0) ==="
g="$(table_generation)"
rstop be1 stream KILL
t_crash=$(date +%s.%N)
wait_generation_past "$g" && t_regen=$(date +%s.%N) || t_regen=""
echo "    be1 up=$(backend_up 0), table generation $(table_generation)" \
     "${t_regen:+(regenerated $(awk "BEGIN { printf \"%.1f\", $t_regen - $t_crash }")s after the crash)}"
echo "    drops_backend_unhealthy so far: $(( $(drops_unhealthy) - baseline_drops ))"
echo "==> Load for ${POSTCRASH_S}s after the crash"
sleep "$POSTCRASH_S"

echo ""
echo "=== DRAIN: gracefully draining be2 (backend id 1) ==="
g="$(table_generation)"
before="$(drops_unhealthy)"
lb_drain 1
wait_generation_past "$g" || true
echo "    table generation $(table_generation), drops_backend_unhealthy during drain:" \
     "$(( $(drops_unhealthy) - before )) (expect 0 - contrast with the crash)"
echo "==> Load for ${POSTDRAIN_S}s: be2's open sessions finish, it gets no new ones"
sleep "$POSTDRAIN_S"
rstop be2 stream

echo ""
echo "=== SCALE-OUT: starting be4's responder ==="
g="$(table_generation)"
start_stream_backend 4
wait_generation_past "$g" || true
echo "    backends eligible $(backends_eligible), table generation $(table_generation)"
echo "==> Load for ${POSTSCALE_S}s: be4's traffic climbs from zero"
sleep "$POSTSCALE_S"

echo ""
echo "=== Wind-down: no new sessions; waiting for open ones to finish ==="
rstop client viewer USR1
for _ in $(seq 1 $((BENCH_SESSION_MAX_S + 30))); do
    ssh_to client "pgrep -f '[v]iewer_gen.py' >/dev/null" || break
    sleep 1
done
sleep 5
mark_window "$WINDOW" end

for r in lb client be1 be2 be3 be4; do ena_allowances "$r" | sed "s/^/    $r: /"; done >"$OUT/ena-after.txt"
{
    echo "=== Cloud streaming demo: $TS (stack $STACK, $(aws configure get region)) ==="
    echo ""
    echo "--- maglev-lb's final /metrics ---"
    m | grep -E '^maglev_lb_(table_generation|table_regen_last_microseconds|rx_|tx_|conntrack_|backends_|drops_total)'
    m | grep -E '^maglev_lb_backend_(up|draining|packets_total|flows_assigned_total)\{'
    echo ""
    echo "--- Client side (viewer_gen) ---"
    rlog client viewer | grep -oP '(?<=final summary: ).*' | tail -1
    echo ""
    echo "--- ENA allowance counters (before -> after; non-zero growth = AWS shaped the traffic) ---"
    paste -d' ' "$OUT/ena-before.txt" "$OUT/ena-after.txt" | awk '{ print "   ", $1, $2, $3, "->", $6 }'
} | tee "$OUT/demo-report.txt"
rlog lb maglev-lb >"$OUT/maglev-lb.log"

if [[ "$NO_REFEREE" -eq 1 ]]; then
    echo "Skipping the AWS referee (NO_REFEREE=1); run later: cloud/dsr-proof.sh $WINDOW"
elif [[ "$(aws_q ec2 describe-flow-logs --filter "Name=log-group-name,Values=$FlowLogGroup" --query 'length(FlowLogs)')" == "0" ]]; then
    echo "Flow logs are off for this stack, so no AWS referee check (cloud/up.sh --flow-logs on)"
else
    stop_all_processes
    trap - EXIT
    echo ""
    echo "==> AWS as referee (flow logs arrive a few minutes after the traffic)"
    "$CLOUD_DIR/dsr-proof.sh" "$WINDOW" | tee "$OUT/referee.txt"
fi
echo ""
echo "Results: $OUT"
echo "DEMO COMPLETE"
