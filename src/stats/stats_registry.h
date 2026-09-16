#ifndef L4MLB_STATS_STATS_REGISTRY_H
#define L4MLB_STATS_STATS_REGISTRY_H

#include <stdatomic.h>
#include <stddef.h>
#include <stdint.h>

struct per_backend_stats {
    _Atomic uint64_t packets;
    _Atomic uint64_t bytes;
    _Atomic uint64_t active_flows;
    _Atomic uint64_t health_check_failures;
};

/* Pure atomic-counter registry, pull-model only: the datapath increments
 * fields directly (they're plain public atomics), and readers call
 * stats_registry_snapshot()/_backend() on their own cadence. No push/observer
 * machinery, so a future TUI (M3) and a future Prometheus exporter (M3+) can
 * both read this without any restructuring. */
struct lb_stats {
    _Atomic uint64_t rx_packets, rx_bytes;
    _Atomic uint64_t tx_packets, tx_bytes;
    _Atomic uint64_t drops_parse_error, drops_no_backend, drops_backend_unhealthy;
    _Atomic uint64_t drops_conntrack_full;
    _Atomic uint64_t conntrack_hits, conntrack_misses, conntrack_evictions;
    _Atomic uint64_t maglev_table_regens;
    _Atomic uint64_t maglev_table_regen_last_us;

    /* Indexed directly by backend id. Only correct while ids stay within
     * [0, max_backends) - true for v1's static backend set (ids 0..n-1
     * assigned once at startup). A later milestone with dynamic
     * add/remove churn will need either id reuse or a map-based store
     * here instead of direct indexing. */
    struct per_backend_stats *backends;
    size_t max_backends;
};

int stats_registry_init(struct lb_stats *s, size_t max_backends);
void stats_registry_destroy(struct lb_stats *s);

/* Bounds-checked per-backend packet/byte accounting; no-op if backend_id is
 * out of range. */
void stats_record_backend_packet(struct lb_stats *s, uint32_t backend_id, size_t bytes);

struct per_backend_stats_snapshot {
    uint64_t packets, bytes, active_flows, health_check_failures;
};

struct lb_stats_snapshot {
    uint64_t rx_packets, rx_bytes;
    uint64_t tx_packets, tx_bytes;
    uint64_t drops_parse_error, drops_no_backend, drops_backend_unhealthy;
    uint64_t drops_conntrack_full;
    uint64_t conntrack_hits, conntrack_misses, conntrack_evictions;
    uint64_t maglev_table_regens;
    uint64_t maglev_table_regen_last_us;
};

void stats_registry_snapshot(const struct lb_stats *s, struct lb_stats_snapshot *out);
void stats_registry_snapshot_backend(const struct lb_stats *s, uint32_t backend_id,
                                      struct per_backend_stats_snapshot *out);

#endif /* L4MLB_STATS_STATS_REGISTRY_H */
