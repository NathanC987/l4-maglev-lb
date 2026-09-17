# Architecture

## Overview

`maglev-lb` is a single binary. Two threads always run:

- **The datapath thread** (`src/forward/datapath.c`) runs an epoll loop over one raw
  socket. This is where every packet is received, parsed, looked up, and forwarded.
  It never blocks on anything. In headless mode (no `--tui`) this *is* the main
  thread; with `--tui` it's spawned onto its own thread instead - see below.
- **The health checker thread** (`src/backend/health_checker.c`) runs its own epoll
  loop doing non-blocking TCP-connect probes against every backend, on a timer. It's
  the only thing that changes which backends are eligible for traffic.

With `--tui`, two more threads run: the **TUI thread** (the main thread, redrawing the
ncurses dashboard - see below) and a small **joiner thread** that exists only to
`pthread_join()` the datapath thread and wake the TUI the instant it stops.

Everything else (`main.c`) is single-threaded setup: parse CLI args, block
SIGINT/SIGTERM (see "Signal handling" below), resolve backend MAC addresses via ARP,
build the first Maglev table, then start the health checker and (depending on mode)
either run the datapath directly or hand off to `--tui`.

## Packet flow

```
raw socket recv
  -> net/eth.c, net/ipv4.c, net/tcp_udp.c   (bounds-checked header parsing)
  -> net/flow.c                              (5-tuple extraction: packet_extract_flow)
  -> conntrack/conntrack.c                   (already-pinned flow? use its backend)
  -> maglev/maglev_table.c                   (new flow: hash the 5-tuple, look up a backend)
  -> conntrack/conntrack.c                   (pin the new flow to that backend)
  -> net/gre.c                               (wrap the original packet in GRE, unmodified)
  -> raw socket send
```

The inner packet (the client's original Ethernet-stripped IP packet) is never
rewritten - DSR means the load balancer only ever adds a GRE+IP+Ethernet wrapper
around it. See `docs/testing-topology.md` for how the backend decapsulates it and
replies directly to the client.

Conntrack is checked *before* the Maglev table on every packet. A hit means the flow
keeps its already-chosen backend even if the table has since been regenerated; a miss
triggers a fresh Maglev lookup and a new conntrack entry.

## Modules

| Module | Responsibility |
|---|---|
| `net/` | Ethernet/IPv4/TCP/UDP/GRE header parsing and building, raw socket I/O, ARP resolution |
| `maglev/` | The consistent-hashing table itself - see `docs/maglev-algorithm.md` |
| `conntrack/` | A slab-allocated hash table of active flows, plus a bounded-per-tick reaper for idle ones |
| `backend/` | The backend set (`backend_manager`), health probing (`health_checker`), and table (re)generation (`table_generator`) |
| `stats/` | Plain atomic counters (`lb_stats`) with a pull-based snapshot function, updated by the datapath, the conntrack reaper, and the health checker; read by the TUI (and, in the future, a Prometheus exporter) |
| `forward/` | The datapath event loop |
| `ui/` | The ncurses TUI (`--tui`) |
| `config/` | CLI argument parsing (`getopt_long`) |

## The routing snapshot: how two threads share the Maglev table safely

The datapath thread reads the current Maglev table on every packet; the health
checker thread rebuilds it whenever a backend's health changes. Making this safe
without a lock on the packet hot path is the one genuinely subtle piece of this
codebase, in `src/backend/table_generator.c`.

A `struct routing_snapshot` (`src/maglev/maglev_table.h`) bundles a Maglev table
together with the exact backend array it was built from - the table's slots hold
backend *ids*, which are only meaningful relative to that specific array. The two are
always swapped in as one atomic unit:

```
_Atomic(struct routing_snapshot *) active;
```

`table_generator_get_active()` does one `atomic_load` (acquire) per packet.
`table_generator_rebuild_and_install()` (called from the health checker thread, via
`backend_manager`'s change callback) builds a whole new snapshot off to the side and
does one `atomic_exchange` (release) to install it.

The swapped-out snapshot **cannot be freed by the thread that swapped it out** - the
datapath thread might still be dereferencing the very pointer it just replaced.
Instead it's pushed onto a small mutex-protected "retired" list. The datapath thread
itself frees everything on that list, but only from `table_generator_reclaim()`,
called once per conntrack-reaper tick (every 500ms) - a point in its own event loop
where, by construction, it cannot still be holding a reference to an older snapshot
from a previous packet. This works because there is exactly one reader thread; a
future milestone adding more datapath threads would need a real epoch-counter scheme
instead. The `Tsan` build exists specifically to keep this honest - see
`scripts/run-flow-regen-test.sh`, which exercises both threads concurrently under
ThreadSanitizer.

`backend_manager` itself (`src/backend/backend_manager.c`) is a fixed-size array
behind one mutex, held only around mutation (add/remove/health/mac changes) and
snapshot reads - the datapath thread never touches it directly, only through the
already-swapped `routing_snapshot`.

## Signal handling: why the block happens before *any* thread exists

`main()` blocks `SIGINT`/`SIGTERM` (`pthread_sigmask(SIG_BLOCK, ...)`) as close to the
top of `main()` as possible - before `health_checker_start()`, and (in `--tui` mode)
long before the datapath/joiner threads exist. Every thread a process creates inherits
its creating thread's signal mask, so blocking this early guarantees *no* thread in
the process can ever receive one of these signals via its default (process-terminating)
disposition; only the datapath thread's own `signalfd` (set up inside
`datapath_run()`) ever actually consumes it, giving one single, deterministic
shutdown path no matter how many other threads exist.

This was originally done later - right before spawning the datapath thread in
`--tui` mode - and it looked correct: that thread and everything spawned after it
were properly protected. It wasn't enough. `health_checker_start()` is called earlier
in `main()`, in *both* modes, so its thread was already running with an *unblocked*
mask by the time the later block executed. A process-directed `SIGTERM` could still
land on it and kill the whole process via that thread's default disposition instead
of the clean shutdown path - non-deterministically, depending on which unblocked
thread the kernel happened to pick. Headless mode has the identical exposure; it
never surfaced there because nothing checked `maglev-lb`'s own exit code on shutdown,
only its behavior *during* a run. `scripts/run-tui-smoke-test.sh` is the test that
actually checks this (both a `q` keypress and a `SIGTERM` must exit code 0), which is
how it was caught in the first place.

## The TUI: a second thread wants the main thread too

ncurses wants to own the main thread for terminal I/O. Headless mode doesn't have
this tension - `datapath_run()` just runs directly on the main thread, as it always
has. `--tui` resolves it by moving the datapath onto its own thread and running
`tui_run()` (`src/ui/tui.c`) on the main one instead.

`tui_run()` redraws once a second, polling `stats_registry_snapshot()`,
`backend_manager_snapshot_all()`, and `table_generator_generation()` /
`_last_regen_us()` - all thread-safe, and deliberately *not*
`table_generator_get_active()`, which only the datapath thread may ever call (see
above). Waiting for input uses `poll()` on stdin directly, not ncurses' own
`getch()`-with-`timeout()` mechanism, alongside a second fd: an `eventfd` that the
joiner thread writes to the instant `pthread_join(dp_thread)` returns, so the TUI
notices a stopped datapath immediately rather than on its next redraw tick.
`datapath_is_running()` is still checked as a fallback on every tick regardless.

## Why one binary, one process

There's no separate control-plane process or IPC. Every thread this binary ever runs
communicates only through the atomically-swapped `routing_snapshot`,
`backend_manager`'s mutex, `stats_registry`'s atomics, and (for `--tui` shutdown) one
`eventfd` - all in-process, all simple to reason about, all trivial to run under
`gdb`. A future eBPF/XDP fast path (see the roadmap) would naturally split into a
kernel program plus this same binary acting as its control-plane loader, so this
isn't a decision that needs revisiting later.
