#include "table_generator.h"

#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

struct table_generator {
    struct backend_manager *bm;
    uint32_t m;
    _Atomic(struct routing_snapshot *) active;
    _Atomic uint64_t generation; /* atomic so table_generator_generation() can be read cross-thread (TUI) */
    _Atomic uint64_t last_regen_us;

    /* Snapshots swapped out by rebuild_and_install() are never freed by the
     * thread that retired them - as of M2, that's the health checker
     * thread, which runs concurrently with the datapath thread that may
     * still be dereferencing the very pointer it just swapped out. Instead
     * they're queued here (under retired_mu) and only actually freed by
     * table_generator_reclaim(), which the datapath thread calls from its
     * own event loop between packets - a point at which, by construction,
     * that same thread cannot still be holding a reference to an older
     * snapshot. This is a single-reader-specific simplification, not
     * general RCU: it relies on there being exactly one reader thread
     * (still true through M2). A future multi-datapath-thread milestone
     * (M6) will need each reader to reclaim only snapshots retired before
     * its own last read, e.g. via a per-reader epoch counter. */
    pthread_mutex_t retired_mu;
    struct routing_snapshot **retired;
    size_t n_retired;
    size_t retired_cap;
};

struct table_generator *table_generator_create(struct backend_manager *bm, uint32_t m) {
    struct table_generator *tg = calloc(1, sizeof(*tg));
    if (tg == NULL) {
        return NULL;
    }
    tg->bm = bm;
    tg->m = m;
    atomic_init(&tg->active, (struct routing_snapshot *)NULL);
    atomic_init(&tg->generation, (uint64_t)0);
    atomic_init(&tg->last_regen_us, (uint64_t)0);
    pthread_mutex_init(&tg->retired_mu, NULL);
    return tg;
}

void table_generator_destroy(struct table_generator *tg) {
    if (tg == NULL) {
        return;
    }
    routing_snapshot_free(atomic_load(&tg->active));
    for (size_t i = 0; i < tg->n_retired; i++) {
        routing_snapshot_free(tg->retired[i]);
    }
    free(tg->retired);
    pthread_mutex_destroy(&tg->retired_mu);
    free(tg);
}

static uint64_t now_us(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000ULL + (uint64_t)ts.tv_nsec / 1000ULL;
}

int table_generator_rebuild_and_install(struct table_generator *tg) {
    struct backend eligible[BACKEND_MAX];
    size_t n = backend_manager_snapshot_eligible(tg->bm, eligible, BACKEND_MAX);

    struct backend_view views[BACKEND_MAX];
    for (size_t i = 0; i < n; i++) {
        views[i].id = eligible[i].id;
        views[i].addr = eligible[i].addr;
        views[i].port = eligible[i].port;
        memcpy(views[i].mac, eligible[i].mac, ETH_ADDR_LEN);
    }

    uint64_t new_generation = atomic_load_explicit(&tg->generation, memory_order_relaxed) + 1;
    uint64_t start = now_us();
    struct routing_snapshot *new_snap = routing_snapshot_create(views, n, tg->m, new_generation);
    if (new_snap == NULL) {
        return -1;
    }
    uint64_t elapsed = now_us() - start;

    struct routing_snapshot *old =
        atomic_exchange_explicit(&tg->active, new_snap, memory_order_release);
    atomic_store_explicit(&tg->generation, new_generation, memory_order_relaxed);
    atomic_store_explicit(&tg->last_regen_us, elapsed, memory_order_relaxed);
    fprintf(stderr,
            "table_generator: regenerated (generation=%llu, %zu eligible backend(s), %llu us)\n",
            (unsigned long long)new_generation, n, (unsigned long long)elapsed);

    if (old != NULL) {
        pthread_mutex_lock(&tg->retired_mu);
        if (tg->n_retired == tg->retired_cap) {
            size_t new_cap = tg->retired_cap == 0 ? 8 : tg->retired_cap * 2;
            struct routing_snapshot **grown =
                realloc(tg->retired, new_cap * sizeof(*tg->retired));
            if (grown != NULL) {
                tg->retired = grown;
                tg->retired_cap = new_cap;
            }
        }
        if (tg->n_retired < tg->retired_cap) {
            tg->retired[tg->n_retired++] = old;
            old = NULL;
        }
        pthread_mutex_unlock(&tg->retired_mu);
    }
    /* old is non-NULL only if the realloc above failed under pressure;
     * freeing it here would race the reader exactly as this whole scheme
     * exists to prevent, so it is deliberately leaked rather than risking a
     * use-after-free - an allocation failure this small is already a sign
     * of a host under severe memory pressure. */

    return 0;
}

void table_generator_reclaim(struct table_generator *tg) {
    pthread_mutex_lock(&tg->retired_mu);
    struct routing_snapshot **to_free = tg->retired;
    size_t n = tg->n_retired;
    tg->retired = NULL;
    tg->n_retired = 0;
    tg->retired_cap = 0;
    pthread_mutex_unlock(&tg->retired_mu);

    for (size_t i = 0; i < n; i++) {
        routing_snapshot_free(to_free[i]);
    }
    free(to_free);
}

static void on_backend_change(void *ctx) {
    struct table_generator *tg = ctx;
    table_generator_rebuild_and_install(tg);
}

void table_generator_install_as_subscriber(struct table_generator *tg) {
    backend_manager_subscribe(tg->bm, on_backend_change, tg);
}

struct routing_snapshot *table_generator_get_active(struct table_generator *tg) {
    return atomic_load_explicit(&tg->active, memory_order_acquire);
}

uint64_t table_generator_last_regen_us(struct table_generator *tg) {
    return atomic_load_explicit(&tg->last_regen_us, memory_order_relaxed);
}

uint64_t table_generator_generation(struct table_generator *tg) {
    return atomic_load_explicit(&tg->generation, memory_order_relaxed);
}
