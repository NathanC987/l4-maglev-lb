"""Shared pieces for the live-mode processes (service_server.py, load_client.py,
and the bridge): a log-bucketed latency histogram, the gamma service-time model,
and JSON-line framing over stdin/stdout.

Python stdlib only - see docs/live-mode.md.
"""
import asyncio
import json
import math
import random
import sys

HIST_BASE_MS = 0.05
HIST_GROWTH = 1.08
HIST_BUCKETS = 200
_LOG_GROWTH = math.log(HIST_GROWTH)


def bucket_of(ms):
    """Bucket i covers [BASE*GROWTH^i, BASE*GROWTH^(i+1)) ms; out-of-range values
    clamp to the end buckets. Relative error of a reported percentile is bounded by
    the 8% bucket width - plenty for latency readouts, and cheap to merge."""
    if ms <= HIST_BASE_MS:
        return 0
    i = int(math.log(ms / HIST_BASE_MS) / _LOG_GROWTH)
    return min(i, HIST_BUCKETS - 1)


def bucket_mid_ms(i):
    lo = HIST_BASE_MS * HIST_GROWTH**i
    return lo * math.sqrt(HIST_GROWTH)  # geometric midpoint of [lo, lo*GROWTH)


class LogHistogram:
    """Counts per bucket. `take_delta()` returns only buckets touched since the
    last call (sparse, JSON-friendly, keys are bucket indices as strings), so the
    bridge can keep its own sliding window by merging deltas."""

    def __init__(self):
        self.counts = [0] * HIST_BUCKETS
        self._delta = {}

    def record(self, ms):
        i = bucket_of(ms)
        self.counts[i] += 1
        self._delta[i] = self._delta.get(i, 0) + 1

    def take_delta(self):
        d = {str(k): v for k, v in self._delta.items()}
        self._delta = {}
        return d

    def reset(self):
        self.counts = [0] * HIST_BUCKETS
        self._delta = {}


def merge_into(counts, delta):
    """Adds a sparse delta (as produced by take_delta) into a dense count list."""
    for k, v in delta.items():
        i = int(k)
        if 0 <= i < HIST_BUCKETS:
            counts[i] += v


def percentile(counts, q):
    """q in [0,1]; returns None for an empty histogram."""
    total = sum(counts)
    if total == 0:
        return None
    target = q * total
    seen = 0
    for i, c in enumerate(counts):
        seen += c
        if seen >= target and c > 0:
            return bucket_mid_ms(i)
    return bucket_mid_ms(HIST_BUCKETS - 1)


def service_time_ms(rng, mean_ms, cv):
    """Same model as breakscale's Rng.serviceTime: deterministic for cv <= 0.01,
    otherwise gamma with shape 1/cv^2 and scale mean*cv^2 (so mean and
    coefficient of variation come out as configured). The variance is what
    produces realistic tail latency."""
    if cv <= 0.01:
        return mean_ms
    shape = 1.0 / (cv * cv)
    return rng.gammavariate(shape, mean_ms * cv * cv)


_BACKGROUND = set()


def spawn(coro):
    """create_task that keeps the task alive until it finishes.

    The event loop holds only a WEAK reference to a task. A fire-and-forget
    `loop.create_task(...)` can therefore be garbage-collected while it is
    suspended, silently, mid-flight ("Task was destroyed but it is pending!").
    Every background task in these processes goes through here instead."""
    task = asyncio.get_running_loop().create_task(coro)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    return task


def emit(obj):
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _reject_constant(name):
    raise ValueError(f"non-finite number {name} not allowed")


def parse_command(line):
    """Strict JSON parse for one control line: rejects NaN/Infinity and anything
    that isn't an object with a string `op`. Returns None if the line is invalid,
    so a bad line is skipped rather than taking the process down."""
    try:
        obj = json.loads(line, parse_constant=_reject_constant)
    except ValueError:
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("op"), str):
        return None
    return obj


async def read_commands(on_command, on_eof):
    """Reads JSON-line commands from stdin until EOF. EOF means the bridge went
    away (it holds our stdin pipe) - `on_eof` should shut the process down, so a
    crashed bridge never leaves orphaned load generators or responders."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=65536)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    while True:
        try:
            line = await reader.readline()
        except (ValueError, asyncio.LimitOverrunError):
            continue  # overlong line: skip it
        if not line:
            on_eof()
            return
        cmd = parse_command(line.decode("utf-8", "replace"))
        if cmd is not None:
            on_command(cmd)


def make_rng(seed):
    return random.Random(seed)


def num(obj, key, lo, hi, default=None, integer=False):
    """Bounded numeric field from a command object; bools are rejected even
    though Python treats them as ints. Returns `default` if absent or invalid."""
    v = obj.get(key, None)
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    if isinstance(v, float) and not math.isfinite(v):
        return default
    if v < lo or v > hi:
        return default
    return int(v) if integer else float(v)
