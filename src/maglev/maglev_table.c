#include "maglev_table.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "hash.h"

int maglev_is_prime(uint32_t n) {
    if (n < 2) {
        return 0;
    }
    if (n % 2 == 0) {
        return n == 2;
    }
    for (uint32_t d = 3; (uint64_t)d * d <= n; d += 2) {
        if (n % d == 0) {
            return 0;
        }
    }
    return 1;
}

static int backend_view_cmp_by_id(const void *a, const void *b) {
    uint32_t id_a = ((const struct backend_view *)a)->id;
    uint32_t id_b = ((const struct backend_view *)b)->id;
    if (id_a < id_b) {
        return -1;
    }
    if (id_a > id_b) {
        return 1;
    }
    return 0;
}

struct maglev_table *maglev_table_generate(const struct backend_view *backends, size_t n,
                                            uint32_t m) {
    struct maglev_table *t = calloc(1, sizeof(*t));
    if (t == NULL) {
        return NULL;
    }
    t->m = m;
    t->lookup = malloc(sizeof(int32_t) * m);
    if (t->lookup == NULL) {
        free(t);
        return NULL;
    }
    for (uint32_t i = 0; i < m; i++) {
        t->lookup[i] = -1;
    }
    if (n == 0 || backends == NULL) {
        return t;
    }

    /* Each backend's own permutation is order-independent (it's derived only
     * from its identity string), but the round-robin populate loop below
     * resolves collisions by visitation order, so the *finished* table would
     * otherwise depend on the caller's array order. Sorting by id first
     * gives a canonical order, so the same backend set always produces the
     * same table regardless of how the caller enumerated it - matching real
     * Maglev deployments, where independent LB instances must all compute
     * the identical table for a given backend set. */
    struct backend_view *sorted = malloc(sizeof(struct backend_view) * n);
    uint64_t *offset = malloc(sizeof(uint64_t) * n);
    uint64_t *skip = malloc(sizeof(uint64_t) * n);
    uint32_t *next = calloc(n, sizeof(uint32_t));
    if (sorted == NULL || offset == NULL || skip == NULL || next == NULL) {
        free(sorted);
        free(offset);
        free(skip);
        free(next);
        maglev_table_free(t);
        return NULL;
    }
    memcpy(sorted, backends, sizeof(struct backend_view) * n);
    qsort(sorted, n, sizeof(struct backend_view), backend_view_cmp_by_id);

    for (size_t i = 0; i < n; i++) {
        char name[32];
        const uint8_t *ip = (const uint8_t *)&sorted[i].addr;
        int name_len = snprintf(name, sizeof(name), "%u.%u.%u.%u:%u", ip[0], ip[1], ip[2], ip[3],
                                 sorted[i].port);
        offset[i] = maglev_hash_offset(name, (size_t)name_len) % m;
        skip[i] = maglev_hash_skip(name, (size_t)name_len) % (m - 1) + 1;
    }

    size_t filled = 0;
    size_t round_robin = 0;
    while (filled < m) {
        size_t b = round_robin % n;
        uint32_t j = next[b];
        uint32_t c = (uint32_t)((offset[b] + (uint64_t)j * skip[b]) % m);
        while (t->lookup[c] != -1) {
            j++;
            c = (uint32_t)((offset[b] + (uint64_t)j * skip[b]) % m);
        }
        t->lookup[c] = (int32_t)sorted[b].id;
        next[b] = j + 1;
        filled++;
        round_robin++;
    }

    free(sorted);
    free(offset);
    free(skip);
    free(next);
    return t;
}

void maglev_table_free(struct maglev_table *t) {
    if (t == NULL) {
        return;
    }
    free(t->lookup);
    free(t);
}

int32_t maglev_table_lookup(const struct maglev_table *t, const struct flow_key *key) {
    uint32_t bucket = (uint32_t)(flow_hash(key) % t->m);
    return t->lookup[bucket];
}

struct routing_snapshot *routing_snapshot_create(const struct backend_view *backends, size_t n,
                                                   uint32_t m, uint64_t generation) {
    struct maglev_table *table = maglev_table_generate(backends, n, m);
    if (table == NULL) {
        return NULL;
    }

    struct routing_snapshot *snap = malloc(sizeof(*snap));
    if (snap == NULL) {
        maglev_table_free(table);
        return NULL;
    }

    struct backend_view *copy = NULL;
    if (n > 0) {
        copy = malloc(sizeof(*copy) * n);
        if (copy == NULL) {
            maglev_table_free(table);
            free(snap);
            return NULL;
        }
        memcpy(copy, backends, sizeof(*copy) * n);
    }

    snap->table = table;
    snap->backends = copy;
    snap->n_backends = n;
    snap->generation = generation;
    return snap;
}

void routing_snapshot_free(struct routing_snapshot *snap) {
    if (snap == NULL) {
        return;
    }
    maglev_table_free(snap->table);
    free(snap->backends);
    free(snap);
}

const struct backend_view *routing_snapshot_find_backend(const struct routing_snapshot *snap,
                                                           int32_t backend_id) {
    for (size_t i = 0; i < snap->n_backends; i++) {
        if ((int32_t)snap->backends[i].id == backend_id) {
            return &snap->backends[i];
        }
    }
    return NULL;
}
