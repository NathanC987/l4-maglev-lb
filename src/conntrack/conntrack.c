#include "conntrack.h"

#include <netinet/in.h>
#include <stdlib.h>
#include <string.h>

#include "../maglev/hash.h"

struct conntrack_entry {
    struct flow_key key;
    int32_t backend_id;
    uint64_t last_seen_ns;
    struct conntrack_entry *next; /* bucket chain link, or free-list link when unused */
};

struct conntrack_table {
    struct conntrack_entry **buckets;
    size_t bucket_count; /* power of two */
    struct conntrack_entry *slab;
    struct conntrack_entry *free_list;
    size_t sweep_cursor;
    size_t active;
};

static size_t next_pow2(size_t x) {
    size_t p = 1;
    while (p < x) {
        p <<= 1;
    }
    return p;
}

struct conntrack_table *conntrack_create(size_t max_flows) {
    struct conntrack_table *ct = calloc(1, sizeof(*ct));
    if (ct == NULL) {
        return NULL;
    }

    size_t desired_buckets = (size_t)((double)max_flows / 0.75) + 1;
    ct->bucket_count = next_pow2(desired_buckets);
    ct->buckets = calloc(ct->bucket_count, sizeof(*ct->buckets));
    ct->slab = max_flows > 0 ? malloc(sizeof(struct conntrack_entry) * max_flows) : NULL;
    if (ct->buckets == NULL || (max_flows > 0 && ct->slab == NULL)) {
        conntrack_destroy(ct);
        return NULL;
    }

    for (size_t i = 0; i < max_flows; i++) {
        ct->slab[i].next = (i + 1 < max_flows) ? &ct->slab[i + 1] : NULL;
    }
    ct->free_list = max_flows > 0 ? &ct->slab[0] : NULL;
    return ct;
}

void conntrack_destroy(struct conntrack_table *ct) {
    if (ct == NULL) {
        return;
    }
    free(ct->buckets);
    free(ct->slab);
    free(ct);
}

int conntrack_lookup(struct conntrack_table *ct, const struct flow_key *key, uint64_t now_ns,
                      int32_t *out_backend_id) {
    size_t idx = (size_t)(flow_hash(key) & (ct->bucket_count - 1));
    for (struct conntrack_entry *e = ct->buckets[idx]; e != NULL; e = e->next) {
        if (memcmp(&e->key, key, sizeof(*key)) == 0) {
            e->last_seen_ns = now_ns;
            *out_backend_id = e->backend_id;
            return 1;
        }
    }
    return 0;
}

int conntrack_insert(struct conntrack_table *ct, const struct flow_key *key, int32_t backend_id,
                      uint64_t now_ns) {
    if (ct->free_list == NULL) {
        return -1;
    }
    struct conntrack_entry *e = ct->free_list;
    ct->free_list = e->next;

    e->key = *key;
    e->backend_id = backend_id;
    e->last_seen_ns = now_ns;

    size_t idx = (size_t)(flow_hash(key) & (ct->bucket_count - 1));
    e->next = ct->buckets[idx];
    ct->buckets[idx] = e;
    ct->active++;
    return 0;
}

size_t conntrack_reap_slice(struct conntrack_table *ct, uint64_t now_ns, uint64_t tcp_timeout_ns,
                             uint64_t udp_timeout_ns, size_t max_buckets_this_tick,
                             conntrack_evict_fn on_evict, void *cb_ctx) {
    size_t reaped = 0;
    size_t buckets_to_scan = max_buckets_this_tick;
    if (buckets_to_scan > ct->bucket_count) {
        buckets_to_scan = ct->bucket_count;
    }

    for (size_t n = 0; n < buckets_to_scan; n++) {
        size_t idx = ct->sweep_cursor;
        ct->sweep_cursor = (ct->sweep_cursor + 1) % ct->bucket_count;

        struct conntrack_entry **link = &ct->buckets[idx];
        while (*link != NULL) {
            struct conntrack_entry *e = *link;
            uint64_t timeout = (e->key.proto == IPPROTO_TCP) ? tcp_timeout_ns : udp_timeout_ns;
            if (now_ns - e->last_seen_ns > timeout) {
                int32_t evicted_backend_id = e->backend_id;
                *link = e->next;
                e->next = ct->free_list;
                ct->free_list = e;
                ct->active--;
                reaped++;
                if (on_evict != NULL) {
                    on_evict(evicted_backend_id, cb_ctx);
                }
            } else {
                link = &e->next;
            }
        }
    }
    return reaped;
}

size_t conntrack_bucket_count(const struct conntrack_table *ct) {
    return ct->bucket_count;
}

size_t conntrack_active_flows(const struct conntrack_table *ct) {
    return ct->active;
}
