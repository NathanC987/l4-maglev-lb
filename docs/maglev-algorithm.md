# The Maglev algorithm

This is Google's Maglev consistent-hashing algorithm ([paper](https://research.google/pubs/maglev-a-fast-and-reliable-software-network-load-balancer/)),
implemented in `src/maglev/maglev_table.c` and `src/maglev/hash.c`.

## The idea

Build a large lookup table (size `M`, default 65537) once per backend-set change.
Each backend gets a *permutation* - a specific order in which it would prefer to
claim table slots, deterministic from its own identity. Backends take turns, round
robin, claiming their next-preferred still-empty slot, until the table is full. To
route a packet, hash its 5-tuple down to a table index and read off the backend id
sitting there.

The property that makes this worth the trouble: when the backend set changes
slightly (one is added, removed, or its health flips), most of the table's slots keep
pointing at the same backend they did before. Existing flows barely notice.

## Table size

`M` must be **prime**. Each backend's permutation visits slots `(offset + j*skip) mod
M` for `j = 0, 1, 2, ...`; that sequence only cycles through *every* slot exactly once
(rather than getting stuck in some subset of them) if `gcd(skip, M) == 1` for every
possible `skip` value - which primality of `M` guarantees regardless of what `skip`
turns out to be. `maglev_is_prime()` checks this at startup and refuses to start
otherwise. The default, 65537 (`2^16 + 1`), comfortably covers hundreds of backends.

## Per-backend permutation

Each backend's `offset` and `skip` are derived from its own identity string
(`"ip:port"`, e.g. `10.99.0.11:9000`) via two independent hashes:

```
offset = h1(name) mod M
skip   = h2(name) mod (M - 1) + 1
```

`h1` and `h2` are both [xxHash](https://github.com/Cyan4973/xxHash) (`XXH64`), keyed
with two different fixed seed constants (`src/maglev/hash.c`) - "independent" here
just means different seeds on the same fast algorithm, not two different algorithms.
This is a deliberate, scoped choice: xxHash is not cryptographically
collision-resistant, which is fine for a controlled dev/benchmark environment but
would be worth revisiting (e.g. a per-process random seed) if this ever routed
untrusted, adversarial traffic.

## Filling the table

Backends are sorted by id first, giving a canonical, caller-order-independent
iteration sequence (see below), then filled round-robin:

```
next[i] = 0 for every backend i
repeat, cycling through backends in order:
    slot = permutation(i, next[i])
    while slot is already taken:
        next[i]++
        slot = permutation(i, next[i])
    table[slot] = backend i
    next[i]++
until the table is full
```

Each backend's `next[i]` cursor persists across rounds rather than resetting, so the
whole fill is `O(M)` total, not `O(M * N)`.

## Why the sort-by-id step matters

A backend's own permutation only depends on its identity string, not its position in
the input array - but the *round-robin visitation order* does depend on array order,
and that order decides who wins when two backends' permutations collide on the same
slot in the same round. Without a canonical order, the same backend set could produce
a *different* finished table depending on what order the caller happened to list
backends in. Sorting by id first fixes that: the same backend set always produces the
exact same table, which matters in production if multiple independent load-balancer
instances need to agree on where a flow goes. `test_deterministic_and_order_independent`
in `test/unit/test_maglev_table.c` is the regression test for this - it's also how
this bug was originally found.

## Lookup

```
bucket = flow_hash(5-tuple) mod M
backend = table[bucket]
```

`flow_hash()` (`src/maglev/hash.c`) is a third, separately-seeded `XXH64` over the
`struct flow_key`. It's also reused as the conntrack hash-table bucket index
(`src/conntrack/conntrack.c`), and will double as the flow-to-worker-thread key if a
future milestone shards the datapath across cores.

## What's *not* implemented

Backend **weights** (unequal traffic shares) aren't supported - every eligible
backend gets one slot per round, so the table converges to an even split. Adding
weights would mean giving a backend multiple turns per round proportional to its
weight; the plumbing (`backend_view`, the round-robin loop) would support it without
much restructuring, but no CLI/config surface for it exists yet.
