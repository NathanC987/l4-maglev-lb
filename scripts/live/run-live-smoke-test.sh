#!/usr/bin/env bash
# End-to-end check of live mode without any frontend: starts bridge/live_bridge.py
# (bench topology + maglev-lb with a backend slot pool), drives it over its HTTP
# API exactly like the breakscale fork would, and asserts on the real
# measurements it streams back. Also checks the security checks, that a
# kill -9 of the bridge leaves no orphaned children, and that SIGTERM tears the
# topology down. See docs/live-mode.md.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "run-live-smoke-test.sh must be run as root (via sudo)" >&2
    exit 1
fi
if [[ -z "${SUDO_UID:-}" ]]; then
    echo "run via sudo from a normal user: the bridge drops its children to \$SUDO_UID" >&2
    exit 1
fi

exec python3 -I "$SCRIPT_DIR/live_smoke.py"
