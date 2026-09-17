#ifndef L4MLB_BACKEND_TABLE_GENERATOR_H
#define L4MLB_BACKEND_TABLE_GENERATOR_H

#include <stdint.h>

#include "../maglev/maglev_table.h"
#include "backend_manager.h"

struct table_generator;

struct table_generator *table_generator_create(struct backend_manager *bm, uint32_t m);
void table_generator_destroy(struct table_generator *tg);

/* Pulls the current eligible backend set from bm, builds a fresh
 * routing_snapshot, and atomically installs it (release ordering) as the
 * active snapshot, freeing the previously active one. Freeing immediately
 * is safe in M1 because the datapath is single-threaded; a later
 * multi-threaded milestone needs real epoch-based reclamation here instead
 * of an immediate free. Returns 0 on success, -1 on allocation failure (the
 * previously active snapshot, if any, is left installed and untouched). */
int table_generator_rebuild_and_install(struct table_generator *tg);

/* Registers tg with bm as its change-subscriber, so future backend_manager
 * mutations automatically trigger a rebuild+install. */
void table_generator_install_as_subscriber(struct table_generator *tg);

/* Acquire-ordered load of the currently active routing_snapshot. Returns
 * NULL only if no rebuild has ever succeeded. */
struct routing_snapshot *table_generator_get_active(struct table_generator *tg);

/* Frees snapshots retired by rebuild_and_install() since the last call.
 * MUST be called only from the datapath thread (the sole reader of
 * table_generator_get_active()), from a point in its own event loop where
 * it holds no other reference to an older snapshot - e.g. once per
 * conntrack-reaper tick, not from within packet handling. See the
 * "retired_mu" comment in table_generator.c for why this is safe despite
 * being a concurrent writer (the health checker thread) retiring
 * snapshots the reader might still be using at the moment they're
 * swapped out. */
void table_generator_reclaim(struct table_generator *tg);

uint64_t table_generator_last_regen_us(struct table_generator *tg);

/* Thread-safe (atomic) read of the current table generation number - how
 * many times rebuild_and_install() has installed a new table. Safe to call
 * from any thread, unlike table_generator_get_active(), which only the
 * single datapath reader thread may call (see the reclaim comment above). */
uint64_t table_generator_generation(struct table_generator *tg);

#endif /* L4MLB_BACKEND_TABLE_GENERATOR_H */
