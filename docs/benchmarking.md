# Benchmarking

## Why a "streaming platform" shape

At L4 the load balancer never sees application content - only 5-tuples and byte
counts. So "realistic, like a video-streaming company's traffic" doesn't mean
anything HTTP- or video-specific here; it means realistic *flow shape*: hundreds of
concurrent, long-lived sessions, heavily asymmetric traffic (a tiny request, a large
sustained response), and backends crashing, being gracefully taken out of rotation,
and being scaled back out - all while real load is flowing. That shape is exactly
what a video-streaming service's edge tier would put through an L4 balancer, and it's
also precisely what's needed to make Direct Server Return's core advantage visible:
the backend pushes megabytes directly to the client while the LB only ever forwards
small requests and ACKs.

Two families of scripts live under `scripts/bench/`, for two different jobs:

- **`scripts/bench/controlled/`** - fixed parameters, fast, deterministic,
  pass/fail-assertable, meant for reproducible measurement and for comparing one
  configuration or code change against another.
- **`scripts/bench/demo/`** - one script, randomized/narratively-paced, meant to be
  *watched* live on the bench Grafana dashboard - a complete tour of the system's
  resilience behaviors in a single ~9-minute run.

Everything here is additive: it doesn't touch `scripts/setup-netns.sh` or anything
the M1-M3 test suite depends on.

## Topology

```
                  br-bench (root netns), 10.98.0.0/24, MTU 1600
                  also has 10.98.0.254/24 itself (HOST_IP)
   ┌──────────────┬───────────────┬─────────────┬─── ··· ───┬─────────────┐
bench-cli-br   bench-lb-br   bench-be1-br                        bench-beN-br
   │               │               │                                     │
[l4mlb-bench-  [l4mlb-bench-  [l4mlb-bench-be1]   ···           [l4mlb-bench-beN]
 client]         lb]           10.98.0.11                        10.98.0.1(10+N)
 10.98.0.2      10.98.0.1      gre1: remote 10.98.0.1        gre1: remote 10.98.0.1
                metrics :9105  VIP 10.98.0.100/32 on lo       VIP 10.98.0.100/32 on lo
                admin FIFO     streaming_server.py (maybe     streaming_server.py (maybe
                                dormant - see below)           dormant - see below)
```

Same single-shared-bridge design as the M1-M3 topology (`docs/testing-topology.md`),
same gotchas (rp_filter, veth checksum offload, wildcard bind, IP_PKTINFO), same
reasons - just parameterized over `BENCH_N_BACKENDS` (default 8) instead of hardcoded
to two, and on its own bridge/subnet (`br-bench`, 10.98.0.0/24) so it can never
collide with the M1-M3 topology. `scripts/bench/setup-bench-netns.sh` deliberately
*duplicates* rather than shares that per-backend setup logic with
`scripts/setup-netns.sh` - see the comment at the top of that file for why.

## The pieces

- **`streaming_server.py`** - not `scripts/echo_server.py` (that stays untouched for
  M1-M3). On a connection, reads one request line, then streams fixed 64 KiB chunks,
  paced to a per-session randomized bitrate (**2000-6000 kbps**, the SD-to-HD range of
  a real adaptive-bitrate ladder), for a per-session randomized duration
  (**8-30s**). Both ranges are deliberately compressed from real streaming-session
  lengths (which can run 20-90 minutes) so a demo run finishes in a few minutes while
  still generating many complete sessions and exercising conntrack's idle-timeout path
  meaningfully relative to the compressed timescale. TCP only - UDP DSR correctness is
  already proven by `test/integration/test_udp_dsr.py`, and a paced UDP path here would
  add complexity for no incremental proof value. Tolerates the health checker's bare
  connect-then-close probes silently (no data ever arrives for those).
- **`viewer_gen.py`** - an asyncio "viewer" traffic generator: continuously launches
  new sessions up to `--concurrency` (default **200**), each one connecting, sending a
  request, and timing/discarding the paced response. Concurrency is asyncio, not
  threads: each session's steady-state work is one non-blocking read plus one sleep
  per ~87-260ms pacing interval (from the bitrate math above) - sparse, I/O-bound work
  where the event loop spends nearly all its time parked in `epoll_wait`, so hundreds
  of concurrent sessions never become the bottleneck ahead of the LB itself. Exposes
  its own tiny Prometheus endpoint (port 9106) - see "Watching it live" below.
  `SIGUSR1` tells it to stop launching new sessions without cutting off ones already
  in flight (the demo's wind-down signal).
- **`tools/maglev_remap_report.c`** - a standalone analysis binary (built by the
  normal CMake build), reusing `maglev_table_generate()` directly. Reports the exact
  fraction of Maglev table slots that change backend when the backend set changes,
  against a naive `hash % N` baseline computed on the same sample. Needs no root, no
  topology, no running `maglev-lb` - pure computation. Run via
  `scripts/bench/controlled/remap-report.sh`.

## Two things the code itself forces on this design

- **Convergence wait before traffic starts.** A newly-added backend defaults to
  `healthy=true` (`src/backend/backend_manager.h`) until the health checker actually
  disproves it. A "dormant" backend (namespace/GRE/VIP already exist, no responder
  running yet - the mechanism behind the scale-out story) therefore looks briefly
  eligible at startup. `run-streaming-demo.sh` polls `maglev_lb_backends_eligible`
  until it matches the intended live count before starting any client traffic -
  without this, early sessions could get routed to a backend nothing is listening on.
- **The 120s conntrack idle timeout is a hard floor on proving reaper behavior.**
  Conntrack has no FIN/RST awareness at all (`src/conntrack/conntrack.h`) - a flow's
  slot is reclaimed only after `TCP_IDLE_TIMEOUT_NS` (120s, hardcoded in `src/main.c`)
  of idleness, regardless of how quickly the TCP connection itself actually closed.
  This means `maglev_lb_conntrack_active_flows` stays high for a couple of minutes
  after traffic stops - **that's correct, not a bug** - and it's why
  `run-streaming-demo.sh`'s cool-down phase is `BENCH_COOLDOWN_S=150` (not 30 or 60):
  anything shorter couldn't honestly show eviction happening. The controlled tests
  that want fast, repeatable runs (`run-crash-vs-drain-test.sh`, `run-churn-test.sh`)
  deliberately do NOT wait for `active_flows` to reach zero for the same reason -
  they check faster-timescale signals instead (traffic to a drained backend actually
  stopping, or zero pool-exhaustion drops under churn) and say so explicitly.

## A DSR subtlety worth knowing before you look at the crash numbers

`drops_backend_unhealthy` (the counter that fires when a conntrack-pinned flow's
backend is no longer in the routing snapshot) is a real, correct signal - but under
DSR it's often **zero even during a genuine crash**, and that's not a bug either. When
a backend process dies, any RST/FIN its kernel generates goes *directly to the
client* (DSR - the LB never sees a backend's outbound traffic at all). The LB only
ever sees this counter move if a *client-originated* packet for an already-pinned
flow happens to arrive in the narrow window between the crash and the table
regenerating (typically well under a second with the health-check settings used
here). For a mostly one-way server-push stream, where the client's own traffic is
just occasional ACKs, that race often just doesn't happen. `run-crash-vs-drain-test.sh`
treats a non-zero crash-side delta as a soft note, not a hard assertion, for exactly
this reason - see its output for a live example.

## Controlled scripts (`scripts/bench/controlled/`)

- **`remap-report.sh`** - the disruption-percentage analysis (see "The pieces"
  above), for this scenario's actual transitions (8→7 backends: the crash; 5→8: the
  scale-out) plus a sweep over N. No root needed.
- **`run-pps-ceiling-test.sh`** - reuses the *M1-M3* 2-backend topology (not the bench
  one - a one-way flood needs no real backend), floods the VIP with minimum-size
  (64-byte) UDP packets via the kernel's `pktgen` module with randomized source ports,
  and measures the datapath's actual sustained pps against its own `rx/tx_packets_total`
  and drop counters, with `perf stat` CPU/cycle accounting if `perf` is available. This
  is a measurement, not a pass/fail on a specific number (that's hardware-dependent);
  its only hard assertion is that `maglev-lb` survives the flood.
- **`run-churn-test.sh`** - fixed concurrency (50) and fixed *short* session duration
  (2s, much shorter than the 120s idle timeout) sustained for a fixed 25s window,
  generating a steady, repeatable flow-open rate. Asserts zero
  `drops_total{reason="conntrack_full"}` despite the churn, and that
  `conntrack_active_flows` genuinely grew (proving the churn was real) - NOT that it
  shrank back down, for the reason above.
- **`run-crash-vs-drain-test.sh`** - the quantitative sibling of
  `scripts/run-drain-test.sh` (which proves draining correct on the minimal
  topology): under identical fixed load, crashes one backend and gracefully drains a
  different one, back to back, and reports detection/regen latency and the
  `drops_backend_unhealthy` delta for each side by side - the headline comparison
  Phase A's draining feature exists to make possible.

## The demo (`scripts/bench/demo/run-streaming-demo.sh`)

Brings up the bench topology with `BENCH_N_BACKENDS` (default 8) backends configured
into `maglev-lb` from the start, but only `BENCH_INITIAL_LIVE` (default 5) actually
running a `streaming_server.py` process. After convergence, starts `viewer_gen.py`
ramping to `BENCH_CONCURRENCY` (default 200) sessions, then works through a scripted
timeline (~9 minutes total; every duration below is an env-overridable variable at
the top of the script):

1. **Sustained load** (`BENCH_PRECRASH_LOAD_S=90`).
2. **Crash**: `SIGKILL` one of the initially-live backends' responder processes.
   Watch: health flip, table regen, a `drops_backend_unhealthy` blip (see the DSR
   subtlety above for why this is sometimes zero) that tapers.
3. **Sustained load** (`BENCH_POSTCRASH_LOAD_S=60`).
4. **Graceful drain**: a *different* still-live backend is sent `DRAIN <id>` over the
   admin FIFO. Watch: table regen with drops staying flat, and that backend's traffic
   tapering to zero as its already-open sessions finish naturally rather than being
   cut off.
5. **Sustained load** (`BENCH_POSTDRAIN_LOAD_S=60`), then the drained backend's now-idle
   responder process is stopped.
6. **Scale-out**: `streaming_server.py` starts on every still-dormant backend (the
   `BENCH_N_BACKENDS - BENCH_INITIAL_LIVE` that were configured into `maglev-lb` from
   the start but never given a live process). Watch: health flip the other way, table
   regen, new backends' packet counters climbing from zero - pure M2 health-checker
   machinery, zero new code for this specific transition.
7. **Sustained load** (`BENCH_POSTSCALE_LOAD_S=90`).
8. **Wind-down**: `SIGUSR1` to `viewer_gen.py` - it stops launching new sessions and
   exits once the last one finishes naturally.
9. **Cool-down** (`BENCH_COOLDOWN_S=150`, no new connections at all): watch
   `conntrack_active_flows` drain toward zero and `conntrack_evictions_total` climb -
   the reaper proof that needs to exceed the 120s idle timeout to be honest.

This continuously exercises real flow churn throughout (not just at the three
scripted events), and produces a final report
(`scripts/bench/.run/results/demo-report-<timestamp>.txt`) with the LB's final
`/metrics` snapshot, the client-side session/latency summary from `viewer_gen.py`, and
`perf stat` output if it ran. Pass `KEEP_RUNNING=1` to skip teardown at the end, for
continued dashboard watching.

## Watching it live

```sh
cd monitoring
docker compose -f docker-compose.yml -f docker-compose-bench.yml up -d
```

Open the "L4 Maglev Load Balancer — Benchmark" dashboard. The headline panel, **"DSR
in action: LB bandwidth vs. client-received bandwidth"**, plots the LB's own total
bandwidth (tiny, request/ACK-only) against what `viewer_gen.py` actually received
(large, the simulated video data) on the same graph - the gap between the two lines
*is* Direct Server Return, made visible. The **Backends** row's new "draining state"
timeline shows the drain event distinctly from the health timeline's crash event. The
new **Viewer Traffic** row is the client-side perspective - active sessions,
completed-by-outcome rate, and time-to-first-byte percentiles - sitting alongside the
LB's own internals for the first time, on the same dashboard, from the same
Prometheus instance.

## Running everything as root

Same pattern as the rest of this project: a narrowly-scoped, temporary `NOPASSWD`
sudoers rule covering `ip`, `nft`, `sysctl`, `ethtool`, `curl`, `python3`, `kill`,
`mkfifo`, `modprobe`, `perf`, and the scripts under `scripts/` and `scripts/bench/`
(including its subdirectories) - see `docs/testing-topology.md`.
