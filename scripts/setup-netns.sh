#!/usr/bin/env bash
# Builds the v1 test topology: one shared Linux bridge (br-lan) with four
# network namespaces (client, lb, backend1, backend2) hanging off it via veth
# pairs. See docs/testing-topology.md for the full design rationale -
# notably why a single shared L2 segment is enough to prove DSR actually
# bypasses the LB on the return path.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=topology.sh
source "$SCRIPT_DIR/topology.sh"

if [[ $EUID -ne 0 ]]; then
    echo "setup-netns.sh must be run as root (it creates network namespaces)" >&2
    exit 1
fi

echo "==> Tearing down any existing topology first (idempotent setup)"
"$SCRIPT_DIR/teardown-netns.sh" >/dev/null 2>&1 || true

mkdir -p "$PID_DIR"

echo "==> Creating namespaces"
ip netns add "$NS_CLIENT"
ip netns add "$NS_LB"
ip netns add "$NS_BE1"
ip netns add "$NS_BE2"

echo "==> Creating bridge $BR in the root netns"
ip link add "$BR" type bridge
ip link set "$BR" up
ip link set "$BR" mtu "$MTU"
# Gives the root netns itself an address on the shared segment, purely for
# observability (reaching the LB's metrics endpoint from outside any of the
# simulated namespaces - e.g. a host-networked Prometheus container). Does
# not participate in the DSR datapath at all: nothing routes client/backend
# traffic through it, so it has no effect on the bypass proof in
# run-integration-tests.sh.
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

    # veth defers UDP/TCP checksum computation to the (nonexistent, for a
    # purely virtual link) NIC - CHECKSUM_PARTIAL survives all the way
    # through the peer device and the bridge, so a raw socket reading these
    # bytes (as our LB's ingestion does) sees an incomplete checksum field,
    # even though a normal kernel-to-kernel delivery would have worked fine.
    # This is a veth/test-topology artifact only: a real NIC always
    # finalizes checksums before anything hits the wire. Disabling tx
    # checksum offload forces the sending kernel to compute the real
    # checksum in software before we ever see the bytes.
    ip netns exec "$ns" ethtool -K "$ns_side" tx off >/dev/null
}

echo "==> Wiring client ($CLIENT_IP)"
create_veth_pair "$NS_CLIENT" "$VETH_CLIENT_NS" "$VETH_CLIENT_BR" "$CLIENT_IP"
echo "==> Wiring lb ($LB_IP)"
create_veth_pair "$NS_LB" "$VETH_LB_NS" "$VETH_LB_BR" "$LB_IP"
echo "==> Wiring backend1 ($BE1_IP)"
create_veth_pair "$NS_BE1" "$VETH_BE1_NS" "$VETH_BE1_BR" "$BE1_IP"
echo "==> Wiring backend2 ($BE2_IP)"
create_veth_pair "$NS_BE2" "$VETH_BE2_NS" "$VETH_BE2_BR" "$BE2_IP"

echo "==> Client route: $VIP/32 via $LB_IP (this is the entire reason client"
echo "    traffic reaches the LB's raw socket instead of being unreachable)"
ip netns exec "$NS_CLIENT" ip route add "$VIP/32" via "$LB_IP" dev "$VETH_CLIENT_NS"

echo "==> lb: leaving ip_forward off - our raw socket does the forwarding,"
echo "    not the kernel; this also proves AF_PACKET taps independent of it"
ip netns exec "$NS_LB" sysctl -qw net.ipv4.ip_forward=0

setup_backend() {
    local ns=$1 own_ip=$2 label=$3

    echo "==> $label ($own_ip): GRE tunnel + VIP on lo + echo server"
    # rp_filter must be off for the decapsulated packet to survive: after
    # gre1 decapsulates, the inner packet's source is the *client's* real
    # IP, which has no route back out via gre1 (a point-to-point tunnel to
    # the LB, not to arbitrary clients) - so strict/loose reverse-path
    # filtering on gre1 (or on "all", since Linux enforces the max of the
    # two) would otherwise silently drop every decapsulated packet as a
    # martian source. This is the standard, well-documented gotcha for any
    # DSR-over-tunnel setup (classic LVS-DR/GRE deployments hit the same
    # thing), not specific to this project.
    ip netns exec "$ns" sysctl -qw net.ipv4.conf.all.rp_filter=0
    ip netns exec "$ns" sysctl -qw net.ipv4.conf.default.rp_filter=0
    ip netns exec "$ns" ip tunnel add gre1 mode gre remote "$LB_IP" local "$own_ip" ttl 255
    ip netns exec "$ns" ip link set gre1 up
    ip netns exec "$ns" sysctl -qw "net.ipv4.conf.gre1.rp_filter=0"
    ip netns exec "$ns" ip addr add "$VIP/32" dev lo

    # Bind to 0.0.0.0, not the VIP specifically: a wildcard bind is what
    # real backend services do, and it's what makes this one listening
    # socket answer BOTH the VIP (GRE-decapsulated client traffic - the
    # accepted connection's local address is naturally the VIP, since
    # that's what the client's packet was addressed to) AND the backend's
    # own real IP (the health checker's direct TCP-connect probes, added in
    # M2). Binding only to the VIP - the M1 setup - left the real IP
    # completely unreachable, so every health probe got ECONNREFUSED
    # regardless of whether the backend was actually up.
    ip netns exec "$ns" python3 "$SCRIPT_DIR/echo_server.py" "0.0.0.0" "$BACKEND_PORT" "$label" \
        >"$PID_DIR/$label.log" 2>&1 &
    echo $! >"$PID_DIR/$label.pid"
}

setup_backend "$NS_BE1" "$BE1_IP" BACKEND1
setup_backend "$NS_BE2" "$BE2_IP" BACKEND2

# Give the echo servers a moment to bind before anything tries to talk to them.
sleep 0.3

echo ""
echo "==> Topology ready:"
echo "    client   ($NS_CLIENT): $CLIENT_IP"
echo "    lb       ($NS_LB):     $LB_IP   <- run maglev-lb here, see scripts/run-lb.sh"
echo "    backend1 ($NS_BE1):    $BE1_IP  -> VIP $VIP:$BACKEND_PORT (gre1 remote=$LB_IP)"
echo "    backend2 ($NS_BE2):    $BE2_IP  -> VIP $VIP:$BACKEND_PORT (gre1 remote=$LB_IP)"
echo "    VIP: $VIP"
echo ""
echo "Tear down with: sudo scripts/teardown-netns.sh"
