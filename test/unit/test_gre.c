#include <arpa/inet.h>
#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "net/eth.h"
#include "net/gre.h"
#include "net/ipv4.h"

static void test_gre_header_roundtrip(void) {
    uint8_t buf[GRE_HDR_LEN];
    gre_build(buf, ETH_TYPE_IP);
    assert(buf[0] == 0x00 && buf[1] == 0x00);
    assert(buf[2] == 0x08 && buf[3] == 0x00);

    struct gre_hdr_view v;
    assert(gre_parse(buf, sizeof(buf), &v) == 0);
    assert(v.flags_version == 0);
    assert(v.protocol_type == ETH_TYPE_IP);

    assert(gre_parse(buf, GRE_HDR_LEN - 1, &v) == -1);

    uint8_t bad[GRE_HDR_LEN] = {0x80, 0x00, 0x08, 0x00}; /* checksum-present bit set */
    assert(gre_parse(bad, sizeof(bad), &v) == -1);
}

static void test_gre_encap_build(void) {
    uint8_t dst_mac[ETH_ADDR_LEN] = {0xaa, 0xaa, 0xaa, 0xaa, 0xaa, 0xaa};
    uint8_t src_mac[ETH_ADDR_LEN] = {0xbb, 0xbb, 0xbb, 0xbb, 0xbb, 0xbb};
    struct in_addr outer_src, outer_dst;
    inet_pton(AF_INET, "10.99.0.1", &outer_src);
    inet_pton(AF_INET, "10.99.0.11", &outer_dst);

    uint8_t inner[40];
    for (size_t i = 0; i < sizeof(inner); i++) {
        inner[i] = (uint8_t)i;
    }

    uint8_t out[128];
    size_t n = gre_encap_build(out, sizeof(out), dst_mac, src_mac, outer_src.s_addr,
                                outer_dst.s_addr, 64, inner, sizeof(inner));
    assert(n == ETH_HDR_LEN + IPV4_MIN_HDR_LEN + GRE_HDR_LEN + sizeof(inner));

    struct eth_hdr_view eth;
    assert(eth_parse(out, n, &eth) == 0);
    assert(memcmp(eth.dst_mac, dst_mac, ETH_ADDR_LEN) == 0);
    assert(memcmp(eth.src_mac, src_mac, ETH_ADDR_LEN) == 0);
    assert(eth.ethertype == ETH_TYPE_IP);

    struct ipv4_hdr_view ip;
    assert(ipv4_parse(out + ETH_HDR_LEN, n - ETH_HDR_LEN, &ip) == 0);
    assert(ip.protocol == IPPROTO_GRE_VAL);
    assert(ip.src_ip == outer_src.s_addr);
    assert(ip.dst_ip == outer_dst.s_addr);
    assert(ip.total_length == IPV4_MIN_HDR_LEN + GRE_HDR_LEN + sizeof(inner));

    size_t gre_off = ETH_HDR_LEN + IPV4_MIN_HDR_LEN;
    struct gre_hdr_view gre;
    assert(gre_parse(out + gre_off, n - gre_off, &gre) == 0);
    assert(gre.protocol_type == ETH_TYPE_IP);

    size_t inner_off = gre_off + GRE_HDR_LEN;
    assert(memcmp(out + inner_off, inner, sizeof(inner)) == 0); /* byte-for-byte, untouched */

    assert(gre_encap_build(out, 10, dst_mac, src_mac, outer_src.s_addr, outer_dst.s_addr, 64,
                            inner, sizeof(inner)) == 0);
}

int main(void) {
    test_gre_header_roundtrip();
    test_gre_encap_build();
    printf("test_gre: all tests passed\n");
    return 0;
}
