"""Strict validation for everything the bridge accepts over HTTP.

The bridge runs as root and spawns processes, so every input is treated as
hostile: unknown keys are rejected, numbers must be finite and in range,
booleans are never accepted where numbers are expected (Python treats True as
1), and canvas ids must match a conservative pattern. Ids are only ever used as
dictionary keys - they never reach a command line or a file path.
"""
import json
import math
import re

SCHEMA = "l4mlb-live/1"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_CLIENTS = 4
MAX_BACKENDS = 16
MAX_TOTAL_RPS = 4000

CLIENT_FIELDS = {
    "rps": (0, 2000, False),
    "timeoutMs": (50, 10000, False),
    "retries": (0, 5, True),
    "poolSize": (0, 64, True),
    "maxConnAgeMs": (0, 60000, True),
}
BACKEND_FIELDS = {
    "capacity": (1, 256, True),
    "serviceMs": (1, 2000, False),
    "serviceCv": (0, 3, False),
    "queueLimit": (0, 4096, True),
    "errorRate": (0, 1, False),
}
CLIENT_DEFAULTS = {"rps": 50, "timeoutMs": 1000, "retries": 1, "poolSize": 0, "maxConnAgeMs": 0}
BACKEND_DEFAULTS = {"capacity": 8, "serviceMs": 25, "serviceCv": 0.6, "queueLimit": 64,
                    "errorRate": 0}
ACTIONS = {"crash", "restart", "slow", "errors", "partition", "clear", "drain", "undrain"}


class ValidationError(Exception):
    def __init__(self, errors):
        super().__init__("; ".join(f"{e['path']}: {e['msg']}" for e in errors))
        self.errors = errors


def _reject_constant(name):
    raise ValueError(f"{name} is not allowed")


def loads(body):
    """json.loads that rejects NaN/Infinity and duplicate keys."""
    def no_dupes(pairs):
        d = {}
        for k, v in pairs:
            if k in d:
                raise ValueError(f"duplicate key {k!r}")
            d[k] = v
        return d
    return json.loads(body, parse_constant=_reject_constant, object_pairs_hook=no_dupes)


class _Errs:
    def __init__(self):
        self.items = []

    def add(self, path, msg):
        self.items.append({"path": path, "msg": msg})

    def raise_if_any(self):
        if self.items:
            raise ValidationError(self.items)


def _number(errs, path, v, lo, hi, integer):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        errs.add(path, "must be a number")
        return None
    if isinstance(v, float) and not math.isfinite(v):
        errs.add(path, "must be finite")
        return None
    if integer and (isinstance(v, float) and not v.is_integer()):
        errs.add(path, "must be an integer")
        return None
    if v < lo or v > hi:
        errs.add(path, f"must be in [{lo}, {hi}]")
        return None
    return int(v) if integer else float(v)


def _obj(errs, path, v, allowed):
    if not isinstance(v, dict):
        errs.add(path, "must be an object")
        return {}
    for k in v:
        if k not in allowed:
            errs.add(f"{path}.{k}", "unknown key")
    return v


def _id(errs, path, v):
    if not isinstance(v, str) or not ID_RE.match(v):
        errs.add(path, "must match ^[A-Za-z0-9_-]{1,64}$")
        return None
    return v


def _node_list(errs, path, v, fields, defaults, limit):
    if not isinstance(v, list):
        errs.add(path, "must be a list")
        return []
    if len(v) > limit:
        errs.add(path, f"at most {limit} allowed")
    out = []
    for i, n in enumerate(v[:limit]):
        p = f"{path}[{i}]"
        n = _obj(errs, p, n, {"id", *fields})
        nid = _id(errs, f"{p}.id", n.get("id"))
        cfg = dict(defaults)
        for k, (lo, hi, integer) in fields.items():
            if k in n:
                val = _number(errs, f"{p}.{k}", n[k], lo, hi, integer)
                if val is not None:
                    cfg[k] = val
        if nid is not None:
            out.append({"id": nid, **cfg})
    return out


def validate_desired(doc):
    """Returns a normalized desired-state dict or raises ValidationError.

    Graph rules: exactly one lb; edges only client->lb and lb->backend (a client
    only knows the VIP, so a client->backend edge can't exist in this topology);
    ids unique across all nodes. A client without an edge to the lb is
    'disconnected' (generates no load); a backend without one is 'detached'
    (not started). Both are allowed - they are mid-edit states on the canvas."""
    errs = _Errs()
    doc = _obj(errs, "$", doc, {"schema", "nodes", "edges", "load"})
    if doc.get("schema") != SCHEMA:
        errs.add("$.schema", f"must be {SCHEMA!r}")
    nodes = _obj(errs, "$.nodes", doc.get("nodes"), {"lb", "clients", "backends"})
    lb = _id(errs, "$.nodes.lb", nodes.get("lb"))
    clients = _node_list(errs, "$.nodes.clients", nodes.get("clients", []), CLIENT_FIELDS,
                         CLIENT_DEFAULTS, MAX_CLIENTS)
    backends = _node_list(errs, "$.nodes.backends", nodes.get("backends", []), BACKEND_FIELDS,
                          BACKEND_DEFAULTS, MAX_BACKENDS)
    for i, c in enumerate(clients):
        age = c["maxConnAgeMs"]
        if age != 0 and age < 1000:
            errs.add(f"$.nodes.clients[{i}].maxConnAgeMs", "must be 0 or in [1000, 60000]")
    if sum(c["rps"] for c in clients) > MAX_TOTAL_RPS:
        errs.add("$.nodes.clients", f"total rps must be <= {MAX_TOTAL_RPS}")

    client_ids = {c["id"] for c in clients}
    backend_ids = {b["id"] for b in backends}
    all_ids = [lb] + [c["id"] for c in clients] + [b["id"] for b in backends]
    all_ids = [x for x in all_ids if x is not None]
    if len(all_ids) != len(set(all_ids)):
        errs.add("$.nodes", "node ids must be unique")

    edges_in = doc.get("edges", [])
    edges = []
    if not isinstance(edges_in, list):
        errs.add("$.edges", "must be a list")
        edges_in = []
    if len(edges_in) > MAX_CLIENTS + MAX_BACKENDS:
        errs.add("$.edges", "too many edges")
        edges_in = []
    for i, e in enumerate(edges_in):
        p = f"$.edges[{i}]"
        e = _obj(errs, p, e, {"from", "to"})
        a = _id(errs, f"{p}.from", e.get("from"))
        b = _id(errs, f"{p}.to", e.get("to"))
        if a is None or b is None:
            continue
        if a in client_ids and b == lb:
            edges.append((a, b))
        elif a == lb and b in backend_ids:
            edges.append((a, b))
        elif a in client_ids and b in backend_ids:
            errs.add(p, "a client only knows the VIP: connect it to the load balancer, "
                        "not directly to a backend")
        else:
            errs.add(p, "only client->lb and lb->backend edges are supported")
    if len(set(edges)) != len(edges):
        errs.add("$.edges", "duplicate edge")

    load = _obj(errs, "$.load", doc.get("load", {"enabled": False}), {"enabled"})
    enabled = load.get("enabled", False)
    if not isinstance(enabled, bool):
        errs.add("$.load.enabled", "must be a boolean")
        enabled = False

    errs.raise_if_any()
    connected = {a for a, b in edges if b == lb}
    attached = {b for a, b in edges if a == lb}
    return {
        "lb": lb,
        "clients": [{**c, "connected": c["id"] in connected} for c in clients],
        "backends": [{**b, "attached": b["id"] in attached} for b in backends],
        "loadEnabled": enabled,
    }


def validate_action(doc):
    errs = _Errs()
    doc = _obj(errs, "$", doc, {"action", "target", "factor", "rate"})
    action = doc.get("action")
    if action not in ACTIONS:
        errs.add("$.action", f"must be one of {sorted(ACTIONS)}")
    target = _id(errs, "$.target", doc.get("target"))
    out = {"action": action, "target": target}
    if action == "slow":
        out["factor"] = _number(errs, "$.factor", doc.get("factor", 4), 1, 50, False)
    elif "factor" in doc:
        errs.add("$.factor", "only valid for 'slow'")
    if action == "errors":
        out["rate"] = _number(errs, "$.rate", doc.get("rate", 0.3), 0, 1, False)
    elif "rate" in doc:
        errs.add("$.rate", "only valid for 'errors'")
    errs.raise_if_any()
    return out
