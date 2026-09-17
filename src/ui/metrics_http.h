#ifndef L4MLB_UI_METRICS_HTTP_H
#define L4MLB_UI_METRICS_HTTP_H

#include <stddef.h>
#include <stdint.h>

#include "../backend/backend_manager.h"
#include "../backend/table_generator.h"
#include "../conntrack/conntrack.h"
#include "../stats/stats_registry.h"

struct metrics_http_config {
    uint16_t port; /* host byte order; 0 means "don't start" (caller's choice, not enforced here) */
    struct backend_manager *bm;
    struct table_generator *tg;
    struct conntrack_table *ct;
    struct lb_stats *stats;
    uint32_t table_size;        /* configured Maglev M, exposed as a gauge for context */
    size_t configured_backends; /* count of --backend entries at startup, exposed as a gauge */
};

struct metrics_http;

struct metrics_http *metrics_http_create(const struct metrics_http_config *cfg);
void metrics_http_destroy(struct metrics_http *m);

/* Spawns a thread that listens on cfg.port and serves any HTTP GET request
 * with a Prometheus text-exposition response, built fresh from
 * stats_registry / backend_manager / table_generator / conntrack on every
 * single request - always live, nothing cached or pushed. All of those are
 * already safe to read from a thread other than the datapath thread (see
 * their own headers), so this needs no additional synchronization of its
 * own. Returns 0 on success, -1 if the listening socket or thread could not
 * be created. */
int metrics_http_start(struct metrics_http *m);

/* Signals the thread to stop and joins it. No-op if never started. */
void metrics_http_stop(struct metrics_http *m);

#endif /* L4MLB_UI_METRICS_HTTP_H */
