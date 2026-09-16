#!/usr/bin/env bash
# Runs maglev-lb inside the lb netns, wired up for the standard test
# topology. Extra arguments are passed through, e.g.:
#   sudo scripts/run-lb.sh --max-flows 100000
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=topology.sh
source "$SCRIPT_DIR/topology.sh"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

BIN="${MAGLEV_LB_BIN:-$REPO_ROOT/build/maglev-lb}"

if [[ $EUID -ne 0 ]]; then
    echo "run-lb.sh must be run as root (it runs inside the lb network namespace)" >&2
    exit 1
fi
if [[ ! -x "$BIN" ]]; then
    echo "maglev-lb binary not found at $BIN (build it first, or set MAGLEV_LB_BIN)" >&2
    exit 1
fi

exec ip netns exec "$NS_LB" "$BIN" \
    --iface "$VETH_LB_NS" \
    --vip "$VIP" \
    --director-ip "$LB_IP" \
    --backend "$BE1_IP:$BACKEND_PORT" \
    --backend "$BE2_IP:$BACKEND_PORT" \
    "$@"
