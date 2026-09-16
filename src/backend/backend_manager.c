#include "backend_manager.h"

#include <pthread.h>
#include <stdlib.h>
#include <string.h>

struct backend_manager {
    struct backend backends[BACKEND_MAX];
    bool slot_used[BACKEND_MAX];
    uint32_t next_id;
    pthread_mutex_t mu;
    backend_change_cb cb;
    void *cb_ctx;
};

struct backend_manager *backend_manager_create(void) {
    struct backend_manager *bm = calloc(1, sizeof(*bm));
    if (bm == NULL) {
        return NULL;
    }
    pthread_mutex_init(&bm->mu, NULL);
    return bm;
}

void backend_manager_destroy(struct backend_manager *bm) {
    if (bm == NULL) {
        return;
    }
    pthread_mutex_destroy(&bm->mu);
    free(bm);
}

static void notify(struct backend_manager *bm) {
    if (bm->cb != NULL) {
        bm->cb(bm->cb_ctx);
    }
}

static struct backend *find_slot(struct backend_manager *bm, uint32_t id) {
    for (size_t i = 0; i < BACKEND_MAX; i++) {
        if (bm->slot_used[i] && bm->backends[i].id == id) {
            return &bm->backends[i];
        }
    }
    return NULL;
}

uint32_t backend_manager_add(struct backend_manager *bm, uint32_t addr, uint16_t port) {
    uint32_t id = UINT32_MAX;

    pthread_mutex_lock(&bm->mu);
    for (size_t i = 0; i < BACKEND_MAX; i++) {
        if (!bm->slot_used[i]) {
            bm->slot_used[i] = true;
            id = bm->next_id++;

            struct backend *b = &bm->backends[i];
            memset(b, 0, sizeof(*b));
            b->id = id;
            b->addr = addr;
            b->port = port;
            atomic_init(&b->mac_resolved, false);
            atomic_init(&b->healthy, true);
            b->admin_state = BACKEND_ENABLED;
            break;
        }
    }
    pthread_mutex_unlock(&bm->mu);

    if (id != UINT32_MAX) {
        notify(bm);
    }
    return id;
}

int backend_manager_remove(struct backend_manager *bm, uint32_t id) {
    int found = 0;

    pthread_mutex_lock(&bm->mu);
    for (size_t i = 0; i < BACKEND_MAX; i++) {
        if (bm->slot_used[i] && bm->backends[i].id == id) {
            bm->slot_used[i] = false;
            found = 1;
            break;
        }
    }
    pthread_mutex_unlock(&bm->mu);

    if (found) {
        notify(bm);
    }
    return found ? 0 : -1;
}

void backend_manager_set_health(struct backend_manager *bm, uint32_t id, bool healthy) {
    bool changed = false;

    pthread_mutex_lock(&bm->mu);
    struct backend *b = find_slot(bm, id);
    if (b != NULL && atomic_load(&b->healthy) != healthy) {
        atomic_store(&b->healthy, healthy);
        changed = true;
    }
    pthread_mutex_unlock(&bm->mu);

    if (changed) {
        notify(bm);
    }
}

void backend_manager_set_mac(struct backend_manager *bm, uint32_t id,
                              const uint8_t mac[ETH_ADDR_LEN]) {
    bool changed = false;

    pthread_mutex_lock(&bm->mu);
    struct backend *b = find_slot(bm, id);
    if (b != NULL) {
        memcpy(b->mac, mac, ETH_ADDR_LEN);
        atomic_store(&b->mac_resolved, true);
        changed = true;
    }
    pthread_mutex_unlock(&bm->mu);

    if (changed) {
        notify(bm);
    }
}

void backend_manager_subscribe(struct backend_manager *bm, backend_change_cb cb, void *ctx) {
    pthread_mutex_lock(&bm->mu);
    bm->cb = cb;
    bm->cb_ctx = ctx;
    pthread_mutex_unlock(&bm->mu);
}

size_t backend_manager_snapshot_eligible(struct backend_manager *bm, struct backend *out,
                                          size_t out_cap) {
    size_t n = 0;
    pthread_mutex_lock(&bm->mu);
    for (size_t i = 0; i < BACKEND_MAX && n < out_cap; i++) {
        if (!bm->slot_used[i]) {
            continue;
        }
        struct backend *b = &bm->backends[i];
        if (b->admin_state == BACKEND_ENABLED && atomic_load(&b->healthy) &&
            atomic_load(&b->mac_resolved)) {
            out[n++] = *b;
        }
    }
    pthread_mutex_unlock(&bm->mu);
    return n;
}

size_t backend_manager_snapshot_all(struct backend_manager *bm, struct backend *out,
                                     size_t out_cap) {
    size_t n = 0;
    pthread_mutex_lock(&bm->mu);
    for (size_t i = 0; i < BACKEND_MAX && n < out_cap; i++) {
        if (bm->slot_used[i]) {
            out[n++] = bm->backends[i];
        }
    }
    pthread_mutex_unlock(&bm->mu);
    return n;
}
