#!/usr/bin/env bash
# Request/response sweep on real hosts: the offered load steps up and, at each
# step, records what the client got (ok rate, failures, end-to-end latency
# percentiles), what maglev-lb spent (datapath CPU from schedstat, drops), and
# whether AWS itself shaped the traffic (ENA *_allowance_exceeded counters).
#
#   cloud/run-rr-bench.sh [RPS...]        default: 100 500 1000 2000 3000 4000 6000 8000
#
# Every request opens a new TCP connection, so every request is a new flow
# through the Maglev lookup and the conntrack insert. Backends get ample
# capacity (64 slots, 2 ms), so the LB, the network and the client are what is
# measured. Run with flow logs off (cloud/up.sh --flow-logs off): at these rates
# every short connection becomes several flow-log records.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_tools
load_outputs

STEPS=("$@")
[[ ${#STEPS[@]} -gt 0 ]] || STEPS=(100 500 1000 2000 3000 4000 6000 8000)
STEP_S="${STEP_S:-30}"
WARMUP_S="${WARMUP_S:-5}"
NBE="$(backend_count)"
[[ "$NBE" -ge 3 ]] && NBE=3   # be4 is the demo's dormant backend

if [[ "$(aws_q ec2 describe-flow-logs --filter "Name=log-group-name,Values=$FlowLogGroup" \
          --query 'length(FlowLogs)')" != "0" && "${ALLOW_FLOW_LOGS:-0}" != 1 ]]; then
    die "flow logs are on; turn them off first (cloud/up.sh --flow-logs off) or set ALLOW_FLOW_LOGS=1"
fi

TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$CLOUD_DIR/results/rr-$TS"
mkdir -p "$OUT"
trap 'stop_all_processes' EXIT

echo "==> Is the environment fit to measure?"
require_credits 15 lb client
# Python backends at these rates go well past a T3's baseline: in standard mode,
# without credits, they would be what the sweep measures.
for i in $(seq 1 "$NBE"); do
    mode="$(aws_q ec2 describe-instance-credit-specifications --instance-ids "$(instance_id "be$i")" \
        --query 'InstanceCreditSpecifications[0].CpuCredits' 2>/dev/null || true)"
    if [[ "$mode" == "standard" ]]; then require_credits 15 "be$i"; else echo "    be$i: CPU credits ${mode:-n/a}"; fi
done
PROCS="$(ssh_to client nproc)"
echo "    client: $PROCS load-generator processes (one per vCPU)"

echo "==> $NBE request/response backends, maglev-lb"
for i in $(seq 1 "$NBE"); do start_rr_backend "$i" 64 2 1024; done
sleep 1
start_lb "$NBE" --ct-tcp-timeout-ms 15000
wait_eligible "$NBE" 30 || die "backends never became eligible"

# Sum of all maglev-lb threads' CPU time (ns), and the datapath (main) thread's.
lb_cpu_ns() {
    ssh_to lb "p=\$(pgrep -x maglev-lb); sudo cat /proc/\$p/task/*/schedstat | awk '{ s += \$1 } END { printf \"%d \", s }'; \
        sudo awk '{ print \$1 }' /proc/\$p/task/\$p/schedstat"
}

# AWS shaping anywhere on the path, including the backends' replies.
ena_total() {
    local r
    for r in lb client $(for i in $(seq 1 "$NBE"); do echo "be$i"; done); do ena_allowances "$r"; done \
        | awk '/_exceeded/ { s += $2 } END { print s + 0 }'
}

echo "==> Sweep: ${STEPS[*]} req/s, ${STEP_S}s each"
printf '| offered req/s | ok req/s | failed | p50 ms | p99 ms | LB datapath CPU | LB drops/s | ENA allowance hits |\n' >"$OUT/summary.md"
printf '|---|---|---|---|---|---|---|---|\n' >>"$OUT/summary.md"
for rps in "${STEPS[@]}"; do
    per=$(awk "BEGIN { printf \"%.1f\", $rps / $PROCS }")
    echo "--- $rps req/s ($PROCS x $per) ---"
    ena0="$(ena_total)"
    read -r cpu0 dp0 < <(lb_cpu_ns)
    drops0="$(lb_metrics | grep -oP '^maglev_lb_drops_total\{[^}]*\} \K[0-9]+' | awk '{ s += $1 } END { print s + 0 }')"
    t0=$(date +%s.%N)
    for p in $(seq 1 "$PROCS"); do
        run_rr_client "$OUT/client-$rps-$p.log" "$per" "$STEP_S" &
    done
    wait
    t1=$(date +%s.%N)
    read -r cpu1 dp1 < <(lb_cpu_ns)
    drops1="$(lb_metrics | grep -oP '^maglev_lb_drops_total\{[^}]*\} \K[0-9]+' | awk '{ s += $1 } END { print s + 0 }')"
    ena1="$(ena_total)"

    python3 "$CLOUD_DIR/rrstats.py" "$OUT"/client-"$rps"-*.log --skip-s "$WARMUP_S" >"$OUT/step-$rps.json"
    python3 - "$OUT/step-$rps.json" "$rps" "$t0" "$t1" "$cpu0" "$cpu1" "$dp0" "$dp1" \
        "$drops0" "$drops1" "$ena0" "$ena1" >>"$OUT/summary.md" <<'PY'
import json, sys
f, rps, t0, t1, c0, c1, d0, d1, x0, x1, e0, e1 = sys.argv[1:]
s = json.load(open(f))
secs = float(t1) - float(t0)
s["lbCpuCores"] = (int(c1) - int(c0)) / 1e9 / secs
s["lbDatapathCpuCores"] = (int(d1) - int(d0)) / 1e9 / secs
s["lbDropsPs"] = (float(x1) - float(x0)) / secs
s["enaAllowanceHits"] = int(float(e1) - float(e0))
json.dump(s, open(f, "w"))
if "error" in s:
    print(f"| {rps} | {s['error']} |||||||")
else:
    lat = s["latencyMs"]
    print(f"| {rps} | {s['okRps']} | {s['failed']} {s['failedByReason'] or ''} | {lat['p50']} | "
          f"{lat['p99']} | {s['lbDatapathCpuCores']:.1%} of a core | {s['lbDropsPs']:.1f} | "
          f"{s['enaAllowanceHits']} |")
PY
    tail -n 1 "$OUT/summary.md"
    sleep 3
done

{
    echo ""
    echo "Instance type: $(aws_q ec2 describe-instances --instance-ids "$(instance_id lb)" --query 'Reservations[0].Instances[0].InstanceType'),"
    echo "client processes: $PROCS, backends: $NBE (64 slots, 2 ms each), ${STEP_S}s steps, ${WARMUP_S}s warm-up excluded."
    echo "CPU credits after the run: lb $(cpu_credits lb), client $(cpu_credits client)."
} >>"$OUT/summary.md"
echo ""
cat "$OUT/summary.md"
echo ""
echo "Results: $OUT"
