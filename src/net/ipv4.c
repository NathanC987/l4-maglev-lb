#include "ipv4.h"

#include <stdio.h>
#include <string.h>

int ipv4_parse(const uint8_t *buf, size_t len, struct ipv4_hdr_view *out) {
    if (len < IPV4_MIN_HDR_LEN) {
        return -1;
    }
    uint8_t version = (uint8_t)(buf[0] >> 4);
    uint8_t ihl = (uint8_t)(buf[0] & 0x0F);
    if (version != 4 || ihl < 5) {
        return -1;
    }
    size_t header_len = (size_t)ihl * 4u;
    if (header_len > len) {
        return -1;
    }
    uint16_t total_length = (uint16_t)((buf[2] << 8) | buf[3]);
    if (total_length < header_len || total_length > len) {
        return -1;
    }

    out->ihl_words = ihl;
    out->protocol = buf[9];
    out->total_length = total_length;
    out->header_len_bytes = header_len;
    memcpy(&out->src_ip, buf + 12, 4);
    memcpy(&out->dst_ip, buf + 16, 4);
    return 0;
}

uint16_t ipv4_checksum(const uint8_t *data, size_t len) {
    uint32_t sum = 0;
    size_t i = 0;
    for (; i + 1 < len; i += 2) {
        uint16_t word = (uint16_t)((data[i] << 8) | data[i + 1]);
        sum += word;
    }
    if (i < len) {
        sum += (uint32_t)(data[i] << 8);
    }
    while (sum >> 16) {
        sum = (sum & 0xFFFFu) + (sum >> 16);
    }
    return (uint16_t)~sum;
}

void ipv4_build(uint8_t *out, uint32_t src_ip, uint32_t dst_ip, uint8_t protocol,
                 uint16_t total_len_host, uint8_t ttl) {
    out[0] = 0x45; /* version 4, ihl 5 (no options) */
    out[1] = 0x00; /* tos */
    out[2] = (uint8_t)(total_len_host >> 8);
    out[3] = (uint8_t)(total_len_host & 0xFF);
    out[4] = 0x00; /* identification */
    out[5] = 0x00;
    out[6] = 0x40; /* flags: don't fragment */
    out[7] = 0x00; /* fragment offset */
    out[8] = ttl;
    out[9] = protocol;
    out[10] = 0x00; /* checksum, filled below */
    out[11] = 0x00;
    memcpy(out + 12, &src_ip, 4);
    memcpy(out + 16, &dst_ip, 4);

    uint16_t csum = ipv4_checksum(out, IPV4_MIN_HDR_LEN);
    out[10] = (uint8_t)(csum >> 8);
    out[11] = (uint8_t)(csum & 0xFF);
}

void ipv4_format(uint32_t addr, char *out, size_t out_cap) {
    const uint8_t *b = (const uint8_t *)&addr;
    snprintf(out, out_cap, "%u.%u.%u.%u", b[0], b[1], b[2], b[3]);
}
