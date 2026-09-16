#include "flow.h"

#include <netinet/in.h>

#include "eth.h"
#include "ipv4.h"
#include "tcp_udp.h"

enum parse_status packet_extract_flow(const uint8_t *buf, size_t len,
                                       struct flow_extract_result *out) {
    struct eth_hdr_view eth;
    if (eth_parse(buf, len, &eth) != 0) {
        return PARSE_TRUNCATED;
    }
    if (eth.ethertype != ETH_TYPE_IP) {
        return PARSE_UNSUPPORTED;
    }

    size_t ip_off = ETH_HDR_LEN;
    if (ip_off > len) {
        return PARSE_TRUNCATED;
    }
    struct ipv4_hdr_view ip;
    if (ipv4_parse(buf + ip_off, len - ip_off, &ip) != 0) {
        return PARSE_TRUNCATED;
    }
    if (ip.protocol != IPPROTO_TCP && ip.protocol != IPPROTO_UDP) {
        return PARSE_UNSUPPORTED;
    }

    size_t l4_off = ip_off + ip.header_len_bytes;
    size_t l4_available = len - l4_off; /* safe: header_len_bytes already validated <= len - ip_off */

    uint16_t src_port, dst_port;
    int rc;
    if (ip.protocol == IPPROTO_TCP) {
        rc = tcp_parse_ports(buf + l4_off, l4_available, &src_port, &dst_port);
    } else {
        rc = udp_parse_ports(buf + l4_off, l4_available, &src_port, &dst_port);
    }
    if (rc != 0) {
        return PARSE_TRUNCATED;
    }

    out->key.src_ip = ip.src_ip;
    out->key.dst_ip = ip.dst_ip;
    out->key.src_port = src_port;
    out->key.dst_port = dst_port;
    out->key.proto = ip.protocol;
    out->ip_offset = ip_off;
    out->ip_total_len = ip.total_length;
    return PARSE_OK;
}
