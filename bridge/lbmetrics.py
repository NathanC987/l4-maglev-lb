"""Reads maglev-lb's own view of the world: its Prometheus endpoint
(src/ui/metrics_http.c's build_metrics_body() is the source of truth for every
name here) and its datapath thread's CPU time from /proc."""
import os
import re
import urllib.request

_LABEL_RE = re.compile(r'(\w+)="([^"]*)"')


def parse_prometheus(text):
    """Returns {name: value} for unlabeled series and
    {name: [(labels_dict, value), ...]} for labeled ones."""
    plain, labeled = {}, {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        try:
            series, value = line.rsplit(" ", 1)
            v = float(value)
        except ValueError:
            continue
        if "{" in series:
            name, rest = series.split("{", 1)
            labeled.setdefault(name, []).append((dict(_LABEL_RE.findall(rest)), v))
        else:
            plain[series] = v
    return plain, labeled


def fetch(url, timeout=1.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def summarize(text):
    """Flattens the subset of metrics the bridge uses into one dict:
    counters stay cumulative (rates are computed later, over a window), and
    per-backend series are keyed by maglev-lb backend id."""
    plain, labeled = parse_prometheus(text)
    out = {
        "rxPackets": plain.get("maglev_lb_rx_packets_total", 0.0),
        "txPackets": plain.get("maglev_lb_tx_packets_total", 0.0),
        "rxBytes": plain.get("maglev_lb_rx_bytes_total", 0.0),
        "txBytes": plain.get("maglev_lb_tx_bytes_total", 0.0),
        "ctEvictions": plain.get("maglev_lb_conntrack_evictions_total", 0.0),
        "activeFlows": plain.get("maglev_lb_conntrack_active_flows", 0.0),
        "tableGeneration": plain.get("maglev_lb_table_generation", 0.0),
        "regenLastUs": plain.get("maglev_lb_table_regen_last_microseconds", 0.0),
        "backendsEligible": plain.get("maglev_lb_backends_eligible", 0.0),
        "drops": {}, "lookups": {}, "backends": {},
    }
    for labels, v in labeled.get("maglev_lb_drops_total", []):
        out["drops"][labels.get("reason", "?")] = v
    for labels, v in labeled.get("maglev_lb_conntrack_lookups_total", []):
        out["lookups"][labels.get("result", "?")] = v
    per_backend = {
        "maglev_lb_backend_up": "up",
        "maglev_lb_backend_admin_enabled": "adminEnabled",
        "maglev_lb_backend_draining": "draining",
        "maglev_lb_backend_packets_total": "packets",
        "maglev_lb_backend_active_flows": "activeFlows",
        "maglev_lb_backend_flows_assigned_total": "flowsAssigned",
        "maglev_lb_backend_health_check_failures_total": "hcFailures",
    }
    for metric, key in per_backend.items():
        for labels, v in labeled.get(metric, []):
            try:
                bid = int(labels["backend_id"])
            except (KeyError, ValueError):
                continue
            out["backends"].setdefault(bid, {})[key] = v
    return out


def _schedstat_seconds(path):
    """First field of a schedstat file: nanoseconds spent running on a CPU.

    Used instead of utime+stime from /proc/<pid>/stat, which advance in whole
    clock ticks (10ms at CLK_TCK=100): over the bridge's ~1.5s window that
    rounds anything under ~0.7% of a core to zero, and the datapath at
    demo-scale load sits well under that. schedstat has nanosecond
    resolution, so a light load reads as light rather than as none."""
    try:
        with open(path) as f:
            return int(f.read().split()[0]) / 1e9
    except (OSError, ValueError, IndexError):
        return None


def thread_cpu_seconds(pid, tid=None):
    """CPU seconds of one thread. Headless maglev-lb runs datapath_run() on
    its main thread, so tid == pid is the datapath."""
    tid = pid if tid is None else tid
    return _schedstat_seconds(f"/proc/{pid}/task/{tid}/schedstat")


def process_cpu_seconds(pid):
    """CPU seconds summed over every LIVING thread of the process. A thread
    that exits takes its time with it, so the sum can step down; fine for
    maglev-lb, whose threads all live as long as the process (and the bridge
    clamps a negative rate to zero)."""
    try:
        tids = os.listdir(f"/proc/{pid}/task")
    except OSError:
        return None
    total = 0.0
    for tid in tids:
        v = _schedstat_seconds(f"/proc/{pid}/task/{tid}/schedstat")
        if v is not None:
            total += v
    return total
