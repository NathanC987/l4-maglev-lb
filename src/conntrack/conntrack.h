#ifndef L4MLB_CONNTRACK_CONNTRACK_H
#define L4MLB_CONNTRACK_CONNTRACK_H

#include <stddef.h>
#include <stdint.h>

#include "../net/flow.h"

struct conntrack_table;

/* Creates a conntrack table backed by a pre-allocated slab of max_flows
 * entries (no malloc/free on the packet hot path). Returns NULL on
 * allocation failure. */
struct conntrack_table *conntrack_create(size_t max_flows);
void conntrack_destroy(struct conntrack_table *ct);

/* Looks up key. On a hit, refreshes the entry's last-seen time to now_ns
 * (so it stays alive under the reaper) and writes its pinned backend id to
 * *out_backend_id. Returns 1 on hit, 0 on miss. This must be checked before
 * any Maglev table lookup: a flow that's already pinned must keep going to
 * its original backend even if the Maglev table has since been
 * regenerated. */
int conntrack_lookup(struct conntrack_table *ct, const struct flow_key *key, uint64_t now_ns,
                      int32_t *out_backend_id);

/* Pins key to backend_id. Returns 0 on success, -1 if the slab is
 * exhausted (caller should drop the packet and count it, not evict). */
int conntrack_insert(struct conntrack_table *ct, const struct flow_key *key, int32_t backend_id,
                      uint64_t now_ns);

typedef void (*conntrack_evict_fn)(int32_t backend_id, void *ctx);

/* Sweeps up to max_buckets_this_tick buckets (starting from an internal
 * round-robin cursor that persists across calls), removing any entry whose
 * (now_ns - last_seen) exceeds the timeout for its protocol. Bounding the
 * work per call keeps reaping from causing a latency spike; a full sweep is
 * spread across many ticks. Returns the number of entries reaped.
 *
 * on_evict, if non-NULL, is called once per reaped entry with the backend
 * id it was pinned to (e.g. so the caller can decrement a per-backend
 * active-flow counter) - synchronously, from within this call, on the
 * caller's own thread. */
size_t conntrack_reap_slice(struct conntrack_table *ct, uint64_t now_ns, uint64_t tcp_timeout_ns,
                             uint64_t udp_timeout_ns, size_t max_buckets_this_tick,
                             conntrack_evict_fn on_evict, void *cb_ctx);

size_t conntrack_bucket_count(const struct conntrack_table *ct);

/* Thread-safe (atomic) even though lookup/insert/reap_slice are not - safe
 * to call from e.g. the metrics exporter thread while the datapath thread
 * is concurrently inserting/reaping. */
size_t conntrack_active_flows(const struct conntrack_table *ct);

#endif /* L4MLB_CONNTRACK_CONNTRACK_H */
