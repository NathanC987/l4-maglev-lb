#!/usr/bin/env bash
# Client host: cap the MTU so a request plus the LB's 24-byte GRE header always
# fits in the VPC's 9001-byte MTU without fragmentation. (The local topology does
# the same with 1500 on the client and 1600 on the bridge.)
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
ip link set "$IFACE" mtu 1500
# Plenty of source ports for one-connection-per-request load.
sysctl -qw net.ipv4.ip_local_port_range="15000 65000"
echo "client: iface=$IFACE ip=$SELF_IP mtu=1500"
