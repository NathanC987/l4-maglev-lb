#!/usr/bin/env python3
"""Live-mode bridge: lets a browser canvas (the breakscale fork's Live mode)
drive the real maglev-lb. Run as root via sudo:

    sudo python3 bridge/live_bridge.py [--static-dir <fork>/dist]

It builds the bench topology with a pool of backend slots, starts maglev-lb,
then reconciles the canvas's desired state (PUT /api/v1/desired) into real
processes - load clients, backend responders - and streams real measurements
back (GET /api/v1/stream) four times a second. See docs/live-mode.md.
"""
import argparse
import asyncio
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lbmetrics  # noqa: E402
import livecommon as lc  # noqa: E402
import schema  # noqa: E402
from children import (LB_IP, NS_CLIENT, NS_LB, VIP, BACKEND_PORT, Executor,  # noqa: E402
                      client_ip, tool)
from httpapi import BridgeHTTPServer, Publisher  # noqa: E402
from reconciler import ActionError, Reconciler  # noqa: E402
from windows import RateWindow  # noqa: E402

BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(BRIDGE_DIR)
BENCH_DIR = os.path.join(REPO_ROOT, "scripts", "bench")
METRICS_URL = f"http://{LB_IP}:9105/metrics"
TICK_S = 0.25
HC = {"hcIntervalMs": 500, "hcTimeoutMs": 300, "hcRise": 2, "hcFall": 2}
AUTO_PAUSE_AFTER_S = 10.0
BEHIND_LAG_MS = 50.0
FAILURE_REASONS = ("timeout", "shed", "error", "crashed", "no-route", "client-overload")


def now_ms():
    return time.monotonic() * 1000.0


def run(argv, **kw):
    return subprocess.run(argv, stdin=subprocess.DEVNULL, check=False, timeout=120, **kw)


class Bridge:
    def __init__(self, args, loop):
        self.args = args
        self.loop = loop
        self.run_dir = os.path.join(BRIDGE_DIR, ".run")
        self.admin_fifo = os.path.join(self.run_dir, "admin.fifo")
        self.ip = tool("ip")
        self.setpriv = tool("setpriv")
        self.ex = Executor(loop, self.run_dir, self.admin_fifo, args.drop_uid, args.drop_gid)
        self.r = Reconciler(self.ex, args.slots)
        self.publisher = Publisher()
        self.stop = asyncio.Event()
        self.lb_proc = None
        self.lb_summary = None
        self.lb_obs = {}
        self.lb_rates = RateWindow(1000.0)
        self.cpu_samples = []
        self.phase = "starting"
        self.errors = []
        self.events = []
        self.seq = 0
        self.session_id = secrets.token_hex(8)
        self.session_start = now_ms()
        self.lb_baseline = {}
        self.lb_restart_pending = False
        self.record = open(args.record, "a") if args.record else None

    def event(self, msg, level="info"):
        self.events.append({"tMs": now_ms() - self.session_start, "level": level, "msg": msg})
        print(f"bridge: {msg}", file=sys.stderr)

    # --- topology & maglev-lb ------------------------------------------------------
    def preflight(self):
        out = run([self.ip, "netns", "list"], capture_output=True, text=True).stdout
        if NS_LB in out:
            if not self.args.replace_existing:
                raise SystemExit(
                    "bench topology already exists (another bridge, or "
                    "scripts/bench/demo/run-streaming-demo.sh, is using it). Stop that first, "
                    "or pass --replace-existing to tear it down and rebuild.")
            self.teardown_topology()

    def setup_topology(self):
        os.makedirs(os.path.join(self.run_dir, "logs"), exist_ok=True)
        with open(os.path.join(self.run_dir, "logs", "topology.log"), "ab") as log:
            env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                   "BENCH_N_BACKENDS": str(self.args.slots)}
            rc = run([os.path.join(BENCH_DIR, "setup-bench-netns.sh")], env=env,
                     stdout=log, stderr=log).returncode
            if rc != 0:
                raise SystemExit("setup-bench-netns.sh failed; see bridge/.run/logs/topology.log")
            for k in range(4):
                run([self.ip, "netns", "exec", NS_CLIENT, self.ip, "addr", "add",
                     f"{client_ip(k)}/24", "dev", "bench-cli"], stdout=log, stderr=log)
            # The default 32768-60999 range is ~28k ports; with a connection per
            # request, give clients the widest sensible range.
            run([self.ip, "netns", "exec", NS_CLIENT, tool("sysctl"), "-qw",
                 "net.ipv4.ip_local_port_range=15000 65000"], stdout=log, stderr=log)
        if os.path.exists(self.admin_fifo):
            os.unlink(self.admin_fifo)
        os.mkfifo(self.admin_fifo, 0o600)

    def teardown_topology(self):
        os.makedirs(os.path.join(self.run_dir, "logs"), exist_ok=True)
        with open(os.path.join(self.run_dir, "logs", "topology.log"), "ab") as log:
            run([os.path.join(BENCH_DIR, "teardown-bench-netns.sh")],
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}, stdout=log, stderr=log)

    def start_lb(self):
        argv = [self.ip, "netns", "exec", NS_LB, self.setpriv, "--pdeathsig", "TERM",
                self.args.bin, "--iface", "bench-lb", "--vip", VIP, "--director-ip", LB_IP]
        for i in range(1, self.args.slots + 1):
            argv += ["--backend", f"10.98.0.{10 + i}:{BACKEND_PORT}"]
        argv += ["--hc-interval-ms", str(HC["hcIntervalMs"]), "--hc-timeout-ms",
                 str(HC["hcTimeoutMs"]), "--hc-rise", str(HC["hcRise"]),
                 "--hc-fall", str(HC["hcFall"]), "--admin-fifo", self.admin_fifo,
                 "--metrics-port", "9105",
                 "--ct-tcp-timeout-ms", str(self.args.ct_timeout_ms),
                 "--ct-udp-timeout-ms", str(self.args.ct_timeout_ms)]
        self.lb_log = os.path.join(self.run_dir, "logs", "maglev-lb.log")
        with open(self.lb_log, "wb") as log:
            self.lb_proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log,
                                            stderr=log, start_new_session=True)

    async def wait_lb_ready(self):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.lb_proc.poll() is not None:
                raise SystemExit(f"maglev-lb exited early; see {self.lb_log}")
            with open(self.lb_log, "rb") as f:
                if b"starting datapath" in f.read():
                    return
            await asyncio.sleep(0.2)
        raise SystemExit(f"maglev-lb did not become ready; see {self.lb_log}")

    async def wait_unowned_slots_down(self):
        """New backends default to healthy=true inside maglev-lb until probes
        disprove it, so a dormant slot looks eligible for ~fall x interval after
        startup. Don't route any traffic until every unowned slot is DOWN."""
        self.phase = "converging"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            await self.poll_lb()
            owned = {s.lb_id for s in self.r.slots.values() if s.owner is not None}
            unowned = [i for i in range(self.args.slots) if i not in owned]
            if self.lb_obs and all(not self.lb_obs.get(i, {}).get("up") for i in unowned):
                return
            await asyncio.sleep(0.2)
        self.event("convergence timed out; continuing", "warn")

    async def restart_lb(self):
        self.lb_restart_pending = False
        self.event("restarting maglev-lb (conntrack state is lost)", "warn")
        if self.lb_proc and self.lb_proc.poll() is None:
            self.lb_proc.terminate()
            await asyncio.to_thread(self.lb_proc.wait, 5)
        self.start_lb()
        await self.wait_lb_ready()
        await self.wait_unowned_slots_down()
        self.phase = "ready"
        self.event("maglev-lb restarted")

    # --- polling ---------------------------------------------------------------------
    async def poll_lb(self):
        try:
            text = await asyncio.to_thread(lbmetrics.fetch, METRICS_URL, 0.5)
        except OSError:
            return
        s = lbmetrics.summarize(text)
        self.lb_summary = s
        self.lb_obs = {bid: {"up": b.get("up", 0) >= 1, "adminEnabled": b.get("adminEnabled", 0) >= 1,
                             "draining": b.get("draining", 0) >= 1}
                       for bid, b in s["backends"].items()}
        flat = {k: s[k] for k in ("rxPackets", "txPackets", "rxBytes", "txBytes", "ctEvictions")}
        for k, v in s["drops"].items():
            flat[f"drops:{k}"] = v
        for k, v in s["lookups"].items():
            flat[f"lookups:{k}"] = v
        for bid, b in s["backends"].items():
            flat[f"be:{bid}:flowsAssigned"] = b.get("flowsAssigned", 0)
            flat[f"be:{bid}:packets"] = b.get("packets", 0)
            flat[f"be:{bid}:hcFailures"] = b.get("hcFailures", 0)
        t = now_ms()
        self.lb_rates.push(t, flat)
        if self.lb_proc and self.lb_proc.poll() is None:
            cpu = lbmetrics.thread_cpu_seconds(self.lb_proc.pid)
            pcpu = lbmetrics.process_cpu_seconds(self.lb_proc.pid)
            if cpu is not None:
                self.cpu_samples.append((t, cpu, pcpu or 0.0))
                self.cpu_samples = [x for x in self.cpu_samples if x[0] >= t - 1500]

    def _cpu(self):
        if len(self.cpu_samples) < 2:
            return 0.0, 0.0
        (t0, c0, p0), (t1, c1, p1) = self.cpu_samples[0], self.cpu_samples[-1]
        dt = (t1 - t0) / 1000.0
        return (max(0.0, (c1 - c0) / dt), max(0.0, (p1 - p0) / dt)) if dt > 0 else (0.0, 0.0)

    # --- commands (run on the loop thread, called from HTTP threads) -------------------
    async def apply_desired(self, desired):
        self.r.set_desired(desired)
        self.r.step(now_ms(), self.lb_obs)
        plan = {"backends": {b["id"]: (self.r.slot_of(b["id"]).phase if self.r.slot_of(b["id"])
                                        else ("pending" if b["id"] in self.r.pending else "detached"))
                             for b in desired["backends"]},
                "clients": sorted(c["id"] for c in desired["clients"])}
        return {"accepted": True, "plan": plan}

    async def apply_action(self, act):
        try:
            self.r.action(act, now_ms())
        except ActionError as e:
            return 409, {"error": str(e)}
        self.r.step(now_ms(), self.lb_obs)
        return 200, {"ok": True}

    async def reset_session(self):
        self.r.reset(now_ms())
        for p in self.ex.procs:
            p.reset_windows()
        self.lb_rates.clear()
        self.session_id = secrets.token_hex(8)
        self.session_start = now_ms()
        self.lb_baseline = dict(self.lb_summary["drops"]) if self.lb_summary else {}
        self.event("session reset")
        return {"sessionId": self.session_id}

    async def request_lb_restart(self):
        if not self.lb_restart_pending:
            self.lb_restart_pending = True
            lc.spawn(self.restart_lb())
        return {"accepted": True}

    # --- snapshot --------------------------------------------------------------------
    def build_snapshot(self):
        t = now_ms()
        d = self.r.desired
        s = self.lb_summary or {"drops": {}, "backends": {}}
        rates = self.lb_rates
        cpu_dp, cpu_proc = self._cpu()
        lb_alive = self.lb_proc is not None and self.lb_proc.poll() is None
        owner_by_slot = {sl.n: sl.owner for sl in self.r.slots.values() if sl.owner}
        client_by_ip = {client_ip(rec.index): cid for cid, rec in self.r.clients.items()}

        snap = {
            "schema": schema.SCHEMA, "sessionId": self.session_id, "seq": self.seq,
            "tMs": round(t - self.session_start, 1),
            "rateWindowMs": 1000, "latencyWindowMs": 5000,
            "status": {"phase": self.phase if lb_alive else "degraded",
                       "loadEnabled": d["loadEnabled"] and not self.r.load_suppressed,
                       "autoPaused": self.r.load_suppressed,
                       "errors": list(self.errors) + ([] if lb_alive else ["maglev-lb is not running"])
                       + ([f"no free backend slot for {sorted(self.r.pending)}"] if self.r.pending else [])},
            "lb": {
                "id": d["lb"], "state": "running" if lb_alive else "stopped",
                "pid": self.lb_proc.pid if self.lb_proc else None,
                "cpuDatapath": round(cpu_dp, 4), "cpuProcess": round(cpu_proc, 4),
                "rxPps": rates.rate("rxPackets"), "txPps": rates.rate("txPackets"),
                "rxBps": rates.rate("rxBytes"), "txBps": rates.rate("txBytes"),
                "dropsPps": rates.rates_with_prefix("drops:"),
                "dropsTotal": {k: v - self.lb_baseline.get(k, 0) for k, v in s["drops"].items()},
                "conntrack": {"activeFlows": s.get("activeFlows", 0),
                              "hitsPs": rates.rate("lookups:hit"),
                              "missesPs": rates.rate("lookups:miss"),
                              "evictionsPs": rates.rate("ctEvictions")},
                "tableGeneration": s.get("tableGeneration", 0), "regenLastUs": s.get("regenLastUs", 0),
                "backendsEligible": s.get("backendsEligible", 0),
                "config": {"slots": self.args.slots, **HC,
                           "ctTcpTimeoutMs": self.args.ct_timeout_ms,
                           "ctUdpTimeoutMs": self.args.ct_timeout_ms, "tableSize": 65537,
                           "measuredCeilingPps": 520000},
            },
            "clients": {}, "backends": {}, "retiring": [], "events": [],
        }

        for c in d["clients"]:
            rec = self.r.clients.get(c["id"])
            proc = rec.handle if rec else None
            st = proc.latest if proc and proc.latest else {}
            alive = self.ex.alive(proc)
            if not c["connected"]:
                state = "disconnected"
            elif not alive:
                state = "exited"
            else:
                state = "running" if st.get("loadEnabled") else "paused"
            pr = proc.rates if proc else RateWindow()
            counters = st.get("counters", {})
            trace = st.get("lastTrace")
            if trace:
                trace = {**trace, "backend": owner_by_slot.get(trace.get("slot"))}
            lag = st.get("lagMsMax", 0.0)
            snap["clients"][c["id"]] = {
                "ip": client_ip(rec.index) if rec else None, "state": state,
                "config": {k: c[k] for k in ("rps", "timeoutMs", "retries", "poolSize", "maxConnAgeMs")},
                "rates": {k: pr.rate(k) for k in ("scheduled", "attempts", "ok", "failed", "retries")},
                "failedPsByReason": pr.rates_with_prefix("failedByReason:"),
                "totals": {"requests": counters.get("scheduled", 0), "ok": counters.get("ok", 0),
                           "failed": counters.get("failed", 0),
                           "failedByReason": st.get("failedByReason", {})},
                "latencyMs": proc.lat.summary() if proc else {"p50": 0, "p95": 0, "p99": 0, "n": 0},
                "inFlight": st.get("inFlight", 0), "lagMs": lag, "behind": lag > BEHIND_LAG_MS,
                "openConns": st.get("openConns", 0),
                "respPsByBackend": {owner_by_slot[int(k)]: v
                                    for k, v in pr.rates_with_prefix("respBySlot:").items()
                                    if k.isdigit() and int(k) in owner_by_slot},
                "lastTrace": trace,
            }

        for b in d["backends"]:
            slot = self.r.slot_of(b["id"])
            cfg = {k: b[k] for k in ("capacity", "serviceMs", "serviceCv", "queueLimit", "errorRate")}
            if slot is None:
                phase = "pending" if b["id"] in self.r.pending else "detached"
                snap["backends"][b["id"]] = {"slot": None, "lbId": None, "addr": None,
                                             "phase": phase, "config": cfg, "fault": None}
                continue
            proc = slot.handle
            alive = self.ex.alive(proc)
            # A dead process reports nothing. Its last stats line and rate
            # window are frozen at the moment it died, and showing them would
            # draw a crashed backend as busy and still replying.
            live_proc = proc if alive else None
            st = live_proc.latest if live_proc and live_proc.latest else {}
            pr = live_proc.rates if live_proc else RateWindow()
            lbb = s["backends"].get(slot.lb_id, {})
            busy_rate = pr.rate("busyIntegralMs")
            cap = st.get("capacity", cfg["capacity"]) or 1
            snap["backends"][b["id"]] = {
                "slot": slot.n, "lbId": slot.lb_id, "addr": f"10.98.0.{10 + slot.n}:{BACKEND_PORT}",
                "phase": slot.phase,
                "lb": {"up": lbb.get("up", 0) >= 1, "adminEnabled": lbb.get("adminEnabled", 0) >= 1,
                       "draining": lbb.get("draining", 0) >= 1,
                       "activeFlows": lbb.get("activeFlows", 0),
                       "flowsAssignedPs": rates.rate(f"be:{slot.lb_id}:flowsAssigned"),
                       "pps": rates.rate(f"be:{slot.lb_id}:packets"),
                       # A rate, not maglev-lb's lifetime total: a slot that sat
                       # dormant for an hour has thousands of failures that say
                       # nothing about the backend running in it now.
                       "hcFailuresPs": rates.rate(f"be:{slot.lb_id}:hcFailures")},
                "proc": {"alive": alive, "pid": proc.pid if alive else None},
                "config": cfg,
                "fault": ({**slot.fault, "sinceMs": round(slot.fault["sinceMs"] - self.session_start, 1)}
                          if slot.fault else None),
                "rates": {"arrivals": pr.rate("arrivals"), "ok": pr.rate("ok"),
                          "errors": pr.rate("err"), "shed": pr.rate("shed"),
                          "probes": pr.rate("probes"), "accepts": pr.rate("accepts")},
                "totals": {k: st.get("counters", {}).get(src, 0) for k, src in
                           (("arrivals", "arrivals"), ("ok", "ok"), ("errors", "err"), ("shed", "shed"))},
                "inFlight": st.get("busy", 0), "queued": st.get("queued", 0),
                "utilization": min(1.0, busy_rate / 1000.0 / cap),
                "openConns": st.get("openConns", 0),
                "latencyMs": (live_proc.lat.summary() if live_proc
                              else {"p50": 0, "p95": 0, "p99": 0, "n": 0}),
                "serviceMs": (live_proc.lat_service.summary() if live_proc
                              else {"p50": 0, "p95": 0, "p99": 0, "n": 0}),
                "respPsToClient": {client_by_ip[k]: v
                                   for k, v in pr.rates_with_prefix("respByPeer:").items()
                                   if k in client_by_ip},
            }

        # System-wide latency: merge every client's histogram window. Averaging
        # per-client percentiles would be wrong (percentiles don't average).
        merged = [0] * lc.HIST_BUCKETS
        for c in d["clients"]:
            rec = self.r.clients.get(c["id"])
            if rec and rec.handle:
                for i, v in enumerate(rec.handle.lat.counts):
                    merged[i] += v
        snap["system"] = {"latencyMs": {
            "p50": round(lc.percentile(merged, 0.5) or 0.0, 3),
            "p95": round(lc.percentile(merged, 0.95) or 0.0, 3),
            "p99": round(lc.percentile(merged, 0.99) or 0.0, 3),
            "n": sum(merged)}}

        attached = {b["id"] for b in d["backends"]}
        for sl in self.r.slots.values():
            if sl.owner is not None and sl.removing and sl.owner not in attached:
                # Off the canvas but still finishing pinned connections: the
                # LB really is still sending it traffic, and that has to show
                # up somewhere or the LB's numbers stop adding up.
                p = sl.handle if self.ex.alive(sl.handle) else None
                snap["retiring"].append({"slot": sl.n, "formerId": sl.owner, "phase": sl.phase,
                                         "arrivalsPs": p.rates.rate("arrivals") if p else 0.0,
                                         "okPs": p.rates.rate("ok") if p else 0.0})

        snap["events"] = [{**e, "tMs": round(e["tMs"] - self.session_start, 1)}
                          for e in self.r.take_events()] + self.events
        self.events = []
        self.seq += 1
        return snap

    # --- main loop -------------------------------------------------------------------
    async def tick(self):
        await self.poll_lb()
        idle = self.publisher.seconds_without_streams()
        suppress = not self.args.no_auto_pause and idle > AUTO_PAUSE_AFTER_S
        if suppress != self.r.load_suppressed:
            self.r.load_suppressed = suppress
            self.event("no browser connected for 10s: load paused" if suppress
                       else "browser connected: load resumed")
        if self.lb_proc.poll() is not None and self.phase == "ready":
            self.phase = "degraded"
            self.event("maglev-lb exited unexpectedly", "error")
        self.r.step(now_ms(), self.lb_obs)
        self.ex.reap()
        body = json.dumps(self.build_snapshot(), separators=(",", ":"))
        self.publisher.publish(body.encode())
        if self.record:
            self.record.write(body + "\n")
            self.record.flush()

    async def run(self):
        self.preflight()
        self.setup_topology()
        self.start_lb()
        await self.wait_lb_ready()
        await self.wait_unowned_slots_down()
        self.phase = "ready"

        token = self.args.token or secrets.token_hex(16)
        httpd = BridgeHTTPServer(self.args.port, token, self.loop, self, self.publisher,
                                 self.args.static_dir, self.args.dev_origin)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        print(f"live bridge ready: http://127.0.0.1:{self.args.port}/#token={token}", flush=True)
        if self.args.token_file:
            fd = os.open(self.args.token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(token)
            os.chown(self.args.token_file, self.args.drop_uid, self.args.drop_gid)

        try:
            while not self.stop.is_set():
                await self.tick()
                try:
                    await asyncio.wait_for(self.stop.wait(), TICK_S)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.phase = "stopping"
            self.publisher.close()
            httpd.shutdown()
            self.ex.stop_all()
            if self.lb_proc and self.lb_proc.poll() is None:
                self.lb_proc.terminate()
                try:
                    self.lb_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.lb_proc.kill()
            self.teardown_topology()
            if os.path.exists(self.admin_fifo):
                os.unlink(self.admin_fifo)
            if self.record:
                self.record.close()
            print("live bridge stopped; topology torn down", flush=True)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--bin", default=os.path.join(REPO_ROOT, "build", "maglev-lb"))
    p.add_argument("--port", type=int, default=8790)
    p.add_argument("--slots", type=int, default=16, help="backend slot pool size (1-64)")
    p.add_argument("--ct-timeout-ms", type=int, default=15000,
                   help="maglev-lb conntrack idle timeout for this session (see docs)")
    p.add_argument("--static-dir", help="serve a built frontend bundle from this directory")
    p.add_argument("--dev-origin", action="append", default=[],
                   help="extra allowed Origin, e.g. http://localhost:5173 for the Vite dev server")
    p.add_argument("--token", help=argparse.SUPPRESS)  # fixed token, for automated tests only
    p.add_argument("--token-file", help="also write the token here (mode 0600, owned by you)")
    p.add_argument("--record", help="append every snapshot (JSON lines) to this file")
    p.add_argument("--replace-existing", action="store_true",
                   help="tear down an existing bench topology instead of refusing to start")
    p.add_argument("--no-auto-pause", action="store_true",
                   help="keep load running even with no browser connected")
    a = p.parse_args()
    if os.geteuid() != 0:
        p.error("must run as root (network namespaces, raw sockets) - use sudo")
    if not 1 <= a.slots <= 64:
        p.error("--slots must be 1..64")
    if not 1 <= a.ct_timeout_ms <= 3600000:
        p.error("--ct-timeout-ms must be 1..3600000")
    if a.token is not None and (len(a.token) < 16 or not a.token.isalnum()):
        p.error("--token must be >= 16 alphanumeric characters")
    try:
        a.drop_uid, a.drop_gid = int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"])
    except (KeyError, ValueError):
        p.error("run via sudo, so child processes can drop root to your user")
    if a.drop_uid == 0:
        p.error("refusing to run children as root; run sudo from a normal user")
    if not os.access(a.bin, os.X_OK):
        p.error(f"maglev-lb binary not found at {a.bin} (build it first)")
    return a


def main():
    args = parse_args()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bridge = Bridge(args, loop)
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, bridge.stop.set)
    try:
        loop.run_until_complete(bridge.run())
    finally:
        # Children are gone by now; their stdout readers are still waiting.
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for t in pending:
            t.cancel()
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
