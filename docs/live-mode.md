# Live mode: a breakscale canvas driving the real load balancer

The streaming demo and the Grafana dashboards are accurate, but they are hard to
*read* for someone who has not been staring at this codebase. Live mode puts the
system on a canvas instead: clients, the load balancer, and backends as boxes and
wires, an offered-load slider, and buttons to crash, slow, partition, or drain a
backend. Then you watch where traffic goes and what breaks.

The canvas is a fork of [breakscale](https://github.com/xevrion/breakscale), a
frontend-only system-design simulator (MIT). The fork adds a Live mode: the
simulator is swapped out for a bridge to **this** project, so every number on the
screen is measured from real processes. The fork lives in its own repo
(`NathanC987/breakscale`, branch `live-mode`); this repo provides the bridge
daemon (`bridge/`), the two workload processes it runs, and two small C changes.

## What is real, and what is configured

Be precise about this when demoing:

- **Real:** `maglev-lb` itself: raw-socket ingestion, the Maglev table,
  conntrack, GRE encapsulation, Direct Server Return, active health checks,
  table regeneration, and draining. Real TCP connections from real client
  processes, each with its own source IP, through real network namespaces. Every
  rate, latency, flow count, and CPU number is measured.
- **Configured:** how much work a backend can do. A backend is
  `bridge/service_server.py`: it serves `capacity` requests at once, each taking a
  service time drawn from a gamma distribution (mean `serviceMs`, coefficient of
  variation `serviceCv`, the same model breakscale's simulator uses), queues up to
  `queueLimit` more, and sheds the rest. The work is `asyncio.sleep`, not CPU burn,
  so the capacity is the number you set rather than whatever the GIL allows.

So when a backend "saturates", that is a configured limit being hit by real
traffic. When the load balancer keeps routing to a crashed backend for a second,
that is the real health checker's real detection delay.

## Running it

Needs everything the bench topology needs (see
[`testing-topology.md`](testing-topology.md)), plus `setpriv` (util-linux),
Python 3 (stdlib only; tested with 3.14), and Node/npm to build the frontend.

```sh
# 1. this repo
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j"$(nproc)"

# 2. the fork
git clone -b live-mode https://github.com/NathanC987/breakscale ../breakscale
(cd ../breakscale && npm ci && npm run build:live)      # -> ../breakscale/dist-live

# 3. the bridge (root: it builds network namespaces and runs maglev-lb)
sudo python3 bridge/live_bridge.py --static-dir ../breakscale/dist-live
```

The bridge prints a link of the form `http://127.0.0.1:8790/#token=…`. Open
**that exact link** (the token is the only credential). Ctrl-C tears everything
down: children, namespaces, and the `maglev-lb` process.

To work on the frontend with hot reload, run `npm run dev:live` in the fork
(Vite proxies `/api` to the bridge) and start the bridge with
`--dev-origin http://localhost:5173`. Then open
`http://localhost:5173/#token=…` with the token the bridge printed.

The bridge refuses to start if the bench topology is already up (the streaming
demo, for example); `--replace-existing` tears that down first.

Other flags: `--ct-timeout-ms` (conntrack idle timeout given to `maglev-lb`,
default 15000), `--slots` (backend slot pool, default 16), `--record FILE`
(append every snapshot as JSON lines; this is how the fork's test fixtures were
captured), `--token-file`, `--no-auto-pause`, `--bin`.

## Architecture

```
browser (fork, Live mode)              bridge (root, 127.0.0.1:8790, Python stdlib)
 LiveEngine ──PUT desired / POST actions──▶ reconciler: desired graph -> slots -> processes
            ◀──SSE snapshot, 4 per second─  ├─ maglev-lb      (lb netns, 16 --backend slots, --admin-fifo)
 mapping.ts: bridge JSON -> breakscale's    ├─ service_server ×N (one per backend slot netns)
             SimSnapshot                    ├─ load_client    ×M (client netns, own source IP each)
                                            └─ reads :9105/metrics, children's stdout, /proc
```

- **Topology.** `BENCH_N_BACKENDS=16 scripts/bench/setup-bench-netns.sh`, then
  `maglev-lb` with all 16 backends configured up front: slot *i* is
  `10.98.0.(10+i)`, LB backend id *i−1*. Client *k* gets source address
  `10.98.0.(200+k)` on the client side, so each client is a distinct flow source to
  Maglev. Unused slots have nothing listening, fail health checks, and own no
  table entries.
- **Reconciliation.** The browser always sends the *whole* desired design
  (idempotent, last write wins). Each canvas backend gets a slot, keeps it for the
  session (deleting and re-adding, or undo, gets the same slot and address back),
  and goes through `starting -> serving`. Removing one goes
  `draining` (admin `DRAIN`, until it is idle for 1 s or 30 s pass) `-> stopping`
  (SIGTERM, then SIGKILL) `-> free`, and its slot is `UNDRAIN`ed only after the LB
  reports it down, so a recycled slot never inherits a drain. Re-adding during
  the drain cancels it.
- **Processes.** Each child runs as
  `ip netns exec <ns> setpriv --pdeathsig TERM --reuid … --no-new-privs python3 -I …`:
  the workload processes drop root (only `maglev-lb` needs it), read JSON
  commands on stdin, write JSON stats on stdout every 250 ms, and exit when stdin
  closes. `pdeathsig` is the backstop if the bridge is killed outright (the smoke
  test `kill -9`s it and checks nothing is left).
- **Measurement.** Rates over a 1 s window; latency percentiles over 5 s, from
  merged log-bucketed histograms (×1.08 buckets, so within 8%). The LB's
  per-backend numbers come from its Prometheus exporter, including the new
  `maglev_lb_backend_flows_assigned_total`.
- **LB CPU** is read from `/proc/<pid>/task/<tid>/schedstat` (nanoseconds on CPU)
  for the datapath thread, which is the main thread when headless. The tick-based
  `utime+stime` from `/proc/<pid>/stat` advances in 10 ms steps and reads a
  lightly loaded datapath as exactly 0%.
- **Auto-pause.** If no browser has the stream open for 10 s, load generation
  pauses, so a closed tab does not leave thousands of requests a second running.

## The workload

**Clients** (`load_client.py`) send open-loop Poisson arrivals at `rps`, measure
latency from the *scheduled* send time (so a stalled client cannot hide its own
queueing: no coordinated omission), time out after `timeoutMs`, and retry up to
`retries` times with jittered exponential backoff. A retry is a new connection
with a new source port, so Maglev may send it to a different backend. Failures are
classified: `timeout`, `shed` (backend said no), `error` (backend failed it),
`crashed` (connection reset or closed before a reply), `no-route`, and
`client-overload` (the generator itself fell behind, flagged separately and never
blamed on the system).

`poolSize` switches the client from one connection per request to that many
long-lived connections carrying many requests each. That is the most useful
lesson in live mode: **an L4 load balancer balances connections, not requests.**
With 4 pooled connections over 3 backends the split can never be even (2/1/1 at
best; 2/2/0 happens), the LB sees zero new flows, adding a backend does nothing for
existing traffic, and a drain never finishes on its own. `maxConnAgeMs` recycles
each connection after that long, which lets Maglev rebalance. This is the
gRPC-behind-an-L4-balancer problem.

**Backends** (`service_server.py`) speak a line protocol (`REQ id close|keep` ->
`OK|ERR id slot queued_us service_us` or `SHED id slot`). The server closes first
on per-request connections, so `TIME_WAIT` piles up on backends rather than
exhausting the client's ephemeral ports. Connections that send no bytes (the LB's
TCP-connect health probes) are counted as probes, never as requests.

## Breaking things

| Button | What actually happens | What you see |
|---|---|---|
| Crash the process | SIGKILL the backend | Requests to it fail as `crashed` until the LB has seen 2 failed probes (500 ms apart, so about 1-1.5 s), regenerates the table, and stops sending it new flows. The wire stays drawn at the LB's new-flow rate until then. |
| Restart | Respawn in the same slot | `starting` until 2 probes pass, then `serving` |
| Slow it down ×N | Multiply service time | Real queueing and tail latency; health stays green, since a TCP connect still succeeds |
| Make it fail requests | Error rate | `error` failures; health stays green |
| Cut its data path | `ip link set gre1 down` in the backend's namespace | **Gray failure:** probes go to the backend's own address over the veth, not through GRE, so the LB keeps it `up` while every request routed to it disappears. Clients see timeouts. |
| Stop new connections | `DRAIN` on the admin FIFO | New flows go elsewhere; pinned flows finish; nothing fails |

There are no LB or client faults. Restarting `maglev-lb` would re-hash every
flow for a moment, because its backends start out healthy before the first
probes run; an `--initial-health down` flag would fix that (a possible later
change).

## How measurements map onto the canvas

breakscale's UI consumes simulator snapshots. `src/live/mapping.ts` in the fork
translates each bridge snapshot into one, and some of the choices are not obvious:

- **Client:** sent = attempts/s, ok/s, failure share, end-to-end p50/p95/p99.
- **Load balancer:** "busy" is the **real datapath CPU** (share of one core).
  "Served" is requests the backends received, including from a backend that has
  been removed but is still draining. The LB has **no latency**, by design: with
  DSR it never sees a reply. Its inspector shows facts instead: CPU, packets per
  second against the measured ~520k pps ceiling, conntrack flows, eligible
  backends, table generation and rebuild time, health-check settings, drops per
  second. The simulator's capacity math is hidden for it.
- **Backend:** busy = fraction of capacity slots in use, waiting = queue length,
  p99 = time at the backend (queue + service), shed and error rates.
- **LB -> backend wire:** requests the backend received per second, **except
  while the backend's process is dead**: then it is the LB's own new-flow rate to
  it. That shows the LB still routing to a crashed backend until the health checks
  catch up, which is the point of the crash demo.
- **Reply wires:** drawn from each backend back to the client on a path *around*
  the LB, labelled as direct replies, because with DSR the reply never passes
  through it.
- **Wire states:** cut (partition), blocked (starting, down, detached), standby
  (draining).
- **`partitioned` failures stay at 0** on purpose: a client cannot tell a
  blackhole from slowness, so it shows up as timeouts. That is what makes it a
  gray failure.
- A dead backend process reports zeros, not its last stats line. Failed health
  checks are shown per second, not as a lifetime total.

## Conntrack and the 15 s timeout

`maglev-lb`'s conntrack forgets a flow after it has been idle for the timeout; it
does not watch for FIN/RST. With the default 120 s, one-connection-per-request at
a few hundred requests a second would show tens of thousands of "active" flows
that ended long ago. The bridge passes `--ct-tcp-timeout-ms 15000` (more than the
largest service time plus the largest client timeout, so no live request loses
its pin), and "flows in conntrack" then reads roughly as new flows per second
× 15. With pooled connections it reads as the number of connections, which makes
the pooled-connection point in a single number. The flags
`--ct-tcp-timeout-ms` / `--ct-udp-timeout-ms` default to the old hard-coded values,
so nothing else changes.

## Limits

- Up to 4 clients, 16 backends, 2000 rps per client and 4000 total. One
  `load_client.py` process sustains about 4000 rps on loopback (both connection
  modes, ~1 ms generator lag); the smoke test runs 1000 rps per-request at ~99%
  ok. A client that falls behind is flagged `behind` and its shortfall is counted as
  `client-overload`, not blamed on the system.
- `maglev-lb`'s own ceiling (~520k pps, see [`benchmarking.md`](benchmarking.md))
  is far above anything Python clients can offer, so the LB will not be the
  bottleneck in live mode. Its CPU is shown so you can see that it isn't.
- LB settings (health-check timing, conntrack timeout, slot count) are fixed when
  the bridge starts and shown read-only.

## Security

The bridge runs as root and spawns processes, so its HTTP API is locked down:

- Listens on 127.0.0.1 only. A random 128-bit token is required on every `/api`
  call. It is passed in the URL fragment, which browsers never send to a server or
  log, and the frontend moves it to session storage and strips it from the address
  bar.
- `Host` must be the bridge itself (defeats DNS rebinding), `Origin` must be
  allowlisted, writes must be `application/json`, bodies are capped at 64 KB, at
  most 4 streams, no CORS headers ever.
- Strict validation: unknown keys, out-of-range numbers, NaN/Infinity, booleans
  posing as numbers, and malformed ids are all rejected with a 422.
- Commands are argv lists built from fixed paths, slot numbers and validated
  numbers. There is never a shell, and no canvas id or label reaches a command
  line. The workload processes drop root.

## A demo walkthrough (about 10 minutes)

1. **Baseline.** 100 req/s, three backends. Point out the even Maglev split on the
   LB -> backend wires, the reply wires going around the LB, and the LB at a
   fraction of a percent of one core.
2. **Overload.** Drag the slider to ~1000. Backends fill up, queues grow, p99
   climbs, then shedding starts. Lower a backend's capacity to make one the
   bottleneck.
3. **Scale out.** Drag in a new backend and wire the LB to it: `starting`, then it
   takes about a quarter of new flows once its probes pass.
4. **Crash vs drain.** Crash one backend: a burst of `crashed` failures for about
   a second while the LB still routes to it, then the table regenerates and
   failures stop. Restart it, then drain it instead: its traffic falls away and
   nothing fails.
5. **Gray failure.** Cut a backend's data path. The LB still says `up`, the
   backend gets nothing, and clients see timeouts. Health checks only prove what
   they test.
6. **Pooled connections.** Set the client to 4 kept-open connections. The LB's
   new-flow rate falls to zero, the split goes lopsided (maybe one backend idle),
   and adding a backend changes nothing. Set "reconnect after" to a few seconds
   and the split evens out.

## Testing

- `bridge/tests/` (no root): `python3 -m unittest discover -s bridge/tests`. Covers
  the workload processes, schema, reconciler state machine, HTTP API, histogram,
  and the CPU readers.
- `sudo scripts/live/run-live-smoke-test.sh`: brings the real system up and
  checks the security rules, baseline split, crash (including that a dead backend
  reports nothing), slow, partition, drain, 1000 rps, pooled connections, slot
  removal, the SSE stream, and that both `kill -9` and SIGTERM leave nothing
  behind.
- In the fork: `npm test` includes `src/live/*.test.ts`, run against hand-written
  frames and against 8 frames **recorded from the real bridge** (baseline,
  starting, crashed before and after detection, partitioned, draining, 1000 rps,
  pooled).
