#include "metrics_http.h"

#include <errno.h>
#include <netinet/in.h>
#include <poll.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/eventfd.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#include "../net/ipv4.h"

#define METRICS_BODY_CAP (128u * 1024u)
#define REQUEST_TIMEOUT_SEC 5

struct metrics_http {
    struct metrics_http_config cfg;
    time_t start_time;
    int listen_fd;
    int wake_fd;
    pthread_t thread;
    _Atomic bool running;
};

struct metrics_http *metrics_http_create(const struct metrics_http_config *cfg) {
    struct metrics_http *m = calloc(1, sizeof(*m));
    if (m == NULL) {
        return NULL;
    }
    m->cfg = *cfg;
    m->start_time = time(NULL);
    m->listen_fd = -1;
    m->wake_fd = eventfd(0, EFD_NONBLOCK);
    if (m->wake_fd < 0) {
        free(m);
        return NULL;
    }
    atomic_init(&m->running, false);
    return m;
}

void metrics_http_destroy(struct metrics_http *m) {
    if (m == NULL) {
        return;
    }
    if (m->listen_fd >= 0) {
        close(m->listen_fd);
    }
    if (m->wake_fd >= 0) {
        close(m->wake_fd);
    }
    free(m);
}

/* --- Prometheus text-exposition body, built into a fixed-capacity buffer --- */

struct metrics_buf {
    char *data;
    size_t cap;
    size_t len;
};

static void mb_appendf(struct metrics_buf *b, const char *fmt, ...) {
    if (b->len >= b->cap) {
        return;
    }
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(b->data + b->len, b->cap - b->len, fmt, ap);
    va_end(ap);
    if (n > 0) {
        size_t written = (size_t)n;
        size_t room = b->cap - b->len;
        b->len += written < room ? written : room;
    }
}

static void build_metrics_body(struct metrics_http *m, struct metrics_buf *b) {
    struct lb_stats_snapshot snap;
    stats_registry_snapshot(m->cfg.stats, &snap);

    mb_appendf(b, "# HELP maglev_lb_start_time_seconds Unix time the process started.\n");
    mb_appendf(b, "# TYPE maglev_lb_start_time_seconds gauge\n");
    mb_appendf(b, "maglev_lb_start_time_seconds %ld\n", (long)m->start_time);

    mb_appendf(b, "# HELP maglev_lb_table_size Configured Maglev lookup table size (M).\n");
    mb_appendf(b, "# TYPE maglev_lb_table_size gauge\n");
    mb_appendf(b, "maglev_lb_table_size %u\n", m->cfg.table_size);

    mb_appendf(b, "# HELP maglev_lb_rx_packets_total Packets received on the ingestion raw "
                  "socket (clients -> LB).\n");
    mb_appendf(b, "# TYPE maglev_lb_rx_packets_total counter\n");
    mb_appendf(b, "maglev_lb_rx_packets_total %llu\n", (unsigned long long)snap.rx_packets);

    mb_appendf(b, "# HELP maglev_lb_rx_bytes_total Bytes received on the ingestion raw socket.\n");
    mb_appendf(b, "# TYPE maglev_lb_rx_bytes_total counter\n");
    mb_appendf(b, "maglev_lb_rx_bytes_total %llu\n", (unsigned long long)snap.rx_bytes);

    mb_appendf(b, "# HELP maglev_lb_tx_packets_total GRE-encapsulated packets forwarded to "
                  "backends (LB -> backends).\n");
    mb_appendf(b, "# TYPE maglev_lb_tx_packets_total counter\n");
    mb_appendf(b, "maglev_lb_tx_packets_total %llu\n", (unsigned long long)snap.tx_packets);

    mb_appendf(b, "# HELP maglev_lb_tx_bytes_total Bytes forwarded to backends, including the "
                  "GRE+IP+Ethernet wrapper.\n");
    mb_appendf(b, "# TYPE maglev_lb_tx_bytes_total counter\n");
    mb_appendf(b, "maglev_lb_tx_bytes_total %llu\n", (unsigned long long)snap.tx_bytes);

    mb_appendf(b, "# HELP maglev_lb_drops_total Packets the datapath dropped, by reason.\n");
    mb_appendf(b, "# TYPE maglev_lb_drops_total counter\n");
    mb_appendf(b, "maglev_lb_drops_total{reason=\"parse_error\"} %llu\n",
               (unsigned long long)snap.drops_parse_error);
    mb_appendf(b, "maglev_lb_drops_total{reason=\"no_backend\"} %llu\n",
               (unsigned long long)snap.drops_no_backend);
    mb_appendf(b, "maglev_lb_drops_total{reason=\"backend_unhealthy\"} %llu\n",
               (unsigned long long)snap.drops_backend_unhealthy);
    mb_appendf(b, "maglev_lb_drops_total{reason=\"conntrack_full\"} %llu\n",
               (unsigned long long)snap.drops_conntrack_full);

    mb_appendf(b, "# HELP maglev_lb_conntrack_lookups_total Conntrack lookups, by result.\n");
    mb_appendf(b, "# TYPE maglev_lb_conntrack_lookups_total counter\n");
    mb_appendf(b, "maglev_lb_conntrack_lookups_total{result=\"hit\"} %llu\n",
               (unsigned long long)snap.conntrack_hits);
    mb_appendf(b, "maglev_lb_conntrack_lookups_total{result=\"miss\"} %llu\n",
               (unsigned long long)snap.conntrack_misses);

    mb_appendf(b, "# HELP maglev_lb_conntrack_evictions_total Conntrack entries reaped for "
                  "being idle past their timeout.\n");
    mb_appendf(b, "# TYPE maglev_lb_conntrack_evictions_total counter\n");
    mb_appendf(b, "maglev_lb_conntrack_evictions_total %llu\n",
               (unsigned long long)snap.conntrack_evictions);

    mb_appendf(b, "# HELP maglev_lb_conntrack_active_flows Flows currently pinned in the "
                  "conntrack table.\n");
    mb_appendf(b, "# TYPE maglev_lb_conntrack_active_flows gauge\n");
    mb_appendf(b, "maglev_lb_conntrack_active_flows %zu\n", conntrack_active_flows(m->cfg.ct));

    uint64_t generation = table_generator_generation(m->cfg.tg);
    uint64_t last_regen_us = table_generator_last_regen_us(m->cfg.tg);
    mb_appendf(b, "# HELP maglev_lb_table_generation Number of times the Maglev lookup table "
                  "has been (re)generated, including the initial build.\n");
    mb_appendf(b, "# TYPE maglev_lb_table_generation counter\n");
    mb_appendf(b, "maglev_lb_table_generation %llu\n", (unsigned long long)generation);

    mb_appendf(b, "# HELP maglev_lb_table_regen_last_microseconds Wall-clock duration of the "
                  "most recent table regeneration.\n");
    mb_appendf(b, "# TYPE maglev_lb_table_regen_last_microseconds gauge\n");
    mb_appendf(b, "maglev_lb_table_regen_last_microseconds %llu\n",
               (unsigned long long)last_regen_us);

    struct backend backends[BACKEND_MAX];
    size_t n = backend_manager_snapshot_all(m->cfg.bm, backends, BACKEND_MAX);

    size_t eligible = 0;
    for (size_t i = 0; i < n; i++) {
        bool healthy = atomic_load_explicit(&backends[i].healthy, memory_order_relaxed);
        bool mac_resolved = atomic_load_explicit(&backends[i].mac_resolved, memory_order_relaxed);
        if (healthy && mac_resolved && backends[i].admin_state == BACKEND_ENABLED) {
            eligible++;
        }
    }

    mb_appendf(b, "# HELP maglev_lb_backends_configured Backends configured at startup via "
                  "--backend.\n");
    mb_appendf(b, "# TYPE maglev_lb_backends_configured gauge\n");
    mb_appendf(b, "maglev_lb_backends_configured %zu\n", m->cfg.configured_backends);

    mb_appendf(b, "# HELP maglev_lb_backends_eligible Backends currently eligible for new "
                  "flows (healthy, admin-enabled, MAC resolved).\n");
    mb_appendf(b, "# TYPE maglev_lb_backends_eligible gauge\n");
    mb_appendf(b, "maglev_lb_backends_eligible %zu\n", eligible);

    mb_appendf(b, "# HELP maglev_lb_backend_up Whether the backend is currently healthy (1) or "
                  "not (0), as last determined by the health checker.\n");
    mb_appendf(b, "# TYPE maglev_lb_backend_up gauge\n");
    for (size_t i = 0; i < n; i++) {
        char addr[16];
        ipv4_format(backends[i].addr, addr, sizeof(addr));
        bool healthy = atomic_load_explicit(&backends[i].healthy, memory_order_relaxed);
        mb_appendf(b, "maglev_lb_backend_up{backend_id=\"%u\",backend_addr=\"%s:%u\"} %d\n",
                   backends[i].id, addr, backends[i].port, healthy ? 1 : 0);
    }

    mb_appendf(b, "# HELP maglev_lb_backend_admin_enabled Whether the backend's admin state is "
                  "ENABLED (1) rather than DRAINING/DISABLED (0).\n");
    mb_appendf(b, "# TYPE maglev_lb_backend_admin_enabled gauge\n");
    for (size_t i = 0; i < n; i++) {
        char addr[16];
        ipv4_format(backends[i].addr, addr, sizeof(addr));
        mb_appendf(b, "maglev_lb_backend_admin_enabled{backend_id=\"%u\",backend_addr=\"%s:%u\"} %d\n",
                   backends[i].id, addr, backends[i].port,
                   backends[i].admin_state == BACKEND_ENABLED ? 1 : 0);
    }

    mb_appendf(b, "# HELP maglev_lb_backend_draining Whether the backend is currently DRAINING "
                  "(1): no new flows, but flows already pinned to it via conntrack keep being "
                  "forwarded until they finish naturally.\n");
    mb_appendf(b, "# TYPE maglev_lb_backend_draining gauge\n");
    for (size_t i = 0; i < n; i++) {
        char addr[16];
        ipv4_format(backends[i].addr, addr, sizeof(addr));
        mb_appendf(b, "maglev_lb_backend_draining{backend_id=\"%u\",backend_addr=\"%s:%u\"} %d\n",
                   backends[i].id, addr, backends[i].port,
                   backends[i].admin_state == BACKEND_DRAINING ? 1 : 0);
    }

    mb_appendf(b, "# HELP maglev_lb_backend_packets_total GRE-encapsulated packets forwarded "
                  "to this specific backend.\n");
    mb_appendf(b, "# TYPE maglev_lb_backend_packets_total counter\n");
    for (size_t i = 0; i < n; i++) {
        char addr[16];
        ipv4_format(backends[i].addr, addr, sizeof(addr));
        struct per_backend_stats_snapshot bs;
        stats_registry_snapshot_backend(m->cfg.stats, backends[i].id, &bs);
        mb_appendf(b, "maglev_lb_backend_packets_total{backend_id=\"%u\",backend_addr=\"%s:%u\"} %llu\n",
                   backends[i].id, addr, backends[i].port, (unsigned long long)bs.packets);
    }

    mb_appendf(b, "# HELP maglev_lb_backend_bytes_total Bytes forwarded to this specific "
                  "backend (GRE-encapsulated, including the wrapper).\n");
    mb_appendf(b, "# TYPE maglev_lb_backend_bytes_total counter\n");
    for (size_t i = 0; i < n; i++) {
        char addr[16];
        ipv4_format(backends[i].addr, addr, sizeof(addr));
        struct per_backend_stats_snapshot bs;
        stats_registry_snapshot_backend(m->cfg.stats, backends[i].id, &bs);
        mb_appendf(b, "maglev_lb_backend_bytes_total{backend_id=\"%u\",backend_addr=\"%s:%u\"} %llu\n",
                   backends[i].id, addr, backends[i].port, (unsigned long long)bs.bytes);
    }

    mb_appendf(b, "# HELP maglev_lb_backend_active_flows Flows in the conntrack table "
                  "currently pinned to this backend.\n");
    mb_appendf(b, "# TYPE maglev_lb_backend_active_flows gauge\n");
    for (size_t i = 0; i < n; i++) {
        char addr[16];
        ipv4_format(backends[i].addr, addr, sizeof(addr));
        struct per_backend_stats_snapshot bs;
        stats_registry_snapshot_backend(m->cfg.stats, backends[i].id, &bs);
        mb_appendf(b, "maglev_lb_backend_active_flows{backend_id=\"%u\",backend_addr=\"%s:%u\"} %llu\n",
                   backends[i].id, addr, backends[i].port,
                   (unsigned long long)bs.active_flows);
    }

    mb_appendf(b, "# HELP maglev_lb_backend_health_check_failures_total Failed health-check "
                  "probes against this backend (every failure, not just threshold-crossing "
                  "ones).\n");
    mb_appendf(b, "# TYPE maglev_lb_backend_health_check_failures_total counter\n");
    for (size_t i = 0; i < n; i++) {
        char addr[16];
        ipv4_format(backends[i].addr, addr, sizeof(addr));
        struct per_backend_stats_snapshot bs;
        stats_registry_snapshot_backend(m->cfg.stats, backends[i].id, &bs);
        mb_appendf(b,
                   "maglev_lb_backend_health_check_failures_total{backend_id=\"%u\",backend_addr=\"%s:%u\"} %llu\n",
                   backends[i].id, addr, backends[i].port,
                   (unsigned long long)bs.health_check_failures);
    }
}

/* --- HTTP layer: minimal, single-request-at-a-time, no real parsing --- */

static void handle_client(struct metrics_http *m, int cfd) {
    struct timeval tv = {.tv_sec = REQUEST_TIMEOUT_SEC, .tv_usec = 0};
    setsockopt(cfd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(cfd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));

    /* We serve the same response regardless of method/path, so the request
     * itself only needs to be drained, not parsed - one recv() is enough
     * for any real HTTP client's request line + headers in one segment. */
    char req[2048];
    ssize_t n = recv(cfd, req, sizeof(req), 0);
    (void)n;

    char *body = malloc(METRICS_BODY_CAP);
    if (body == NULL) {
        close(cfd);
        return;
    }
    struct metrics_buf mb = {.data = body, .cap = METRICS_BODY_CAP, .len = 0};
    build_metrics_body(m, &mb);

    char header[256];
    int hlen = snprintf(header, sizeof(header),
                         "HTTP/1.1 200 OK\r\n"
                         "Content-Type: text/plain; version=0.0.4\r\n"
                         "Content-Length: %zu\r\n"
                         "Connection: close\r\n"
                         "\r\n",
                         mb.len);
    if (hlen > 0) {
        ssize_t w1 = send(cfd, header, (size_t)hlen, MSG_NOSIGNAL);
        ssize_t w2 = send(cfd, mb.data, mb.len, MSG_NOSIGNAL);
        (void)w1;
        (void)w2;
    }

    free(body);
    close(cfd);
}

static void *thread_main(void *arg) {
    struct metrics_http *m = arg;
    for (;;) {
        struct pollfd pfds[2];
        pfds[0].fd = m->listen_fd;
        pfds[0].events = POLLIN;
        pfds[1].fd = m->wake_fd;
        pfds[1].events = POLLIN;

        int pr;
        do {
            pr = poll(pfds, 2, -1);
        } while (pr < 0 && errno == EINTR);
        if (pr <= 0) {
            continue;
        }
        if (pfds[1].revents & POLLIN) {
            break; /* stop requested */
        }
        if (pfds[0].revents & POLLIN) {
            int cfd = accept(m->listen_fd, NULL, NULL);
            if (cfd >= 0) {
                handle_client(m, cfd);
            }
        }
    }
    return NULL;
}

int metrics_http_start(struct metrics_http *m) {
    m->listen_fd = socket(AF_INET, SOCK_STREAM, 0);
    if (m->listen_fd < 0) {
        return -1;
    }
    int one = 1;
    setsockopt(m->listen_fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = INADDR_ANY;
    addr.sin_port = htons(m->cfg.port);
    if (bind(m->listen_fd, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        close(m->listen_fd);
        m->listen_fd = -1;
        return -1;
    }
    if (listen(m->listen_fd, 16) != 0) {
        close(m->listen_fd);
        m->listen_fd = -1;
        return -1;
    }

    if (pthread_create(&m->thread, NULL, thread_main, m) != 0) {
        close(m->listen_fd);
        m->listen_fd = -1;
        return -1;
    }
    atomic_store(&m->running, true);
    return 0;
}

void metrics_http_stop(struct metrics_http *m) {
    if (m == NULL || !atomic_load(&m->running)) {
        return;
    }
    uint64_t one = 1;
    ssize_t w = write(m->wake_fd, &one, sizeof(one));
    (void)w;
    pthread_join(m->thread, NULL);
    atomic_store(&m->running, false);
}
