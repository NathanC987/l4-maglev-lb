# l4-maglev-lb

A software-defined L4 load balancer implementing Google's Maglev consistent-hashing
algorithm, written from scratch in C. It sits between clients and backend servers,
picks a backend per flow using a Maglev lookup table, and forwards packets to it via
Direct Server Return (DSR): the backend replies straight to the client, bypassing the
load balancer entirely on the return path.

See [`docs/architecture.md`](docs/architecture.md) for how it's put together,
[`docs/maglev-algorithm.md`](docs/maglev-algorithm.md) for the consistent-hashing
algorithm, [`docs/testing-topology.md`](docs/testing-topology.md) for the netns/veth
test environment, and [`docs/roadmap.md`](docs/roadmap.md) for what's built and what's
next.

## Status

- **M1** (thin end-to-end datapath) and **M2** (active health checking + live table
  regeneration) are done and verified. See the roadmap doc for details.

## Requirements

- Linux, a C11 compiler, CMake ≥ 3.20
- `libxxhash` (dev package) and `pkg-config`
- `pthread` (part of glibc on modern systems)
- For the test topology: `iproute2`, `nftables`, `python3`, `ethtool`
- Root, for anything that touches raw sockets or network namespaces

## Building

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Debug
cmake --build build -j"$(nproc)"
ctest --test-dir build
```

Other build types: `Release`, `Asan` (ASan+UBSan, the primary memory-safety check
since valgrind isn't assumed to be installed), and `Tsan` (ThreadSanitizer, for the
health-checker/datapath concurrency).

## Running against the test topology

The load balancer needs raw sockets and network namespaces, so this all needs root.

```sh
sudo scripts/setup-netns.sh          # client + lb + backend1 + backend2 on one bridge
sudo scripts/run-lb.sh               # runs maglev-lb inside the lb namespace
sudo scripts/teardown-netns.sh       # tear it back down
```

Or run the full automated checks, which bring the topology up, run the load balancer,
verify behavior, and tear down afterward:

```sh
sudo scripts/run-integration-tests.sh    # TCP stickiness, UDP DSR, LB never sees replies
sudo scripts/run-flow-regen-test.sh      # a flow survives a table regen after a backend dies
```

Pass a specific binary as the first argument to either, e.g.
`sudo scripts/run-integration-tests.sh build-tsan/maglev-lb`, to run the same checks
against the Asan or Tsan build.

## Project layout

```
src/
  net/        Ethernet/IPv4/TCP/UDP/GRE parsing and building, raw sockets, ARP
  maglev/     Consistent-hash table generation and lookup
  conntrack/  Per-flow state so a flow keeps going to the same backend
  backend/    Backend set, health checking, table (re)generation
  stats/      Atomic counters, pull-based (for a future TUI/exporter)
  forward/    The datapath event loop tying the above together
  config/     CLI parsing
  main.c
test/unit/         CTest binaries for the modules above
test/integration/  Python test clients driven from scripts/run-*-test.sh
scripts/           Netns/veth topology setup and orchestration scripts
docs/              Architecture, algorithm, topology, and roadmap notes
```
