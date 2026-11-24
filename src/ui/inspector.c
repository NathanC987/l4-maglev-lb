#include "inspector.h"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "../backend/backend_manager.h"
#include "../maglev/hash.h"
#include "../net/ipv4.h"

struct row {
    struct flow_key key;
    uint64_t last_seen_ns;
    uint32_t slot;
};

struct backend_info {
    uint32_t id;
    uint32_t addr;
    uint16_t port;
    uint32_t slots; /* Maglev slots it owns; 0 for a draining backend */
};

/* One complete copy. The datapath fills one while the HTTP thread may be
 * reading the other, and they swap under the mutex. */
struct capture {
    bool valid;
    uint64_t taken_ns;
    uint64_t generation;
    uint32_t m;
    uint8_t *table; /* m entries: backend id + 1, or 0 for an empty slot */
    size_t table_cap;
    size_t n_backends;
    struct backend_info backends[BACKEND_MAX];
    uint64_t scan_us;
    uint64_t active;
    /* Indexed by backend id. Ids at or above BACKEND_MAX cannot happen today
     * (backend_manager hands out 0..BACKEND_MAX-1) and are only counted. */
    uint64_t flows[BACKEND_MAX];
    uint64_t flows_other;
    uint8_t n_rows[BACKEND_MAX];
    struct row rows[BACKEND_MAX][INSPECTOR_ROWS_PER_BACKEND];
};

struct inspector {
    _Atomic uint64_t watch_until_ns;
    uint64_t last_capture_ns; /* datapath thread only */
    struct capture *staging;  /* datapath thread only */
    pthread_mutex_t mu;       /* guards published */
    struct capture *published;
};

struct inspector *inspector_create(void) {
    struct inspector *in = calloc(1, sizeof(*in));
    if (in == NULL) {
        return NULL;
    }
    in->staging = calloc(1, sizeof(*in->staging));
    in->published = calloc(1, sizeof(*in->published));
    if (in->staging == NULL || in->published == NULL || pthread_mutex_init(&in->mu, NULL) != 0) {
        free(in->staging);
        free(in->published);
        free(in);
        return NULL;
    }
    atomic_init(&in->watch_until_ns, 0);
    return in;
}

void inspector_destroy(struct inspector *in) {
    if (in == NULL) {
        return;
    }
    pthread_mutex_destroy(&in->mu);
    free(in->staging->table);
    free(in->published->table);
    free(in->staging);
    free(in->published);
    free(in);
}

void inspector_want(struct inspector *in, uint64_t now_ns) {
    atomic_store_explicit(&in->watch_until_ns, now_ns + (uint64_t)INSPECTOR_WATCH_MS * 1000000ULL,
                          memory_order_relaxed);
}

/* --- capture (datapath thread) --- */

static int copy_table(struct capture *c, const struct routing_snapshot *snap) {
    const struct maglev_table *t = snap->table;
    if (c->table_cap < t->m) {
        uint8_t *grown = realloc(c->table, t->m);
        if (grown == NULL) {
            return -1;
        }
        c->table = grown;
        c->table_cap = t->m;
    }
    c->m = t->m;
    c->generation = snap->generation;

    uint32_t slots[BACKEND_MAX] = {0};
    for (uint32_t i = 0; i < t->m; i++) {
        int32_t id = t->lookup[i];
        if (id >= 0 && (uint32_t)id < BACKEND_MAX) {
            c->table[i] = (uint8_t)(id + 1);
            slots[id]++;
        } else {
            c->table[i] = 0;
        }
    }
    c->n_backends = 0;
    for (size_t i = 0; i < snap->n_backends && c->n_backends < BACKEND_MAX; i++) {
        const struct backend_view *b = &snap->backends[i];
        c->backends[c->n_backends++] = (struct backend_info){
            .id = b->id,
            .addr = b->addr,
            .port = b->port,
            .slots = b->id < BACKEND_MAX ? slots[b->id] : 0,
        };
    }
    return 0;
}

struct scan_ctx {
    struct capture *c;
    uint32_t m;
};

static void visit_flow(const struct flow_key *key, int32_t backend_id, uint64_t last_seen_ns,
                       void *arg) {
    struct scan_ctx *sc = arg;
    struct capture *c = sc->c;
    c->active++;
    if (backend_id < 0 || (uint32_t)backend_id >= BACKEND_MAX) {
        c->flows_other++;
        return;
    }
    c->flows[backend_id]++;

    /* Keep the INSPECTOR_ROWS_PER_BACKEND most recently seen: replace the
     * stalest kept row when this one is newer. */
    uint8_t n = c->n_rows[backend_id];
    struct row *rows = c->rows[backend_id];
    size_t at;
    if (n < INSPECTOR_ROWS_PER_BACKEND) {
        at = n;
        c->n_rows[backend_id] = (uint8_t)(n + 1);
    } else {
        at = 0;
        for (size_t i = 1; i < n; i++) {
            if (rows[i].last_seen_ns < rows[at].last_seen_ns) {
                at = i;
            }
        }
        if (rows[at].last_seen_ns >= last_seen_ns) {
            return;
        }
    }
    rows[at].key = *key;
    rows[at].last_seen_ns = last_seen_ns;
    rows[at].slot = sc->m > 0 ? (uint32_t)(flow_hash(key) % sc->m) : 0;
}

static uint64_t mono_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

void inspector_on_tick(struct inspector *in, const struct routing_snapshot *snap,
                       const struct conntrack_table *ct, uint64_t now_ns) {
    if (now_ns > atomic_load_explicit(&in->watch_until_ns, memory_order_relaxed)) {
        return;
    }
    if (in->last_capture_ns != 0 &&
        now_ns - in->last_capture_ns < (uint64_t)INSPECTOR_CAPTURE_MS * 1000000ULL) {
        return;
    }
    in->last_capture_ns = now_ns;

    struct capture *c = in->staging;
    /* The staging copy is the one published two captures ago. Its table is
     * still right if the generation has not moved since. */
    if (snap == NULL) {
        c->m = 0;
        c->n_backends = 0;
        c->generation = 0;
    } else if (!c->valid || c->generation != snap->generation || c->m != snap->table->m) {
        if (copy_table(c, snap) != 0) {
            return;
        }
    }

    c->active = 0;
    c->flows_other = 0;
    memset(c->flows, 0, sizeof(c->flows));
    memset(c->n_rows, 0, sizeof(c->n_rows));
    struct scan_ctx sc = {.c = c, .m = c->m};
    uint64_t t0 = mono_ns();
    conntrack_for_each(ct, visit_flow, &sc);
    c->scan_us = (mono_ns() - t0) / 1000u;
    c->taken_ns = now_ns;
    c->valid = true;

    /* Never block the datapath on a reader: if one is mid-render, this copy
     * is simply not published and the next tick makes another. */
    if (pthread_mutex_trylock(&in->mu) == 0) {
        in->staging = in->published;
        in->published = c;
        pthread_mutex_unlock(&in->mu);
    }
}

/* --- rendering (any thread) --- */

struct jbuf {
    char *data;
    size_t cap;
    size_t len;
};

static void jb_appendf(struct jbuf *b, const char *fmt, ...) {
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

static void jb_base64(struct jbuf *b, const uint8_t *src, size_t n) {
    static const char alphabet[] =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    for (size_t i = 0; i < n && b->len + 4 < b->cap; i += 3) {
        uint32_t v = (uint32_t)src[i] << 16;
        if (i + 1 < n) {
            v |= (uint32_t)src[i + 1] << 8;
        }
        if (i + 2 < n) {
            v |= src[i + 2];
        }
        b->data[b->len++] = alphabet[(v >> 18) & 63u];
        b->data[b->len++] = alphabet[(v >> 12) & 63u];
        b->data[b->len++] = i + 1 < n ? alphabet[(v >> 6) & 63u] : '=';
        b->data[b->len++] = i + 2 < n ? alphabet[v & 63u] : '=';
    }
}

static const char *proto_name(uint8_t proto) {
    return proto == IPPROTO_TCP ? "tcp" : proto == IPPROTO_UDP ? "udp" : "other";
}

static void render(const struct capture *c, bool with_table, uint64_t now_ns, struct jbuf *b) {
    char addr[16];
    uint64_t age_ms = now_ns > c->taken_ns ? (now_ns - c->taken_ns) / 1000000u : 0;
    jb_appendf(b, "{\"generation\":%llu,\"ageMs\":%llu,\"m\":%u,\"backends\":[",
               (unsigned long long)c->generation, (unsigned long long)age_ms, c->m);
    for (size_t i = 0; i < c->n_backends; i++) {
        const struct backend_info *bi = &c->backends[i];
        ipv4_format(bi->addr, addr, sizeof(addr));
        jb_appendf(b, "%s{\"id\":%u,\"addr\":\"%s\",\"port\":%u,\"slots\":%u}", i ? "," : "",
                   bi->id, addr, bi->port, bi->slots);
    }
    jb_appendf(b, "]");
    if (with_table) {
        jb_appendf(b, ",\"table\":\"");
        jb_base64(b, c->table, c->m);
        jb_appendf(b, "\"");
    }
    jb_appendf(b, ",\"conntrack\":{\"active\":%llu,\"scanUs\":%llu,\"rowsPerBackend\":%u,\"flows\":[",
               (unsigned long long)c->active, (unsigned long long)c->scan_us,
               INSPECTOR_ROWS_PER_BACKEND);
    bool first = true;
    for (uint32_t id = 0; id < BACKEND_MAX; id++) {
        if (c->flows[id] > 0) {
            jb_appendf(b, "%s{\"id\":%u,\"n\":%llu}", first ? "" : ",", id,
                       (unsigned long long)c->flows[id]);
            first = false;
        }
    }
    jb_appendf(b, "],\"rows\":[");
    first = true;
    for (uint32_t id = 0; id < BACKEND_MAX; id++) {
        for (size_t r = 0; r < c->n_rows[id]; r++) {
            const struct row *row = &c->rows[id][r];
            ipv4_format(row->key.src_ip, addr, sizeof(addr));
            uint64_t idle_ms =
                c->taken_ns > row->last_seen_ns ? (c->taken_ns - row->last_seen_ns) / 1000000u : 0;
            jb_appendf(b,
                       "%s{\"src\":\"%s\",\"sport\":%u,\"dport\":%u,\"proto\":\"%s\",\"id\":%u,"
                       "\"slot\":%u,\"idleMs\":%llu}",
                       first ? "" : ",", addr, ntohs(row->key.src_port),
                       ntohs(row->key.dst_port), proto_name(row->key.proto), id, row->slot,
                       (unsigned long long)idle_ms);
            first = false;
        }
    }
    jb_appendf(b, "]}}");
}

size_t inspector_render_json(struct inspector *in, bool have_generation, uint64_t known_generation,
                             uint64_t now_ns, char **out) {
    *out = NULL;
    pthread_mutex_lock(&in->mu);
    const struct capture *c = in->published;
    if (!c->valid) {
        pthread_mutex_unlock(&in->mu);
        static const char pending[] = "{\"pending\":true}";
        *out = strdup(pending);
        return *out ? sizeof(pending) - 1 : 0;
    }
    bool with_table = c->m > 0 && !(have_generation && known_generation == c->generation);
    size_t n_rows = 0;
    for (uint32_t id = 0; id < BACKEND_MAX; id++) {
        n_rows += c->n_rows[id];
    }
    /* Generous upper bounds per item, so nothing is ever cut short. */
    size_t cap = 512 + (with_table ? (c->m + 2) / 3 * 4 : 0) + c->n_backends * 96 +
                 BACKEND_MAX * 40 + n_rows * 160;
    struct jbuf b = {.data = malloc(cap), .cap = cap, .len = 0};
    if (b.data != NULL) {
        render(c, with_table, now_ns, &b);
    }
    pthread_mutex_unlock(&in->mu);
    *out = b.data;
    return b.data ? b.len : 0;
}
