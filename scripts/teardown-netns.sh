#!/usr/bin/env bash
# Tears down everything setup-netns.sh created. Best-effort: deliberately
# does not use `set -e`, so a partially-created topology (e.g. from a
# previous failed run) still gets cleaned up as far as possible instead of
# aborting on the first missing resource.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=topology.sh
source "$SCRIPT_DIR/topology.sh"

if [[ $EUID -ne 0 ]]; then
    echo "teardown-netns.sh must be run as root" >&2
    exit 1
fi

echo "==> Stopping echo servers"
for label in BACKEND1 BACKEND2; do
    pidfile="$PID_DIR/$label.pid"
    if [[ -f "$pidfile" ]]; then
        pid="$(cat "$pidfile")"
        kill "$pid" 2>/dev/null || true
        rm -f "$pidfile" "$PID_DIR/$label.log"
    fi
done

echo "==> Deleting namespaces (each takes its veth half and gre1 tunnel with it)"
for ns in "$NS_CLIENT" "$NS_LB" "$NS_BE1" "$NS_BE2"; do
    ip netns del "$ns" 2>/dev/null || true
done

# Deleting a netns deletes any veth end still inside it, which deletes its
# peer too - but a veth end that was ever left orphaned in the root netns
# (e.g. an interrupted setup run) has no netns to be cleaned up by, so it
# survives teardown and collides with the next setup run ("RTNETLINK
# answers: File exists"). Sweep them explicitly as a safety net.
echo "==> Removing any orphaned bridge-side veth ends"
for br_side in "$VETH_CLIENT_BR" "$VETH_LB_BR" "$VETH_BE1_BR" "$VETH_BE2_BR"; do
    ip link del "$br_side" 2>/dev/null || true
done

echo "==> Deleting bridge"
ip link del "$BR" 2>/dev/null || true

echo "==> Teardown complete"
