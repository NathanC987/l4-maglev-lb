#include "tcp_udp.h"

#include <string.h>

int tcp_parse_ports(const uint8_t *buf, size_t len, uint16_t *src_port, uint16_t *dst_port) {
    if (len < TCP_MIN_HDR_LEN) {
        return -1;
    }
    memcpy(src_port, buf + 0, 2);
    memcpy(dst_port, buf + 2, 2);
    return 0;
}

int udp_parse_ports(const uint8_t *buf, size_t len, uint16_t *src_port, uint16_t *dst_port) {
    if (len < UDP_HDR_LEN) {
        return -1;
    }
    memcpy(src_port, buf + 0, 2);
    memcpy(dst_port, buf + 2, 2);
    return 0;
}
