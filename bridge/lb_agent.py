#!/usr/bin/env python3
"""Runs maglev-lb on the LB host of the AWS canvas, for the bridge on the operator's
laptop, which reaches it over one SSH channel:

    sudo python3 -I lb_agent.py --backend IP:PORT@MAC ... (see parse_args)

Reports maglev-lb's metrics and datapath CPU as JSON lines on stdout. A single channel
that pushes is what makes this work across an ocean: the bridge polls a local
maglev-lb four times a second, but one scrape over a 200 ms link takes longer than
the 0.5 s it allows, and its CPU counters are in this host's /proc anyway.

Reads {"op": "admin", "verb": "DRAIN"|"UNDRAIN", "id": N} on stdin, and
{"op": "tables", "gen": N|null}, which it answers with {"type": "tables", "doc": ...}:
maglev-lb's /tables document (see lbtables.py), or null if it did not answer. When stdin closes
(the bridge exited, or the connection dropped) it stops maglev-lb and exits, and
pdeathsig stops maglev-lb if this process is killed outright.
"""
import argparse
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lbmetrics  # noqa: E402
import lbtables  # noqa: E402

BACKEND_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}:\d{1,5}@[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
ADMIN_VERBS = {"DRAIN", "UNDRAIN"}
READY_TIMEOUT_S = 20.0
STOP_TIMEOUT_S = 5.0


_emit_lock = threading.Lock()


def emit(obj):
    # Two threads write here: the report loop and the command reader.
    with _emit_lock:
        sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def default_iface():
    out = subprocess.run(["ip", "-o", "route", "show", "default"], capture_output=True,
                         text=True, timeout=5, check=True).stdout.split()
    return out[out.index("dev") + 1]


def make_fifo(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.lexists(path):
        os.unlink(path)
    os.mkfifo(path, 0o600)


def admin_line(msg, n_backends):
    """The FIFO line for a command, or None if it is not a valid one."""
    if not isinstance(msg, dict) or msg.get("op") != "admin" or msg.get("verb") not in ADMIN_VERBS:
        return None
    bid = msg.get("id")
    if isinstance(bid, bool) or not isinstance(bid, int) or not 0 <= bid < n_backends:
        return None
    return f"{msg['verb']} {bid}\n"


def write_admin(path, line):
    # maglev-lb holds its end open, so this never waits for a reader; O_NONBLOCK
    # guards against a dead LB instead of hanging.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return
    try:
        if stat.S_ISFIFO(os.fstat(fd).st_mode):
            os.write(fd, line.encode())
    finally:
        os.close(fd)


def exporter_url(args, path):
    return f"http://{args.metrics_host or args.director_ip}:{args.metrics_port}{path}"


def tables_gen(msg):
    """The generation in a tables request: None to ask for the table too, False if
    this is not a tables request at all."""
    if not isinstance(msg, dict) or msg.get("op") != "tables":
        return False
    gen = msg.get("gen")
    return gen if isinstance(gen, int) and not isinstance(gen, bool) and gen >= 0 else None


def send_tables(url, gen):
    try:
        doc = lbtables.fetch(url, gen, 1.0)
    except (OSError, ValueError):
        doc = None
    emit({"type": "tables", "doc": doc})


def read_commands(args, stop):
    for raw in sys.stdin:
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        gen = tables_gen(msg)
        if gen is not False:
            send_tables(exporter_url(args, "/tables"), gen)
            continue
        line = admin_line(msg, len(args.backend))
        if line:
            write_admin(args.fifo, line)
    stop.set()


def lb_argv(args):
    argv = ["setpriv", "--pdeathsig", "TERM", args.bin, "--iface", args.iface or default_iface(),
            "--vip", args.vip, "--director-ip", args.director_ip]
    for b in args.backend:
        argv += ["--backend", b]
    return argv + ["--hc-interval-ms", str(args.hc_interval_ms), "--hc-timeout-ms",
                   str(args.hc_timeout_ms), "--hc-rise", str(args.hc_rise), "--hc-fall",
                   str(args.hc_fall), "--admin-fifo", args.fifo, "--metrics-port",
                   str(args.metrics_port), "--ct-tcp-timeout-ms", str(args.ct_timeout_ms),
                   "--ct-udp-timeout-ms", str(args.ct_timeout_ms)]


def wait_ready(proc, log_path, stop):
    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline and not stop.is_set():
        if proc.poll() is not None:
            return False
        with open(log_path, "rb") as f:
            if b"starting datapath" in f.read():
                return True
        time.sleep(0.1)
    return False


def report(proc, url):
    try:
        summary = lbmetrics.summarize(lbmetrics.fetch(url, 1.0))
    except OSError:
        return
    emit({"type": "lb", "tMs": time.monotonic() * 1000.0, "pid": proc.pid, "metrics": summary,
          "cpu": lbmetrics.thread_cpu_seconds(proc.pid),
          "pcpu": lbmetrics.process_cpu_seconds(proc.pid)})


def parse_args():
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--bin", required=True)
    p.add_argument("--iface")
    p.add_argument("--vip", required=True)
    p.add_argument("--director-ip", required=True)
    p.add_argument("--backend", action="append", required=True, help="IP:PORT@MAC")
    p.add_argument("--fifo", required=True)
    p.add_argument("--log", required=True)
    p.add_argument("--metrics-host", help="where maglev-lb's exporter answers (default: --director-ip)")
    p.add_argument("--metrics-port", type=int, default=9105)
    p.add_argument("--ct-timeout-ms", type=int, default=15000)
    p.add_argument("--hc-interval-ms", type=int, default=500)
    p.add_argument("--hc-timeout-ms", type=int, default=300)
    p.add_argument("--hc-rise", type=int, default=2)
    p.add_argument("--hc-fall", type=int, default=2)
    p.add_argument("--interval-ms", type=int, default=500)
    a = p.parse_args()
    for b in a.backend:
        if not BACKEND_RE.match(b):
            p.error(f"--backend must be IP:PORT@MAC, got {b!r}")
    return a


def main():
    args = parse_args()
    make_fifo(args.fifo)
    stop = threading.Event()
    threading.Thread(target=read_commands, args=(args, stop), daemon=True).start()
    os.makedirs(os.path.dirname(args.log), exist_ok=True)
    with open(args.log, "wb") as log:
        proc = subprocess.Popen(lb_argv(args), stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    try:
        if not wait_ready(proc, args.log, stop):
            emit({"type": "exit", "reason": "maglev-lb did not start"})
            return 1
        emit({"type": "ready", "pid": proc.pid})
        url = exporter_url(args, "/metrics")
        while not stop.wait(args.interval_ms / 1000.0):
            if proc.poll() is not None:
                emit({"type": "exit", "reason": f"maglev-lb exited ({proc.returncode})"})
                return 1
            report(proc, url)
        return 0
    except BrokenPipeError:
        return 0
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())
