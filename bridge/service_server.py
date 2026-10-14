#!/usr/bin/env python3
"""Live-mode backend: a request/response server with an explicit capacity model.

One process per active backend slot, run by the bridge inside that slot's
network namespace. Listens on 0.0.0.0 so it answers both the VIP (client
traffic, after the kernel's gre1 tunnel decapsulates it) and the backend's own
real IP (maglev-lb's health-check probes) - same reason as scripts/echo_server.py.

Capacity model (the part that is *configured*, not emergent - say so in docs):
  - `capacity` requests are served concurrently; each holds a slot for a service
    time drawn from gamma(mean=serviceMs, cv=serviceCv) - livecommon.service_time_ms,
    matching breakscale's simulator.
  - Beyond that, up to `queueLimit` requests wait in a FIFO queue.
  - Beyond that, the request is shed immediately with an explicit SHED reply, so a
    client can tell "rejected" from "timed out".
  - Work is `asyncio.sleep`, not CPU burn: burning CPU in Python would make the
    capacity depend on the GIL rather than on the configured numbers.
Connections are accepted eagerly, so the kernel's accept backlog never becomes a
second, unconfigured queue.

Wire protocol (ASCII lines, at most 256 bytes; anything else closes the connection):
  client -> server   REQ <id> <close|keep>
  server -> client   OK <id> <slot> <queued_us> <service_us>
                     ERR <id> <slot> <queued_us> <service_us>
                     SHED <id> <slot>
A connection may carry many outstanding requests; replies can come back out of
order and are matched by <id>. On `close` the SERVER closes after replying, so
TIME_WAIT lands here rather than exhausting the client's ephemeral ports.

Control: JSON lines on stdin ({"op": "config"|"fault"|"clear"|"reset_stats", ...});
stdin EOF (the bridge went away) exits. Stats: one JSON line on stdout every
--stats-interval-ms.
"""
import argparse
import asyncio
import collections
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import livecommon as lc  # noqa: E402

MAX_LINE = 256


def now_ms():
    return time.monotonic() * 1000.0


class Server:
    def __init__(self, args):
        self.slot = args.slot
        self.lb_ip = args.lb_ip
        self.capacity = args.capacity
        self.service_ms = args.service_ms
        self.service_cv = args.service_cv
        self.queue_limit = args.queue_limit
        self.error_rate = args.error_rate
        self.idle_timeout_s = args.idle_timeout_s
        self.slow_factor = 1.0
        self.fault_error_rate = 0.0
        self.rng = lc.make_rng(args.seed)

        self.busy = 0
        self.queue = collections.deque()  # (conn, req_id, close, enq_ms)
        self.open_conns = 0
        self.busy_integral_ms = 0.0
        self._busy_since = now_ms()
        self.reset_stats()

    def reset_stats(self):
        self.c = {"arrivals": 0, "ok": 0, "err": 0, "shed": 0, "probes": 0,
                  "accepts": 0, "abandoned": 0}
        self.hist_backend = lc.LogHistogram()
        self.hist_service = lc.LogHistogram()
        self.resp_by_peer = collections.Counter()
        self.busy_integral_ms = 0.0
        self._busy_since = now_ms()

    def _account_busy(self):
        t = now_ms()
        self.busy_integral_ms += self.busy * (t - self._busy_since)
        self._busy_since = t

    # --- admission -------------------------------------------------------
    def submit(self, conn, req_id, close):
        self.c["arrivals"] += 1
        if self.busy < self.capacity:
            self._start(conn, req_id, close, now_ms())
        elif len(self.queue) < self.queue_limit:
            self.queue.append((conn, req_id, close, now_ms()))
        else:
            self.c["shed"] += 1
            conn.reply(f"SHED {req_id} {self.slot}\n", close)

    def _start(self, conn, req_id, close, enq_ms):
        self._account_busy()
        self.busy += 1
        lc.spawn(self._serve(conn, req_id, close, enq_ms))

    async def _serve(self, conn, req_id, close, enq_ms):
        start = now_ms()
        svc = lc.service_time_ms(self.rng, self.service_ms, self.service_cv) * self.slow_factor
        await asyncio.sleep(svc / 1000.0)
        end = now_ms()
        # Same order as breakscale: the error roll happens after service, so a
        # failing backend still spends its capacity on the doomed request.
        p_err = 1.0 - (1.0 - self.error_rate) * (1.0 - self.fault_error_rate)
        failed = self.rng.random() < p_err
        queued_us = int((start - enq_ms) * 1000)
        service_us = int((end - start) * 1000)
        self.hist_backend.record(end - enq_ms)
        self.hist_service.record(end - start)
        self.c["err" if failed else "ok"] += 1
        # Work is done whether or not anyone is still listening - a client that
        # timed out and left still cost us this slot (the retry-storm mechanism).
        if conn.alive:
            self.resp_by_peer[conn.peer_ip] += 1
            verb = "ERR" if failed else "OK"
            conn.reply(f"{verb} {req_id} {self.slot} {queued_us} {service_us}\n", close)
        else:
            self.c["abandoned"] += 1
        self._account_busy()
        self.busy -= 1
        while self.queue and self.busy < self.capacity:
            qconn, qid, qclose, qenq = self.queue.popleft()
            self._start(qconn, qid, qclose, qenq)

    # --- control ---------------------------------------------------------
    def on_command(self, cmd):
        op = cmd["op"]
        if op == "config":
            self.capacity = lc.num(cmd, "capacity", 1, 256, self.capacity, integer=True)
            self.service_ms = lc.num(cmd, "serviceMs", 1, 2000, self.service_ms)
            self.service_cv = lc.num(cmd, "serviceCv", 0, 3, self.service_cv)
            self.queue_limit = lc.num(cmd, "queueLimit", 0, 4096, self.queue_limit, integer=True)
            self.error_rate = lc.num(cmd, "errorRate", 0, 1, self.error_rate)
            # A raised capacity admits queued work immediately; a lowered one only
            # affects new admissions - in-flight work is never pre-empted.
            while self.queue and self.busy < self.capacity:
                qconn, qid, qclose, qenq = self.queue.popleft()
                self._start(qconn, qid, qclose, qenq)
        elif op == "fault":
            self.slow_factor = lc.num(cmd, "slowFactor", 1, 50, 1.0)
            self.fault_error_rate = lc.num(cmd, "errorRate", 0, 1, 0.0)
        elif op == "clear":
            self.slow_factor = 1.0
            self.fault_error_rate = 0.0
        elif op == "reset_stats":
            self.reset_stats()

    def stats(self):
        self._account_busy()
        return {
            "type": "stats", "tMs": now_ms(), "slot": self.slot,
            "counters": dict(self.c),
            "busy": self.busy, "queued": len(self.queue), "openConns": self.open_conns,
            "capacity": self.capacity, "busyIntegralMs": self.busy_integral_ms,
            "config": {"capacity": self.capacity, "serviceMs": self.service_ms,
                       "serviceCv": self.service_cv, "queueLimit": self.queue_limit,
                       "errorRate": self.error_rate},
            "fault": {"slowFactor": self.slow_factor, "errorRate": self.fault_error_rate},
            "histBackend": self.hist_backend.take_delta(),
            "histService": self.hist_service.take_delta(),
            "respByPeer": dict(self.resp_by_peer),
        }


class Conn(asyncio.Protocol):
    def __init__(self, server):
        self.server = server
        self.transport = None
        self.buf = b""
        self.got_bytes = False
        self.alive = False
        self.peer_ip = "?"
        self.idle_handle = None

    def connection_made(self, transport):
        self.transport = transport
        self.alive = True
        peer = transport.get_extra_info("peername")
        self.peer_ip = peer[0] if peer else "?"
        self.server.open_conns += 1
        self._arm_idle()

    def _arm_idle(self):
        if self.idle_handle is not None:
            self.idle_handle.cancel()
        loop = asyncio.get_running_loop()
        self.idle_handle = loop.call_later(self.server.idle_timeout_s, self._on_idle)

    def _on_idle(self):
        if self.alive:
            self.transport.close()

    def data_received(self, data):
        if not self.got_bytes:
            self.got_bytes = True
            self.server.c["accepts"] += 1
        self._arm_idle()
        self.buf += data
        while True:
            nl = self.buf.find(b"\n")
            if nl < 0:
                if len(self.buf) > MAX_LINE:
                    self.transport.close()
                return
            line, self.buf = self.buf[:nl], self.buf[nl + 1:]
            parts = line.decode("ascii", "replace").split()
            if len(parts) != 3 or parts[0] != "REQ" or parts[2] not in ("close", "keep") \
                    or len(parts[1]) > 32:
                self.transport.close()
                return
            self.server.submit(self, parts[1], parts[2] == "close")

    def reply(self, text, close):
        if not self.alive:
            return
        self.transport.write(text.encode("ascii"))
        if close:
            self.transport.close()  # flushes first; we close first -> TIME_WAIT is ours

    def connection_lost(self, exc):
        self.alive = False
        self.server.open_conns -= 1
        if self.idle_handle is not None:
            self.idle_handle.cancel()
        # A zero-byte connection is a TCP-connect health probe (or a port scan),
        # not a request - never counted as one.
        if not self.got_bytes and self.peer_ip == self.server.lb_ip:
            self.server.c["probes"] += 1


async def main_async(args):
    loop = asyncio.get_running_loop()
    server = Server(args)
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    srv = await loop.create_server(lambda: Conn(server), args.bind, args.port,
                                   reuse_address=True, backlog=4096)
    lc.emit({"type": "ready", "slot": args.slot, "port": args.port, "pid": os.getpid()})

    if args.stdin_control:
        lc.spawn(lc.read_commands(server.on_command, stop.set))

    async def stats_loop():
        while True:
            await asyncio.sleep(args.stats_interval_ms / 1000.0)
            lc.emit(server.stats())

    stats_task = loop.create_task(stats_loop())
    await stop.wait()
    stats_task.cancel()
    srv.close()


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--port", type=int, default=9000)
    p.add_argument("--slot", type=int, required=True)
    p.add_argument("--lb-ip", default="10.98.0.1")
    p.add_argument("--capacity", type=int, default=8)
    p.add_argument("--service-ms", type=float, default=25.0)
    p.add_argument("--service-cv", type=float, default=0.6)
    p.add_argument("--queue-limit", type=int, default=64)
    p.add_argument("--error-rate", type=float, default=0.0)
    p.add_argument("--idle-timeout-s", type=float, default=60.0)
    p.add_argument("--stats-interval-ms", type=int, default=250)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--no-stdin-control", dest="stdin_control", action="store_false",
                   help="for manual runs: don't read commands from stdin (and don't "
                        "exit on stdin EOF)")
    return p.parse_args(argv)


def main():
    asyncio.run(main_async(parse_args()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
