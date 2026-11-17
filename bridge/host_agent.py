#!/usr/bin/env python3
"""Runs on each instance of the AWS canvas and reports the machine's own CPU and network
use, one JSON line per interval, to the bridge on the operator's laptop:

    python3 -I host_agent.py [--interval-ms 1000]

    {"type": "host", "cpu": 0.12, "rxBps": ..., "txBps": ...}

cpu is the share of all of the instance's vCPUs busy over the interval, from
/proc/stat, so it counts everything the machine does, not only our processes. The
network counters are its primary interface's. It exits when stdin closes.
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time


def cpu_times():
    with open("/proc/stat") as f:
        v = [int(x) for x in f.readline().split()[1:]]
    idle = v[3] + (v[4] if len(v) > 4 else 0)  # idle + iowait
    return sum(v[:8]), idle  # guest time is already inside user


def net_bytes(iface):
    with open("/proc/net/dev") as f:
        for line in f:
            name, _, rest = line.partition(":")
            if name.strip() == iface:
                v = rest.split()
                return int(v[0]), int(v[8])
    return 0, 0


def default_iface():
    out = subprocess.run(["ip", "-o", "route", "show", "default"], capture_output=True, text=True,
                         timeout=5, check=False).stdout.split()
    return out[out.index("dev") + 1] if "dev" in out else "lo"


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--interval-ms", type=int, default=1000)
    args = p.parse_args()
    if not 100 <= args.interval_ms <= 60000:
        p.error("--interval-ms must be 100..60000")
    stop = threading.Event()
    threading.Thread(target=lambda: (sys.stdin.read(), stop.set()), daemon=True).start()
    iface = default_iface()
    t0, (c0, i0), (r0, x0) = time.monotonic(), cpu_times(), net_bytes(iface)
    while not stop.wait(args.interval_ms / 1000.0):
        t1, (c1, i1), (r1, x1) = time.monotonic(), cpu_times(), net_bytes(iface)
        dt, dc = t1 - t0, c1 - c0
        msg = {"type": "host", "tMs": t1 * 1000.0, "pid": os.getpid(),
               "cpu": round(1.0 - (i1 - i0) / dc, 4) if dc > 0 else 0.0,
               "rxBps": round((r1 - r0) / dt) if dt > 0 else 0,
               "txBps": round((x1 - x0) / dt) if dt > 0 else 0}
        try:
            sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
            sys.stdout.flush()
        except BrokenPipeError:
            return 0
        t0, c0, i0, r0, x0 = t1, c1, i1, r1, x1
    return 0


if __name__ == "__main__":
    sys.exit(main())
