#ifndef L4MLB_NET_ETH_H
#define L4MLB_NET_ETH_H

#include <stddef.h>
#include <stdint.h>

#define ETH_ADDR_LEN 6u
#define ETH_HDR_LEN 14u
#define ETH_TYPE_IP 0x0800u
#define ETH_TYPE_ARP 0x0806u

struct eth_hdr_view {
    const uint8_t *dst_mac; /* points into the original buffer, ETH_ADDR_LEN bytes */
    const uint8_t *src_mac; /* points into the original buffer, ETH_ADDR_LEN bytes */
    uint16_t ethertype;     /* host byte order */
};

/* Parses the Ethernet header at the start of buf. Returns 0 on success,
 * -1 if len < ETH_HDR_LEN (truncated). Never dereferences past len. */
int eth_parse(const uint8_t *buf, size_t len, struct eth_hdr_view *out);

/* Writes an ETH_HDR_LEN-byte Ethernet header into out. out must have at
 * least ETH_HDR_LEN bytes of space. */
void eth_build(uint8_t *out, const uint8_t dst_mac[ETH_ADDR_LEN],
               const uint8_t src_mac[ETH_ADDR_LEN], uint16_t ethertype_host);

#endif /* L4MLB_NET_ETH_H */
