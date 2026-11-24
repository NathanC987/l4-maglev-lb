"""AWS's own view of the canvas's EC2 instances, from CloudWatch, for the Grafana
dashboard (bridge/promexport.py renders it next to the canvas's numbers).

These are the hypervisor's measurements, not anything this project runs: CPU,
bytes and packets per instance. They arrive late (a minute or more), and only
the LB has 1-minute metrics (detailed monitoring, cloud/stack.yaml); the rest,
and every instance's packet counts, are 5-minute.

GetMetricData is billed per metric asked for (about $0.01 per 1,000), so this
asks only while Grafana is scraping, only about running instances, and for the
5-minute metrics only every 5 minutes: well under a cent an hour.
"""
import json
import threading
import time

FAST_S = 60     # the LB's 1-minute metrics
SLOW_S = 300    # everything 5-minute
WATCH_S = 120   # keep asking this long after the last scrape
DENIED_RETRY_S = 600
LOOKBACK_S = 1200

# (metric, statistic, per-second?) - bytes and packets are sums per period.
FAST = [("CPUUtilization", "Average", False), ("NetworkIn", "Sum", True), ("NetworkOut", "Sum", True)]
SLOW = FAST + [("NetworkPacketsIn", "Sum", True), ("NetworkPacketsOut", "Sum", True)]
LB_ROLE = "lb"

NAMES = {
    "CPUUtilization": ("canvas_cloudwatch_cpu_percent",
                       "CPUUtilization: the instance's CPU as the hypervisor measures it."),
    "NetworkIn": ("canvas_cloudwatch_network_in_bytes_per_second",
                  "NetworkIn per second, averaged over the CloudWatch period."),
    "NetworkOut": ("canvas_cloudwatch_network_out_bytes_per_second",
                   "NetworkOut per second, averaged over the CloudWatch period."),
    "NetworkPacketsIn": ("canvas_cloudwatch_network_packets_in_per_second",
                         "NetworkPacketsIn per second (5-minute periods only)."),
    "NetworkPacketsOut": ("canvas_cloudwatch_network_packets_out_per_second",
                          "NetworkPacketsOut per second (5-minute periods only)."),
}


def queries(targets, slow):
    """GetMetricData queries for (role, instance_id) targets: the LB's 1-minute
    metrics always, and with slow=True every instance's 5-minute ones."""
    out, keys = [], {}
    for role, iid in targets:
        wanted = []
        if role == LB_ROLE:
            wanted += [(m, st, ps, 60) for m, st, ps in FAST]
        if slow:
            wanted += [(m, st, ps, 300) for m, st, ps in SLOW
                       if not (role == LB_ROLE and (m, st, ps) in FAST)]
        for m, st, ps, period in wanted:
            qid = f"q{len(out)}"
            keys[qid] = (role, iid, m, period, ps)
            out.append({"Id": qid, "ReturnData": True, "MetricStat": {
                "Metric": {"Namespace": "AWS/EC2", "MetricName": m,
                           "Dimensions": [{"Name": "InstanceId", "Value": iid}]},
                "Period": period, "Stat": st}})
    return out, keys


def parse(result, keys):
    """{(role, iid, metric): (value, timestamp)} from GetMetricData's output, the
    newest datapoint of each; sums become per-second rates."""
    out = {}
    for r in result.get("MetricDataResults", []):
        k = keys.get(r.get("Id"))
        if not k or not r.get("Values"):
            continue
        role, iid, metric, period, per_s = k
        pairs = list(zip(r.get("Timestamps", []), r["Values"]))
        if not pairs:
            continue
        ts, v = max(pairs, key=lambda p: p[0])
        out[(role, iid, metric)] = (v / period if per_s else v, _epoch(ts))
    return out


def _epoch(ts):
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class CloudWatchPoller:
    """Polls in its own thread. targets() returns the running (role, instance_id)
    pairs; watched() says whether Grafana scraped recently."""

    def __init__(self, aws, targets, watched, clock=time.time):
        self.aws, self.targets, self.watched, self.clock = aws, targets, watched, clock
        self.lock = threading.Lock()
        self.samples = {}
        self.state = "idle"      # idle, ok, denied, error
        self.error = None
        self.last_fast = self.last_slow = 0.0
        self.retry_at = 0.0
        self.stop = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="cloudwatch").start()

    def _loop(self):
        while not self.stop.wait(5.0):
            try:
                self.poll_once()
            except Exception as e:  # keep polling; the dashboard shows the state
                with self.lock:
                    self.state, self.error = "error", str(e)[:200]

    def poll_once(self):
        now = self.clock()
        if not self.watched() or now < self.retry_at:
            return False
        fast_due = now - self.last_fast >= FAST_S
        slow_due = now - self.last_slow >= SLOW_S
        if not (fast_due or slow_due):
            return False
        targets = list(self.targets())
        live = {iid for _, iid in targets}
        with self.lock:
            self.samples = {k: v for k, v in self.samples.items() if k[1] in live}
        qs, keys = queries(targets, slow_due)
        if not qs:
            return False
        try:
            got = self.aws.metric_data(qs, int(now - LOOKBACK_S), int(now))
        except Exception as e:
            msg = str(e)
            with self.lock:
                if "AccessDenied" in msg or "not authorized" in msg:
                    self.state = "denied"
                    self.error = ("the bridge's role cannot read CloudWatch yet: run "
                                  "cloud/canvas.sh up once to update the stack")
                    self.retry_at = now + DENIED_RETRY_S
                else:
                    self.state, self.error = "error", msg[:200]
                    self.retry_at = now + FAST_S
            return False
        with self.lock:
            self.samples.update(parse(got, keys))
            self.state, self.error = "ok", None
        self.last_fast = now
        if slow_due:
            self.last_slow = now
        return True

    def render_into(self, o):
        now = self.clock()
        with self.lock:
            samples = dict(self.samples)
            state = self.state
        o.add("canvas_cloudwatch_ok", "1 if the last CloudWatch read worked; 0 if it failed or was "
              "denied; absent until Grafana has been watching for a moment.",
              {"ok": 1, "denied": 0, "error": 0}.get(state), {"state": state} if state != "idle" else None)
        for (role, iid, metric), (v, ts) in sorted(samples.items()):
            name, help_ = NAMES[metric]
            lab = {"role": role, "instance_id": iid}
            o.add(name, help_, v, lab)
            if ts is not None:
                o.add("canvas_cloudwatch_age_seconds", "How old the newest CloudWatch datapoint is.",
                      max(0.0, now - ts), {**lab, "metric": metric})


def metric_data_args(queries_, start, end):
    """The AWS CLI arguments for one GetMetricData call."""
    return ["cloudwatch", "get-metric-data", "--metric-data-queries",
            json.dumps(queries_, separators=(",", ":")),
            "--start-time", _iso(start), "--end-time", _iso(end),
            "--scan-by", "TimestampDescending"]


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(t)))
