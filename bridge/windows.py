"""Sliding windows the bridge keeps per process: counter rates over ~1s and
latency percentiles over ~5s (merged log-histogram deltas). Children report
cumulative counters plus histogram *deltas*; turning those into rates and
percentiles here keeps the bridge's output frontend-agnostic and keeps the
histograms themselves off the wire to the browser."""
import collections

import livecommon as lc


class RateWindow:
    def __init__(self, window_ms=1000.0):
        self.window_ms = window_ms
        self.samples = collections.deque()  # (t_ms, flat counters dict)

    def push(self, t_ms, counters):
        if self.samples:
            last = self.samples[-1][1]
            # A counter going backwards means the process restarted or its
            # stats were reset: start over rather than report a negative rate.
            if any(counters.get(k, 0) < v for k, v in last.items()):
                self.samples.clear()
        self.samples.append((t_ms, counters))
        # keep exactly one sample older than the window, as the rate's base
        while len(self.samples) > 2 and self.samples[1][0] <= t_ms - self.window_ms:
            self.samples.popleft()

    def clear(self):
        self.samples.clear()

    def rate(self, key):
        """Per second."""
        if len(self.samples) < 2:
            return 0.0
        (t0, c0), (t1, c1) = self.samples[0], self.samples[-1]
        dt = t1 - t0
        if dt <= 0:
            return 0.0
        return max(0.0, (c1.get(key, 0) - c0.get(key, 0)) * 1000.0 / dt)

    def rates_with_prefix(self, prefix):
        if not self.samples:
            return {}
        keys = set(self.samples[-1][1])
        return {k[len(prefix):]: self.rate(k) for k in keys if k.startswith(prefix)}

    def latest(self, key, default=0):
        return self.samples[-1][1].get(key, default) if self.samples else default


class LatencyWindow:
    def __init__(self, window_ms=5000.0):
        self.window_ms = window_ms
        self.parts = collections.deque()  # (t_ms, sparse delta)
        self.counts = [0] * lc.HIST_BUCKETS

    def push(self, t_ms, delta):
        if delta:
            self.parts.append((t_ms, delta))
            lc.merge_into(self.counts, delta)
        while self.parts and self.parts[0][0] < t_ms - self.window_ms:
            _, old = self.parts.popleft()
            for k, v in old.items():
                self.counts[int(k)] -= v

    def clear(self):
        self.parts.clear()
        self.counts = [0] * lc.HIST_BUCKETS

    def summary(self):
        n = sum(self.counts)
        def p(q):
            v = lc.percentile(self.counts, q)
            return round(v, 3) if v is not None else 0.0
        return {"p50": p(0.5), "p95": p(0.95), "p99": p(0.99), "n": n}


def flatten(stats, *nested):
    """{"counters": {...}, "failedByReason": {...}} -> one flat dict with
    prefixed keys, so one RateWindow can rate everything."""
    flat = dict(stats.get("counters", {}))
    for name in nested:
        for k, v in (stats.get(name) or {}).items():
            flat[f"{name}:{k}"] = v
    return flat
