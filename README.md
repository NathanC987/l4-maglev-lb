# l4-maglev-lb

A software-defined L4 load balancer implementing Google's Maglev consistent-hashing
algorithm, written from scratch in C. It sits between clients and backend servers,
picks a backend per flow using a Maglev lookup table, and forwards packets to it via
Direct Server Return (DSR): the backend replies straight to the client, bypassing the
load balancer entirely on the return path.

See [`docs/architecture.md`](docs/architecture.md) for how it's put together,
[`docs/maglev-algorithm.md`](docs/maglev-algorithm.md) for the consistent-hashing
algorithm, [`docs/testing-topology.md`](docs/testing-topology.md) for the netns/veth
test environment, [`docs/draining.md`](docs/draining.md) for graceful backend removal,
[`docs/benchmarking.md`](docs/benchmarking.md) for the benchmarking harness, and
[`docs/roadmap.md`](docs/roadmap.md) for what's built and what's next.

## Status

- **M1** (thin end-to-end datapath), **M2** (active health checking + live table
  regeneration), **M3** (ncurses TUI), a Prometheus/Grafana visualization round, and
  **M4** (a backend-draining feature plus a benchmarking harness - see
  [Benchmarking](#benchmarking) below) are done and verified. See the roadmap doc for
  details.

## Requirements

- Linux, a C11 compiler, CMake ≥ 3.20
- `libxxhash` and `ncursesw` (dev packages) and `pkg-config`
- `pthread` (part of glibc on modern systems)
- For the test topology: `iproute2`, `nftables`, `python3`, `ethtool`
- Root, for anything that touches raw sockets or network namespaces
- For the pps-ceiling benchmark specifically: a kernel with `pktgen` (`modprobe
  pktgen`) and, optionally, `perf` for CPU accounting

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

Add `--tui` to `run-lb.sh` for the live ncurses dashboard instead of plain log output
(needs a real terminal - run it directly, not with output redirected to a file).

Or run the full automated checks, which bring the topology up, run the load balancer,
verify behavior, and tear down afterward:

```sh
sudo scripts/run-integration-tests.sh    # TCP stickiness, UDP DSR, LB never sees replies
sudo scripts/run-flow-regen-test.sh      # a flow survives a table regen after a backend dies
sudo scripts/run-tui-smoke-test.sh       # --tui starts, renders, and exits cleanly (q and SIGTERM)
```

Pass a specific binary as the first argument to any of these, e.g.
`sudo scripts/run-integration-tests.sh build-tsan/maglev-lb`, to run the same checks
against the Asan or Tsan build.

## Monitoring: Prometheus + Grafana

With the topology up and `maglev-lb` running, bring up a pre-wired Prometheus +
Grafana stack (needs Docker):

```sh
cd monitoring && docker compose up -d
```

Grafana on http://localhost:3000 has the dashboard loaded already. See
[`monitoring/README.md`](monitoring/README.md) for what's on it and how the containers
reach into the netns topology.

## Benchmarking

A separate, larger topology (`scripts/bench/`) simulates a Netflix-style streaming
workload - many concurrent long-lived sessions, asymmetric bandwidth (small requests,
large paced responses), and scripted backend crash / graceful-drain / scale-out
events - plus a set of fast, fixed-parameter controlled benchmarks (flow-churn, raw
pps ceiling via `pktgen`, and a Maglev-vs-naive-modulo disruption-percentage report).

```sh
sudo scripts/bench/demo/run-streaming-demo.sh          # the live, ~9-minute scenario
sudo scripts/bench/controlled/run-crash-vs-drain-test.sh
sudo scripts/bench/controlled/run-churn-test.sh
sudo scripts/bench/controlled/run-pps-ceiling-test.sh
scripts/bench/controlled/remap-report.sh                # no root needed
```

See [`docs/benchmarking.md`](docs/benchmarking.md) for what each one does, why the
default parameters are what they are, and how to watch the demo live on its own
Grafana dashboard.

## Project layout

```
src/
  net/        Ethernet/IPv4/TCP/UDP/GRE parsing and building, raw sockets, ARP
  maglev/     Consistent-hash table generation and lookup
  conntrack/  Per-flow state so a flow keeps going to the same backend
  backend/    Backend set, health checking, table (re)generation
  stats/      Atomic counters, pull-based (read by the TUI, and by a future exporter)
  forward/    The datapath event loop tying the above together
  ui/         The ncurses TUI (--tui) and the Prometheus exporter (metrics_http.c)
  config/     CLI parsing
  main.c
test/unit/         CTest binaries for the modules above
test/integration/  Python test clients driven from scripts/run-*-test.sh
scripts/           Netns/veth topology setup and orchestration scripts
scripts/bench/     The benchmark topology, controlled/ tests, and the demo/ scenario
tools/             Standalone analysis binaries (maglev_remap_report)
monitoring/        Prometheus + Grafana docker-compose stack and dashboards
docs/              Architecture, algorithm, topology, draining, benchmarking, roadmap
```
