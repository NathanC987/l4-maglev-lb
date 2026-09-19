#!/usr/bin/env python3
"""Concurrent "viewer" traffic generator for the M4 benchmark harness. Run
inside the bench client netns: sudo ip netns exec l4mlb-bench-client \
python3 viewer_gen.py VIP PORT [options]

Continuously launches new sessions up to --concurrency, each one connecting
to VIP:PORT, sending a short request line, then reading/timing/discarding
the paced response streaming_server.py sends back until it ends or the
connection drops. Records per-session time-to-first-byte (a proxy for
LB-added latency - connection setup through the Maglev lookup and GRE
encapsulation of the first data segment) and total bytes received.

Concurrency model is asyncio, not threads: each session's steady-state work
is one non-blocking read plus one sleep-equivalent wait per pacing interval
(tens to a few hundred ms - see streaming_server.py) - sparse, I/O-bound
work where the event loop spends nearly all its time parked in epoll, so a
single event loop handles hundreds of concurrent sessions without becoming
its own bottleneck ahead of the LB. See docs/benchmarking.md.

SIGUSR1 stops launching NEW sessions but lets already-open ones finish
naturally (the orchestration script's "wind-down" signal) - the process
then exits once the last session completes. SIGINT/SIGTERM stop immediately.

Exposes its own tiny Prometheus endpoint on --metrics-port (default 9106,
0 disables it) - the "viewer-side" perspective, meant to sit on the same
Grafana dashboard as maglev-lb's own metrics endpoint. See
monitoring/prometheus-bench.yml and docs/benchmarking.md.
"""
import argparse
import asyncio
import collections
import json
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Stats:
    def __init__(self, ttfb_window=2000):
        self.active_sessions = 0
        self.sessions_started = 0
        self.sessions_completed = collections.Counter()  # outcome -> count
        self.bytes_received = 0
        self.ttfb_samples = collections.deque(maxlen=ttfb_window)

    def snapshot_summary(self):
        ttfb_sorted = sorted(self.ttfb_samples)
        return {
            "active_sessions": self.active_sessions,
            "sessions_started": self.sessions_started,
            "sessions_completed": dict(self.sessions_completed),
            "bytes_received": self.bytes_received,
            "ttfb_p50": _percentile(ttfb_sorted, 0.50),
            "ttfb_p90": _percentile(ttfb_sorted, 0.90),
            "ttfb_p99": _percentile(ttfb_sorted, 0.99),
        }


def _percentile(sorted_vals, q):
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, int(q * len(sorted_vals)))
    return sorted_vals[idx]


def render_prometheus(stats: Stats) -> bytes:
    s = stats.snapshot_summary()
    lines = []

    def metric(name, mtype, help_text):
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {mtype}")

    metric("viewer_active_sessions", "gauge", "Sessions currently open and streaming.")
    lines.append(f"viewer_active_sessions {s['active_sessions']}")

    metric("viewer_sessions_started_total", "counter", "Sessions this generator has opened.")
    lines.append(f"viewer_sessions_started_total {s['sessions_started']}")

    metric(
        "viewer_sessions_completed_total",
        "counter",
        "Sessions that finished, by outcome (content_end/server_closed_early/connect_error).",
    )
    for outcome, count in s["sessions_completed"].items():
        lines.append(f'viewer_sessions_completed_total{{outcome="{outcome}"}} {count}')

    metric("viewer_bytes_received_total", "counter", "Total response bytes received from backends.")
    lines.append(f"viewer_bytes_received_total {s['bytes_received']}")

    metric(
        "viewer_ttfb_seconds",
        "gauge",
        "Time-to-first-byte percentiles over the last N completed sessions (a bounded window,"
        " not lifetime).",
    )
    for q, val in (("0.5", s["ttfb_p50"]), ("0.9", s["ttfb_p90"]), ("0.99", s["ttfb_p99"])):
        if val is not None:
            lines.append(f'viewer_ttfb_seconds{{quantile="{q}"}} {val:.6f}')

    return ("\n".join(lines) + "\n").encode()


def make_metrics_handler(stats: Stats):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = render_prometheus(stats)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *a):
            pass  # keep stdout to session-level prints only

    return Handler


def start_metrics_server(stats: Stats, port: int):
    if port == 0:
        return None
    server = ThreadingHTTPServer(("0.0.0.0", port), make_metrics_handler(stats))
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"viewer_gen: metrics on :{port}/metrics", flush=True)
    return server


async def run_session(vip, port, request_line, stats: Stats, max_session_s):
    stats.active_sessions += 1
    stats.sessions_started += 1
    outcome = "content_end"
    total_bytes = 0
    start = time.monotonic()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(vip, port), timeout=5.0
        )
    except (OSError, asyncio.TimeoutError):
        stats.active_sessions -= 1
        stats.sessions_completed["connect_error"] += 1
        return

    try:
        writer.write(request_line)
        await writer.drain()

        first_byte_recorded = False
        deadline = start + max_session_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                outcome = "server_closed_early"  # safety cap hit, not a clean end
                break
            try:
                chunk = await asyncio.wait_for(reader.read(65536), timeout=remaining)
            except asyncio.TimeoutError:
                outcome = "server_closed_early"
                break
            if not chunk:
                break  # clean EOF: the simulated "content" ended
            if not first_byte_recorded:
                stats.ttfb_samples.append(time.monotonic() - start)
                first_byte_recorded = True
            total_bytes += len(chunk)
    except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
        outcome = "server_closed_early"
    finally:
        stats.bytes_received += total_bytes
        stats.active_sessions -= 1
        stats.sessions_completed[outcome] += 1
        try:
            writer.close()
        except Exception:
            pass


async def supervisor(args, stats: Stats, stop_launching: asyncio.Event):
    request_line = args.request_line
    if not request_line.endswith("\n"):
        request_line += "\n"
    request_line = request_line.encode()
    tasks = set()

    while True:
        if stop_launching.is_set() and not tasks:
            break
        if not stop_launching.is_set() and len(tasks) < args.concurrency:
            t = asyncio.ensure_future(
                run_session(args.vip, args.port, request_line, stats, args.max_session_s)
            )
            tasks.add(t)
            # Small stagger so `concurrency` sessions don't all connect in
            # the same event-loop tick, spreading arrivals like real
            # traffic rather than a synchronized burst.
            await asyncio.sleep(args.arrival_jitter_s)
            continue
        if not tasks:
            await asyncio.sleep(0.1)
            continue
        done, tasks = await asyncio.wait(tasks, timeout=0.5, return_when=asyncio.FIRST_COMPLETED)


async def main_async(args):
    stats = Stats()
    start_metrics_server(stats, args.metrics_port)

    loop = asyncio.get_running_loop()
    stop_launching = asyncio.Event()
    stop_now = asyncio.Event()

    def on_wind_down():
        print("viewer_gen: SIGUSR1 received, no longer launching new sessions", flush=True)
        stop_launching.set()

    def on_stop():
        print("viewer_gen: stopping immediately", flush=True)
        stop_launching.set()
        stop_now.set()

    loop.add_signal_handler(signal.SIGUSR1, on_wind_down)
    loop.add_signal_handler(signal.SIGINT, on_stop)
    loop.add_signal_handler(signal.SIGTERM, on_stop)

    if args.max_runtime_s > 0:
        loop.call_later(args.max_runtime_s, on_wind_down)

    sup = asyncio.ensure_future(supervisor(args, stats, stop_launching))
    stopper = asyncio.ensure_future(stop_now.wait())
    await asyncio.wait({sup, stopper}, return_when=asyncio.FIRST_COMPLETED)

    summary = stats.snapshot_summary()
    print("viewer_gen: final summary: " + json.dumps(summary), flush=True)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("vip")
    p.add_argument("port", type=int)
    p.add_argument("--concurrency", type=int, default=200)
    p.add_argument("--metrics-port", type=int, default=9106)
    p.add_argument("--request-line", type=str, default="GET /video")
    p.add_argument("--max-session-s", type=float, default=90.0,
                    help="Safety cap: abandon a session that hasn't finished by then")
    p.add_argument("--arrival-jitter-s", type=float, default=0.05,
                    help="Delay between launching consecutive sessions while under the "
                         "concurrency cap, so arrivals spread out instead of bursting")
    p.add_argument("--max-runtime-s", type=float, default=1800.0,
                    help="Safety net: stop launching new sessions after this long even if "
                         "no SIGUSR1 arrives (0 disables)")
    return p.parse_args()


def main():
    args = parse_args()
    try:
        socket.inet_aton(args.vip)
    except OSError:
        print(f"invalid VIP: {args.vip}", file=sys.stderr)
        return 2
    asyncio.run(main_async(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
