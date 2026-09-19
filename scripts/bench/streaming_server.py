#!/usr/bin/env python3
"""Asymmetric-bandwidth "video" backend responder for the M4 benchmark
harness - NOT used by the M1-M3 test suite, which keeps using the simple
instant-echo scripts/echo_server.py unchanged.

On a TCP connection, reads one short request line, then streams back fixed
64 KiB chunks at a randomized simulated bitrate for a randomized simulated
session duration - deliberately paced (not a bulk transfer), so a "5 second"
stream actually takes close to 5 seconds, giving conntrack entries a
realistic lifetime and demonstrating DSR's core advantage: this process
pushes megabytes directly to the client while maglev-lb, which only ever
forwards the tiny request and the client's ACKs, sees almost none of it.
See docs/benchmarking.md for the default bitrate/duration ranges and why.

Must tolerate maglev-lb's health checker, which opens a TCP connection and
closes it immediately without sending anything (src/backend/health_checker.c
does a bare non-blocking connect() + close(), never any application data) -
that shows up here as an empty read or a timeout waiting for the request
line, and is handled silently, not as an error.

Usage: streaming_server.py BIND_IP PORT LABEL
       [--bitrate-min-kbps N] [--bitrate-max-kbps N]
       [--duration-min-s N] [--duration-max-s N]
"""
import argparse
import asyncio
import os
import random
import sys
import time

CHUNK_BYTES = 65536
REQUEST_TIMEOUT_S = 2.0


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("bind_ip")
    p.add_argument("port", type=int)
    p.add_argument("label")
    p.add_argument("--bitrate-min-kbps", type=float, default=2000.0)
    p.add_argument("--bitrate-max-kbps", type=float, default=6000.0)
    p.add_argument("--duration-min-s", type=float, default=8.0)
    p.add_argument("--duration-max-s", type=float, default=30.0)
    return p.parse_args()


async def handle_session(reader, writer, args, chunk_buf, label):
    peer = writer.get_extra_info("peername")
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=REQUEST_TIMEOUT_S)
    except asyncio.TimeoutError:
        writer.close()
        return
    if not line:
        # EOF with no data: a health-check probe (connect then close), or an
        # abandoned client. Not an error - just nothing to stream.
        writer.close()
        return

    bitrate_kbps = random.uniform(args.bitrate_min_kbps, args.bitrate_max_kbps)
    duration_s = random.uniform(args.duration_min_s, args.duration_max_s)
    interval_s = (CHUNK_BYTES * 8) / (bitrate_kbps * 1000)

    print(
        f"{label}: session from {peer} bitrate={bitrate_kbps:.0f}kbps "
        f"duration={duration_s:.1f}s",
        flush=True,
    )

    start = time.monotonic()
    sent_bytes = 0
    try:
        while time.monotonic() - start < duration_s:
            writer.write(chunk_buf)
            await writer.drain()
            sent_bytes += len(chunk_buf)
            await asyncio.sleep(interval_s)
    except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
        pass  # client disconnected mid-stream - not a server-wide error
    finally:
        elapsed = time.monotonic() - start
        print(
            f"{label}: session from {peer} ended after {elapsed:.1f}s, "
            f"{sent_bytes} bytes",
            flush=True,
        )
        try:
            writer.close()
        except Exception:
            pass


async def main_async(args):
    chunk_buf = os.urandom(CHUNK_BYTES)  # one buffer, reused for every chunk/session -
    # payload content is never inspected downstream, only byte counts

    async def on_connect(reader, writer):
        await handle_session(reader, writer, args, chunk_buf, args.label)

    server = await asyncio.start_server(on_connect, args.bind_ip, args.port)
    print(f"{args.label}: listening on {args.bind_ip}:{args.port}", flush=True)
    async with server:
        await server.serve_forever()


def main():
    args = parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
