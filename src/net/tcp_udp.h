#ifndef L4MLB_NET_TCP_UDP_H
#define L4MLB_NET_TCP_UDP_H

#include <stddef.h>
#include <stdint.h>

#define TCP_MIN_HDR_LEN 20u
#define UDP_HDR_LEN 8u

/* Extracts src/dst ports (opaque network-byte-order values) from a TCP
 * header at buf (len bytes available, starting at the TCP header). Requires
 * the full fixed TCP header (20 bytes) to be present, even though only the
 * first 4 bytes are read, so truncated TCP headers are rejected uniformly.
 * Returns 0 on success, -1 if len < TCP_MIN_HDR_LEN. */
int tcp_parse_ports(const uint8_t *buf, size_t len, uint16_t *src_port, uint16_t *dst_port);

/* Extracts src/dst ports (opaque network-byte-order values) from a UDP
 * header at buf (len bytes available, starting at the UDP header). Returns
 * 0 on success, -1 if len < UDP_HDR_LEN. */
int udp_parse_ports(const uint8_t *buf, size_t len, uint16_t *src_port, uint16_t *dst_port);

#endif /* L4MLB_NET_TCP_UDP_H */
