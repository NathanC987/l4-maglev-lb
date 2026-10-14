"""Desired state (the canvas) -> actual state (processes, admin-FIFO commands).

A pure state machine: it never touches the OS itself. Every side effect goes
through an injected executor (children.Executor in production, a fake in
bridge/tests/test_reconciler.py), and every observation (is backend slot N up
according to maglev-lb's health checker? is that child process alive? what are
its latest stats?) is passed in. `step()` is called every tick and after every
command, and is idempotent: calling it twice with nothing changed does nothing.

Backend slots. maglev-lb is started once with a fixed list of N `--backend`
entries (slot n <-> maglev-lb backend id n-1); each slot has its own network
namespace, GRE tunnel and VIP from scripts/bench/setup-bench-netns.sh, but no
process listening until a canvas backend is assigned to it. Adding a backend on
the canvas = starting a responder in a free slot; maglev-lb's health checker
brings it up exactly as it does for any recovering backend (rise probes). This
is M4's "pre-provisioned, started late" mechanism, needing no runtime
backend-add API in maglev-lb.

Slot lifecycle:
  free -> starting   (responder spawned; waiting for LB up && admin enabled)
       -> serving
       -> draining   (removed from canvas: `DRAIN id` - no new flows, pinned ones
                      finish; until the responder is idle for 1s, or 30s)
       -> stopping   (TERM, KILL after 2s; until maglev-lb marks the slot DOWN)
       -> free       (`UNDRAIN id` - only now: undraining a slot whose health is
                      still UP would make a dead slot eligible for new flows)
Re-adding an id while its slot is draining cancels the drain. Canvas ids map to
the same slot for the whole session where possible (undo after delete gets the
same backend back).
"""

BACKEND_SLOT_DRAIN_QUIET_MS = 1000
BACKEND_SLOT_DRAIN_MAX_MS = 30000
BACKEND_STOP_KILL_AFTER_MS = 2000
MAX_CLIENT_SLOTS = 4

BACKEND_CONFIG_KEYS = ("capacity", "serviceMs", "serviceCv", "queueLimit", "errorRate")
CLIENT_CONFIG_KEYS = ("rps", "timeoutMs", "retries", "poolSize", "maxConnAgeMs")


class ActionError(Exception):
    pass


class Slot:
    def __init__(self, n):
        self.n = n
        self.lb_id = n - 1
        self.clear()

    def clear(self):
        self.owner = None
        self.phase = "free"
        self.handle = None
        self.config = None
        self.fault = None
        self.manual_drain = False
        self.removing = False
        self.drain_since = None
        self.quiet_since = None
        self.stop_since = None
        self.killed = False
        self.partitioned = False


class ClientRec:
    def __init__(self, cid, index):
        self.id = cid
        self.index = index
        self.handle = None
        self.config = None
        self.enabled = None


def _cfg(d, keys):
    return {k: d[k] for k in keys}


class Reconciler:
    def __init__(self, executor, n_slots):
        self.ex = executor
        self.slots = {n: Slot(n) for n in range(1, n_slots + 1)}
        self.slot_memory = {}    # canvas id -> slot number it last used
        self.clients = {}        # canvas id -> ClientRec
        self.client_memory = {}  # canvas id -> client index it last used
        self.desired = {"lb": None, "clients": [], "backends": [], "loadEnabled": False}
        self.load_suppressed = False  # e.g. no browser watching (auto-pause)
        self.pending = set()     # backend ids waiting for a free slot
        self.events = []

    # --- lookups -------------------------------------------------------------
    def slot_of(self, cid):
        for s in self.slots.values():
            if s.owner == cid:
                return s
        return None

    def _desired_backends(self):
        return {b["id"]: b for b in self.desired["backends"]}

    def _event(self, now, msg, level="info"):
        self.events.append({"tMs": now, "level": level, "msg": msg})

    def take_events(self):
        ev, self.events = self.events, []
        return ev

    # --- inputs --------------------------------------------------------------
    def set_desired(self, desired):
        self.desired = desired

    def _spawn(self, slot, cfg, now):
        slot.handle = self.ex.spawn_backend(slot.n, cfg)
        slot.config = cfg
        slot.phase = "starting"
        slot.killed = False
        self._event(now, f"slot {slot.n}: responder started for {slot.owner}")

    def _clear_fault(self, slot, now):
        """Undo whatever the current fault did. One fault per node, as in the
        simulator: injecting a new one first clears the old one."""
        f = slot.fault
        slot.fault = None
        if f is None:
            return
        if f["kind"] == "crash" and slot.owner in self._desired_backends():
            self._spawn(slot, _cfg(self._desired_backends()[slot.owner], BACKEND_CONFIG_KEYS), now)
        elif f["kind"] == "partition" and slot.partitioned:
            self.ex.set_gre(slot.n, True)
            slot.partitioned = False
        elif f["kind"] in ("slow", "errors") and slot.handle is not None:
            self.ex.send(slot.handle, {"op": "clear"})

    def action(self, act, now):
        cid = act["target"]
        slot = self.slot_of(cid)
        if slot is None or cid not in self._desired_backends() or slot.removing:
            raise ActionError(f"{cid} is not a running backend")
        kind = act["action"]
        if kind in ("crash", "slow", "errors", "partition"):
            self._clear_fault(slot, now)
        if kind == "crash":
            if self.ex.alive(slot.handle):
                self.ex.kill(slot.handle)
            slot.killed = True
            slot.phase = "crashed"
            slot.fault = {"kind": "crash", "sinceMs": now}
            self._event(now, f"{cid}: crashed (SIGKILL)", "warn")
        elif kind == "slow":
            self.ex.send(slot.handle, {"op": "fault", "slowFactor": act["factor"], "errorRate": 0})
            slot.fault = {"kind": "slow", "sinceMs": now, "factor": act["factor"]}
        elif kind == "errors":
            self.ex.send(slot.handle, {"op": "fault", "slowFactor": 1, "errorRate": act["rate"]})
            slot.fault = {"kind": "errors", "sinceMs": now, "rate": act["rate"]}
        elif kind == "partition":
            self.ex.set_gre(slot.n, False)
            slot.partitioned = True
            slot.fault = {"kind": "partition", "sinceMs": now}
            self._event(now, f"{cid}: partitioned (gre1 down; health checks still pass)", "warn")
        elif kind in ("restart", "clear"):
            self._clear_fault(slot, now)
        elif kind == "drain":
            self.ex.admin("DRAIN", slot.lb_id)
            slot.manual_drain = True
            if slot.phase in ("serving", "unhealthy", "starting"):
                slot.phase = "draining"
        elif kind == "undrain":
            self.ex.admin("UNDRAIN", slot.lb_id)
            slot.manual_drain = False
            if slot.phase == "draining":
                slot.phase = "serving"

    def reset(self, now):
        """New session: clear every fault and manual drain, zero every child's
        counters. Topology and maglev-lb itself are left alone."""
        for slot in self.slots.values():
            if slot.owner is None or slot.removing:
                continue
            self._clear_fault(slot, now)
            if slot.manual_drain:
                self.ex.admin("UNDRAIN", slot.lb_id)
                slot.manual_drain = False
                if slot.phase == "draining":
                    slot.phase = "serving"
            if slot.handle is not None and self.ex.alive(slot.handle):
                self.ex.send(slot.handle, {"op": "reset_stats"})
        for rec in self.clients.values():
            if rec.handle is not None:
                self.ex.send(rec.handle, {"op": "reset_stats"})

    # --- the loop --------------------------------------------------------------
    def step(self, now, lb):
        """`lb` maps maglev-lb backend id -> {"up", "adminEnabled"}; missing ids
        are treated as down."""
        self._step_clients(now)
        self._step_backends(now, lb)

    def _step_clients(self, now):
        want = {c["id"]: c for c in self.desired["clients"]}
        for cid in list(self.clients):
            if cid not in want:
                rec = self.clients.pop(cid)
                self.ex.terminate(rec.handle)
        used = {r.index for r in self.clients.values()}
        for cid, c in want.items():
            rec = self.clients.get(cid)
            cfg = _cfg(c, CLIENT_CONFIG_KEYS)
            enabled = (self.desired["loadEnabled"] and c["connected"]
                       and not self.load_suppressed)
            if rec is None:
                idx = self.client_memory.get(cid)
                if idx is None or idx in used:
                    free = [i for i in range(MAX_CLIENT_SLOTS) if i not in used]
                    if not free:
                        continue
                    idx = free[0]
                used.add(idx)
                self.client_memory[cid] = idx
                rec = self.clients[cid] = ClientRec(cid, idx)
            if rec.handle is None or not self.ex.alive(rec.handle):
                if rec.handle is not None:
                    self._event(now, f"client {cid}: process exited, restarting", "warn")
                rec.handle = self.ex.spawn_client(rec.index, cfg, enabled)
                rec.config, rec.enabled = cfg, enabled
                continue
            if cfg != rec.config:
                self.ex.send(rec.handle, {"op": "config", **cfg})
                rec.config = cfg
            if enabled != rec.enabled:
                self.ex.send(rec.handle, {"op": "load", "enabled": enabled})
                rec.enabled = enabled

    def _step_backends(self, now, lb):
        want = self._desired_backends()
        active = {cid for cid, b in want.items() if b["attached"]}

        # Removals (or re-adds that cancel an in-progress drain).
        for slot in self.slots.values():
            if slot.owner is None:
                continue
            if slot.owner not in active and not slot.removing:
                slot.removing = True
                slot.manual_drain = False
                if self.ex.alive(slot.handle) and slot.phase != "crashed":
                    self.ex.admin("DRAIN", slot.lb_id)
                    slot.phase, slot.drain_since, slot.quiet_since = "draining", now, None
                    self._event(now, f"slot {slot.n}: draining {slot.owner} before removal")
                else:
                    self._begin_stop(slot, now)
            elif slot.owner in active and slot.removing and slot.phase == "draining":
                self.ex.admin("UNDRAIN", slot.lb_id)
                slot.removing = False
                slot.phase = "serving"
                self._event(now, f"slot {slot.n}: {slot.owner} re-added, drain cancelled")

        # Additions. An id whose slot is still stopping from an earlier removal
        # still owns that slot (slot_of finds it), so it simply waits for the
        # slot to free up and is re-started there on a later tick.
        self.pending = set()
        for cid in sorted(active):
            if self.slot_of(cid) is not None:
                continue
            mem = self.slot_memory.get(cid)
            if mem is not None and self.slots[mem].phase == "free":
                slot = self.slots[mem]
            else:
                free = [s for s in self.slots.values() if s.phase == "free"]
                slot = free[0] if free else None
            if slot is None:
                self.pending.add(cid)
                continue
            slot.owner = cid
            self.slot_memory[cid] = slot.n
            self._spawn(slot, _cfg(want[cid], BACKEND_CONFIG_KEYS), now)

        # Progress every occupied slot.
        for slot in self.slots.values():
            if slot.owner is None:
                continue
            obs = lb.get(slot.lb_id, {})
            up = bool(obs.get("up"))
            alive = self.ex.alive(slot.handle)

            if slot.phase in ("starting", "serving", "unhealthy", "draining") and not alive \
                    and not slot.removing:
                slot.phase = "crashed"
                if slot.fault is None:
                    slot.fault = {"kind": "crash", "sinceMs": now}
                self._event(now, f"{slot.owner}: responder exited unexpectedly", "error")
                continue

            if slot.phase == "starting" and up and obs.get("adminEnabled", True):
                slot.phase = "draining" if slot.manual_drain else "serving"
                self._event(now, f"slot {slot.n}: {slot.owner} HEALTHY")
            elif slot.phase == "serving" and not up:
                slot.phase = "unhealthy"
                self._event(now, f"slot {slot.n}: {slot.owner} UNHEALTHY", "warn")
            elif slot.phase == "unhealthy" and up:
                slot.phase = "serving"
                self._event(now, f"slot {slot.n}: {slot.owner} HEALTHY")
            elif slot.phase == "draining" and slot.removing:
                st = self.ex.latest(slot.handle) or {}
                idle = (st.get("busy", 1) == 0 and st.get("queued", 1) == 0
                        and st.get("openConns", 1) == 0)
                if idle:
                    slot.quiet_since = slot.quiet_since or now
                else:
                    slot.quiet_since = None
                if (slot.quiet_since and now - slot.quiet_since >= BACKEND_SLOT_DRAIN_QUIET_MS) \
                        or now - slot.drain_since >= BACKEND_SLOT_DRAIN_MAX_MS:
                    self._begin_stop(slot, now)
            elif slot.phase == "stopping":
                if alive and now - slot.stop_since >= BACKEND_STOP_KILL_AFTER_MS:
                    self.ex.kill(slot.handle)
                if not alive and not up:
                    self.ex.admin("UNDRAIN", slot.lb_id)
                    if slot.partitioned:
                        self.ex.set_gre(slot.n, True)
                    self._event(now, f"slot {slot.n}: freed (was {slot.owner})")
                    slot.clear()

            if alive and slot.phase in ("starting", "serving", "unhealthy", "draining") \
                    and slot.owner in want and not slot.removing:
                cfg = _cfg(want[slot.owner], BACKEND_CONFIG_KEYS)
                if cfg != slot.config:
                    self.ex.send(slot.handle, {"op": "config", **cfg})
                    slot.config = cfg

    def _begin_stop(self, slot, now):
        if self.ex.alive(slot.handle):
            self.ex.terminate(slot.handle)
        slot.phase, slot.stop_since = "stopping", now
