#ifndef L4MLB_BACKEND_BACKEND_MANAGER_H
#define L4MLB_BACKEND_BACKEND_MANAGER_H

#include <stddef.h>
#include <stdint.h>

#include "backend.h"

#define BACKEND_MAX 64u

struct backend_manager;

typedef void (*backend_change_cb)(void *ctx);

struct backend_manager *backend_manager_create(void);
void backend_manager_destroy(struct backend_manager *bm);

/* Adds a backend (addr network byte order, port host byte order). Starts
 * with mac_resolved=false, healthy=true (v1 runs no health checker - see
 * health_checker.h), admin_state=BACKEND_ENABLED. Returns the assigned id,
 * or UINT32_MAX if the backend table is full. Triggers the subscribed
 * callback (if any) after the change. */
uint32_t backend_manager_add(struct backend_manager *bm, uint32_t addr, uint16_t port);

int backend_manager_remove(struct backend_manager *bm, uint32_t id);
void backend_manager_set_health(struct backend_manager *bm, uint32_t id, bool healthy);
void backend_manager_set_mac(struct backend_manager *bm, uint32_t id,
                              const uint8_t mac[ETH_ADDR_LEN]);

/* Registers the (single, for v1) subscriber invoked after any add / remove /
 * health / mac change. Invoked synchronously, after the internal lock has
 * been released (never call this while holding a lock that could deadlock
 * against a reentrant backend_manager_* call from within the callback). */
void backend_manager_subscribe(struct backend_manager *bm, backend_change_cb cb, void *ctx);

/* Copies up to out_cap backends eligible for routing (admin_state ==
 * BACKEND_ENABLED && healthy && mac_resolved) into out. Returns the count
 * copied. */
size_t backend_manager_snapshot_eligible(struct backend_manager *bm, struct backend *out,
                                          size_t out_cap);

/* Copies ALL backends regardless of eligibility (e.g. for a startup ARP
 * pass or a future TUI) into out. Returns the count copied. */
size_t backend_manager_snapshot_all(struct backend_manager *bm, struct backend *out,
                                     size_t out_cap);

#endif /* L4MLB_BACKEND_BACKEND_MANAGER_H */
