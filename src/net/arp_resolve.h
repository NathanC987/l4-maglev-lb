#ifndef L4MLB_NET_ARP_RESOLVE_H
#define L4MLB_NET_ARP_RESOLVE_H

#include <stdint.h>

#include "eth.h"

/* Resolves target_ip's MAC address on ifname by sending an ARP request and
 * waiting (up to timeout_ms) for a matching reply. Opens and closes its own
 * short-lived ETH_P_ARP raw socket; does not touch the caller's data-plane
 * socket. src_mac/src_ip identify this host in the request.
 *
 * target_ip and src_ip are opaque network-byte-order values (as from
 * inet_addr() or a flow_key field).
 *
 * Returns 0 and fills out_mac on success, -1 on timeout or error. */
int arp_resolve(const char *ifname, uint32_t target_ip, const uint8_t src_mac[ETH_ADDR_LEN],
                 uint32_t src_ip, uint8_t out_mac[ETH_ADDR_LEN], int timeout_ms);

#endif /* L4MLB_NET_ARP_RESOLVE_H */
