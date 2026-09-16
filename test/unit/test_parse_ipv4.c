#include <arpa/inet.h>
#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "net/eth.h"
#include "net/flow.h"
#include "net/ipv4.h"
#include "net/tcp_udp.h"

static void build_test_frame(uint8_t *buf, size_t *out_len, uint8_t proto, uint16_t payload_len) {
    uint8_t dst_mac[6] = {0x02, 0, 0, 0, 0, 0x01};
    uint8_t src_mac[6] = {0x02, 0, 0, 0, 0, 0x02};
    eth_build(buf, dst_mac, src_mac, ETH_TYPE_IP);

    size_t l4_hdr_len = (proto == 6) ? TCP_MIN_HDR_LEN : UDP_HDR_LEN;
    uint16_t ip_total = (uint16_t)(IPV4_MIN_HDR_LEN + l4_hdr_len + payload_len);

    struct in_addr src, dst;
    inet_pton(AF_INET, "10.0.0.5", &src);
    inet_pton(AF_INET, "10.99.0.100", &dst);
    ipv4_build(buf + ETH_HDR_LEN, src.s_addr, dst.s_addr, proto, ip_total, 64);

    uint8_t *l4 = buf + ETH_HDR_LEN + IPV4_MIN_HDR_LEN;
    memset(l4, 0, l4_hdr_len + payload_len);
    l4[0] = 51234 >> 8;
    l4[1] = 51234 & 0xFF;
    l4[2] = 80 >> 8;
    l4[3] = 80 & 0xFF;

    *out_len = ETH_HDR_LEN + ip_total;
}

static void test_eth_roundtrip(void) {
    uint8_t buf[64];
    memset(buf, 0, sizeof(buf));
    uint8_t dst[6] = {1, 2, 3, 4, 5, 6};
    uint8_t src[6] = {6, 5, 4, 3, 2, 1};
    eth_build(buf, dst, src, 0x0800);

    struct eth_hdr_view v;
    assert(eth_parse(buf, sizeof(buf), &v) == 0);
    assert(memcmp(v.dst_mac, dst, 6) == 0);
    assert(memcmp(v.src_mac, src, 6) == 0);
    assert(v.ethertype == 0x0800);

    assert(eth_parse(buf, 13, &v) == -1);
}

static void test_ipv4_checksum_known_value(void) {
    /* Wikipedia's worked IPv4-header-checksum example. */
    uint8_t hdr[20] = {
        0x45, 0x00, 0x00, 0x3c, 0x1c, 0x46, 0x40, 0x00, 0x40, 0x06,
        0x00, 0x00, 0xac, 0x10, 0x0a, 0x63, 0xac, 0x10, 0x0a, 0x0c,
    };
    uint16_t csum = ipv4_checksum(hdr, sizeof(hdr));
    assert(csum == 0xb1e6); /* known answer for this exact worked example */
    hdr[10] = (uint8_t)(csum >> 8);
    hdr[11] = (uint8_t)(csum & 0xFF);
    /* Recomputing over the header with the correct checksum filled in must
     * come back as the ones'-complement of all-ones, i.e. zero - the
     * standard verification identity used to validate a received checksum. */
    assert(ipv4_checksum(hdr, sizeof(hdr)) == 0x0000);
}

static void test_ipv4_build_and_parse(void) {
    uint8_t buf[28]; /* 20-byte header + 8 bytes of payload, matching total_length below */
    memset(buf, 0, sizeof(buf));
    struct in_addr src, dst;
    inet_pton(AF_INET, "10.99.0.1", &src);
    inet_pton(AF_INET, "10.99.0.100", &dst);
    ipv4_build(buf, src.s_addr, dst.s_addr, 17, 28, 64);

    struct ipv4_hdr_view v;
    assert(ipv4_parse(buf, sizeof(buf), &v) == 0);
    assert(v.protocol == 17);
    assert(v.total_length == 28);
    assert(v.header_len_bytes == 20);
    assert(v.src_ip == src.s_addr);
    assert(v.dst_ip == dst.s_addr);
    assert(ipv4_checksum(buf, 20) == 0x0000);

    /* A header claiming a total_length that exceeds the actual buffer must
     * be rejected, even though the 20-byte header itself is intact. */
    assert(ipv4_parse(buf, 20, &v) == -1);
    /* Truncating the header itself must also be rejected. */
    assert(ipv4_parse(buf, 19, &v) == -1);
}

static void test_extract_flow_tcp(void) {
    uint8_t buf[128];
    size_t len;
    build_test_frame(buf, &len, 6, 4);

    struct flow_extract_result fx;
    assert(packet_extract_flow(buf, len, &fx) == PARSE_OK);
    assert(fx.key.proto == 6);
    assert(fx.key.src_port == htons(51234));
    assert(fx.key.dst_port == htons(80));
    assert(fx.ip_offset == ETH_HDR_LEN);
}

static void test_extract_flow_truncated(void) {
    uint8_t buf[128];
    size_t len;
    build_test_frame(buf, &len, 6, 4);

    size_t full_hdr_len = ETH_HDR_LEN + IPV4_MIN_HDR_LEN + TCP_MIN_HDR_LEN;
    for (size_t truncated = 0; truncated < full_hdr_len; truncated++) {
        struct flow_extract_result fx;
        assert(packet_extract_flow(buf, truncated, &fx) == PARSE_TRUNCATED);
    }
}

static void test_extract_flow_unsupported_ethertype(void) {
    uint8_t buf[64];
    memset(buf, 0, sizeof(buf));
    uint8_t dst[6] = {0}, src[6] = {0};
    eth_build(buf, dst, src, ETH_TYPE_ARP);
    struct flow_extract_result fx;
    assert(packet_extract_flow(buf, sizeof(buf), &fx) == PARSE_UNSUPPORTED);
}

int main(void) {
    test_eth_roundtrip();
    test_ipv4_checksum_known_value();
    test_ipv4_build_and_parse();
    test_extract_flow_tcp();
    test_extract_flow_truncated();
    test_extract_flow_unsupported_ethertype();
    printf("test_parse_ipv4: all tests passed\n");
    return 0;
}
