#ifndef L4MLB_FORWARD_DATAPATH_H
#define L4MLB_FORWARD_DATAPATH_H

#include <stdint.h>

#include "../backend/table_generator.h"
#include "../conntrack/conntrack.h"
#include "../stats/stats_registry.h"

struct datapath_config {
    const char *ifname; /* single shared interface: both ingress and egress in v1 */
    uint32_t vip;         /* opaque network-byte-order IPv4; only dst==vip packets are handled */
    uint32_t director_ip; /* opaque network-byte-order IPv4; GRE outer source */
    uint8_t ttl;           /* outer IP TTL for GRE-encapsulated packets */
    struct table_generator *tg;
    struct conntrack_table *ct;
    struct lb_stats *stats;
    uint32_t reaper_interval_ms;
    uint64_t tcp_timeout_ns;
    uint64_t udp_timeout_ns;
};

struct datapath;

/* Does not open any sockets/fds yet - that happens in datapath_run(), so a
 * create/destroy pair with no run() in between is always safe to tear down. */
struct datapath *datapath_create(const struct datapath_config *cfg);
void datapath_destroy(struct datapath *dp);

/* Opens the raw socket on cfg.ifname, sets up the epoll loop (raw socket +
 * conntrack reaper timerfd + SIGINT/SIGTERM signalfd), and blocks running
 * ingest -> parse -> 5-tuple -> conntrack/Maglev lookup -> GRE encap ->
 * forward until a shutdown signal arrives. Returns 0 on clean shutdown, -1
 * on a fatal setup error (message already printed to stderr). */
int datapath_run(struct datapath *dp);

#endif /* L4MLB_FORWARD_DATAPATH_H */
