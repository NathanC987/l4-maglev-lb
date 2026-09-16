#ifndef L4MLB_NET_GRE_H
#define L4MLB_NET_GRE_H

#include <stddef.h>
#include <stdint.h>

#include "eth.h"

#define GRE_HDR_LEN 4u  /* RFC 2784 minimal header: flags/version + protocol type */
#define IPPROTO_GRE_VAL 47u

struct gre_hdr_view {
    uint16_t flags_version; /* host byte order; must be 0 for the minimal form we support */
    uint16_t protocol_type; /* host byte order */
};

/* Parses the minimal 4-byte GRE header at buf. Rejects any header that sets
 * flag bits we don't support (checksum/key/sequence present), since we only
 * ever emit and expect the minimal form. Returns 0 on success, -1 on
 * truncation or an unsupported flags_version. */
int gre_parse(const uint8_t *buf, size_t len, struct gre_hdr_view *out);

/* Writes the minimal 4-byte GRE header (flags_version=0) into out. */
void gre_build(uint8_t *out, uint16_t protocol_type_host);

/* Builds a full Ethernet + IPv4 + GRE frame encapsulating inner_packet
 * (an untouched IPv4 packet, including its own header and payload) and
 * writes it into out (out_cap bytes available). This is the DSR encap step:
 * the inner packet is copied byte-for-byte, never modified.
 *
 * Returns the total frame length on success, or 0 if out_cap is too small. */
size_t gre_encap_build(uint8_t *out, size_t out_cap, const uint8_t dst_mac[ETH_ADDR_LEN],
                        const uint8_t src_mac[ETH_ADDR_LEN], uint32_t outer_src_ip,
                        uint32_t outer_dst_ip, uint8_t ttl, const uint8_t *inner_packet,
                        size_t inner_len);

#endif /* L4MLB_NET_GRE_H */
