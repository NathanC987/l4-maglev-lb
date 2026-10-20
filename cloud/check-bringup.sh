#!/usr/bin/env bash
# Bring-up checks on a fresh stack: does each assumption the local veth topology
# made still hold on a real cloud network? Runs maglev-lb with request/response
# backends and a short, light client load, and watches each hop with tcpdump.
#
#   cloud/check-bringup.sh
#
# Checks: ARP to the backends from maglev-lb's raw socket; health checks pass
# over the VPC; client packets for the VIP reach the LB (VPC route + LB
# source/dest check off); GRE from the LB reaches each backend (security group);
# replies from the VIP reach the client (backend source/dest check off); GRO is
# off on the LB; Python and the bridge unit tests on Ubuntu's Python; requests
# succeed end to end and every backend serves some; CPU credits.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_tools
load_outputs

NBE="$(backend_count)"
RESULTS=()
FAILED=0
check() { # NAME OK(0/1) DETAIL
    local tag=PASS
    [[ "$2" -eq 0 ]] && { tag=FAIL; FAILED=1; }
    RESULTS+=("$(printf '  %-4s %s: %s' "$tag" "$1" "$3")")
    echo "    $tag $1: $3"
}
trap 'stop_all_processes' EXIT

echo "==> Hosts"
for r in $(roles); do
    printf '    %-6s %s\n' "$r" "$(ssh_to "$r" 'echo "$(uname -r), $(python3 --version 2>&1), $(nproc) vCPU, $(ip -o route show default | awk "{print \$5}") mtu $(cat /sys/class/net/$(ip -o route show default | awk "{print \$5; exit}")/mtu)"')"
done

echo "==> Bridge unit tests with the client's Python"
if out="$(ssh_to client "cd ~/$REMOTE_REPO && python3 -m unittest discover -s bridge/tests 2>&1 | tail -n 3")" \
        && grep -q '^OK' <<<"$out"; then
    check "bridge unit tests on $(ssh_to client 'python3 --version')" 1 "$(grep -o 'Ran [0-9]* tests' <<<"$out")"
else
    check "bridge unit tests on the client's Python" 0 "$(tr '\n' ' ' <<<"$out")"
fi

gro="$(ssh_to lb "ethtool -k \$(ip -o route show default | awk '{print \$5; exit}') | awk '/^generic-receive-offload:/ {print \$2}'")"
check "GRO off on the LB" "$([[ "$gro" == off* ]] && echo 1 || echo 0)" "generic-receive-offload: $gro"

echo "==> Request/response backends on be1..be$NBE"
for i in $(seq 1 "$NBE"); do start_rr_backend "$i"; done
sleep 1

echo "==> maglev-lb with $NBE backend(s)"
start_lb "$NBE"
resolved="$(rlog lb maglev-lb | grep -c 'resolved to')"
check "ARP to every backend from the raw socket" "$([[ "$resolved" -eq "$NBE" ]] && echo 1 || echo 0)" \
    "$resolved of $NBE resolved"
if wait_eligible "$NBE" 20; then
    check "health checks pass over the VPC" 1 "$NBE of $NBE backends eligible"
else
    check "health checks pass over the VPC" 0 "$(backends_eligible) of $NBE backends eligible"
fi

echo "==> Watching each hop while the client sends 50 req/s for 20 s"
cap() { # ROLE FILTER OUTFILE - counts matching packets for 18 s
    ssh_to "$1" "sudo timeout 18 tcpdump -lni \$(ip -o route show default | awk '{print \$5; exit}') \
        -c 100000 '$2' 2>/dev/null | wc -l" >"$3" 2>/dev/null &
}
cap lb "dst host $VIP and tcp" "$RUN_DIR/cap-lb"
for i in $(seq 1 "$NBE"); do cap "be$i" "ip proto 47 and src host $LB_IP" "$RUN_DIR/cap-be$i"; done
cap client "src host $VIP" "$RUN_DIR/cap-client"
sleep 2
mark_window "$RUN_DIR/window.json.tmp" start
run_rr_client "$RUN_DIR/bringup-client.log" 50 20
mark_window "$RUN_DIR/window.json.tmp" end
wait
check "client packets for the VIP reach the LB (VPC route, LB source/dest check off)" \
    "$([[ "$(cat "$RUN_DIR/cap-lb")" -gt 0 ]] && echo 1 || echo 0)" "$(cat "$RUN_DIR/cap-lb") packets seen on lb"
for i in $(seq 1 "$NBE"); do
    n="$(cat "$RUN_DIR/cap-be$i")"
    check "GRE from the LB reaches be$i (security group allows protocol 47)" \
        "$([[ "$n" -gt 0 ]] && echo 1 || echo 0)" "$n GRE packets seen on be$i"
done
check "replies from the VIP reach the client (backend source/dest check off)" \
    "$([[ "$(cat "$RUN_DIR/cap-client")" -gt 0 ]] && echo 1 || echo 0)" "$(cat "$RUN_DIR/cap-client") packets seen on client"

summary="$(python3 "$CLOUD_DIR/rrstats.py" "$RUN_DIR/bringup-client.log" --skip-s 2)"
echo "    client: $summary"
read -r ok failed spread < <(python3 -c '
import json, sys
s = json.loads(sys.argv[1]); n = int(sys.argv[2])
slots = s.get("repliesBySlot", {})
print(s.get("ok", 0), s.get("failed", 0), sum(1 for i in range(n) if slots.get(str(i), 0) > 0))' "$summary" "$NBE")
check "requests succeed end to end" "$([[ "$ok" -gt 0 && "$failed" -eq 0 ]] && echo 1 || echo 0)" \
    "$ok ok, $failed failed"
check "Maglev spreads new connections over every backend" "$([[ "$spread" -eq "$NBE" ]] && echo 1 || echo 0)" \
    "$spread of $NBE backends served replies"
mv "$RUN_DIR/window.json.tmp" "$RUN_DIR/window.json"

echo "==> CPU credits (CloudWatch, may be empty for the first ~10 min)"
for r in lb client; do
    v="$(aws_q cloudwatch get-metric-statistics --namespace AWS/EC2 --metric-name CPUCreditBalance \
        --dimensions "Name=InstanceId,Value=$(instance_id "$r")" \
        --start-time "$(date -u -d '-30 min' +%FT%TZ)" --end-time "$(date -u +%FT%TZ)" \
        --period 300 --statistics Average --query 'sort_by(Datapoints,&Timestamp)[-1].Average')"
    echo "    $r: ${v:-no datapoints yet}"
done

echo ""
echo "=== Bring-up summary ==="
printf '%s\n' "${RESULTS[@]}"
if [[ "$(aws_q ec2 describe-flow-logs --filter "Name=log-group-name,Values=$FlowLogGroup" \
          --query 'length(FlowLogs)')" != "0" ]]; then
    echo ""
    echo "Flow logs for this run arrive in a few minutes; then check them with:"
    echo "    cloud/dsr-proof.sh"
fi
if [[ "$FAILED" -eq 0 ]]; then echo "BRING-UP CHECKS PASSED"; else echo "BRING-UP CHECKS FAILED"; exit 1; fi
