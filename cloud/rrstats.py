#!/usr/bin/env python3
"""Summarizes bridge/load_client.py's JSON stats lines into one JSON object:
request counts and rates, failures by reason, end-to-end latency percentiles,
and how the replies were spread over backends. Several FILEs (one per client
process of the same run) are merged: counts add up, latency histograms merge.

    rrstats.py [FILE...] [--skip-s S]

--skip-s drops the first S seconds (warm-up) from the rates and percentiles.
Counters in the stats lines are cumulative; latency histograms are deltas.
"""
import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bridge"))
import livecommon as lc  # noqa: E402


def read_stats(lines):
    out = []
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("type") == "stats":
            out.append(obj)
    return out


def _window(stats, skip_s):
    """(base, last, lines after base) for one process's run, or None."""
    if len(stats) < 2:
        return None
    t_first = stats[0]["tMs"]
    base = next((s for s in stats if s["tMs"] - t_first >= skip_s * 1000.0), None)
    if base is None or base is stats[-1]:
        return None
    return base, stats[-1], [s for s in stats if s["tMs"] > base["tMs"]]


def _diff(a, b):
    return {k: v - a.get(k, 0) for k, v in b.items()}


def summarize(runs, skip_s=0.0):
    """runs: one list of stats lines per client process."""
    if runs and isinstance(runs[0], dict):
        runs = [runs]
    windows = [w for w in (_window(r, skip_s) for r in runs) if w]
    if not windows or len(windows) != len(runs):
        return {"error": "a client produced fewer than two stats lines after the "
                         f"{skip_s}s warm-up (did it run?)"}
    secs = min((last["tMs"] - base["tMs"]) / 1000.0 for base, last, _ in windows)
    delta, failed, by_slot = {}, {}, {}
    hist = [0] * lc.HIST_BUCKETS
    for base, last, lines in windows:
        for acc, key in ((delta, "counters"), (failed, "failedByReason"),
                         (by_slot, "respBySlot")):
            for k, v in _diff(base.get(key, {}), last.get(key, {})).items():
                acc[k] = acc.get(k, 0) + v
        for line in lines:
            lc.merge_into(hist, line.get("hist", {}))
    pct = {f"p{int(q * 100)}": lc.percentile(hist, q) for q in (0.5, 0.9, 0.99)}
    return {
        "processes": len(windows),
        "seconds": round(secs, 2),
        "offeredRps": round(delta.get("scheduled", 0) / secs, 1),
        "okRps": round(delta.get("ok", 0) / secs, 1),
        "ok": delta.get("ok", 0),
        "failed": delta.get("failed", 0),
        "failedByReason": {k: v for k, v in failed.items() if v},
        "latencyMs": {k: (round(v, 2) if v is not None else None) for k, v in pct.items()},
        "maxLagMs": round(max(line.get("lagMsMax", 0.0) for _, _, ls in windows for line in ls),
                          1),
        "repliesBySlot": by_slot,
    }


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("files", nargs="*")
    p.add_argument("--skip-s", type=float, default=0.0)
    a = p.parse_args(argv)
    if a.files:
        runs = []
        for f in a.files:
            with open(f) as src:
                runs.append(read_stats(src))
    else:
        runs = [read_stats(sys.stdin)]
    print(json.dumps(summarize(runs, a.skip_s)))


if __name__ == "__main__":
    main()
