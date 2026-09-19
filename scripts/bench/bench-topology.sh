# Shared benchmark-topology parameters, sourced by setup-bench-netns.sh,
# teardown-bench-netns.sh, and every scripts/bench/{controlled,demo} script.
# Deliberately independent from scripts/topology.sh (own bridge, own subnet,
# own netns/veth names) so the M1-M3 test topology and this one can never
# collide, even if both happened to be up at once - see docs/benchmarking.md.

BR=br-bench
SUBNET_CIDR=24
MTU=1600

# Backend count is configurable; every script here loops over it rather than
# hardcoding backend1/backend2 the way the M1-M3 topology does. Default 8:
# enough for Maglev's per-backend split to look non-trivial on the
# dashboard, enough headroom for the demo's crash+drain+scale-out story with
# backends to spare, well inside BACKEND_MAX=64 in src/backend/backend_manager.h.
BENCH_N_BACKENDS="${BENCH_N_BACKENDS:-8}"

LB_IP=10.98.0.1
CLIENT_IP=10.98.0.2
VIP=10.98.0.100
BACKEND_PORT=9000
METRICS_PORT=9105
VIEWER_METRICS_PORT=9106

# Root netns's own address on the bridge, same purpose as topology.sh's
# HOST_IP: lets a host-networked Prometheus reach in. See docs/testing-topology.md.
HOST_IP=10.98.0.254

NS_CLIENT=l4mlb-bench-client
NS_LB=l4mlb-bench-lb
NS_BE_PREFIX=l4mlb-bench-be # + backend index, e.g. l4mlb-bench-be3

# veth pair naming: <ns-side>@<bridge-side>, both halves must stay under
# IFNAMSIZ-1 (15 chars). Longest here is bench-be99-br at 13 chars, so this
# scheme is safe up to 2-digit backend indices (BENCH_N_BACKENDS <= 99,
# already far beyond BACKEND_MAX=64).
VETH_CLIENT_NS=bench-cli
VETH_CLIENT_BR=bench-cli-br
VETH_LB_NS=bench-lb
VETH_LB_BR=bench-lb-br
VETH_BE_NS_PREFIX=bench-be    # + index, e.g. bench-be3
VETH_BE_BR_PREFIX=bench-be    # + index + "-br", e.g. bench-be3-br

be_ip() { # $1 = 1-based backend index
    echo "10.98.0.$((10 + $1))"
}
be_ns() { echo "${NS_BE_PREFIX}$1"; }
be_veth_ns() { echo "${VETH_BE_NS_PREFIX}$1"; }
be_veth_br() { echo "${VETH_BE_BR_PREFIX}$1-br"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_DIR="$SCRIPT_DIR/.run"
ADMIN_FIFO="$PID_DIR/admin.fifo"
RESULTS_DIR="$SCRIPT_DIR/.run/results"
