"""The canvas's numbers as Prometheus metrics, for the Grafana dashboard that
cloud/canvas.sh grafana starts (monitoring/docker-compose-canvas.yml).

Everything here is rendered from the snapshot the canvas itself is sent, so
Grafana and the canvas can never disagree; on AWS, CloudWatch's own view of the
instances (bridge/cloudwatch.py) is added beside it.

Read-only, and separate from the bridge's API: it listens on 127.0.0.1 only,
needs no token (Prometheus has no way to learn a per-run one), and answers only
GET /metrics with a Host header naming itself, which defeats DNS rebinding. It
shows rates, states, instance ids and the spend estimate; no token, address
outside the VPC, or credential is ever in it.
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = 9109


def _esc(v):
    return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # NaN never leaves here


class _Out:
    """Collects samples per family, so each family's HELP and TYPE come once."""

    def __init__(self):
        self.families = {}

    def add(self, name, help_, value, labels=None, kind="gauge"):
        v = _num(value)
        if v is None:
            return
        fam = self.families.setdefault(name, (help_, kind, []))
        lab = ""
        if labels:
            lab = "{" + ",".join(f'{k}="{_esc(x)}"' for k, x in labels.items() if x is not None) + "}"
        fam[2].append(f"{name}{lab} {v:g}")

    def text(self):
        lines = []
        for name, (help_, kind, samples) in self.families.items():
            lines += [f"# HELP {name} {help_}", f"# TYPE {name} {kind}", *samples]
        return "\n".join(lines) + "\n"


def render(snap, cloudwatch=None):
    """Prometheus text for one canvas snapshot (and CloudWatch's samples, on AWS)."""
    o = _Out()
    status = snap.get("status", {})
    o.add("canvas_up", "1 while the bridge is publishing snapshots.", 1)
    o.add("canvas_load_enabled", "1 while the clients are sending load.",
          1 if status.get("loadEnabled") else 0)
    o.add("canvas_load_auto_paused", "1 while load is paused because no canvas tab is open.",
          1 if status.get("autoPaused") else 0)
    o.add("canvas_errors", "Problems the canvas's status line is showing.",
          len(status.get("errors", [])))

    # --- the whole system, as the clients see it ------------------------------------
    clients = snap.get("clients", {})
    total = {"offered": 0.0, "ok": 0.0, "failed": 0.0}
    for cid, c in clients.items():
        rates = c.get("rates", {})
        lab = {"client": cid, "ip": c.get("ip")}
        for k in ("scheduled", "ok", "failed", "retries"):
            o.add("canvas_client_requests_per_second", "Requests per second by one client, by result "
                  "(scheduled = offered load).", rates.get(k, 0), {**lab, "result": k})
        o.add("canvas_client_latency_p99_ms", "One client's p99 request latency over 5 s.",
              c.get("latencyMs", {}).get("p99"), lab)
        o.add("canvas_client_open_connections", "Connections one client has open.",
              c.get("openConns"), lab)
        total["offered"] += _num(rates.get("scheduled")) or 0.0
        total["ok"] += _num(rates.get("ok")) or 0.0
        total["failed"] += _num(rates.get("failed")) or 0.0
    for k, v in total.items():
        o.add("canvas_requests_per_second", "Requests per second across every client "
              "(offered, ok, failed).", v, {"result": k})
    lat = snap.get("system", {}).get("latencyMs", {})
    for q in ("p50", "p95", "p99"):
        o.add("canvas_latency_ms", "System request latency over 5 s, every client merged.",
              lat.get(q), {"quantile": q})

    # --- backends -------------------------------------------------------------------
    for bid, b in snap.get("backends", {}).items():
        slot = b.get("slot")
        addr = b.get("addr")
        lab = {"backend": bid, "host": f"be{slot}" if slot is not None else None,
               "addr": addr.rsplit(":", 1)[0] if addr else None}
        o.add("canvas_backend_info", "A backend on the canvas and the phase it is in (1 per backend).",
              1, {**lab, "phase": b.get("phase")})
        if slot is None:
            continue
        rates = b.get("rates", {})
        for k in ("arrivals", "ok", "errors", "shed"):
            o.add("canvas_backend_requests_per_second", "Requests per second at one backend, by result.",
                  rates.get(k, 0), {**lab, "result": k})
        o.add("canvas_backend_latency_p99_ms", "One backend's p99 time in system over 5 s.",
              b.get("latencyMs", {}).get("p99"), lab)
        o.add("canvas_backend_utilization", "Share of one backend's capacity busy (0-1).",
              b.get("utilization"), lab)
        o.add("canvas_backend_queued", "Requests waiting at one backend.", b.get("queued"), lab)
        lb = b.get("lb") or {}
        o.add("canvas_backend_lb_up", "1 if maglev-lb's health check says the backend is up.",
              1 if lb.get("up") else 0, lab)
        o.add("canvas_backend_lb_draining", "1 while maglev-lb is draining the backend.",
              1 if lb.get("draining") else 0, lab)
        o.add("canvas_backend_lb_active_flows", "Connections maglev-lb has pinned to the backend.",
              lb.get("activeFlows"), lab)
        o.add("canvas_backend_lb_packets_per_second", "Packets per second maglev-lb sends the backend.",
              lb.get("pps"), lab)
        o.add("canvas_backend_lb_new_flows_per_second", "New connections per second maglev-lb "
              "assigns the backend.", lb.get("flowsAssignedPs"), lab)

    # --- maglev-lb --------------------------------------------------------------------
    lb = snap.get("lb", {})
    if lb:
        o.add("canvas_lb_info", "The load balancer and its state (1).", 1,
              {"node": lb.get("id"), "state": lb.get("state")})
        for k, name in (("rxPps", "rx"), ("txPps", "tx")):
            o.add("canvas_lb_packets_per_second", "Packets per second through maglev-lb.",
                  lb.get(k), {"direction": name})
        for k, name in (("rxBps", "rx"), ("txBps", "tx")):
            o.add("canvas_lb_bytes_per_second", "Bytes per second through maglev-lb.",
                  lb.get(k), {"direction": name})
        cpu_help = "Share of one core maglev-lb uses (datapath thread, whole process)."
        o.add("canvas_lb_cpu_ratio", cpu_help, lb.get("cpuDatapath"), {"part": "datapath"})
        o.add("canvas_lb_cpu_ratio", cpu_help, lb.get("cpuProcess"), {"part": "process"})
        for reason, v in (lb.get("dropsPps") or {}).items():
            o.add("canvas_lb_drops_per_second", "Packets per second maglev-lb drops, by reason.",
                  v, {"reason": reason})
        ct = lb.get("conntrack", {})
        o.add("canvas_lb_conntrack_active_flows", "Connections in maglev-lb's conntrack table.",
              ct.get("activeFlows"))
        for k, name in (("hitsPs", "hit"), ("missesPs", "miss"), ("evictionsPs", "eviction")):
            o.add("canvas_lb_conntrack_per_second", "Conntrack lookups per second (hit, miss) "
                  "and evictions.", ct.get(k), {"result": name})
        o.add("canvas_lb_table_generation", "Maglev table generation: +1 per rebuild.",
              lb.get("tableGeneration"))
        o.add("canvas_lb_table_regen_last_microseconds", "How long the last Maglev rebuild took.",
              lb.get("regenLastUs"))
        o.add("canvas_lb_backends_eligible", "Backends in the Maglev table.", lb.get("backendsEligible"))

    # --- AWS --------------------------------------------------------------------------
    cloud = snap.get("cloud")
    if cloud:
        o.add("canvas_aws_info", "The CloudFormation stack and region the canvas drives (1).", 1,
              {"stack": cloud.get("stack"), "region": cloud.get("region")})
        for h in cloud.get("hosts", []):
            if not h:
                continue
            lab = {"role": h.get("role"), "instance_id": h.get("id"), "type": h.get("type")}
            o.add("canvas_aws_instance_state", "Each EC2 instance's phase (1): stopped, starting, "
                  "ready or stopping.", 1, {**lab, "state": h.get("state")})
            o.add("canvas_aws_instance_ready", "1 while the instance is running and prepared.",
                  1 if h.get("state") == "ready" else 0, lab)
            if "cpu" in h:
                o.add("canvas_aws_instance_cpu_ratio", "Share of all the instance's vCPUs busy, "
                      "measured on the instance (/proc/stat).", h.get("cpu"), lab)
                net_help = ("Bytes per second on the instance's network interface, measured on "
                            "the instance.")
                o.add("canvas_aws_instance_network_bytes_per_second", net_help, h.get("rxBps"),
                      {**lab, "direction": "rx"})
                o.add("canvas_aws_instance_network_bytes_per_second", net_help, h.get("txBps"),
                      {**lab, "direction": "tx"})
        o.add("canvas_aws_instances_running", "EC2 instances pending or running.", cloud.get("running"))
        o.add("canvas_aws_usd_per_hour", "Estimated cost per hour of what is running now "
              "(list prices; AWS's bill is the real number).", cloud.get("usdPerHour"))
        o.add("canvas_aws_spent_usd", "Estimated spend this bridge session.", cloud.get("spentUsd"))
        o.add("canvas_aws_budget_usd", "Everything stops when the estimate reaches this.",
              cloud.get("budgetUsd"))
        o.add("canvas_aws_session_left_seconds", "Time before the bridge stops everything.",
              cloud.get("sessionLeftS"))

    if cloudwatch is not None:
        cloudwatch.render_into(o)
    return o.text()


class MetricsServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port, publisher, cloudwatch=None):
        self.publisher = publisher
        self.cloudwatch = cloudwatch
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.lock = threading.Lock()
        self.last_scrape = None
        super().__init__(("127.0.0.1", port), _Handler)

    def scraped_within(self, seconds):
        with self.lock:
            return self.last_scrape is not None and time.monotonic() - self.last_scrape < seconds


class _Handler(BaseHTTPRequestHandler):
    server_version = "l4mlb-bridge-metrics"
    sys_version = ""
    timeout = 5
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # Prometheus scrapes every few seconds; errors go to stderr below

    def _send(self, status, body, ctype="text/plain; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.headers.get("Host") not in self.server.allowed_hosts:
            return self._send(421, b"wrong host\n")
        if self.path.split("?", 1)[0] != "/metrics":
            return self._send(404, b"only /metrics here\n")
        with self.server.lock:
            self.server.last_scrape = time.monotonic()
        try:
            snap = json.loads(self.server.publisher.body or b"{}")
            body = render(snap, self.server.cloudwatch).encode()
        except Exception as e:  # a bad snapshot must not take the exporter down
            sys.stderr.write(f"bridge metrics: {e!r}\n")
            return self._send(500, b"render failed\n")
        self._send(200, body, "text/plain; version=0.0.4; charset=utf-8")

    def do_POST(self):
        self._send(405, b"read only\n")

    do_PUT = do_DELETE = do_POST
