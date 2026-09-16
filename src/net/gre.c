#include "gre.h"

#include "ipv4.h"

int gre_parse(const uint8_t *buf, size_t len, struct gre_hdr_view *out) {
    if (len < GRE_HDR_LEN) {
        return -1;
    }
    uint16_t flags_version = (uint16_t)((buf[0] << 8) | buf[1]);
    if (flags_version != 0) {
        /* Checksum/key/sequence-present bits set, or non-zero version: not
         * the minimal form we emit and expect. */
        return -1;
    }
    out->flags_version = flags_version;
    out->protocol_type = (uint16_t)((buf[2] << 8) | buf[3]);
    return 0;
}

void gre_build(uint8_t *out, uint16_t protocol_type_host) {
    out[0] = 0x00;
    out[1] = 0x00;
    out[2] = (uint8_t)(protocol_type_host >> 8);
    out[3] = (uint8_t)(protocol_type_host & 0xFF);
}

size_t gre_encap_build(uint8_t *out, size_t out_cap, const uint8_t dst_mac[ETH_ADDR_LEN],
                        const uint8_t src_mac[ETH_ADDR_LEN], uint32_t outer_src_ip,
                        uint32_t outer_dst_ip, uint8_t ttl, const uint8_t *inner_packet,
                        size_t inner_len) {
    size_t total = ETH_HDR_LEN + IPV4_MIN_HDR_LEN + GRE_HDR_LEN + inner_len;
    if (total > out_cap) {
        return 0;
    }

    eth_build(out, dst_mac, src_mac, ETH_TYPE_IP);

    size_t ip_off = ETH_HDR_LEN;
    uint16_t outer_total_len = (uint16_t)(IPV4_MIN_HDR_LEN + GRE_HDR_LEN + inner_len);
    ipv4_build(out + ip_off, outer_src_ip, outer_dst_ip, (uint8_t)IPPROTO_GRE_VAL,
               outer_total_len, ttl);

    size_t gre_off = ip_off + IPV4_MIN_HDR_LEN;
    gre_build(out + gre_off, ETH_TYPE_IP);

    size_t inner_off = gre_off + GRE_HDR_LEN;
    for (size_t i = 0; i < inner_len; i++) {
        out[inner_off + i] = inner_packet[i];
    }

    return total;
}
