#!/usr/bin/env bash
# End-to-end check of the AWS deployment, with assertions:
#   1. bring-up (cloud/check-bringup.sh): ARP, health checks, every hop, requests
#   2. crash: SIGKILL a backend under load; the LB takes it out within the health
#      checker's window, failures stop, and it comes back after a restart
#   3. drain: a drained backend gets no new flows, and nothing fails
#   4. AWS as referee (cloud/dsr-proof.sh) on a streaming run
#
#   cloud/run-cloud-smoke-test.sh
#
# Creates the stack (full profile, flow logs on) if it does not exist, and then
# deletes it again at the end (KEEP_STACK=1 keeps it). A stack that was already
# up is left up.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_tools

CREATED=0
if [[ "$(stack_status)" == "NONE" ]]; then
    "$CLOUD_DIR/up.sh" --profile full --flow-logs on || die "cloud/up.sh failed"
    CREATED=1
fi
refresh_outputs
[[ "${Profile:-}" == "full" ]] || die "the smoke test needs the full profile (cloud/up.sh --profile full)"
[[ "$(aws_q ec2 describe-flow-logs --filter "Name=log-group-name,Values=$FlowLogGroup" \
      --query 'length(FlowLogs)')" != "0" ]] || die "the smoke test needs flow logs on (cloud/up.sh --flow-logs on)"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$CLOUD_DIR/results/smoke-$TS"
mkdir -p "$OUT"
RESULTS=()
FAILED=0
check() { # NAME OK(0/1) DETAIL
    local tag=PASS
    [[ "$2" -eq 0 ]] && { tag=FAIL; FAILED=1; }
    RESULTS+=("$(printf '  %-4s %s: %s' "$tag" "$1" "$3")")
    echo "    $tag $1: $3"
}
finish() {
    stop_all_processes
    echo ""
    echo "=== Cloud smoke test summary ==="
    printf '%s\n' "${RESULTS[@]}"
    if [[ "$CREATED" -eq 1 && "${KEEP_STACK:-0}" != 1 ]]; then
        echo ""
        "$CLOUD_DIR/down.sh"
    fi
    echo "Results: $OUT"
    if [[ "$FAILED" -eq 0 ]]; then echo "ALL CLOUD SMOKE TESTS PASSED"; else echo "CLOUD SMOKE TESTS FAILED"; fi
}
trap finish EXIT

# Failures in the last N seconds of a load_client log (cumulative counters).
failures_in_last() { # FILE SECONDS
    python3 - "$1" "$2" <<'PY'
import json, sys
lines = [d for d in (json.loads(l) for l in open(sys.argv[1]) if l.startswith("{"))
         if d.get("type") == "stats"]
end = lines[-1]["tMs"]
base = next(l for l in lines if l["tMs"] >= end - float(sys.argv[2]) * 1000)
print(lines[-1]["counters"]["failed"] - base["counters"]["failed"])
PY
}
flows_assigned() { lb_metrics | grep -oP "maglev_lb_backend_flows_assigned_total\{backend_id=\"$1\"[^}]*\} \K[0-9]+"; }
backend_up() { lb_metrics | grep -oP "maglev_lb_backend_up\{backend_id=\"$1\"[^}]*\} \K[0-9]+"; }

echo "=== 1. Bring-up ==="
if "$CLOUD_DIR/check-bringup.sh" | tee "$OUT/bringup.txt"; then
    check "bring-up checks" 1 "see $OUT/bringup.txt"
else
    check "bring-up checks" 0 "see $OUT/bringup.txt"
    exit 1
fi

echo ""
echo "=== 2. Crash under load ==="
for i in 1 2 3; do start_rr_backend "$i"; done
sleep 1
start_lb 3
wait_eligible 3 30 || check "3 backends eligible before the crash" 0 "$(backends_eligible)"
run_rr_client "$OUT/crash-client.log" 100 45 &
client=$!
sleep 10
g="$(table_generation)"
rstop be1 svc KILL
t0=$(date +%s.%N)
detected=""
for _ in $(seq 1 50); do
    [[ "$(backend_up 0)" == "0" ]] && { detected=$(date +%s.%N); break; }
    sleep 0.1
done
if [[ -n "$detected" ]]; then
    secs=$(awk "BEGIN { printf \"%.1f\", $detected - $t0 }")
    check "the LB marks a crashed backend down within its health-check window" \
        "$(awk "BEGIN { print ($secs <= 3.0) }")" "${secs}s (2 failed checks at 500 ms + 300 ms timeout)"
else
    check "the LB marks a crashed backend down within its health-check window" 0 "not within 5 s"
fi
check "the Maglev table is regenerated without it" \
    "$([[ "$(table_generation)" -gt "$g" ]] && echo 1 || echo 0)" "generation $g -> $(table_generation)"
sleep 10
start_rr_backend 1
wait_eligible 3 20 && back=1 || back=0
check "a restarted backend is health-checked back in" "$back" "$(backends_eligible) of 3 eligible"
wait "$client"
crash_summary="$(python3 "$CLOUD_DIR/rrstats.py" "$OUT/crash-client.log")"
echo "    client: $crash_summary"
late="$(failures_in_last "$OUT/crash-client.log" 15)"
check "failures stop once the LB has reacted (none in the last 15 s)" \
    "$([[ "$late" -eq 0 ]] && echo 1 || echo 0)" "$late failures in the last 15 s; whole run: $(python3 -c 'import json,sys; s=json.loads(sys.argv[1]); print(s["failed"], "failed", s["failedByReason"], "of", s["ok"] + s["failed"])' "$crash_summary")"

echo ""
echo "=== 3. Graceful drain under load ==="
before_drops="$(lb_metrics | metric_value 'maglev_lb_drops_total\{reason="backend_unhealthy"\}')"
run_rr_client "$OUT/drain-client.log" 100 30 &
client=$!
sleep 8
lb_drain 1
sleep 2
a="$(flows_assigned 1)"
sleep 12
b="$(flows_assigned 1)"
check "a draining backend gets no new flows" "$([[ "$a" == "$b" ]] && echo 1 || echo 0)" \
    "be2 flows assigned: $a -> $b over 12 s"
wait "$client"
lb_undrain 1
drain_summary="$(python3 "$CLOUD_DIR/rrstats.py" "$OUT/drain-client.log")"
echo "    client: $drain_summary"
dfail="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["failed"])' "$drain_summary")"
after_drops="$(lb_metrics | metric_value 'maglev_lb_drops_total\{reason="backend_unhealthy"\}')"
check "nothing fails during a drain" \
    "$([[ "$dfail" -eq 0 && "${after_drops%.*}" -eq "${before_drops%.*}" ]] && echo 1 || echo 0)" \
    "$dfail client failures, $(( ${after_drops%.*} - ${before_drops%.*} )) drops_backend_unhealthy"

echo ""
echo "=== 4. AWS as referee, on a streaming run ==="
stop_all_processes
export BENCH_BITRATE_MIN_KBPS=500 BENCH_BITRATE_MAX_KBPS=1500   # as cloud/run-demo.sh
for i in 1 2 3; do start_stream_backend "$i"; done
sleep 1
start_lb 3
wait_eligible 3 30 || true
sleep 95   # keep the window clear of the previous scenes' traffic (flow-log slack)
WINDOW="$OUT/window.json"
mark_window "$WINDOW" start
rstart client viewer "python3 -u scripts/bench/viewer_gen.py $VIP $BACKEND_PORT --concurrency 20 \
    --metrics-port $VIEWER_METRICS_PORT --arrival-jitter-s 0.5 --max-session-s 40 --max-runtime-s 0"
sleep 60
rstop client viewer USR1
for _ in $(seq 1 60); do ssh_to client "pgrep -f '[v]iewer_gen.py' >/dev/null" || break; sleep 1; done
sleep 5
mark_window "$WINDOW" end
stop_all_processes
if "$CLOUD_DIR/dsr-proof.sh" "$WINDOW" | tee "$OUT/referee.txt"; then
    check "AWS's flow logs and counters agree with the system's claims" 1 "see $OUT/referee.txt"
else
    check "AWS's flow logs and counters agree with the system's claims" 0 "see $OUT/referee.txt"
fi
