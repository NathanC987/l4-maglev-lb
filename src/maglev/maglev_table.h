#ifndef L4MLB_MAGLEV_TABLE_H
#define L4MLB_MAGLEV_TABLE_H

#include <stddef.h>
#include <stdint.h>

#include "../net/eth.h"
#include "../net/flow.h"

#define MAGLEV_DEFAULT_M 65537u /* prime (2^16 + 1); comfortably covers hundreds of backends */

/* A minimal, self-contained view of a backend, independent of the
 * backend_manager module, so this module stays unit-testable on its own. */
struct backend_view {
    uint32_t id;
    uint32_t addr;             /* opaque network-byte-order IPv4 - the backend's real IP (GRE remote) */
    uint16_t port;             /* host byte order; service/health-check port, not used in forwarding */
    uint8_t mac[ETH_ADDR_LEN]; /* resolved via ARP before a backend becomes eligible */
};

struct maglev_table {
    uint32_t m;
    int32_t *lookup; /* size m; each slot holds a backend_view.id, or -1 if m==0 backends */
};

/* A backend set paired with the exact lookup table generated from it. These
 * two must always be swapped into the datapath together (never mismatched),
 * since table bucket values are only meaningful relative to this specific
 * backends array. */
struct routing_snapshot {
    struct maglev_table *table;
    struct backend_view *backends; /* owned copy, size n_backends */
    size_t n_backends;
    uint64_t generation;
};

/* True iff n is prime. M must be prime so that gcd(skip, M) == 1 for every
 * possible skip in [1, M-1], which is what makes each backend's permutation
 * a full cycle over Z_M (the property the whole algorithm depends on). */
int maglev_is_prime(uint32_t n);

/* Generates a fresh Maglev lookup table of size m for the given backend set,
 * per Google's Maglev paper: offset = h1(name) % m, skip = h2(name) % (m-1)
 * + 1, then the round-robin populate algorithm. backends may be NULL/n==0,
 * in which case every slot is left as -1 (no backend available). Returns
 * NULL on allocation failure. */
struct maglev_table *maglev_table_generate(const struct backend_view *backends, size_t n,
                                            uint32_t m);
void maglev_table_free(struct maglev_table *t);

/* Looks up the backend id for a flow: hashes the 5-tuple to a bucket
 * (bucket = flow_hash(key) % m) and returns table->lookup[bucket], which is
 * -1 if no backend was available when the table was built. */
int32_t maglev_table_lookup(const struct maglev_table *t, const struct flow_key *key);

/* Builds a routing_snapshot: generates the table from `candidates` (the
 * backends allowed to receive NEW slots - see maglev_table_generate()), then
 * takes an owned copy of the (usually larger) `routable` set as the
 * snapshot's resolvable backend array, so a flow already pinned via
 * conntrack to a backend that's routable-but-not-a-candidate (draining)
 * still resolves via routing_snapshot_find_backend(). `candidates` must be a
 * subset of `routable` by id - table slot values are backend ids, not array
 * indices, so this works without any change to maglev_table_generate()
 * itself. Both input arrays may be freed/reused by the caller afterward.
 * Returns NULL on allocation failure. */
struct routing_snapshot *routing_snapshot_create(const struct backend_view *routable,
                                                   size_t n_routable,
                                                   const struct backend_view *candidates,
                                                   size_t n_candidates, uint32_t m,
                                                   uint64_t generation);
void routing_snapshot_free(struct routing_snapshot *snap);

/* Linear scan for the backend_view matching backend_id within snap. n_backends
 * is small (tens of backends), so this is fine on the per-packet hot path. */
const struct backend_view *routing_snapshot_find_backend(const struct routing_snapshot *snap,
                                                           int32_t backend_id);

#endif /* L4MLB_MAGLEV_TABLE_H */
