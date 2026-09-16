#include "eth.h"

int eth_parse(const uint8_t *buf, size_t len, struct eth_hdr_view *out) {
    if (len < ETH_HDR_LEN) {
        return -1;
    }
    out->dst_mac = buf;
    out->src_mac = buf + ETH_ADDR_LEN;
    out->ethertype = (uint16_t)((buf[12] << 8) | buf[13]);
    return 0;
}

void eth_build(uint8_t *out, const uint8_t dst_mac[ETH_ADDR_LEN],
               const uint8_t src_mac[ETH_ADDR_LEN], uint16_t ethertype_host) {
    for (unsigned i = 0; i < ETH_ADDR_LEN; i++) {
        out[i] = dst_mac[i];
        out[ETH_ADDR_LEN + i] = src_mac[i];
    }
    out[12] = (uint8_t)(ethertype_host >> 8);
    out[13] = (uint8_t)(ethertype_host & 0xFF);
}
