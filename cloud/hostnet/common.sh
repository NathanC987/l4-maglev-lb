# Sourced by the hostnet scripts, which run as root on an instance.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "must run as root" >&2; exit 1; }
# The primary interface: ens5 on Nitro (ENA) instances, eth0 on Xen (t2).
IFACE="$(ip -o route show default | awk '{ print $5; exit }')"
[[ -n "$IFACE" ]] || { echo "no default route" >&2; exit 1; }
SELF_IP="$(ip -o -4 addr show dev "$IFACE" | awk '{ split($4, a, "/"); print a[1]; exit }')"
