#!/usr/bin/env python3
"""AWS as referee: checks the system's claims against VPC Flow Logs and CloudWatch,
which AWS records independently of anything this project runs.

    referee.py DIR

DIR (filled by cloud/dsr-proof.sh) holds:
  window.json    {"start": {"epoch", "txPackets", ...}, "end": {...}} - the measured
                 run, marked by cloud/lib.sh's mark_window, with maglev-lb's own counters
  roles.json     {"i-0abc...": "lb", ...}
  flowlogs.json  `aws logs filter-log-events --output json` for the window
  cw-*.json      `aws cloudwatch get-metric-statistics` results, cw-<role>-<Metric>.json,
                 over the metric periods that cover the window, plus the next one

Flow-log records use the custom format in cloud/stack.yaml. The fields that make
this work are pkt-srcaddr / pkt-dstaddr: the addresses written in the packet,
which for the client's replies is the VIP, whichever host sent them.

Observed on AWS: a backend's egress with the VIP as its source address (which
source/dest check off allows) is not logged on the backend's interface; the
interface gets SKIPDATA records for those intervals instead. So which host sent
the replies comes from CloudWatch's per-instance packet counters, which the
hypervisor keeps for every packet, rather than from the backends' flow logs.

Exits 0 when every check passes, 1 when one fails, 2 when the data is unusable.
"""
import collections
import json
import pathlib
import sys

FIELDS = ("version", "eni", "instance", "direction", "srcaddr", "dstaddr", "pktsrc",
          "pktdst", "srcport", "dstport", "protocol", "packets", "bytes", "start", "end",
          "action", "status")
PROTO_NAMES = {"1": "ICMP", "6": "TCP", "17": "UDP", "47": "GRE"}

# Must match cloud/stack.yaml and cloud/lib.sh.
VIP = "10.200.0.10"
CLIENT_IP = "10.99.1.10"
LB_IP = "10.99.1.20"

# A record covers an aggregation interval of up to 60 s, and AWS can stamp it up to
# 60 s after its packets. So the records holding the window's packets are those that
# end after it starts and start no later than this long after it ends. Earlier
# traffic can be stamped late too (seen: about 70 s), so the run scripts keep the 95 s
# before a window free of VIP traffic, and they send none after it.
LATE_S = 60


def parse_record(message):
    parts = message.split()
    if len(parts) != len(FIELDS):
        return None
    r = dict(zip(FIELDS, parts))
    if r["status"] != "OK":
        return r  # NODATA / SKIPDATA: no traffic fields
    try:
        for k in ("packets", "bytes", "start", "end"):
            r[k] = int(r[k])
    except ValueError:
        return None
    return r


def load_records(events, roles, t0, t1):
    """Flow-log records holding the window's packets, with a 'host' role name.
    Returns (records, skipped) where skipped counts SKIPDATA records per host (AWS
    dropped some of that interface's flow records: its totals are lower bounds)."""
    out, skipped = [], collections.Counter()
    for ev in events:
        r = parse_record(ev.get("message", ""))
        if r is None:
            continue
        host = roles.get(r["instance"], r["instance"])
        if r["status"] == "SKIPDATA":
            if int(r["end"]) > t0 and int(r["start"]) <= t1 + LATE_S:
                skipped[host] += 1
            continue
        if r["status"] != "OK" or r["action"] != "ACCEPT":
            continue
        if r["end"] <= t0 or r["start"] > t1 + LATE_S:
            continue
        r["host"] = host
        out.append(r)
    return out, skipped


def summarize(records):
    """(host, direction, protocol, pktsrc, pktdst) -> [packets, bytes]."""
    t = collections.defaultdict(lambda: [0, 0])
    for r in records:
        k = (r["host"], r["direction"], PROTO_NAMES.get(r["protocol"], r["protocol"]),
             r["pktsrc"], r["pktdst"])
        t[k][0] += r["packets"]
        t[k][1] += r["bytes"]
    return dict(t)


def total(table, host=None, direction=None, proto=None, src=None, dst=None, idx=1):
    s = 0
    for (h, d, p, ps, pd), v in table.items():
        if host is not None and not (h == host if isinstance(host, str) else host(h)):
            continue
        if direction is not None and d != direction:
            continue
        if proto is not None and p != proto:
            continue
        if src is not None and ps != src:
            continue
        if dst is not None and pd != dst:
            continue
        s += v[idx]
    return s


def is_backend(h):
    return h.startswith("be")


def checks(table, window, skipped, cw=None):
    """Returns [(name, ok, detail)]. ok is True, False, None (inconclusive) or
    "info" (a measurement, not a pass/fail claim). cw is cw_sums()' result."""
    cw = cw or {}
    res = []
    lb_gre_out = total(table, "lb", "egress", "GRE", src=LB_IP, idx=0)
    lb_reply = total(table, "lb", "egress", "TCP", src=VIP) + \
        total(table, "lb", "egress", None, dst=CLIENT_IP)
    res.append(("The LB sends no reply traffic: nothing from the VIP, nothing to the client",
                lb_reply == 0 and lb_gre_out > 0,
                f"{lb_reply} bytes of replies from the LB, {lb_gre_out} GRE packets out"))

    client_in = total(table, "client", "ingress", "TCP", src=VIP)
    client_in_pk = total(table, "client", "ingress", "TCP", src=VIP, idx=0)
    res.append(("The client gets its replies from the VIP",
                client_in_pk > 0, f"{client_in_pk} packets, {client_in} B"))

    # Who sent them: not the LB (above), so the backends - which CloudWatch's
    # per-instance counters must then show sending at least as many packets.
    name = "The backends sent at least as many packets as the client got from the VIP"
    be_cw = {r: v for (r, m), v in cw.items() if is_backend(r) and m == "NetworkPacketsOut"}
    if be_cw and client_in_pk:
        sent = sum(be_cw.values())
        res.append((name, sent >= client_in_pk,
                    f"CloudWatch NetworkPacketsOut {', '.join(f'{r} {int(v)}' for r, v in sorted(be_cw.items()))}"
                    f" vs {client_in_pk} (the backends' counts also hold health-check replies and SSH)"))
    else:
        res.append((name, None, "no CloudWatch datapoints for the backends yet"))

    be_reply = total(table, is_backend, "egress", "TCP", src=VIP, dst=CLIENT_IP)
    if be_reply and client_in:
        diff = abs(client_in - be_reply) / be_reply
        res.append(("What the client received from the VIP is what the backends' flow logs show sent",
                    diff <= 0.05, f"client {client_in} B, backends {be_reply} B ({diff:.1%} apart)"))
    else:
        be_skips = sum(n for h, n in skipped.items() if is_backend(h))
        res.append(("Backends' own flow logs of their VIP-sourced replies", "info",
                    f"none logged ({be_skips} SKIPDATA records on the backends' interfaces)"))

    lb_out_bytes = total(table, "lb", "egress")
    if lb_out_bytes and client_in:
        res.append(("Reply bytes per byte the LB sent (the DSR ratio)", "info",
                    f"{client_in / lb_out_bytes:.1f}x"))

    if window and "start" in window and "end" in window:
        own = window["end"]["txPackets"] - window["start"]["txPackets"]
        name = "maglev-lb's count of packets it forwarded matches AWS's GRE count"
        if own > 0:
            diff = abs(lb_gre_out - own) / own
            lb_skips = skipped.get("lb", 0)
            res.append((name, diff <= 0.02 if lb_skips == 0 else None,
                        f"maglev-lb {int(own)}, flow logs {lb_gre_out} ({diff:.2%} apart)"
                        + (f"; {lb_skips} SKIPDATA records on the LB, so AWS's count is a lower bound"
                           if lb_skips else "")))
            lb_cw = cw.get(("lb", "NetworkPacketsOut"))
            if lb_cw:
                res.append(("CloudWatch counts at least as many packets out of the LB as it forwarded",
                            lb_cw >= own, f"NetworkPacketsOut {int(lb_cw)} vs maglev-lb {int(own)}"
                            " (AWS's count also holds health checks and SSH)"))
        else:
            res.append((name, False, "maglev-lb forwarded nothing in the window"))
    return res


def cw_sums(directory):
    """cw-<role>-<Metric>.json -> {(role, metric): sum of datapoints}. A metric
    with no datapoints (not published yet) is left out rather than read as 0."""
    out = {}
    for f in sorted(pathlib.Path(directory).glob("cw-*.json")):
        _, role, metric = f.stem.split("-", 2)
        pts = json.loads(f.read_text()).get("Datapoints", [])
        if pts:
            out[(role, metric)] = sum(p.get("Sum", 0) for p in pts)
    return out


def fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0


def main(argv):
    if len(argv) != 2:
        sys.exit(__doc__)
    d = pathlib.Path(argv[1])
    try:
        window = json.loads((d / "window.json").read_text())
        roles = json.loads((d / "roles.json").read_text())
        events = json.loads((d / "flowlogs.json").read_text()).get("events", [])
        t0, t1 = window["start"]["epoch"], window["end"]["epoch"]
    except (OSError, ValueError, KeyError) as e:
        print(f"referee: unusable input in {d}: {e}", file=sys.stderr)
        return 2

    records, skipped = load_records(events, roles, t0, t1)
    if not records:
        print("referee: no flow-log records for this window yet (they arrive a few "
              "minutes late) - re-run cloud/dsr-proof.sh shortly", file=sys.stderr)
        return 2
    table = summarize(records)

    print(f"VPC Flow Logs, {t0}..{t1}: {len(records)} records")
    print(f"  {'host':6} {'dir':7} {'proto':5} {'packet source':15} {'packet dest':15} "
          f"{'packets':>9} {'bytes':>10}")
    for k, (pk, by) in sorted(table.items(), key=lambda kv: -kv[1][1])[:25]:
        h, dr, p, ps, pd = k
        print(f"  {h:6} {dr:7} {p:5} {ps:15} {pd:15} {pk:>9} {fmt_bytes(by):>10}")

    cw = cw_sums(d)
    if cw:
        print("CloudWatch (AWS's per-instance counters, the periods covering the window and the next):")
        for (role, metric), v in sorted(cw.items()):
            print(f"  {role:6} {metric:18} {fmt_bytes(v) if metric.endswith(('In', 'Out')) and 'Packets' not in metric else int(v)}")

    print("Checks:")
    failed = False
    for name, ok, detail in checks(table, window, skipped, cw):
        tag = {True: "PASS", False: "FAIL", None: "INCONCLUSIVE", "info": "INFO"}[ok]
        failed |= ok is False
        print(f"  {tag:12} {name}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
