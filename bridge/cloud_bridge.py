#!/usr/bin/env python3
"""The live canvas driving real EC2 instances instead of local network namespaces.
Run by cloud/canvas.sh, as your normal user (nothing here needs root on the laptop):

    python3 bridge/cloud_bridge.py --stack l4mlb --region us-east-1 --role-arn ARN ...

The canvas is the same and so is its API. Every node on the canvas is one of the stack's
instances: adding a backend starts one and runs service_server.py there; removing it
drains it, stops the process, and stops the instance. Each client and the LB own an
instance the same way. The pool (2 clients, 1 LB, 5 backends) is the account's 16-vCPU
quota exactly. See docs/cloud.md.

What limits the damage if something here is wrong:
  - It runs with short-lived credentials for a role that can only start and stop the
    instances tagged for this stack. It cannot launch one, so the pool in
    cloud/stack.yaml is a hard limit that holds in AWS, not in this code.
  - The browser never names anything in AWS. Canvas ids stay dictionary keys; instance
    ids come from AWS, and remote commands are built from fixed paths and validated
    numbers, each argument quoted.
  - Spending stops by itself: at --budget-usd or --max-minutes, --idle-stop-min after the
    last browser tab closes, or when this process exits. If it dies without stopping
    anything, each host's lease runs out (see cloud_hosts.py).
"""
import argparse
import asyncio
import json
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import livecommon as lc  # noqa: E402
from children import BACKEND_PORT, BaseExecutor, Proc, backend_args, client_args  # noqa: E402
import cloudwatch  # noqa: E402
import promexport  # noqa: E402
from cloud_hosts import HostManager  # noqa: E402
from reconciler import Reconciler  # noqa: E402
from httpapi import BridgeHTTPServer, Publisher  # noqa: E402
from live_bridge import AUTO_PAUSE_AFTER_S, HC, Bridge, now_ms  # noqa: E402

BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(BRIDGE_DIR)

# Must match cloud/stack.yaml and cloud/lib.sh.
VIP = "10.200.0.10"
LB_IP = "10.99.1.20"
CLIENT_IPS = ("10.99.1.10", "10.99.1.11")
CLIENT_ROLES = ("client", "client2")  # canvas client index -> its instance
ROLE_RE = re.compile(r"^(client2?|lb|be[1-5])$")
INSTANCE_RE = re.compile(r"^i-[0-9a-f]{8,32}$")
MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
REMOTE_REPO = "/home/ubuntu/l4-maglev-lb"
REMOTE_RUN = "/home/ubuntu/l4mlb-run"
STATS_INTERVAL_MS = 500   # half the local rate: this crosses the internet, and egress is billed
HOST_STATS_INTERVAL_MS = 1000
LEASE_MIN = 20
CREDS_SECONDS = 3600
CREDS_REFRESH_S = 2700
CONVERGE_TIMEOUT_S = 15.0
LB_RETRY_S = 5.0
TABLES_TIMEOUT_S = 3.0


def backend_ip(slot):
    return f"10.99.1.{30 + slot}"


class AwsError(Exception):
    pass


class AwsCli:
    """The few AWS calls the canvas makes, through the AWS CLI (the bridge stays
    stdlib-only). After assume_role() every call uses the role's temporary credentials,
    passed in that one process's environment; nothing logs or returns them."""

    def __init__(self, stack, region, role_arn, aws_bin="aws"):
        self.stack, self.region, self.role_arn, self.aws_bin = stack, region, role_arn, aws_bin
        self.creds = None
        self.creds_at = 0.0
        self.lock = threading.Lock()

    def _env(self, creds):
        env = {k: os.environ[k] for k in ("PATH", "HOME", "AWS_CA_BUNDLE") if k in os.environ}
        env["AWS_DEFAULT_REGION"] = self.region
        env["AWS_PAGER"] = ""
        if creds:
            env.update(AWS_ACCESS_KEY_ID=creds["AccessKeyId"],
                       AWS_SECRET_ACCESS_KEY=creds["SecretAccessKey"],
                       AWS_SESSION_TOKEN=creds["SessionToken"])
        else:
            for k in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
                if k in os.environ:
                    env[k] = os.environ[k]
        return env

    def _run(self, args, creds):
        try:
            r = subprocess.run([self.aws_bin, *args, "--output", "json"], env=self._env(creds),
                               stdin=subprocess.DEVNULL, capture_output=True, text=True,
                               timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise AwsError(f"aws {args[0]} {args[1]}: {e}") from e
        if r.returncode != 0:
            raise AwsError(f"aws {args[0]} {args[1]}: {r.stderr.strip()[:300]}")
        return r.stdout

    def assume_role(self):
        out = self._run(["sts", "assume-role", "--role-arn", self.role_arn, "--role-session-name",
                         "l4mlb-canvas", "--duration-seconds", str(CREDS_SECONDS)], None)
        with self.lock:
            self.creds = json.loads(out)["Credentials"]
            self.creds_at = time.monotonic()

    def _call(self, args):
        with self.lock:
            stale = time.monotonic() - self.creds_at > CREDS_REFRESH_S
        if stale:
            self.assume_role()
        with self.lock:
            creds = self.creds
        return self._run(args, creds)

    def describe(self):
        out = self._call(["ec2", "describe-instances", "--filters",
                          f"Name=tag:l4mlb-canvas,Values={self.stack}",
                          "Name=instance-state-name,Values=pending,running,stopping,stopped,shutting-down"])
        return parse_instances(json.loads(out))

    def start(self, ids):
        self._call(["ec2", "start-instances", "--instance-ids", *checked_ids(ids)])

    def stop(self, ids):
        self._call(["ec2", "stop-instances", "--instance-ids", *checked_ids(ids)])

    def metric_data(self, queries, start, end):
        """CloudWatch GetMetricData (read-only), for the Grafana dashboard."""
        return json.loads(self._call(cloudwatch.metric_data_args(queries, start, end)))


def checked_ids(ids):
    for i in ids:
        if not INSTANCE_RE.match(i):
            raise AwsError(f"refusing odd instance id {i!r}")
    return ids


def parse_instances(doc):
    out = []
    for res in doc.get("Reservations", []):
        for inst in res.get("Instances", []):
            tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
            role = tags.get("l4mlb-role", "")
            nics = inst.get("NetworkInterfaces") or [{}]
            if not ROLE_RE.match(role) or not INSTANCE_RE.match(inst.get("InstanceId", "")):
                continue
            mac = nics[0].get("MacAddress", "").lower()
            if not MAC_RE.match(mac):
                continue
            out.append({"role": role, "id": inst["InstanceId"], "type": inst.get("InstanceType", "?"),
                        "state": inst["State"]["Name"], "publicIp": inst.get("PublicIpAddress"),
                        "privateIp": inst.get("PrivateIpAddress"), "mac": mac})
    return out


def check_pool(infos):
    """The roles and addresses the rest of this file assumes, or a reason to refuse."""
    by_role = {i["role"]: i for i in infos}
    missing = {"client", "client2", "lb"} - set(by_role)
    backends = sorted(r for r in by_role if r.startswith("be"))
    if missing or not backends:
        return f"the stack has no {sorted(missing) or 'backends'}: run cloud/canvas.sh up"
    for r, i in by_role.items():
        want = {"client": CLIENT_IPS[0], "client2": CLIENT_IPS[1], "lb": LB_IP}.get(r) \
            or backend_ip(int(r[2:]))
        if i["privateIp"] != want:
            return f"{r} has address {i['privateIp']}, expected {want}"
    if backends != [f"be{n}" for n in range(1, len(backends) + 1)]:
        return f"backend hosts are not contiguous: {backends}"
    return None


class Ssh:
    """Commands over one multiplexed connection per host. Host keys were pinned by
    instance id when the stack was deployed, so they still verify after a stop and
    start hands the host a new public address, and a mismatch is refused."""

    def __init__(self, key, run_dir, known_hosts, ssh_bin="ssh"):
        self.key, self.run_dir, self.known_hosts, self.ssh_bin = key, run_dir, known_hosts, ssh_bin
        os.makedirs(self.run_dir, mode=0o700, exist_ok=True)

    def _env(self):
        return {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}

    def _control(self, host):
        return os.path.join(self.run_dir, f"cm-{host.role}")

    def _base(self, host):
        return [self.ssh_bin, "-F", "/dev/null", "-T", "-i", self.key,
                "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={self.known_hosts}",
                "-o", f"HostKeyAlias={host.id}", "-o", "ConnectTimeout=8",
                "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=6",
                "-o", "ForwardAgent=no", "-o", "ForwardX11=no", "-o", "Compression=yes",
                "-o", "LogLevel=ERROR", "-o", f"ControlPath={self._control(host)}"]

    def argv(self, host, remote):
        return [*self._base(host), "-o", "ControlMaster=no", f"ubuntu@{host.public_ip}", remote]

    def connect(self, host):
        self.disconnect(host)
        # To a file, not a pipe: the backgrounded master could hold a pipe open, and
        # waiting for it to close would hang here.
        with tempfile.TemporaryFile(dir=self.run_dir) as err:
            r = subprocess.run([*self._base(host), "-f", "-N", "-M", "-o", "ControlMaster=yes",
                                f"ubuntu@{host.public_ip}"], env=self._env(),
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                               timeout=30, check=False)
            if r.returncode != 0:
                err.seek(0)
                raise AwsError(f"ssh: {err.read().decode(errors='replace').strip()[:200]}")

    def disconnect(self, host):
        path = self._control(host)
        if os.path.exists(path):
            subprocess.run([*self._base(host), "-O", "exit", f"ubuntu@{host.public_ip}"],
                           env=self._env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10, check=False)
            try:
                os.unlink(path)
            except OSError:
                pass

    def run(self, host, remote, timeout=30):
        r = subprocess.run(self.argv(host, remote), env=self._env(), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=timeout, check=False)
        if r.returncode != 0:
            raise AwsError(f"ssh {host.role}: {r.stderr.strip()[:200]}")
        return r.stdout

    def popen(self, host, remote, stderr):
        return subprocess.Popen(self.argv(host, remote), env=self._env(), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=stderr, close_fds=True,
                                start_new_session=True)


class HostPrep:
    """What a host needs after every boot: routes and tunnels do not survive a stop."""

    def __init__(self, ssh):
        self.ssh = ssh

    def prepare(self, host, lease_min):
        self.ssh.connect(host)
        self.ssh.run(host, f"sudo -n shutdown -h +{int(lease_min)} l4mlb-lease && " + self._netsetup(host))

    def _netsetup(self, host):
        d = f"{REMOTE_REPO}/cloud/hostnet"
        if host.role == "lb":
            return f"sudo -n {d}/lb.sh"
        if host.role in CLIENT_ROLES:
            return f"sudo -n {d}/client.sh"
        return f"sudo -n {d}/backend.sh {LB_IP} {VIP}"

    def renew_lease(self, host, lease_min):
        self.ssh.run(host, f"sudo -n shutdown -h +{int(lease_min)} l4mlb-lease")

    def disconnect(self, host):
        self.ssh.disconnect(host)


class LbProc(Proc):
    def __init__(self, kind, key, popen, on_report):
        super().__init__(kind, key, popen)
        self.on_report = on_report
        self.ready = False

    def on_line(self, msg):
        kind = msg.get("type")
        if kind == "ready":
            self.ready = True
            self.remote_pid = msg.get("pid")
        self.on_report(self, msg)


class HostProc(Proc):
    """host_agent.py's channel: the instance's own CPU and network use."""

    def on_line(self, msg):
        if msg.get("type") == "host":
            self.latest = msg


class CloudExecutor(BaseExecutor):
    """Same interface as children.Executor, with each process at the far end of SSH. A
    closed stdin still means exit, so a dropped connection or a dead bridge stops them."""

    def __init__(self, loop, run_dir, ssh):
        super().__init__(loop, run_dir)
        self.ssh = ssh
        self.hosts = None  # set once HostManager exists
        self.lb = None

    def host_ready(self, slot):
        return self.hosts.ready(f"be{slot}")

    def client_host_ready(self, index):
        return self.hosts.ready(CLIENT_ROLES[index])

    def client_ip(self, index):
        return CLIENT_IPS[index]

    def backend_addr(self, slot):
        return f"{backend_ip(slot)}:{BACKEND_PORT}"

    def _spawn_remote(self, host, kind, key, script, args, logname, make_proc=None, sudo=False):
        # -B: never write __pycache__ into the checkout; the LB agent runs as root, and
        # root-owned cache files there would make the next deploy fail to replace it.
        cmd = shlex.join(["python3", "-I", "-B", f"{REMOTE_REPO}/bridge/{script}", *args])
        remote = "exec " + ("sudo -n " if sudo else "") + cmd
        with self._log(logname) as err:
            p = self.ssh.popen(host, remote, err)
        proc = make_proc(kind, key, p) if make_proc else Proc(kind, key, p)
        proc.host = host
        proc.kill_at = None
        self.procs.append(proc)
        lc.spawn(self._pump(proc))
        return proc

    def spawn_backend(self, slot, cfg):
        self.seed += 1
        args = backend_args(slot, cfg, LB_IP, self.seed) + ["--stats-interval-ms", str(STATS_INTERVAL_MS)]
        return self._spawn_remote(self.hosts.hosts[f"be{slot}"], "backend", slot, "service_server.py",
                                  args, f"backend-slot{slot}.log")

    def spawn_client(self, index, cfg, enabled):
        self.seed += 1
        args = client_args(cfg, enabled, VIP, CLIENT_IPS[index], self.seed) + \
            ["--stats-interval-ms", str(STATS_INTERVAL_MS)]
        return self._spawn_remote(self.hosts.hosts[CLIENT_ROLES[index]], "client", index,
                                  "load_client.py", args, f"client{index}.log")

    def spawn_host_agent(self, role):
        return self._spawn_remote(self.hosts.hosts[role], "host", role, "host_agent.py",
                                  ["--interval-ms", str(HOST_STATS_INTERVAL_MS)], f"host-{role}.log",
                                  make_proc=HostProc)

    def spawn_lb(self, args, on_report):
        return self._spawn_remote(self.hosts.hosts["lb"], "lb", 0, "lb_agent.py", args, "lb-agent.log",
                                  make_proc=lambda k, key, p: LbProc(k, key, p, on_report), sudo=True)

    def kill(self, proc):
        """SIGKILL at the far end, so "crash" is a real crash: killing the local ssh
        would only close stdin, and the process would exit politely. The pid is its own
        report over this channel, and is used as a number."""
        if proc is None or proc.p.poll() is not None:
            return
        now = time.monotonic()
        if proc.kill_at is not None:
            if now - proc.kill_at > 3.0:
                proc.p.kill()  # the host stopped answering; don't wait for it
            return
        proc.kill_at = now
        if isinstance(proc.remote_pid, int) and proc.remote_pid > 1:
            lc.spawn(asyncio.to_thread(self._remote_kill, proc.host, proc.remote_pid))
        else:
            proc.p.kill()

    def _remote_kill(self, host, pid):
        try:
            self.ssh.run(host, f"kill -9 {int(pid)}", timeout=10)
        except (AwsError, subprocess.TimeoutExpired):
            pass

    def lost(self, proc):
        if proc is None or proc.p.poll() != 255 or proc.kill_at is not None:
            return False  # ssh exits 255 for its own failures, else with the remote status
        self.hosts.mark_unreachable(proc.host.role)
        return True

    def admin(self, verb, lb_id):
        self.send(self.lb, {"op": "admin", "verb": verb, "id": int(lb_id)})

    def set_gre(self, slot, up):
        lc.spawn(asyncio.to_thread(self._set_gre, slot, up))

    def _set_gre(self, slot, up):
        word = "up" if up else "down"
        try:
            self.ssh.run(self.hosts.hosts[f"be{slot}"], f"sudo -n ip link set gre1 {word}", timeout=15)
        except (AwsError, subprocess.TimeoutExpired) as e:
            print(f"bridge: could not set gre1 {word} on be{slot}: {e}", file=sys.stderr)


class CloudBridge(Bridge):
    measured_ceiling_pps = 0  # only measured on the local machine, not on these hosts

    def make_executor(self):
        known = os.path.join(self.args.cloud_run_dir, "known_hosts_canvas")
        self.ssh = Ssh(self.args.ssh_key, os.path.join(self.args.cloud_run_dir, "ssh"), known,
                       self.args.ssh_bin)
        return CloudExecutor(self.loop, self.run_dir, self.ssh)

    def __init__(self, args, loop, infos, aws):
        super().__init__(args, loop)
        self.r = Reconciler(self.ex, args.slots, n_clients=args.clients)
        self.host_agents = {}
        self.host_agent_retry = {}
        self.run_dir = os.path.join(args.cloud_run_dir, "canvas")
        self.ex.run_dir = self.run_dir
        self.aws = aws
        self.hosts = HostManager(aws, HostPrep(self.ssh), infos, self.event,
                                 stop_grace_s=args.stop_grace_s, lease_min=LEASE_MIN)
        self.ex.hosts = self.hosts
        self.lb_agent = None
        self.tables_wait = None
        self.lb_pid_remote = None
        self.lb_ready_at = None
        self.lb_retry_at = 0.0
        self.converged = False
        self.session_began = time.monotonic()
        self.stop_reason = None
        self.metrics_httpd = None
        self.cw_targets = ()
        self.cloudwatch = cloudwatch.CloudWatchPoller(
            aws, lambda: self.cw_targets,
            lambda: self.metrics_httpd is not None and self.metrics_httpd.scraped_within(cloudwatch.WATCH_S))

    # --- the LB, run by its agent --------------------------------------------------
    def lb_alive(self):
        return self.lb_agent is not None and self.ex.alive(self.lb_agent) and self.lb_agent.ready

    def lb_pid(self):
        return self.lb_pid_remote if self.lb_alive() else None

    def lb_wanted(self):
        return self.r.desired["lb"] not in (None, "no-lb")

    def lb_state(self):
        if self.lb_alive():
            return "running"
        return "booting" if self.lb_wanted() else "stopped"

    def status_phase(self):
        if not self.lb_wanted():
            return "ready"
        if self.phase == "degraded":
            return "degraded"
        if not self.lb_alive():
            return "starting"
        return "ready" if self.converged else "converging"

    def lb_errors(self):
        return []

    def client_state_override(self, rec):
        if rec is None:
            return "pending"  # more clients on the canvas than client instances
        return "booting" if not self.hosts.ready(CLIENT_ROLES[rec.index]) else None

    def _lb_report(self, proc, msg):
        if proc is not self.lb_agent:
            return
        kind = msg.get("type")
        if kind == "ready":
            self.lb_pid_remote = msg.get("pid")
            self.lb_ready_at = time.monotonic()
            self.converged = False
            self.event("maglev-lb running on the LB host")
        elif kind == "exit":
            self.event(f"LB host: {msg.get('reason')}", "error")
        elif kind == "tables":
            if self.tables_wait is not None and not self.tables_wait.done():
                self.tables_wait.set_result(msg.get("doc"))
        elif kind == "lb":
            s = msg["metrics"]
            s["backends"] = {int(k): v for k, v in s.get("backends", {}).items()}
            self.ingest_lb(s, msg["tMs"], msg.get("cpu"), msg.get("pcpu"))

    def _start_agent(self):
        n = self.args.slots
        macs = [self.hosts.hosts[f"be{i}"].mac for i in range(1, n + 1)]
        args = ["--bin", f"{REMOTE_REPO}/build/maglev-lb", "--vip", VIP, "--director-ip", LB_IP,
                "--fifo", f"{REMOTE_RUN}/admin.fifo", "--log", f"{REMOTE_RUN}/maglev-lb.log",
                "--ct-timeout-ms", str(self.args.ct_timeout_ms),
                "--hc-interval-ms", str(HC["hcIntervalMs"]), "--hc-timeout-ms", str(HC["hcTimeoutMs"]),
                "--hc-rise", str(HC["hcRise"]), "--hc-fall", str(HC["hcFall"]),
                "--interval-ms", str(STATS_INTERVAL_MS)]
        for i, mac in enumerate(macs, 1):
            args += ["--backend", f"{backend_ip(i)}:{BACKEND_PORT}@{mac}"]
        self.lb_summary = None
        self.lb_obs = {}
        self.lb_rates.clear()
        self.cpu_samples = []
        self.lb_agent = self.ex.spawn_lb(args, self._lb_report)
        self.ex.lb = self.lb_agent

    def _step_lb(self):
        ready_host = self.hosts.ready("lb")
        alive = self.lb_agent is not None and self.ex.alive(self.lb_agent)
        if self.lb_agent is not None and not alive:
            self.ex.lost(self.lb_agent)
            self.lb_agent = self.ex.lb = None
            self.converged = False
            self.lb_summary, self.lb_obs = None, {}
            self.lb_retry_at = time.monotonic() + LB_RETRY_S
        if self.lb_wanted() and ready_host and self.lb_agent is None \
                and time.monotonic() >= self.lb_retry_at:
            self._start_agent()
        elif not self.lb_wanted() and self.lb_agent is not None:
            self.ex.terminate(self.lb_agent)
        # A backend starts out "healthy" inside maglev-lb until probes disprove it, so
        # nothing may be routed until the powered-off ones have been marked down. A
        # restarted maglev-lb has also forgotten any drains; give them back.
        if self.lb_alive() and not self.converged and self.lb_summary:
            owned = {s.lb_id for s in self.r.slots.values() if s.owner is not None}
            unowned = [i for i in range(self.args.slots) if i not in owned]
            timed_out = time.monotonic() - self.lb_ready_at > CONVERGE_TIMEOUT_S
            if timed_out or all(not self.lb_obs.get(i, {}).get("up") for i in unowned):
                self.converged = True
                for s in self.r.slots.values():
                    if s.owner is not None and (s.manual_drain or s.removing) and s.phase == "draining":
                        self.ex.admin("DRAIN", s.lb_id)

    async def fetch_lb_tables(self, gen):
        """One round trip to lb_agent.py, which asks maglev-lb on the LB host. Callers
        that arrive while one is in flight share its answer."""
        if self.tables_wait is None or self.tables_wait.done():
            self.tables_wait = self.loop.create_future()
            self.ex.send(self.lb_agent, {"op": "tables", "gen": gen})
        return await asyncio.wait_for(asyncio.shield(self.tables_wait), TABLES_TIMEOUT_S)

    def tables_source(self):
        h = self.hosts.hosts.get("lb")
        return {"where": "aws", "instance": h.id if h else None, "type": h.type if h else None}

    async def restart_lb(self):
        self.lb_restart_pending = False
        if self.lb_agent is not None:
            self.ex.terminate(self.lb_agent)
            self.event("restarting maglev-lb (conntrack state is lost)", "warn")

    async def request_cloud_stop(self):
        self.shut_down("stopped from the canvas")
        return {"accepted": True}

    def shut_down(self, reason):
        if self.stop_reason is None:
            self.stop_reason = reason
            self.event(f"stopping everything on AWS: {reason}", "warn")
            self.stop.set()

    # --- the loop --------------------------------------------------------------------
    def wanted_roles(self):
        d = self.r.desired
        want = {f"be{s.n}" for s in self.r.slots.values() if s.owner is not None}
        if self.lb_wanted():
            want.add("lb")
        connected = {c["id"] for c in d["clients"] if c["connected"]}
        for cid, rec in self.r.clients.items():
            if cid in connected:
                want.add(CLIENT_ROLES[rec.index])
        return want

    def _step_host_agents(self):
        """One small reporter per ready instance: its CPU and network, so the canvas shows
        load landing on the real machine and not only in our own processes' counters."""
        now = time.monotonic()
        for role in self.hosts.hosts:
            agent = self.host_agents.get(role)
            if self.hosts.ready(role):
                if (agent is None or not self.ex.alive(agent)) and now >= self.host_agent_retry.get(role, 0):
                    if agent is not None:
                        self.ex.lost(agent)
                    self.host_agents[role] = self.ex.spawn_host_agent(role)
                    self.host_agent_retry[role] = now + LB_RETRY_S
            elif agent is not None:
                self.ex.terminate(agent)
                del self.host_agents[role]

    def _session_left_s(self):
        return self.args.max_minutes * 60.0 - (time.monotonic() - self.session_began)

    def _check_limits(self, idle):
        if self.hosts.spent_usd >= self.args.budget_usd:
            self.shut_down(f"the session reached its ${self.args.budget_usd:.2f} budget")
        elif self._session_left_s() <= 0:
            self.shut_down(f"the session reached its {self.args.max_minutes} minute limit")
        elif idle > self.args.idle_stop_min * 60.0:
            self.shut_down(f"no browser connected for {self.args.idle_stop_min} minutes")

    async def tick(self):
        idle = self.publisher.seconds_without_streams()
        self._check_limits(idle)
        self.hosts.tick(time.monotonic(), self.wanted_roles())
        self._step_lb()
        self._step_host_agents()
        paused = idle > AUTO_PAUSE_AFTER_S
        hold = self.lb_wanted() and not (self.lb_alive() and self.converged)
        if paused != getattr(self, "was_paused", False):
            self.was_paused = paused
            self.event("no browser connected for 10s: load paused" if paused
                       else "browser connected: load resumed")
        self.r.load_suppressed = paused or hold
        self.r.step(now_ms(), self.lb_obs)
        self.ex.reap()
        body = json.dumps(self.build_snapshot(), separators=(",", ":"))
        self.publisher.publish(body.encode())
        if self.record:
            self.record.write(body + "\n")
            self.record.flush()

    def build_snapshot(self):
        snap = super().build_snapshot()
        # Waiting for hosts isn't "autoPaused": that word means nobody is watching.
        snap["status"]["autoPaused"] = getattr(self, "was_paused", False)
        hosts = self.hosts.hosts

        def view(role):
            h = hosts.get(role)
            if h is None:
                return None
            v = {"role": role, "id": h.id, "type": h.type, "state": h.phase}
            agent = self.host_agents.get(role)
            st = agent.latest if agent is not None and self.ex.alive(agent) and h.phase == "ready" else None
            if st:
                v.update(cpu=st.get("cpu"), rxBps=st.get("rxBps"), txBps=st.get("txBps"))
            return v
        for b in snap["backends"].values():
            if b["slot"] is not None:
                b["host"] = view(f"be{b['slot']}")
        for cid, c in snap["clients"].items():
            rec = self.r.clients.get(cid)
            c["host"] = view(CLIENT_ROLES[rec.index]) if rec else None
        snap["lb"]["host"] = view("lb")
        # What CloudWatch is asked about: instances that are up (read by its thread).
        self.cw_targets = tuple(sorted((r, h.id) for r, h in hosts.items()
                                       if h.state in ("pending", "running") and h.id))
        snap["cloud"] = {
            "provider": "aws", "region": self.args.region, "stack": self.args.stack,
            "maxBackends": self.args.slots, "maxClients": self.args.clients,
            "hosts": [view(r) for r in sorted(hosts)],
            "running": sum(1 for h in hosts.values() if h.state in ("pending", "running")),
            "usdPerHour": round(self.hosts.usd_per_hour(), 4),
            "spentUsd": round(self.hosts.spent_usd, 4),
            "budgetUsd": self.args.budget_usd,
            "sessionLeftS": max(0, round(self._session_left_s())),
            "stopping": self.stop_reason,
        }
        return snap

    def start_metrics(self):
        """The Grafana feed (bridge/promexport.py). Optional: a busy port costs only
        the dashboard."""
        if not self.args.metrics_port:
            return
        try:
            self.metrics_httpd = promexport.MetricsServer(self.args.metrics_port, self.publisher,
                                                          self.cloudwatch)
        except OSError as e:
            print(f"metrics for Grafana: port {self.args.metrics_port} unavailable ({e.strerror}); "
                  "the dashboard will be empty", flush=True)
            return
        threading.Thread(target=self.metrics_httpd.serve_forever, daemon=True).start()
        self.cloudwatch.start()
        print(f"metrics for Grafana: http://127.0.0.1:{self.args.metrics_port}/metrics "
              "(cloud/canvas.sh grafana)", flush=True)

    async def run(self):
        token = self.args.token or secrets.token_hex(16)
        httpd = BridgeHTTPServer(self.args.port, token, self.loop, self, self.publisher,
                                 self.args.static_dir, self.args.dev_origin)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.start_metrics()
        self.phase = "ready"
        print(f"live bridge ready: http://127.0.0.1:{self.args.port}/#token={token}", flush=True)
        print("Nothing on AWS starts until that page is open.", flush=True)
        if self.args.token_file:
            fd = os.open(self.args.token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(token)
        leftover = []
        try:
            while not self.stop.is_set():
                await self.tick()
                try:
                    await asyncio.wait_for(self.stop.wait(), 0.25)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.phase = "stopping"
            self.publisher.publish(json.dumps(self.build_snapshot(), separators=(",", ":")).encode())
            self.publisher.close()
            httpd.shutdown()
            if self.metrics_httpd is not None:
                self.cloudwatch.stop.set()
                self.metrics_httpd.shutdown()
            self.ex.stop_all()
            print("stopping every instance...", flush=True)
            leftover = await self.hosts.stop_all()
            if self.record:
                self.record.close()
        print(f"estimated spend this session: ${self.hosts.spent_usd:.2f}", flush=True)
        if leftover:
            print(f"NOT confirmed stopped: {', '.join(leftover)}. They stop themselves within "
                  f"{LEASE_MIN} minutes; check with cloud/status.sh", flush=True)
            return 1
        print("every instance is stopped", flush=True)
        return 0


def parse_args():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--stack", required=True)
    p.add_argument("--region", required=True)
    p.add_argument("--role-arn", required=True)
    p.add_argument("--ssh-key", required=True)
    p.add_argument("--cloud-run-dir", default=os.path.join(REPO_ROOT, "cloud", ".run"))
    p.add_argument("--port", type=int, default=8790)
    p.add_argument("--static-dir")
    p.add_argument("--dev-origin", action="append", default=[])
    p.add_argument("--ct-timeout-ms", type=int, default=15000)
    p.add_argument("--budget-usd", type=float, default=3.0,
                   help="stop everything when the session's estimated spend reaches this")
    p.add_argument("--max-minutes", type=int, default=120,
                   help="stop everything after this long")
    p.add_argument("--idle-stop-min", type=int, default=10,
                   help="stop everything after this long with no browser connected")
    p.add_argument("--stop-grace-s", type=float, default=60.0,
                   help="keep an instance nobody wants for this long before stopping it")
    p.add_argument("--metrics-port", type=int, default=promexport.DEFAULT_PORT,
                   help="serve read-only Prometheus metrics on 127.0.0.1:PORT for "
                        "cloud/canvas.sh grafana (0: off)")
    p.add_argument("--token", help=argparse.SUPPRESS)  # for automated tests only
    p.add_argument("--token-file")
    p.add_argument("--record")
    p.add_argument("--aws-bin", default="aws", help=argparse.SUPPRESS)  # for automated tests only
    p.add_argument("--ssh-bin", default="ssh", help=argparse.SUPPRESS)  # for automated tests only
    a = p.parse_args()
    if os.geteuid() == 0:
        p.error("run this as your normal user: it uses your AWS CLI sign-in and SSH key, and "
                "needs no root on this machine")
    if not re.match(r"^[A-Za-z][A-Za-z0-9-]{0,127}$", a.stack):
        p.error("bad --stack")
    if not re.match(r"^[a-z]{2}(-[a-z]+)+-\d$", a.region):
        p.error("bad --region")
    if not re.match(r"^arn:aws:iam::\d{12}:role/[\w+=,.@/-]+$", a.role_arn):
        p.error("bad --role-arn")
    if not os.path.isfile(a.ssh_key):
        p.error(f"SSH key {a.ssh_key} not found")
    if not 0 <= a.metrics_port <= 65535 or (a.metrics_port and a.metrics_port == a.port):
        p.error("bad --metrics-port")
    if not 0 < a.budget_usd <= 50:
        p.error("--budget-usd must be in (0, 50]")
    if not 1 <= a.max_minutes <= 720:
        p.error("--max-minutes must be 1..720")
    if a.idle_stop_min < 1:
        p.error("--idle-stop-min must be at least 1")
    if a.token is not None and (len(a.token) < 16 or not a.token.isalnum()):
        p.error("--token must be >= 16 alphanumeric characters")
    return a


async def assume_with_retry(aws, tries=8):
    """A role created moments ago can take a few seconds to become assumable."""
    for i in range(tries):
        try:
            aws.assume_role()
            return
        except AwsError:
            if i == tries - 1:
                raise
            await asyncio.sleep(3)


def main():
    args = parse_args()
    aws = AwsCli(args.stack, args.region, args.role_arn, args.aws_bin)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(assume_with_retry(aws))
        infos = aws.describe()
    except AwsError as e:
        raise SystemExit(f"cannot use the stack's AWS role: {e}") from e
    why = check_pool(infos)
    if why:
        raise SystemExit(why)
    args.slots = sum(1 for i in infos if i["role"].startswith("be"))
    args.clients = sum(1 for i in infos if i["role"] in CLIENT_ROLES)
    bridge = CloudBridge(args, loop, infos, aws)
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: bridge.shut_down("the bridge was told to exit"))
    try:
        rc = loop.run_until_complete(bridge.run())
    finally:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for t in pending:
            t.cancel()
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
