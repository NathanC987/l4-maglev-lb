#!/usr/bin/env python3
"""Live-mode client: an open-loop request generator aimed at the VIP.

One process per client node on the canvas, run by the bridge inside the client
network namespace, each bound to its own source IP. Speaks the line protocol
documented in service_server.py.

Open loop: arrivals are Poisson at the configured rps (exponential gaps, like
breakscale's traffic model) and are launched whether or not earlier requests
have finished - a closed-loop generator would quietly slow down exactly when the
system is overloaded, hiding the overload. Latency is measured from each
request's *scheduled* time, not the moment it was actually sent, so a generator
that falls behind can't under-report latency (coordinated omission); how far it
fell behind is reported as lagMs.

Connection modes (`poolSize`):
  0  - a new TCP connection per request. Every request is a new 5-tuple, so
       Maglev balances requests.
  K  - K persistent connections, requests multiplexed round-robin over them.
       Maglev only ever sees K flows, so it balances *connections*: with few
       connections the per-backend split is lopsided, adding a backend doesn't
       help pinned connections, and a drain never completes. `maxConnAgeMs`
       recycles connections (like gRPC's MAX_CONNECTION_AGE) - the fix.

Retries: up to `retries` extra attempts per request, backoff 25ms * 2^(n-1)
with +-50% jitter (breakscale's constants). A retry in per-request mode gets a
new source port, so Maglev may send it to a different backend.

Control: JSON lines on stdin ({"op": "config"|"load"|"reset_stats", ...}); stdin
EOF exits. Stats: one JSON line on stdout every --stats-interval-ms.
"""
import argparse
import asyncio
import collections
import errno
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import livecommon as lc  # noqa: E402

RETRY_BASE_BACKOFF_MS = 25.0
NO_ROUTE_ERRNOS = {errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EADDRNOTAVAIL}
MAX_LINE = 256


class Crashed(Exception):
    """The connection broke before a reply arrived (RST, refused, EOF)."""


def classify(exc):
    if isinstance(exc, asyncio.TimeoutError):
        return "timeout"
    if isinstance(exc, OSError) and exc.errno in NO_ROUTE_ERRNOS:
        return "no-route"
    return "crashed"


class ClientConn(asyncio.Protocol):
    def __init__(self, client):
        self.client = client
        self.transport = None
        self.buf = b""
        self.pending = {}
        self.alive = False
        self.retiring = False
        self.opened_ms = 0.0

    def connection_made(self, transport):
        self.transport = transport
        self.alive = True
        self.opened_ms = time.monotonic() * 1000.0
        self.client.open_conns += 1

    def send(self, req_id, close):
        fut = asyncio.get_running_loop().create_future()
        self.pending[req_id] = fut
        self.transport.write(f"REQ {req_id} {'close' if close else 'keep'}\n".encode("ascii"))
        return fut

    def data_received(self, data):
        self.buf += data
        while True:
            nl = self.buf.find(b"\n")
            if nl < 0:
                if len(self.buf) > MAX_LINE:
                    self.transport.abort()
                return
            line, self.buf = self.buf[:nl], self.buf[nl + 1:]
            parts = line.decode("ascii", "replace").split()
            if len(parts) < 3 or parts[0] not in ("OK", "ERR", "SHED"):
                continue
            fut = self.pending.pop(parts[1], None)
            if fut is None or fut.done():
                continue  # reply to a request we already timed out
            try:
                slot = int(parts[2])
                q_us = int(parts[3]) if len(parts) > 3 else 0
                s_us = int(parts[4]) if len(parts) > 4 else 0
            except ValueError:
                continue
            fut.set_result((parts[0], slot, q_us, s_us))

    def connection_lost(self, exc):
        self.alive = False
        self.client.open_conns -= 1
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(Crashed())
        self.pending.clear()


class Client:
    def __init__(self, args):
        self.vip = args.vip
        self.port = args.port
        self.src_ip = args.src_ip
        self.rps = args.rps
        self.timeout_ms = args.timeout_ms
        self.retries = args.retries
        self.pool_size = args.pool_size
        self.max_conn_age_ms = args.max_conn_age_ms
        self.load_enabled = not args.paused
        self.rng = lc.make_rng(args.seed)
        self.next_id = 0
        self.open_conns = 0
        self.in_flight = 0
        self.pool = []
        self.rr = 0
        self.reset_stats()

    def reset_stats(self):
        self.c = {"scheduled": 0, "attempts": 0, "ok": 0, "failed": 0, "retries": 0,
                  "localDrops": 0}
        self.failed_by_reason = collections.Counter()
        self.resp_by_slot = collections.Counter()
        self.hist = lc.LogHistogram()
        self.lag_max_ms = 0.0
        self.last_trace = None

    def max_in_flight(self):
        return int(max(64, min(20000, self.rps * (self.timeout_ms / 1000.0) * 2 * (self.retries + 1))))

    # --- attempts ----------------------------------------------------------
    async def _attempt_per_request(self, timeout_s):
        loop = asyncio.get_running_loop()
        self.next_id += 1
        req_id = str(self.next_id)
        t0 = loop.time()
        try:
            transport, conn = await asyncio.wait_for(
                loop.create_connection(lambda: ClientConn(self), self.vip, self.port,
                                       local_addr=(self.src_ip, 0)),
                timeout_s)
        except (asyncio.TimeoutError, OSError) as e:
            return classify(e), None
        try:
            return "reply", await asyncio.wait_for(conn.send(req_id, close=True),
                                                    max(0.001, timeout_s - (loop.time() - t0)))
        except asyncio.TimeoutError:
            transport.abort()
            return "timeout", None
        except Crashed:
            return "crashed", None
        # No explicit close on success: the server closes first after replying,
        # keeping TIME_WAIT off our ephemeral ports.

    def _pick_conn(self):
        live = [c for c in self.pool if c.alive and not c.retiring]
        if not live:
            return None
        self.rr = (self.rr + 1) % len(live)
        return live[self.rr]

    async def _attempt_pooled(self, timeout_s):
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        conn = self._pick_conn()
        # No open connection (startup, or every pooled connection just broke):
        # wait for the maintainer to reconnect, within this attempt's timeout,
        # the way a real pooled client blocks on its pool.
        while conn is None:
            if loop.time() - t0 >= timeout_s:
                return "timeout", None
            await asyncio.sleep(0.01)
            conn = self._pick_conn()
        self.next_id += 1
        req_id = str(self.next_id)
        try:
            return "reply", await asyncio.wait_for(conn.send(req_id, close=False),
                                                    max(0.001, timeout_s - (loop.time() - t0)))
        except asyncio.TimeoutError:
            conn.pending.pop(req_id, None)
            return "timeout", None
        except Crashed:
            return "crashed", None

    async def _request(self, sched_s):
        loop = asyncio.get_running_loop()
        self.in_flight += 1
        try:
            reason = None
            for attempt in range(self.retries + 1):
                self.c["attempts"] += 1
                if attempt > 0:
                    self.c["retries"] += 1
                timeout_s = self.timeout_ms / 1000.0
                if self.pool_size > 0:
                    kind, res = await self._attempt_pooled(timeout_s)
                else:
                    kind, res = await self._attempt_per_request(timeout_s)
                if kind == "reply":
                    verb, slot, q_us, s_us = res
                    self.resp_by_slot[str(slot)] += 1
                    if verb == "OK":
                        total_ms = (loop.time() - sched_s) * 1000.0
                        self.c["ok"] += 1
                        self.hist.record(total_ms)
                        self.last_trace = {"totalMs": total_ms, "ok": True, "reason": None,
                                           "slot": slot, "queuedMs": q_us / 1000.0,
                                           "serviceMs": s_us / 1000.0}
                        return
                    reason = "shed" if verb == "SHED" else "error"
                else:
                    reason = kind
                if attempt < self.retries:
                    backoff = RETRY_BASE_BACKOFF_MS * (2 ** attempt) * (0.5 + self.rng.random())
                    await asyncio.sleep(backoff / 1000.0)
            self.c["failed"] += 1
            self.failed_by_reason[reason] += 1
            self.last_trace = {"totalMs": (loop.time() - sched_s) * 1000.0, "ok": False,
                               "reason": reason, "slot": None, "queuedMs": 0, "serviceMs": 0}
        finally:
            self.in_flight -= 1

    # --- loops -------------------------------------------------------------
    async def scheduler(self):
        loop = asyncio.get_running_loop()
        next_t = loop.time()
        while True:
            if not self.load_enabled or self.rps <= 0:
                await asyncio.sleep(0.05)
                next_t = loop.time()
                continue
            now = loop.time()
            while next_t <= now:
                lag_ms = (now - next_t) * 1000.0
                if lag_ms > self.lag_max_ms:
                    self.lag_max_ms = lag_ms
                self.c["scheduled"] += 1
                if self.in_flight >= self.max_in_flight():
                    self.c["localDrops"] += 1
                    self.c["failed"] += 1
                    self.failed_by_reason["client-overload"] += 1
                else:
                    lc.spawn(self._request(next_t))
                next_t += self.rng.expovariate(self.rps)
            await asyncio.sleep(max(0.0, next_t - loop.time()))

    async def pool_maintainer(self):
        loop = asyncio.get_running_loop()
        while True:
            self.pool = [c for c in self.pool if c.alive]
            now_ms = loop.time() * 1000.0
            active = [c for c in self.pool if not c.retiring]
            for c in active:
                aged = self.max_conn_age_ms > 0 and now_ms - c.opened_ms > self.max_conn_age_ms
                if aged:
                    c.retiring = True
            active = [c for c in self.pool if not c.retiring]
            for c in active[self.pool_size:]:
                c.retiring = True
            for c in self.pool:
                if c.retiring and not c.pending:
                    c.transport.close()
            want = self.pool_size - len([c for c in self.pool if not c.retiring])
            for _ in range(max(0, want)):
                try:
                    _, conn = await asyncio.wait_for(
                        loop.create_connection(lambda: ClientConn(self), self.vip, self.port,
                                               local_addr=(self.src_ip, 0)),
                        max(0.05, self.timeout_ms / 1000.0))
                    self.pool.append(conn)
                except (asyncio.TimeoutError, OSError):
                    break  # try again on the next tick
            await asyncio.sleep(0.1)

    def on_command(self, cmd):
        op = cmd["op"]
        if op == "config":
            self.rps = lc.num(cmd, "rps", 0, 2000, self.rps)
            self.timeout_ms = lc.num(cmd, "timeoutMs", 50, 10000, self.timeout_ms)
            self.retries = lc.num(cmd, "retries", 0, 5, self.retries, integer=True)
            self.pool_size = lc.num(cmd, "poolSize", 0, 64, self.pool_size, integer=True)
            age = lc.num(cmd, "maxConnAgeMs", 0, 60000, None, integer=True)
            if age is not None and (age == 0 or age >= 1000):
                self.max_conn_age_ms = age
        elif op == "load":
            if isinstance(cmd.get("enabled"), bool):
                self.load_enabled = cmd["enabled"]
        elif op == "reset_stats":
            self.reset_stats()

    def stats(self):
        s = {
            "type": "stats", "tMs": time.monotonic() * 1000.0,
            "counters": dict(self.c), "failedByReason": dict(self.failed_by_reason),
            "inFlight": self.in_flight, "openConns": self.open_conns,
            "lagMsMax": self.lag_max_ms, "loadEnabled": self.load_enabled,
            "config": {"rps": self.rps, "timeoutMs": self.timeout_ms, "retries": self.retries,
                       "poolSize": self.pool_size, "maxConnAgeMs": self.max_conn_age_ms},
            "hist": self.hist.take_delta(), "respBySlot": dict(self.resp_by_slot),
            "lastTrace": self.last_trace,
        }
        self.lag_max_ms = 0.0
        return s


async def main_async(args):
    loop = asyncio.get_running_loop()
    client = Client(args)
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    lc.emit({"type": "ready", "srcIp": args.src_ip, "pid": os.getpid()})
    if args.stdin_control:
        lc.spawn(lc.read_commands(client.on_command, stop.set))
    tasks = [loop.create_task(client.scheduler()), loop.create_task(client.pool_maintainer())]

    async def stats_loop():
        while True:
            await asyncio.sleep(args.stats_interval_ms / 1000.0)
            lc.emit(client.stats())

    tasks.append(loop.create_task(stats_loop()))
    await stop.wait()
    for t in tasks:
        t.cancel()


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--vip", required=True)
    p.add_argument("--port", type=int, default=9000)
    p.add_argument("--src-ip", required=True)
    p.add_argument("--rps", type=float, default=50.0)
    p.add_argument("--timeout-ms", type=float, default=1000.0)
    p.add_argument("--retries", type=int, default=1)
    p.add_argument("--pool-size", type=int, default=0)
    p.add_argument("--max-conn-age-ms", type=int, default=0)
    p.add_argument("--paused", action="store_true", help="start with load disabled")
    p.add_argument("--stats-interval-ms", type=int, default=250)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--no-stdin-control", dest="stdin_control", action="store_false")
    return p.parse_args(argv)


def main():
    asyncio.run(main_async(parse_args()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
