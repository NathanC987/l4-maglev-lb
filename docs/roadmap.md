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

## M3 - visualization

An ncurses TUI (`src/ui/`, not yet started) polling `stats_registry_snapshot()`
(already implemented and updated by the datapath on every packet, just with no
consumer yet) once a second. A Prometheus text-exposition HTTP endpoint would follow,
reading the same registry - no restructuring needed, by design.

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
