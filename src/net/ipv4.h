#ifndef L4MLB_NET_IPV4_H
#define L4MLB_NET_IPV4_H

#include <stddef.h>
#include <stdint.h>

#define IPV4_MIN_HDR_LEN 20u

struct ipv4_hdr_view {
    uint8_t ihl_words;        /* header length in 32-bit words, from the packet */
    uint8_t protocol;
    uint16_t total_length;    /* host byte order, as declared in the header */
    uint32_t src_ip;          /* opaque network-byte-order value, do not byte-swap */
    uint32_t dst_ip;          /* opaque network-byte-order value, do not byte-swap */
    size_t header_len_bytes;  /* ihl_words * 4, already validated to fit in len */
};

/* Parses an IPv4 header from buf (len bytes available, starting at the IP
 * header, i.e. after any Ethernet header). Validates that ihl*4 and
 * total_length both fit within len before trusting anything derived from
 * them. Returns 0 on success, -1 on truncation/malformed input. */
int ipv4_parse(const uint8_t *buf, size_t len, struct ipv4_hdr_view *out);

/* Standard Internet checksum (RFC 1071) over data/len, computed as if the
 * checksum field itself were zero. Returned value is a host-order integer;
 * callers must split it into two big-endian bytes when storing it. */
uint16_t ipv4_checksum(const uint8_t *data, size_t len);

/* Builds a 20-byte IPv4 header (no options) into out, including a computed
 * checksum. src_ip/dst_ip are opaque network-byte-order values as returned
 * by ipv4_parse or inet_addr(). total_len_host is the full IP packet length
 * (header + payload). out must have at least IPV4_MIN_HDR_LEN bytes. */
void ipv4_build(uint8_t *out, uint32_t src_ip, uint32_t dst_ip, uint8_t protocol,
                 uint16_t total_len_host, uint8_t ttl);

#endif /* L4MLB_NET_IPV4_H */
