#ifndef L4MLB_NET_FLOW_H
#define L4MLB_NET_FLOW_H

#include <stddef.h>
#include <stdint.h>

/* The 5-tuple identifying a client flow. All fields are opaque
 * network-byte-order values (never byte-swapped, only compared/hashed). */
struct flow_key {
    uint32_t src_ip;
    uint32_t dst_ip;
    uint16_t src_port;
    uint16_t dst_port;
    uint8_t proto; /* IPPROTO_TCP or IPPROTO_UDP */
} __attribute__((packed));

enum parse_status {
    PARSE_OK = 0,
    PARSE_TRUNCATED,   /* buffer too short for a header a prior header claimed */
    PARSE_UNSUPPORTED, /* well-formed but not something we route (ARP, IPv6, ICMP, ...) */
};

/* Result of extracting a flow from a raw Ethernet frame. */
struct flow_extract_result {
    struct flow_key key;
    size_t ip_offset;   /* byte offset of the IPv4 header within buf */
    size_t ip_total_len; /* IPv4 total_length as declared in the header (validated to fit) */
};

/* Runs the full L2/L3/L4 parse chain (Ethernet -> IPv4 -> TCP/UDP) over a raw
 * frame received from a raw socket, and extracts the 5-tuple. Every length
 * used is validated against the actual received len at each step before
 * being trusted. Only IPv4 TCP/UDP packets are supported in v1; anything
 * else (ARP, IPv6, ICMP, etc.) is reported as PARSE_UNSUPPORTED. */
enum parse_status packet_extract_flow(const uint8_t *buf, size_t len,
                                       struct flow_extract_result *out);

#endif /* L4MLB_NET_FLOW_H */
