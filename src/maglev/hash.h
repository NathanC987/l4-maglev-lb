#ifndef L4MLB_MAGLEV_HASH_H
#define L4MLB_MAGLEV_HASH_H

#include <stddef.h>
#include <stdint.h>

#include "../net/flow.h"

/* Two independent hashes of a backend "name" (its stable identity string,
 * e.g. "ip:port"), used to derive that backend's Maglev permutation
 * (offset, skip). Independence comes from distinct fixed seeds on the same
 * underlying xxHash algorithm - sufficient for a controlled dev/benchmark
 * environment; revisit (e.g. a keyed/randomized seed) if this ever becomes
 * adversarial-input-facing. */
uint64_t maglev_hash_offset(const void *data, size_t len);
uint64_t maglev_hash_skip(const void *data, size_t len);

/* Hash of a flow's 5-tuple: picks its Maglev lookup-table bucket, and (in a
 * later multi-threaded milestone) will double as the worker-shard key. */
uint64_t flow_hash(const struct flow_key *key);

#endif /* L4MLB_MAGLEV_HASH_H */
