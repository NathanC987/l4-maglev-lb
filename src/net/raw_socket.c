#include "raw_socket.h"

#include <arpa/inet.h>
#include <errno.h>
#include <net/if.h>
#include <netpacket/packet.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <unistd.h>

int raw_socket_open(const char *ifname, uint16_t ethertype_host, int *out_ifindex) {
    int fd = socket(AF_PACKET, SOCK_RAW, htons(ethertype_host));
    if (fd < 0) {
        return -1;
    }

    int ifindex = (int)if_nametoindex(ifname);
    if (ifindex == 0) {
        int saved_errno = errno != 0 ? errno : ENODEV;
        close(fd);
        errno = saved_errno;
        return -1;
    }

    struct sockaddr_ll sll;
    memset(&sll, 0, sizeof(sll));
    sll.sll_family = AF_PACKET;
    sll.sll_protocol = htons(ethertype_host);
    sll.sll_ifindex = ifindex;
    if (bind(fd, (struct sockaddr *)&sll, sizeof(sll)) != 0) {
        int saved_errno = errno;
        close(fd);
        errno = saved_errno;
        return -1;
    }

    if (setsockopt(fd, SOL_SOCKET, SO_BINDTODEVICE, ifname, (socklen_t)strlen(ifname)) != 0) {
        int saved_errno = errno;
        close(fd);
        errno = saved_errno;
        return -1;
    }

    *out_ifindex = ifindex;
    return fd;
}

int raw_socket_get_mac(const char *ifname, uint8_t out_mac[ETH_ADDR_LEN]) {
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) {
        return -1;
    }

    struct ifreq ifr;
    memset(&ifr, 0, sizeof(ifr));
    strncpy(ifr.ifr_name, ifname, IFNAMSIZ - 1);
    if (ioctl(fd, SIOCGIFHWADDR, &ifr) != 0) {
        int saved_errno = errno;
        close(fd);
        errno = saved_errno;
        return -1;
    }
    close(fd);

    memcpy(out_mac, ifr.ifr_hwaddr.sa_data, ETH_ADDR_LEN);
    return 0;
}

ssize_t raw_socket_recv(int fd, uint8_t *buf, size_t buflen) {
    return recv(fd, buf, buflen, 0);
}

ssize_t raw_socket_send(int fd, int ifindex, const uint8_t *frame, size_t len) {
    struct sockaddr_ll sll;
    memset(&sll, 0, sizeof(sll));
    sll.sll_family = AF_PACKET;
    sll.sll_ifindex = ifindex;
    sll.sll_halen = ETH_ADDR_LEN;
    memcpy(sll.sll_addr, frame, ETH_ADDR_LEN); /* destination MAC, already in the frame */

    return sendto(fd, frame, len, 0, (struct sockaddr *)&sll, sizeof(sll));
}
