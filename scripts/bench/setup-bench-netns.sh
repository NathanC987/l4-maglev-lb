#!/usr/bin/env bash
# Builds the benchmark topology: one bridge (br-bench) with a client, an lb,
# and BENCH_N_BACKENDS backend namespaces, all wired up identically to
# scripts/setup-netns.sh's per-backend logic (GRE tunnel, rp_filter, VIP on
# lo, checksum offload) but looped over N instead of hardcoded to two. This
# file deliberately DUPLICATES that ~10-line block rather than sharing code
# with setup-netns.sh - see docs/benchmarking.md for why: that logic is
# exactly what docs/testing-topology.md documents as hard-won and fragile,
# and setup-netns.sh is a direct dependency of the already-passing M1-M3
# test suite, which this script must never risk regressing. If a gotcha like
# the ones in docs/testing-topology.md's "Gotchas" section is ever found
# again, both copies need the same fix.
#
# Unlike setup-netns.sh, this does NOT start a responder process on every
# backend - which backends go "live" (get a streaming_server.py process) and
# when is a scenario decision, made by the controlled/demo script that calls
# this, not by topology setup itself. This is the mechanism behind the
# "pre-provisioned, started late" scale-out story: a backend's namespace,
# veth, GRE tunnel, and VIP-on-lo all exist from the start regardless.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=bench-topology.sh
source "$SCRIPT_DIR/bench-topology.sh"

if [[ $EUID -ne 0 ]]; then
    echo "setup-bench-netns.sh must be run as root (it creates network namespaces)" >&2
    exit 1
fi

echo "==> Tearing down any existing bench topology first (idempotent setup)"
"$SCRIPT_DIR/teardown-bench-netns.sh" >/dev/null 2>&1 || true

mkdir -p "$PID_DIR" "$RESULTS_DIR"

echo "==> Creating namespaces (client, lb, $BENCH_N_BACKENDS backend(s))"
ip netns add "$NS_CLIENT"
ip netns add "$NS_LB"
for i in $(seq 1 "$BENCH_N_BACKENDS"); do
    ip netns add "$(be_ns "$i")"
done

echo "==> Creating bridge $BR in the root netns"
ip link add "$BR" type bridge
ip link set "$BR" up
ip link set "$BR" mtu "$MTU"
ip addr add "$HOST_IP/$SUBNET_CIDR" dev "$BR"

create_veth_pair() {
    local ns=$1 ns_side=$2 br_side=$3 ns_ip=$4

    ip link add "$ns_side" type veth peer name "$br_side"
    ip link set "$ns_side" netns "$ns"
    ip link set "$br_side" master "$BR"
    ip link set "$br_side" up
    ip link set "$br_side" mtu "$MTU"

    ip netns exec "$ns" ip link set lo up
    ip netns exec "$ns" ip link set "$ns_side" up
    ip netns exec "$ns" ip link set "$ns_side" mtu "$MTU"
    ip netns exec "$ns" ip addr add "$ns_ip/$SUBNET_CIDR" dev "$ns_side"

    # See scripts/setup-netns.sh's identical comment: veth defers TCP/UDP
    # checksum computation to a NIC that doesn't exist for a virtual link.
    ip netns exec "$ns" ethtool -K "$ns_side" tx off >/dev/null
}

echo "==> Wiring client ($CLIENT_IP)"
create_veth_pair "$NS_CLIENT" "$VETH_CLIENT_NS" "$VETH_CLIENT_BR" "$CLIENT_IP"
echo "==> Wiring lb ($LB_IP)"
create_veth_pair "$NS_LB" "$VETH_LB_NS" "$VETH_LB_BR" "$LB_IP"

echo "==> Client route: $VIP/32 via $LB_IP"
ip netns exec "$NS_CLIENT" ip route add "$VIP/32" via "$LB_IP" dev "$VETH_CLIENT_NS"

echo "==> lb: leaving ip_forward off (see scripts/setup-netns.sh's identical comment)"
ip netns exec "$NS_LB" sysctl -qw net.ipv4.ip_forward=0

setup_backend() {
    local idx=$1 ns br_ns own_ip veth_ns veth_br
    ns="$(be_ns "$idx")"
    own_ip="$(be_ip "$idx")"
    veth_ns="$(be_veth_ns "$idx")"
    veth_br="$(be_veth_br "$idx")"

    create_veth_pair "$ns" "$veth_ns" "$veth_br" "$own_ip"

    # See scripts/setup-netns.sh's setup_backend() / docs/testing-topology.md
    # "Gotchas" for why each of these lines exists - rp_filter would drop
    # the decapsulated packet as a spoofed source, and the VIP must live on
    # lo (not the tunnel device) for the kernel to locally deliver it.
    ip netns exec "$ns" sysctl -qw net.ipv4.conf.all.rp_filter=0
    ip netns exec "$ns" sysctl -qw net.ipv4.conf.default.rp_filter=0
    ip netns exec "$ns" ip tunnel add gre1 mode gre remote "$LB_IP" local "$own_ip" ttl 255
    ip netns exec "$ns" ip link set gre1 up
    ip netns exec "$ns" sysctl -qw "net.ipv4.conf.gre1.rp_filter=0"
    ip netns exec "$ns" ip addr add "$VIP/32" dev lo
}

echo "==> Wiring $BENCH_N_BACKENDS backend(s)"
for i in $(seq 1 "$BENCH_N_BACKENDS"); do
    echo "    backend$i ($(be_ip "$i")): GRE tunnel + VIP on lo"
    setup_backend "$i"
done

echo ""
echo "==> Bench topology ready:"
echo "    client ($NS_CLIENT): $CLIENT_IP"
echo "    lb     ($NS_LB):     $LB_IP"
for i in $(seq 1 "$BENCH_N_BACKENDS"); do
    echo "    backend$i ($(be_ns "$i")): $(be_ip "$i")  (no responder started yet)"
done
echo "    VIP: $VIP"
echo ""
echo "Tear down with: sudo scripts/bench/teardown-bench-netns.sh"
