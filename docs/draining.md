# Backend draining

## The gap this fills

Before this, a backend's only state affecting routing was `healthy` - a single
boolean the health checker flips based on TCP-connect probes. The moment it goes
false, `table_generator` excludes that backend from the *entire* routing snapshot:
every flow already pinned to it via conntrack starts getting dropped immediately
(`maglev_lb_drops_total{reason="backend_unhealthy"}` in `src/forward/datapath.c`),
not just new ones. That's the right behavior for a real crash - there's nothing left
to send traffic to - but it means a *planned* removal (taking a backend out of
rotation for maintenance, or scaling one down deliberately) looked, from the LB's and
every connected client's point of view, identical to a crash: same immediate drops,
same disrupted connections. Real load balancers distinguish these; ours didn't.

## The model

`struct backend` (`src/backend/backend.h`) already had an `admin_state` field -
`BACKEND_ENABLED` / `BACKEND_DRAINING` / `BACKEND_DISABLED` - scaffolded from early on
but never actually settable or treated differently from `BACKEND_ENABLED` anywhere.
This feature is what wires it up:

- **`BACKEND_ENABLED`** (default): eligible for new flows, and routable for flows
  already pinned to it. Normal operation.
- **`BACKEND_DRAINING`**: *not* eligible for new flows, but still routable - a flow
  already pinned to it via conntrack keeps being forwarded normally. It simply
  receives zero new Maglev table slots, so its share of *new* traffic drops to
  nothing while whatever it's already serving finishes naturally.
- **`BACKEND_DISABLED`**: excluded from both - the same all-or-nothing treatment
  `healthy=false` already gave every backend. If a draining backend genuinely crashes
  (fails health checks) it falls into the same unhealthy exclusion as any other
  backend, regardless of admin_state - draining is a courtesy, not a promise the
  backend is still there.

Two backend_manager queries now exist instead of one:

- `backend_manager_snapshot_eligible()` (unchanged) - candidates for *new* Maglev
  table slots: `admin_state == BACKEND_ENABLED && healthy && mac_resolved`.
- `backend_manager_snapshot_routable()` (new) - everyone a conntrack-pinned flow may
  still resolve to: `admin_state != BACKEND_DISABLED && healthy && mac_resolved`. A
  strict superset of the eligible set whenever any backend is draining.

## Why the atomic-swap mechanism itself needed zero changes

`docs/architecture.md` documents `table_generator`'s single-reader-safe atomic
snapshot swap as the most carefully-reasoned part of this codebase, so the first
question for any change here is whether it touches that scheme at all. It doesn't.

Maglev table slots store backend **ids**, not array indices
(`t->lookup[c] = (int32_t)sorted[b].id;` in `src/maglev/maglev_table.c`) - a table
built from a *subset* of backends is still perfectly meaningful to a *superset* array,
as long as the superset contains every id the subset does. So
`table_generator_rebuild_and_install()` just builds two lists instead of one -
`routable` (passed as the snapshot's stored backend array, for
`routing_snapshot_find_backend()` id lookups) and `eligible` (passed to
`maglev_table_generate()`, unchanged, as the candidates for new slots) - and
`routing_snapshot_create()` takes both instead of a single array. Nothing about
*how* the snapshot is built, swapped, or reclaimed changed; only *what backends feed
into which half* of it did. `maglev_table_generate()` itself required no
modification at all.

## Triggering it: `--admin-fifo`

An optional CLI flag, `--admin-fifo <path>` (off by default, same pattern as
`--metrics-port 0`). The path must already exist as a named pipe (`mkfifo`) before
`maglev-lb` starts. If set, the health checker thread - which already owns every
`backend_manager` mutation, via `backend_manager_set_health()` on every probe
transition - adds the FIFO's fd to its existing `poll()`/`epoll()` loop alongside its
probe sockets and its shutdown `wake_fd`, rather than spinning up a dedicated thread
for this. One command per line:

```
DRAIN <backend_id>
UNDRAIN <backend_id>
```

`<backend_id>` is the numeric id `maglev-lb` assigns backends in `--backend` order at
startup (printed at startup, and available live as the `backend_id` label on every
`maglev_lb_backend_*` metric). A command is applied via
`backend_manager_set_admin_state()`, which triggers the same
change-callback-to-table-rebuild path every health transition already goes through -
this feature adds a second *trigger* for that pipeline, not a second pipeline.

The FIFO is opened `O_RDWR | O_NONBLOCK`, not `O_RDONLY`: a read-only FIFO sees
`EPOLLHUP` / a zero-byte read ("EOF") the instant no writer currently has it open,
which is the normal state between commands, not a real end of stream. Holding our own
write end open the whole time (even though we never write to it) means there's always
at least one writer from the kernel's point of view, so the read side never spuriously
EOFs.

## What changed where

- `src/backend/backend.h` - no change; `admin_state` already existed.
- `src/backend/backend_manager.c/.h` - `backend_manager_set_admin_state()` (mirrors
  `backend_manager_set_health()`'s shape) and `backend_manager_snapshot_routable()`.
- `src/maglev/maglev_table.c/.h` - `routing_snapshot_create()` takes a `routable` set
  and a `candidates` set instead of one array.
- `src/backend/table_generator.c` - builds both sets each rebuild.
- `src/backend/health_checker.c/.h` - opens and polls the admin FIFO, parses
  `DRAIN`/`UNDRAIN` lines.
- `src/config/config.c/.h`, `src/main.c` - `--admin-fifo` flag, threaded through.
- `src/ui/metrics_http.c` - a new `maglev_lb_backend_draining` gauge (the pre-existing
  `maglev_lb_backend_admin_enabled` gauge already existed but doesn't distinguish
  DRAINING from DISABLED, so this adds an unambiguous one).
- `src/ui/tui.c` - no change; its per-backend `ADMIN` column already printed
  `admin_state_str()`, which now actually shows `draining` once something sets it.

## Verification

`scripts/run-drain-test.sh` (same topology and style as `scripts/run-flow-regen-test.sh`,
run on the default, Asan, and Tsan builds): opens a flow, drains *that flow's own*
backend over the FIFO, and asserts the property a plain unhealthy transition can never
have - `maglev_lb_drops_total{reason="backend_unhealthy"}` stays at **exactly** its
pre-drain value for the whole drain window, while the held flow keeps getting answered
by the same (draining) backend and a brand new flow opened during the drain lands only
on the other one. Contrast with `run-flow-regen-test.sh`, where that same drop counter
is expected to move.
