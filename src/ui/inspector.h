#ifndef L4MLB_UI_INSPECTOR_H
#define L4MLB_UI_INSPECTOR_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "../conntrack/conntrack.h"
#include "../maglev/maglev_table.h"

/* A copy of the Maglev lookup table and of the conntrack table, for showing
 * them while the LB runs: GET /tables on the metrics port serves it as JSON,
 * and the live canvas draws it.
 *
 * Only the datapath thread may read either table (see table_generator.h and
 * conntrack.h), so the datapath makes the copy, on its reaper tick, and the
 * HTTP thread reads the copy. It copies only while someone has asked within
 * the last INSPECTOR_WATCH_MS; with nobody watching, each tick costs one
 * atomic load. A copy walks every conntrack bucket (2M of them at the default
 * 1M max flows: a couple of milliseconds) and happens at most once per
 * INSPECTOR_CAPTURE_MS. The Maglev table is re-copied only when its
 * generation changes. */

#define INSPECTOR_WATCH_MS 3000u
#define INSPECTOR_CAPTURE_MS 1000u
/* Conntrack rows kept per backend: the ones seen most recently. Counts per
 * backend are exact; the rows are a sample, since a busy table holds tens of
 * thousands. */
#define INSPECTOR_ROWS_PER_BACKEND 16u

struct inspector;

struct inspector *inspector_create(void);
void inspector_destroy(struct inspector *in);

/* Any thread: someone wants the tables. Starts copying, or keeps it going. */
void inspector_want(struct inspector *in, uint64_t now_ns);

/* Datapath thread only, between packets (now_ns on CLOCK_MONOTONIC). snap may
 * be NULL before the first table build. */
void inspector_on_tick(struct inspector *in, const struct routing_snapshot *snap,
                       const struct conntrack_table *ct, uint64_t now_ns);

/* Any thread: renders the latest copy as JSON into a malloc'd buffer that the
 * caller frees, and returns its length (0 on allocation failure, with *out
 * NULL). Before the first copy the document is {"pending":true}. The table
 * itself is left out when have_generation is set and known_generation equals
 * the copy's generation: the reader already holds that table. */
size_t inspector_render_json(struct inspector *in, bool have_generation, uint64_t known_generation,
                             uint64_t now_ns, char **out);

#endif /* L4MLB_UI_INSPECTOR_H */
