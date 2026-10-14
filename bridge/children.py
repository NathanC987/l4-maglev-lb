"""The real executor behind reconciler.Reconciler: spawns and signals child
processes inside the bench topology's network namespaces, and writes admin
commands to maglev-lb's FIFO.

Every command is an argv list built from fixed absolute paths, slot numbers and
already-validated numbers - never a shell, and never a canvas id or label.

Process model: `ip netns exec <ns> setpriv --pdeathsig TERM [--reuid/--regid
<invoking user> --clear-groups --no-new-privs] <program>`. Both `ip netns exec`
and setpriv exec in place, so the Popen pid IS the program's pid. Responders and
load clients drop root (only maglev-lb needs it, for raw sockets); they read
commands from stdin and exit when it closes, and pdeathsig is a second safety
net if the bridge dies without closing it. All spawning happens on the asyncio
loop thread, because pdeathsig fires when the *thread* that forked exits.
"""
import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys

import livecommon as lc
from windows import LatencyWindow, RateWindow, flatten

BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
LB_IP = "10.98.0.1"
VIP = "10.98.0.100"
BACKEND_PORT = 9000
NS_CLIENT = "l4mlb-bench-client"
NS_LB = "l4mlb-bench-lb"


def be_ns(slot):
    return f"l4mlb-bench-be{slot}"


def client_ip(index):
    return f"10.98.0.{200 + index}"


def tool(name):
    path = shutil.which(name, path="/usr/sbin:/usr/bin:/sbin:/bin")
    if path is None:
        raise SystemExit(f"required tool not found: {name}")
    return path


class Proc:
    def __init__(self, kind, key, popen):
        self.kind, self.key, self.p = kind, key, popen
        self.latest = None
        self.rates = RateWindow(1000.0)
        self.lat = LatencyWindow(5000.0)
        self.lat_service = LatencyWindow(5000.0)

    @property
    def pid(self):
        return self.p.pid

    def on_line(self, msg):
        if msg.get("type") != "stats":
            return
        self.latest = msg
        t = msg["tMs"]
        if self.kind == "backend":
            flat = flatten(msg, "respByPeer")
            flat["busyIntegralMs"] = msg.get("busyIntegralMs", 0.0)
            self.rates.push(t, flat)
            self.lat.push(t, msg.get("histBackend"))
            self.lat_service.push(t, msg.get("histService"))
        else:
            self.rates.push(t, flatten(msg, "failedByReason", "respBySlot"))
            self.lat.push(t, msg.get("hist"))

    def reset_windows(self):
        self.rates.clear()
        self.lat.clear()
        self.lat_service.clear()


class Executor:
    def __init__(self, loop, run_dir, admin_fifo, drop_uid, drop_gid):
        self.loop = loop
        self.run_dir = run_dir
        self.admin_fifo = admin_fifo
        self.ip = tool("ip")
        self.setpriv = tool("setpriv")
        self.drop = ["--reuid", str(drop_uid), "--regid", str(drop_gid),
                     "--clear-groups", "--no-new-privs"]
        self.procs = []
        self.seed = 1

    def _log(self, name):
        os.makedirs(os.path.join(self.run_dir, "logs"), exist_ok=True)
        return open(os.path.join(self.run_dir, "logs", name), "ab")

    def _spawn_python(self, kind, key, ns, script, args, logname):
        argv = [self.ip, "netns", "exec", ns, self.setpriv, "--pdeathsig", "TERM", *self.drop,
                sys.executable, "-I", os.path.join(BRIDGE_DIR, script), *args]
        with self._log(logname) as err:
            p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=err, close_fds=True, start_new_session=True)
        proc = Proc(kind, key, p)
        self.procs.append(proc)
        lc.spawn(self._pump(proc))
        return proc

    async def _pump(self, proc):
        reader = asyncio.StreamReader(limit=1 << 20)
        transport, _ = await self.loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), proc.p.stdout)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                try:
                    proc.on_line(json.loads(line))
                except ValueError:
                    pass
        finally:
            transport.close()

    def spawn_backend(self, slot, cfg):
        self.seed += 1
        args = ["--slot", str(slot), "--lb-ip", LB_IP, "--port", str(BACKEND_PORT),
                "--capacity", str(int(cfg["capacity"])), "--service-ms", repr(float(cfg["serviceMs"])),
                "--service-cv", repr(float(cfg["serviceCv"])),
                "--queue-limit", str(int(cfg["queueLimit"])),
                "--error-rate", repr(float(cfg["errorRate"])), "--seed", str(self.seed)]
        return self._spawn_python("backend", slot, be_ns(slot), "service_server.py", args,
                                  f"backend-slot{slot}.log")

    def spawn_client(self, index, cfg, enabled):
        self.seed += 1
        args = ["--vip", VIP, "--port", str(BACKEND_PORT), "--src-ip", client_ip(index),
                "--rps", repr(float(cfg["rps"])), "--timeout-ms", repr(float(cfg["timeoutMs"])),
                "--retries", str(int(cfg["retries"])), "--pool-size", str(int(cfg["poolSize"])),
                "--max-conn-age-ms", str(int(cfg["maxConnAgeMs"])), "--seed", str(self.seed)]
        if not enabled:
            args.append("--paused")
        return self._spawn_python("client", index, NS_CLIENT, "load_client.py", args,
                                  f"client{index}.log")

    def send(self, proc, obj):
        if proc is None or proc.p.poll() is not None:
            return
        try:
            proc.p.stdin.write((json.dumps(obj) + "\n").encode())
            proc.p.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            pass

    def terminate(self, proc):
        if proc is not None and proc.p.poll() is None:
            try:
                proc.p.stdin.close()  # the normal shutdown signal
            except OSError:
                pass
            proc.p.send_signal(signal.SIGTERM)

    def kill(self, proc):
        if proc is not None and proc.p.poll() is None:
            proc.p.kill()

    def alive(self, proc):
        return proc is not None and proc.p.poll() is None

    def latest(self, proc):
        return proc.latest if proc is not None else None

    def admin(self, verb, lb_id):
        # maglev-lb keeps its own end open O_RDWR, so this never blocks waiting
        # for a reader; O_NONBLOCK guards against a dead LB instead of hanging.
        try:
            fd = os.open(self.admin_fifo, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            return
        try:
            os.write(fd, f"{verb} {int(lb_id)}\n".encode())
        finally:
            os.close(fd)

    def set_gre(self, slot, up):
        subprocess.run([self.ip, "netns", "exec", be_ns(slot), self.ip, "link", "set", "gre1",
                        "up" if up else "down"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                       check=False)

    def reap(self):
        """Forget exited processes (poll() already reaped them)."""
        self.procs = [p for p in self.procs if p.p.poll() is None]

    def stop_all(self, timeout=3.0):
        for p in self.procs:
            self.terminate(p)
        for p in self.procs:
            try:
                p.p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                p.p.kill()
                p.p.wait()
