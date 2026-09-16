#ifndef L4MLB_BACKEND_HEALTH_CHECKER_H
#define L4MLB_BACKEND_HEALTH_CHECKER_H

#include <stdint.h>

#include "backend_manager.h"

struct health_check_cfg {
    uint32_t interval_ms;
    uint32_t timeout_ms;
    uint32_t rise; /* consecutive successes required to go healthy */
    uint32_t fall; /* consecutive failures required to go unhealthy */
};

struct health_checker;

struct health_checker *health_checker_create(struct backend_manager *bm,
                                              struct health_check_cfg cfg);
void health_checker_destroy(struct health_checker *hc);

/* Spawns a dedicated thread running its own epoll instance: once per
 * cfg.interval_ms it fires a non-blocking TCP connect() at every backend
 * currently known to bm, concurrently, waits up to cfg.timeout_ms for them
 * to resolve, and calls backend_manager_set_health() the moment a backend's
 * consecutive success/failure count first crosses cfg.rise/cfg.fall.
 * Returns 0 on success, -1 if the thread could not be created. */
int health_checker_start(struct health_checker *hc);

/* Signals the thread to stop (waking it even mid-round or mid-interval-wait)
 * and joins it before returning, so it is safe to tear down bm immediately
 * afterward. No-op if the checker was never started. */
void health_checker_stop(struct health_checker *hc);

#endif /* L4MLB_BACKEND_HEALTH_CHECKER_H */
