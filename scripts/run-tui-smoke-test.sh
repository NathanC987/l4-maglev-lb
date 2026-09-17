#!/usr/bin/env bash
# M3 verification: launches maglev-lb --tui under a real pty and checks it
# starts, renders, and exits cleanly both on 'q' and on SIGTERM (the
# Ctrl-C-equivalent path through main.c's run_with_tui(), which spawns the
# datapath on its own thread so the TUI can own the terminal on the main
# one). Does not re-check DSR correctness - see run-integration-tests.sh.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=topology.sh
source "$SCRIPT_DIR/topology.sh"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

BIN="${1:-${MAGLEV_LB_BIN:-$REPO_ROOT/build/maglev-lb}}"

if [[ $EUID -ne 0 ]]; then
    echo "run-tui-smoke-test.sh must be run as root" >&2
    exit 1
fi
if [[ ! -x "$BIN" ]]; then
    echo "maglev-lb binary not found at $BIN (build it first, or pass its path)" >&2
    exit 1
fi

cleanup() {
    echo "==> Cleaning up"
    "$SCRIPT_DIR/teardown-netns.sh"
}
trap cleanup EXIT

echo "==> Setting up topology"
if ! "$SCRIPT_DIR/setup-netns.sh"; then
    echo "FAIL: setup-netns.sh failed; aborting" >&2
    exit 1
fi

echo "==> Running TUI smoke test inside the lb namespace"
ip netns exec "$NS_LB" python3 "$SCRIPT_DIR/tui_smoke_test.py" \
    "$BIN" "$VETH_LB_NS" "$VIP" "$LB_IP" "$BE1_IP:$BACKEND_PORT" "$BE2_IP:$BACKEND_PORT"
rc=$?

if [[ "$rc" -eq 0 ]]; then
    echo "ALL TUI SMOKE TESTS PASSED"
else
    echo "SOME TUI SMOKE TESTS FAILED"
fi
exit "$rc"
