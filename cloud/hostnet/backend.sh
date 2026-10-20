#!/usr/bin/env bash
# Backend host: the same three things scripts/bench/setup-bench-netns.sh does in
# each backend namespace - a GRE tunnel from the LB, the VIP on lo, rp_filter off.
#   backend.sh LB_IP VIP
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
LB_IP=${1:?usage: backend.sh LB_IP VIP}
VIP=${2:?usage: backend.sh LB_IP VIP}

# rp_filter would drop the decapsulated packet (source: the client, arriving on
# gre1, while the route back to the client is via the primary interface).
for k in all default "$IFACE"; do sysctl -qw "net.ipv4.conf.$k.rp_filter=0"; done
if ! ip link show gre1 >/dev/null 2>&1; then
    ip tunnel add gre1 mode gre remote "$LB_IP" local "$SELF_IP" ttl 255
fi
ip link set gre1 up
sysctl -qw net.ipv4.conf.gre1.rp_filter=0
# The VIP lives on lo so the kernel delivers the inner packet locally, and the
# reply leaves with the VIP as its source - straight to the client (DSR).
ip addr replace "$VIP/32" dev lo

echo "backend: iface=$IFACE ip=$SELF_IP gre1 remote=$LB_IP vip=$VIP on lo"
