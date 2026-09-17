#include "stats_registry.h"

#include <stdlib.h>
#include <string.h>

int stats_registry_init(struct lb_stats *s, size_t max_backends) {
    memset(s, 0, sizeof(*s));
    if (max_backends > 0) {
        s->backends = calloc(max_backends, sizeof(struct per_backend_stats));
        if (s->backends == NULL) {
            return -1;
        }
    }
    s->max_backends = max_backends;
    return 0;
}

void stats_registry_destroy(struct lb_stats *s) {
    if (s == NULL) {
        return;
    }
    free(s->backends);
    s->backends = NULL;
}

void stats_record_backend_packet(struct lb_stats *s, uint32_t backend_id, size_t bytes) {
    if (backend_id >= s->max_backends) {
        return;
    }
    struct per_backend_stats *b = &s->backends[backend_id];
    atomic_fetch_add_explicit(&b->packets, 1, memory_order_relaxed);
    atomic_fetch_add_explicit(&b->bytes, bytes, memory_order_relaxed);
}

void stats_backend_flow_opened(struct lb_stats *s, uint32_t backend_id) {
    if (backend_id >= s->max_backends) {
        return;
    }
    atomic_fetch_add_explicit(&s->backends[backend_id].active_flows, 1, memory_order_relaxed);
}

void stats_backend_flow_closed(struct lb_stats *s, uint32_t backend_id) {
    if (backend_id >= s->max_backends) {
        return;
    }
    atomic_fetch_sub_explicit(&s->backends[backend_id].active_flows, 1, memory_order_relaxed);
}

void stats_backend_health_check_failed(struct lb_stats *s, uint32_t backend_id) {
    if (backend_id >= s->max_backends) {
        return;
    }
    atomic_fetch_add_explicit(&s->backends[backend_id].health_check_failures, 1,
                               memory_order_relaxed);
}

void stats_registry_snapshot(const struct lb_stats *s, struct lb_stats_snapshot *out) {
    out->rx_packets = atomic_load_explicit(&s->rx_packets, memory_order_relaxed);
    out->rx_bytes = atomic_load_explicit(&s->rx_bytes, memory_order_relaxed);
    out->tx_packets = atomic_load_explicit(&s->tx_packets, memory_order_relaxed);
    out->tx_bytes = atomic_load_explicit(&s->tx_bytes, memory_order_relaxed);
    out->drops_parse_error = atomic_load_explicit(&s->drops_parse_error, memory_order_relaxed);
    out->drops_no_backend = atomic_load_explicit(&s->drops_no_backend, memory_order_relaxed);
    out->drops_backend_unhealthy =
        atomic_load_explicit(&s->drops_backend_unhealthy, memory_order_relaxed);
    out->drops_conntrack_full =
        atomic_load_explicit(&s->drops_conntrack_full, memory_order_relaxed);
    out->conntrack_hits = atomic_load_explicit(&s->conntrack_hits, memory_order_relaxed);
    out->conntrack_misses = atomic_load_explicit(&s->conntrack_misses, memory_order_relaxed);
    out->conntrack_evictions = atomic_load_explicit(&s->conntrack_evictions, memory_order_relaxed);
}

void stats_registry_snapshot_backend(const struct lb_stats *s, uint32_t backend_id,
                                      struct per_backend_stats_snapshot *out) {
    if (backend_id >= s->max_backends) {
        memset(out, 0, sizeof(*out));
        return;
    }
    const struct per_backend_stats *b = &s->backends[backend_id];
    out->packets = atomic_load_explicit(&b->packets, memory_order_relaxed);
    out->bytes = atomic_load_explicit(&b->bytes, memory_order_relaxed);
    out->active_flows = atomic_load_explicit(&b->active_flows, memory_order_relaxed);
    out->health_check_failures =
        atomic_load_explicit(&b->health_check_failures, memory_order_relaxed);
}
