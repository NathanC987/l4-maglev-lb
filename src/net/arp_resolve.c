#include "arp_resolve.h"

#include <arpa/inet.h>
#include <errno.h>
#include <net/if.h>
#include <netpacket/packet.h>
#include <poll.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define ARP_PAYLOAD_LEN 28u
#define ARP_FRAME_LEN (ETH_HDR_LEN + ARP_PAYLOAD_LEN)
#define ARP_HTYPE_ETHERNET 1u
#define ARP_OP_REQUEST 1u
#define ARP_OP_REPLY 2u

static const uint8_t k_broadcast_mac[ETH_ADDR_LEN] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
static const uint8_t k_zero_mac[ETH_ADDR_LEN] = {0, 0, 0, 0, 0, 0};

static void build_arp_frame(uint8_t out[ARP_FRAME_LEN], const uint8_t src_mac[ETH_ADDR_LEN],
                             uint32_t src_ip, uint32_t target_ip) {
    eth_build(out, k_broadcast_mac, src_mac, ETH_TYPE_ARP);

    uint8_t *p = out + ETH_HDR_LEN;
    p[0] = 0x00;
    p[1] = ARP_HTYPE_ETHERNET;
    p[2] = 0x08;
    p[3] = 0x00; /* ptype = IPv4 */
    p[4] = ETH_ADDR_LEN;
    p[5] = 4; /* hlen, plen */
    p[6] = 0x00;
    p[7] = ARP_OP_REQUEST;
    memcpy(p + 8, src_mac, ETH_ADDR_LEN);
    memcpy(p + 14, &src_ip, 4);
    memcpy(p + 18, k_zero_mac, ETH_ADDR_LEN);
    memcpy(p + 24, &target_ip, 4);
}

static int64_t now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

int arp_resolve(const char *ifname, uint32_t target_ip, const uint8_t src_mac[ETH_ADDR_LEN],
                 uint32_t src_ip, uint8_t out_mac[ETH_ADDR_LEN], int timeout_ms) {
    int fd = socket(AF_PACKET, SOCK_RAW, htons(ETH_TYPE_ARP));
    if (fd < 0) {
        return -1;
    }

    int ifindex = (int)if_nametoindex(ifname);
    if (ifindex == 0) {
        close(fd);
        return -1;
    }

    struct sockaddr_ll sll;
    memset(&sll, 0, sizeof(sll));
    sll.sll_family = AF_PACKET;
    sll.sll_protocol = htons(ETH_TYPE_ARP);
    sll.sll_ifindex = ifindex;
    if (bind(fd, (struct sockaddr *)&sll, sizeof(sll)) != 0) {
        close(fd);
        return -1;
    }

    uint8_t req[ARP_FRAME_LEN];
    build_arp_frame(req, src_mac, src_ip, target_ip);

    struct sockaddr_ll dest = sll;
    dest.sll_halen = ETH_ADDR_LEN;
    memcpy(dest.sll_addr, k_broadcast_mac, ETH_ADDR_LEN);
    if (sendto(fd, req, ARP_FRAME_LEN, 0, (struct sockaddr *)&dest, sizeof(dest)) < 0) {
        close(fd);
        return -1;
    }

    int64_t deadline = now_ms() + timeout_ms;
    uint8_t rx[128];
    for (;;) {
        int64_t remaining = deadline - now_ms();
        if (remaining <= 0) {
            close(fd);
            errno = ETIMEDOUT;
            return -1;
        }

        struct pollfd pfd = {.fd = fd, .events = POLLIN};
        int pr = poll(&pfd, 1, (int)remaining);
        if (pr < 0) {
            if (errno == EINTR) {
                continue;
            }
            close(fd);
            return -1;
        }
        if (pr == 0) {
            continue; /* deadline check above handles real timeout */
        }

        ssize_t n = recv(fd, rx, sizeof(rx), 0);
        if (n < (ssize_t)ARP_FRAME_LEN) {
            continue;
        }

        struct eth_hdr_view eth;
        if (eth_parse(rx, (size_t)n, &eth) != 0 || eth.ethertype != ETH_TYPE_ARP) {
            continue;
        }
        const uint8_t *p = rx + ETH_HDR_LEN;
        uint16_t oper = (uint16_t)((p[6] << 8) | p[7]);
        if (oper != ARP_OP_REPLY) {
            continue;
        }
        uint32_t spa;
        memcpy(&spa, p + 14, 4);
        if (spa != target_ip) {
            continue;
        }

        memcpy(out_mac, p + 8, ETH_ADDR_LEN);
        close(fd);
        return 0;
    }
}
