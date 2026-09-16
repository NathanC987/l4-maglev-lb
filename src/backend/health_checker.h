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

/* M1: no-op stub. v1 backends are static - added once at startup and never
 * probed, so there is nothing to start. The real probing loop (its own
 * thread + epoll instance, non-blocking TCP-connect checks with rise/fall
 * counters calling backend_manager_set_health()) lands in milestone M2
 * behind this same interface, with no datapath changes required. Returns 0. */
int health_checker_start(struct health_checker *hc);
void health_checker_stop(struct health_checker *hc);

#endif /* L4MLB_BACKEND_HEALTH_CHECKER_H */
