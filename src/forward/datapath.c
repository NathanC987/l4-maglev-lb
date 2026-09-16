#include "datapath.h"

#include <errno.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/signalfd.h>
#include <time.h>
#include <unistd.h>

#include "../conntrack/reaper.h"
#include "../maglev/maglev_table.h"
#include "../net/eth.h"
#include "../net/flow.h"
#include "../net/gre.h"
#include "../net/raw_socket.h"

#define RX_BUF_LEN 2048u
#define TX_BUF_LEN 2048u
#define MAX_EPOLL_EVENTS 8

struct datapath {
    struct datapath_config cfg;
    int raw_fd;
    int ifindex;
    uint8_t own_mac[ETH_ADDR_LEN];
    int timerfd;
    int signalfd_fd;
    int epfd;
    size_t reaper_buckets_per_tick;
};

struct datapath *datapath_create(const struct datapath_config *cfg) {
    struct datapath *dp = calloc(1, sizeof(*dp));
    if (dp == NULL) {
        return NULL;
    }
    dp->cfg = *cfg;
    dp->raw_fd = -1;
    dp->timerfd = -1;
    dp->signalfd_fd = -1;
    dp->epfd = -1;
    return dp;
}

void datapath_destroy(struct datapath *dp) {
    if (dp == NULL) {
        return;
    }
    if (dp->raw_fd >= 0) {
        close(dp->raw_fd);
    }
    if (dp->timerfd >= 0) {
        close(dp->timerfd);
    }
    if (dp->signalfd_fd >= 0) {
        close(dp->signalfd_fd);
    }
    if (dp->epfd >= 0) {
        close(dp->epfd);
    }
    free(dp);
}

static uint64_t now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

static void handle_packet(struct datapath *dp, uint8_t *rx_buf, uint8_t *tx_buf) {
    ssize_t n = raw_socket_recv(dp->raw_fd, rx_buf, RX_BUF_LEN);
    if (n <= 0) {
        return;
    }

    struct lb_stats *stats = dp->cfg.stats;
    atomic_fetch_add_explicit(&stats->rx_packets, 1, memory_order_relaxed);
    atomic_fetch_add_explicit(&stats->rx_bytes, (uint64_t)n, memory_order_relaxed);

    struct flow_extract_result fx;
    enum parse_status ps = packet_extract_flow(rx_buf, (size_t)n, &fx);
    if (ps != PARSE_OK) {
        if (ps == PARSE_TRUNCATED) {
            atomic_fetch_add_explicit(&stats->drops_parse_error, 1, memory_order_relaxed);
        }
        /* PARSE_UNSUPPORTED covers ARP and any other background traffic
         * visible on this shared L2 segment - expected, not an error. */
        return;
    }
    if (fx.key.dst_ip != dp->cfg.vip) {
        return; /* not addressed to us */
    }

    uint64_t now = now_ns();
    int32_t backend_id = -1;
    int hit = conntrack_lookup(dp->cfg.ct, &fx.key, now, &backend_id);
    atomic_fetch_add_explicit(hit ? &stats->conntrack_hits : &stats->conntrack_misses, 1,
                               memory_order_relaxed);

    struct routing_snapshot *snap = table_generator_get_active(dp->cfg.tg);
    if (snap == NULL) {
        atomic_fetch_add_explicit(&stats->drops_no_backend, 1, memory_order_relaxed);
        return;
    }

    if (!hit) {
        backend_id = maglev_table_lookup(snap->table, &fx.key);
        if (backend_id < 0) {
            atomic_fetch_add_explicit(&stats->drops_no_backend, 1, memory_order_relaxed);
            return;
        }
        if (conntrack_insert(dp->cfg.ct, &fx.key, backend_id, now) != 0) {
            atomic_fetch_add_explicit(&stats->drops_conntrack_full, 1, memory_order_relaxed);
            return;
        }
    }

    const struct backend_view *backend = routing_snapshot_find_backend(snap, backend_id);
    if (backend == NULL) {
        /* Pinned (via conntrack) to a backend no longer in the active
         * snapshot. Unreachable in v1 (static backend set); M2's dynamic
         * churn will need a real re-pinning policy here. */
        atomic_fetch_add_explicit(&stats->drops_backend_unhealthy, 1, memory_order_relaxed);
        return;
    }

    const uint8_t *inner_packet = rx_buf + fx.ip_offset;
    size_t frame_len = gre_encap_build(tx_buf, TX_BUF_LEN, backend->mac, dp->own_mac,
                                        dp->cfg.director_ip, backend->addr, dp->cfg.ttl,
                                        inner_packet, fx.ip_total_len);
    if (frame_len == 0) {
        atomic_fetch_add_explicit(&stats->drops_parse_error, 1, memory_order_relaxed);
        return;
    }

    ssize_t sent = raw_socket_send(dp->raw_fd, dp->ifindex, tx_buf, frame_len);
    if (sent > 0) {
        atomic_fetch_add_explicit(&stats->tx_packets, 1, memory_order_relaxed);
        atomic_fetch_add_explicit(&stats->tx_bytes, (uint64_t)sent, memory_order_relaxed);
        stats_record_backend_packet(stats, (uint32_t)backend_id, (size_t)sent);
    }
}

static int setup_epoll(struct datapath *dp) {
    dp->epfd = epoll_create1(0);
    if (dp->epfd < 0) {
        return -1;
    }
    struct epoll_event ev;
    memset(&ev, 0, sizeof(ev));
    ev.events = EPOLLIN;

    ev.data.fd = dp->raw_fd;
    if (epoll_ctl(dp->epfd, EPOLL_CTL_ADD, dp->raw_fd, &ev) != 0) {
        return -1;
    }
    ev.data.fd = dp->timerfd;
    if (epoll_ctl(dp->epfd, EPOLL_CTL_ADD, dp->timerfd, &ev) != 0) {
        return -1;
    }
    ev.data.fd = dp->signalfd_fd;
    if (epoll_ctl(dp->epfd, EPOLL_CTL_ADD, dp->signalfd_fd, &ev) != 0) {
        return -1;
    }
    return 0;
}

int datapath_run(struct datapath *dp) {
    dp->raw_fd = raw_socket_open(dp->cfg.ifname, ETH_TYPE_IP, &dp->ifindex);
    if (dp->raw_fd < 0) {
        fprintf(stderr, "datapath: raw_socket_open(%s): %s\n", dp->cfg.ifname, strerror(errno));
        return -1;
    }
    if (raw_socket_get_mac(dp->cfg.ifname, dp->own_mac) != 0) {
        fprintf(stderr, "datapath: raw_socket_get_mac(%s): %s\n", dp->cfg.ifname, strerror(errno));
        return -1;
    }

    dp->timerfd = reaper_timerfd_create(dp->cfg.reaper_interval_ms);
    if (dp->timerfd < 0) {
        fprintf(stderr, "datapath: reaper_timerfd_create: %s\n", strerror(errno));
        return -1;
    }

    sigset_t mask;
    sigemptyset(&mask);
    sigaddset(&mask, SIGINT);
    sigaddset(&mask, SIGTERM);
    if (sigprocmask(SIG_BLOCK, &mask, NULL) != 0) {
        fprintf(stderr, "datapath: sigprocmask: %s\n", strerror(errno));
        return -1;
    }
    dp->signalfd_fd = signalfd(-1, &mask, SFD_NONBLOCK);
    if (dp->signalfd_fd < 0) {
        fprintf(stderr, "datapath: signalfd: %s\n", strerror(errno));
        return -1;
    }

    if (setup_epoll(dp) != 0) {
        fprintf(stderr, "datapath: epoll setup: %s\n", strerror(errno));
        return -1;
    }

    dp->reaper_buckets_per_tick = conntrack_bucket_count(dp->cfg.ct) / 60 + 1;

    uint8_t rx_buf[RX_BUF_LEN];
    uint8_t tx_buf[TX_BUF_LEN];
    struct epoll_event events[MAX_EPOLL_EVENTS];

    int running = 1;
    while (running) {
        int n = epoll_wait(dp->epfd, events, MAX_EPOLL_EVENTS, -1);
        if (n < 0) {
            if (errno == EINTR) {
                continue;
            }
            fprintf(stderr, "datapath: epoll_wait: %s\n", strerror(errno));
            return -1;
        }

        for (int i = 0; i < n; i++) {
            int fd = events[i].data.fd;
            if (fd == dp->signalfd_fd) {
                struct signalfd_siginfo si;
                ssize_t r = read(dp->signalfd_fd, &si, sizeof(si));
                (void)r;
                running = 0;
                break;
            } else if (fd == dp->timerfd) {
                size_t reaped =
                    reaper_on_timer_fired(dp->timerfd, dp->cfg.ct, now_ns(), dp->cfg.tcp_timeout_ns,
                                           dp->cfg.udp_timeout_ns, dp->reaper_buckets_per_tick);
                atomic_fetch_add_explicit(&dp->cfg.stats->conntrack_evictions, reaped,
                                           memory_order_relaxed);
            } else if (fd == dp->raw_fd) {
                handle_packet(dp, rx_buf, tx_buf);
            }
        }
    }

    return 0;
}
