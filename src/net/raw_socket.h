#ifndef L4MLB_NET_RAW_SOCKET_H
#define L4MLB_NET_RAW_SOCKET_H

#include <stddef.h>
#include <stdint.h>
#include <sys/types.h>

#include "eth.h"

/* Opens an AF_PACKET/SOCK_RAW socket bound to ifname, filtered at the
 * kernel level to ethertype_host (host byte order, e.g. ETH_TYPE_IP).
 * Also applies SO_BINDTODEVICE for defense in depth. On success returns
 * the fd (>= 0) and fills *out_ifindex. Returns -1 on failure (errno set). */
int raw_socket_open(const char *ifname, uint16_t ethertype_host, int *out_ifindex);

/* Looks up ifname's MAC address via an AF_PACKET ioctl (SIOCGIFHWADDR).
 * Returns 0 on success, -1 on failure. */
int raw_socket_get_mac(const char *ifname, uint8_t out_mac[ETH_ADDR_LEN]);

/* Receives one frame into buf (buflen bytes of space). Returns the frame
 * length on success, -1 on error (errno set; EAGAIN/EWOULDBLOCK included). */
ssize_t raw_socket_recv(int fd, uint8_t *buf, size_t buflen);

/* Sends one already-fully-built frame (including its Ethernet header) of
 * length len out of ifindex. Returns bytes sent, or -1 on error. */
ssize_t raw_socket_send(int fd, int ifindex, const uint8_t *frame, size_t len);

#endif /* L4MLB_NET_RAW_SOCKET_H */
