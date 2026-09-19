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
 * with mac_resolved=false, healthy=true, admin_state=BACKEND_ENABLED - a
 * newly added backend is assumed healthy until the health checker (see
 * health_checker.h) says otherwise, rather than waiting to "prove" itself
 * healthy first; this keeps a freshly started LB routing traffic
 * immediately instead of dropping everything for the first probe round.
 * Returns the assigned id, or UINT32_MAX if the backend table is full.
 * Triggers the subscribed callback (if any) after the change. */
uint32_t backend_manager_add(struct backend_manager *bm, uint32_t addr, uint16_t port);

int backend_manager_remove(struct backend_manager *bm, uint32_t id);

/* Sets backend id's health. Returns true if this actually changed its
 * health (so callers - the health checker - can log a transition only when
 * one genuinely happened, not on every probe round once a backend is
 * already in the target state), false if id was unknown or already at
 * `healthy`. Triggers the subscribed callback only when something changed. */
bool backend_manager_set_health(struct backend_manager *bm, uint32_t id, bool healthy);
void backend_manager_set_mac(struct backend_manager *bm, uint32_t id,
                              const uint8_t mac[ETH_ADDR_LEN]);

/* Sets backend id's admin_state (BACKEND_ENABLED / BACKEND_DRAINING /
 * BACKEND_DISABLED) - an operator-driven state, independent of `healthy`
 * (which only the health checker ever writes). DRAINING keeps the backend
 * in backend_manager_snapshot_routable()'s output (so flows already pinned
 * to it via conntrack keep resolving) while removing it from
 * backend_manager_snapshot_eligible()'s output (so it receives no new
 * Maglev table slots). DISABLED removes it from both, same as unhealthy.
 * Returns true if this actually changed the state, false if id was unknown
 * or already at `state`. Triggers the subscribed callback only when
 * something changed. */
bool backend_manager_set_admin_state(struct backend_manager *bm, uint32_t id,
                                      enum backend_admin_state state);

/* Registers the (single, for v1) subscriber invoked after any add / remove /
 * health / mac change. Invoked synchronously, after the internal lock has
 * been released (never call this while holding a lock that could deadlock
 * against a reentrant backend_manager_* call from within the callback). */
void backend_manager_subscribe(struct backend_manager *bm, backend_change_cb cb, void *ctx);

/* Copies up to out_cap backends eligible for NEW flow assignment
 * (admin_state == BACKEND_ENABLED && healthy && mac_resolved) into out.
 * This is the candidate set table_generator passes into Maglev's populate
 * step - a DRAINING backend never appears here, so it receives zero new
 * table slots. Returns the count copied. */
size_t backend_manager_snapshot_eligible(struct backend_manager *bm, struct backend *out,
                                          size_t out_cap);

/* Copies up to out_cap backends that should remain resolvable for flows
 * ALREADY pinned to them via conntrack (admin_state != BACKEND_DISABLED &&
 * healthy && mac_resolved) into out - this is a superset of
 * backend_manager_snapshot_eligible()'s output: it also includes
 * BACKEND_DRAINING backends, which get no new slots but must stay
 * resolvable so their existing flows aren't disrupted by the mere act of
 * draining. Returns the count copied. */
size_t backend_manager_snapshot_routable(struct backend_manager *bm, struct backend *out,
                                          size_t out_cap);

/* Copies ALL backends regardless of eligibility (e.g. for a startup ARP
 * pass or a future TUI) into out. Returns the count copied. */
size_t backend_manager_snapshot_all(struct backend_manager *bm, struct backend *out,
                                     size_t out_cap);

#endif /* L4MLB_BACKEND_BACKEND_MANAGER_H */
