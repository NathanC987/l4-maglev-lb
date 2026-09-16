#include "table_generator.h"

#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

struct table_generator {
    struct backend_manager *bm;
    uint32_t m;
    _Atomic(struct routing_snapshot *) active;
    uint64_t generation;
    _Atomic uint64_t last_regen_us;
};

struct table_generator *table_generator_create(struct backend_manager *bm, uint32_t m) {
    struct table_generator *tg = calloc(1, sizeof(*tg));
    if (tg == NULL) {
        return NULL;
    }
    tg->bm = bm;
    tg->m = m;
    atomic_init(&tg->active, (struct routing_snapshot *)NULL);
    atomic_init(&tg->last_regen_us, (uint64_t)0);
    return tg;
}

void table_generator_destroy(struct table_generator *tg) {
    if (tg == NULL) {
        return;
    }
    routing_snapshot_free(atomic_load(&tg->active));
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

    uint64_t start = now_us();
    struct routing_snapshot *new_snap =
        routing_snapshot_create(views, n, tg->m, tg->generation + 1);
    if (new_snap == NULL) {
        return -1;
    }
    uint64_t elapsed = now_us() - start;

    tg->generation++;
    struct routing_snapshot *old =
        atomic_exchange_explicit(&tg->active, new_snap, memory_order_release);
    atomic_store_explicit(&tg->last_regen_us, elapsed, memory_order_relaxed);
    routing_snapshot_free(old);
    return 0;
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
