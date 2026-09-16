# Shared topology parameters, sourced by setup-netns.sh, teardown-netns.sh,
# and run-lb.sh. Keeping these in one place means the three scripts (and the
# integration tests) can never drift out of sync with each other.

BR=br-lan
SUBNET_CIDR=24
MTU=1600

LB_IP=10.99.0.1
CLIENT_IP=10.99.0.2
BE1_IP=10.99.0.11
BE2_IP=10.99.0.12
VIP=10.99.0.100
BACKEND_PORT=9000

NS_CLIENT=l4mlb-client
NS_LB=l4mlb-lb
NS_BE1=l4mlb-be1
NS_BE2=l4mlb-be2

# veth pair naming: <ns-side>@<bridge-side>. Both halves must stay under
# IFNAMSIZ-1 (15 chars); the longest here is veth-client-br at 14.
VETH_CLIENT_NS=veth-client
VETH_CLIENT_BR=veth-client-br
VETH_LB_NS=veth-lb
VETH_LB_BR=veth-lb-br
VETH_BE1_NS=veth-be1
VETH_BE1_BR=veth-be1-br
VETH_BE2_NS=veth-be2
VETH_BE2_BR=veth-be2-br

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_DIR="$SCRIPT_DIR/.run"
