#!/usr/bin/env bash
# Tears down everything setup-bench-netns.sh created, plus any
# streaming_server.py/viewer_gen.py processes a controlled/demo script left
# running. Best-effort, like scripts/teardown-netns.sh: no `set -e`, so a
# partially-created topology still gets cleaned up as far as possible.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=bench-topology.sh
source "$SCRIPT_DIR/bench-topology.sh"

if [[ $EUID -ne 0 ]]; then
    echo "teardown-bench-netns.sh must be run as root" >&2
    exit 1
fi

echo "==> Stopping any backend responder processes"
if [[ -d "$PID_DIR" ]]; then
    for pidfile in "$PID_DIR"/backend*.pid; do
        [[ -f "$pidfile" ]] || continue
        pid="$(cat "$pidfile")"
        kill "$pid" 2>/dev/null || true
        rm -f "$pidfile"
    done
fi

echo "==> Removing admin FIFO"
rm -f "$ADMIN_FIFO"

echo "==> Deleting namespaces (each takes its veth half and gre1 tunnel with it)"
ip netns del "$NS_CLIENT" 2>/dev/null || true
ip netns del "$NS_LB" 2>/dev/null || true
# BENCH_N_BACKENDS may differ between the run that created the topology and
# whatever teardown is invoked with, so sweep every l4mlb-bench-be* netns
# that actually exists rather than trusting the current env var.
for ns in $(ip netns list 2>/dev/null | awk '{print $1}' | grep '^l4mlb-bench-be'); do
    ip netns del "$ns" 2>/dev/null || true
done

echo "==> Removing any orphaned bridge-side veth ends"
ip link del "$VETH_CLIENT_BR" 2>/dev/null || true
ip link del "$VETH_LB_BR" 2>/dev/null || true
for br_side in $(ip -o link show 2>/dev/null | awk -F': ' '{print $2}' | cut -d@ -f1 | grep '^bench-be[0-9]*-br$'); do
    ip link del "$br_side" 2>/dev/null || true
done

echo "==> Deleting bridge"
ip link del "$BR" 2>/dev/null || true

echo "==> Bench teardown complete"
