# Roadmap

## M1 - thin end-to-end datapath (done)

Raw-socket ingestion → parse → 5-tuple → static Maglev lookup → conntrack → GRE
encapsulation → DSR forward, proven over the netns/veth topology for both TCP and
UDP, with an independent proof (an `nftables` counter, see `docs/testing-topology.md`)
that the load balancer never sees a backend's reply. Verified in
`scripts/run-integration-tests.sh`.

## M2 - active health checking + live table regeneration (done)

A real health-checker thread does non-blocking TCP-connect probes against every
backend on a timer, with configurable rise/fall thresholds
(`--hc-interval-ms`/`--hc-timeout-ms`/`--hc-rise`/`--hc-fall`). A backend flipping
health triggers a Maglev table regeneration, installed without disrupting flows
already pinned to a still-healthy backend. Verified in
`scripts/run-flow-regen-test.sh`, including under ThreadSanitizer.

"Dynamic backend add/remove" here means the health checker changing which configured
backends are *eligible* - not a runtime API for adding backends that weren't in the
original `--backend` list. `backend_manager_add`/`_remove` exist and work, but
nothing currently calls them after startup; that would need a config-reload or
control-plane surface, which is out of scope until M7.

## M3 - visualization (done)

An ncurses TUI (`src/ui/tui.c`, opt-in via `--tui`) polling `stats_registry_snapshot()`,
`backend_manager_snapshot_all()`, and `table_generator`'s generation/regen-time
accessors once a second: global rx/tx/conntrack/drop counters plus a per-backend table
(health, packets, bytes, active flows, health-check failures). Two small gaps this
surfaced got filled in along the way, both in `stats_registry`: per-backend
`active_flows` and `health_check_failures` were declared but nothing updated them -
`conntrack_reap_slice()` now takes an eviction callback so the datapath can decrement
`active_flows` when a flow times out, and `health_checker` now records every failed
probe, not just the ones that cross the unhealthy threshold. Two unused, never-written
fields (`maglev_table_regens`/`_last_us`) were removed rather than wired up, since
`table_generator` already tracked the same thing correctly.

Running the datapath and the TUI both need "the main thread" for different reasons
(ncurses wants it for terminal I/O; the datapath's own signal handling assumed it was
running there) - `--tui` now runs the datapath on its own thread while the TUI owns
the main one, coordinated by a small joiner thread + `eventfd` so the TUI can `poll()`
for "the datapath just stopped" instead of only noticing on its next redraw tick. This
surfaced a real, pre-existing bug unrelated to the TUI itself: `health_checker`'s
thread was being spawned *before* the code that blocks SIGINT/SIGTERM, so it inherited
an unblocked signal mask - a process-directed SIGTERM could land on it and terminate
the process via that thread's default disposition instead of the clean shutdown path,
non-deterministically depending on which thread the kernel happened to pick. Headless
mode had the exact same bug; it just never surfaced because nothing was checking
`maglev-lb`'s own shutdown exit code, only its behavior *during* a run. Fixed by
blocking the signals once, at the very top of `main()`, before any thread - including
`health_checker`'s - is ever spawned. Verified in `scripts/run-tui-smoke-test.sh`,
including under ThreadSanitizer with all four threads (main/TUI, datapath, joiner,
health checker) running concurrently.

## Prometheus + Grafana (done)

A Prometheus text-exposition HTTP endpoint (`src/ui/metrics_http.c`, port 9105 by
default, `--metrics-port 0` disables it), built the same way M3 predicted: reading
`stats_registry`, `backend_manager`, `table_generator`, and `conntrack` with no
restructuring. Its own thread, coordinated the same way as the TUI's - poll() with a
stop `eventfd`, no dependency on ncurses' own timeout handling.

Two of those needed a small thread-safety fix first, since they'd only ever had a
single reader (the TUI, and only while a human is looking at it) and now had a second,
independent one that could poll continuously: `conntrack`'s `active` flow counter and
`table_generator`'s `generation` counter both became `_Atomic`.

`monitoring/` has a docker-compose Prometheus + Grafana stack and one dashboard
(`monitoring/grafana/dashboards/maglev-lb.json`, 27 panels across 5 rows: overview,
client→LB traffic, LB-internal decisions, per-backend health/load, and Maglev table
regeneration). `scripts/setup-netns.sh` gives the root netns its own address on the
shared bridge (`HOST_IP` in `topology.sh`) so a host-networked Prometheus container can
reach the LB's metrics endpoint inside its own netns - doesn't touch the DSR datapath
at all, so it has no effect on the bypass proof.

Every panel's query was verified against a live stack under real traffic (not just
checked for valid PromQL syntax): Prometheus scraping with `health: up`, all ~20
queries returning real data through Grafana's own datasource proxy, and a live
backend-failure scenario (kill a backend, watch `maglev_lb_backend_up` flip,
`maglev_lb_table_generation` increment, and per-backend traffic rebalance onto the
survivor) - see `scripts/run-integration-tests.sh`'s metrics-endpoint check for the
automated version of the first part.

## M4 - benchmarking harness

Traffic generation (a small custom sender reusing `net/raw_socket.c`, or the kernel's
built-in `pktgen`), throughput/latency/CPU measurement, and a flow-churn test
exercising conntrack's pre-allocated pool and reaper under load.

## M5 - eBPF/XDP fast path (stretch)

Reuse `backend_manager`/`health_checker`/`table_generator` unchanged as the control
plane; have them populate BPF maps (mirroring the `routing_snapshot` shape) instead
of, or alongside, the current userspace lookup table.

## M6 - multi-core datapath (stretch)

Shard the datapath across threads: each with its own epoll instance, raw socket (or
`AF_PACKET` fanout group), and conntrack shard, all keyed by the same `flow_hash()`
already used for the Maglev bucket lookup, so a flow always lands on the same worker.
This is also where `table_generator`'s single-reader reclamation scheme
(`docs/architecture.md`) would need to become a real per-reader epoch scheme.

## M7 - hardening (stretch)

IPv6, ICMP/PMTU handling (DSR complicates the return path for ICMP too - not solved
by anything here yet), `CAP_NET_RAW`-only privilege drop (currently root-in-netns for
dev simplicity), multi-VIP, config hot-reload, and a real runtime backend
add/remove API.
