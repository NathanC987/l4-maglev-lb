#include "hash.h"

#include <xxhash.h>

#define MAGLEV_SEED_OFFSET 0x4d61676c65764f66ULL
#define MAGLEV_SEED_SKIP 0x4d61676c6576536bULL
#define FLOW_HASH_SEED 0x466c6f7748617368ULL

uint64_t maglev_hash_offset(const void *data, size_t len) {
    return XXH64(data, len, MAGLEV_SEED_OFFSET);
}

uint64_t maglev_hash_skip(const void *data, size_t len) {
    return XXH64(data, len, MAGLEV_SEED_SKIP);
}

uint64_t flow_hash(const struct flow_key *key) {
    return XXH64(key, sizeof(*key), FLOW_HASH_SEED);
}
