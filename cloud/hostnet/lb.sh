#!/usr/bin/env bash
# LB host: keep the kernel out of the way of maglev-lb's raw socket.
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

# GRO merges consecutive TCP segments of a flow into one large buffer before
# packet sockets see it. veth leaves it off; ENA does not. With it on, maglev-lb
# would receive (and try to GRE-encapsulate) frames far larger than the MTU.
ethtool -K "$IFACE" gro off >/dev/null 2>&1 || true
ethtool -K "$IFACE" lro off >/dev/null 2>&1 || true
gro="$(ethtool -k "$IFACE" | awk '/^generic-receive-offload:/ { print $2 }')"
[[ "$gro" == "off" ]] || { echo "lb: could not turn GRO off on $IFACE (still $gro)" >&2; exit 1; }

# The LB is not a router: packets for the VIP must reach maglev-lb, not be
# forwarded by the kernel (same as the local topology's lb namespace).
sysctl -qw net.ipv4.ip_forward=0

echo "lb: iface=$IFACE ip=$SELF_IP mtu=$(cat /sys/class/net/$IFACE/mtu) gro=$gro ip_forward=0"
