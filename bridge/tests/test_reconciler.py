import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from reconciler import ActionError, Reconciler  # noqa: E402
from schema import validate_desired  # noqa: E402


class FakeExecutor:
    def __init__(self):
        self.calls = []
        self.procs = {}
        self.stats = {}
        self.n = 0

    def _new(self, kind, *args):
        self.n += 1
        h = f"{kind}{self.n}"
        self.procs[h] = True
        self.calls.append(("spawn_" + kind, *args))
        return h

    def spawn_backend(self, slot, cfg):
        return self._new("backend", slot, cfg["capacity"])

    def spawn_client(self, index, cfg, enabled):
        return self._new("client", index, enabled)

    def send(self, h, obj):
        self.calls.append(("send", h, obj["op"]))

    def terminate(self, h):
        self.calls.append(("terminate", h))
        self.procs[h] = False

    def kill(self, h):
        self.calls.append(("kill", h))
        self.procs[h] = False

    def alive(self, h):
        return bool(h) and self.procs.get(h, False)

    def latest(self, h):
        return self.stats.get(h)

    def admin(self, verb, lb_id):
        self.calls.append(("admin", verb, lb_id))

    def set_gre(self, slot, up):
        self.calls.append(("gre", slot, up))

    def take(self):
        c, self.calls = self.calls, []
        return c


def desired(backends=("b1", "b2"), clients=("c1",), attached=None, load=True, **cfg):
    attached = set(backends) if attached is None else set(attached)
    return validate_desired({
        "schema": "l4mlb-live/1",
        "nodes": {"lb": "lb",
                  "clients": [{"id": c} for c in clients],
                  "backends": [{"id": b, **cfg} for b in backends]},
        "edges": [{"from": c, "to": "lb"} for c in clients]
                 + [{"from": "lb", "to": b} for b in backends if b in attached],
        "load": {"enabled": load},
    })


def all_up(*ids):
    return {i: {"up": True, "adminEnabled": True} for i in ids}


class TestReconciler(unittest.TestCase):
    def setUp(self):
        self.ex = FakeExecutor()
        self.r = Reconciler(self.ex, n_slots=4)

    def test_add_backends_start_then_serve(self):
        self.r.set_desired(desired())
        self.r.step(0, {})
        calls = self.ex.take()
        self.assertIn(("spawn_backend", 1, 8), calls)
        self.assertIn(("spawn_backend", 2, 8), calls)
        self.assertIn(("spawn_client", 0, True), calls)
        self.assertEqual(self.r.slot_of("b1").phase, "starting")
        self.r.step(100, all_up(0, 1))
        self.assertEqual(self.r.slot_of("b1").phase, "serving")
        self.r.step(200, all_up(1))
        self.assertEqual(self.r.slot_of("b1").phase, "unhealthy")

    def test_removal_drains_then_stops_then_undrains_only_after_down(self):
        self.r.set_desired(desired())
        self.r.step(0, {})
        self.r.step(100, all_up(0, 1))
        h = self.r.slot_of("b2").handle
        self.ex.take()
        self.r.set_desired(desired(backends=("b1",)))
        self.r.step(200, all_up(0, 1))
        self.assertIn(("admin", "DRAIN", 1), self.ex.take())
        slot = self.r.slots[2]
        self.assertEqual(slot.phase, "draining")
        self.ex.stats[h] = {"busy": 0, "queued": 0, "openConns": 0}
        self.r.step(300, all_up(0, 1))
        self.r.step(1400, all_up(0, 1))  # idle for >= 1s
        self.assertIn(("terminate", h), self.ex.take())
        self.assertEqual(slot.phase, "stopping")
        self.r.step(1500, all_up(0, 1))  # process gone but LB still says up
        self.assertNotIn(("admin", "UNDRAIN", 1), self.ex.take())
        self.assertEqual(slot.phase, "stopping")
        self.r.step(1600, all_up(0))     # now down
        self.assertIn(("admin", "UNDRAIN", 1), self.ex.take())
        self.assertEqual(slot.phase, "free")
        self.assertIsNone(slot.owner)

    def test_readd_during_drain_cancels_it(self):
        self.r.set_desired(desired())
        self.r.step(0, {})
        self.r.step(100, all_up(0, 1))
        self.r.set_desired(desired(backends=("b1",)))
        self.r.step(200, all_up(0, 1))
        self.ex.take()
        self.r.set_desired(desired())
        self.r.step(300, all_up(0, 1))
        self.assertIn(("admin", "UNDRAIN", 1), self.ex.take())
        self.assertEqual(self.r.slot_of("b2").phase, "serving")
        self.assertEqual(self.r.slot_of("b2").n, 2)

    def test_drain_times_out_when_never_idle(self):
        self.r.set_desired(desired())
        self.r.step(0, {})
        self.r.step(100, all_up(0, 1))
        h = self.r.slot_of("b2").handle
        self.ex.stats[h] = {"busy": 3, "queued": 0, "openConns": 2}  # pooled conns never leave
        self.r.set_desired(desired(backends=("b1",)))
        self.r.step(200, all_up(0, 1))
        self.r.step(20000, all_up(0, 1))
        self.assertEqual(self.r.slots[2].phase, "draining")
        self.r.step(30300, all_up(0, 1))
        self.assertEqual(self.r.slots[2].phase, "stopping")

    def test_slot_memory_returns_same_slot(self):
        self.r.set_desired(desired())
        self.r.step(0, {})
        self.r.step(100, all_up(0, 1))
        self.r.set_desired(desired(backends=("b2",)))  # remove b1 (slot 1)
        self.r.step(200, all_up(0, 1))
        h = self.r.slots[1].handle
        self.ex.stats[h] = {"busy": 0, "queued": 0, "openConns": 0}
        self.r.step(300, all_up(0, 1))
        self.r.step(1400, all_up(0, 1))
        self.r.step(1500, all_up(1))
        self.assertEqual(self.r.slots[1].phase, "free")
        self.r.set_desired(desired(backends=("b1", "b2")))  # undo
        self.r.step(1600, all_up(1))
        self.assertEqual(self.r.slot_of("b1").n, 1)

    def test_crash_restart_and_faults(self):
        self.r.set_desired(desired())
        self.r.step(0, {})
        self.r.step(100, all_up(0, 1))
        h = self.r.slot_of("b1").handle
        self.ex.take()
        self.r.action({"action": "crash", "target": "b1"}, 200)
        self.assertIn(("kill", h), self.ex.take())
        self.assertEqual(self.r.slot_of("b1").phase, "crashed")
        self.r.step(300, all_up(1))
        self.assertEqual(self.r.slot_of("b1").phase, "crashed")  # stays until restart
        self.r.action({"action": "restart", "target": "b1"}, 400)
        self.assertIn(("spawn_backend", 1, 8), self.ex.take())
        self.assertEqual(self.r.slot_of("b1").phase, "starting")
        self.assertIsNone(self.r.slot_of("b1").fault)

        self.r.action({"action": "partition", "target": "b2"}, 500)
        self.assertIn(("gre", 2, False), self.ex.take())
        self.r.action({"action": "slow", "target": "b2", "factor": 4}, 600)  # replaces partition
        calls = self.ex.take()
        self.assertIn(("gre", 2, True), calls)
        self.assertEqual(self.r.slot_of("b2").fault["kind"], "slow")
        self.r.action({"action": "clear", "target": "b2"}, 700)
        self.assertIsNone(self.r.slot_of("b2").fault)

    def test_unexpected_exit_is_crash(self):
        self.r.set_desired(desired())
        self.r.step(0, {})
        self.r.step(100, all_up(0, 1))
        self.ex.procs[self.r.slot_of("b1").handle] = False
        self.r.step(200, all_up(0, 1))
        self.assertEqual(self.r.slot_of("b1").phase, "crashed")

    def test_manual_drain_and_undrain(self):
        self.r.set_desired(desired())
        self.r.step(0, {})
        self.r.step(100, all_up(0, 1))
        self.ex.take()
        self.r.action({"action": "drain", "target": "b1"}, 200)
        self.assertIn(("admin", "DRAIN", 0), self.ex.take())
        self.r.step(300, all_up(0, 1))
        self.assertEqual(self.r.slot_of("b1").phase, "draining")  # manual drain doesn't stop
        self.r.action({"action": "undrain", "target": "b1"}, 400)
        self.assertEqual(self.r.slot_of("b1").phase, "serving")

    def test_action_on_unknown_target(self):
        self.r.set_desired(desired())
        self.r.step(0, {})
        with self.assertRaises(ActionError):
            self.r.action({"action": "crash", "target": "nope"}, 1)

    def test_detached_backend_is_not_started_and_clients_follow_load(self):
        self.r.set_desired(desired(attached=("b1",), load=False))
        self.r.step(0, {})
        calls = self.ex.take()
        self.assertIn(("spawn_backend", 1, 8), calls)
        self.assertNotIn(("spawn_backend", 2, 8), calls)
        self.assertIn(("spawn_client", 0, False), calls)
        self.r.set_desired(desired(attached=("b1",), load=True))
        self.r.step(100, {})
        self.assertIn(("send", self.r.clients["c1"].handle, "load"), self.ex.take())
        self.r.load_suppressed = True
        self.r.step(200, {})
        self.assertIn(("send", self.r.clients["c1"].handle, "load"), self.ex.take())

    def test_config_change_is_sent_not_respawned(self):
        self.r.set_desired(desired())
        self.r.step(0, {})
        self.r.step(100, all_up(0, 1))
        self.ex.take()
        self.r.set_desired(desired(capacity=32))
        self.r.step(200, all_up(0, 1))
        calls = self.ex.take()
        self.assertEqual([c for c in calls if c[0] == "send" and c[2] == "config"].__len__(), 2)
        self.assertFalse(any(c[0] == "spawn_backend" for c in calls))

    def test_no_free_slot_is_pending(self):
        self.r.set_desired(desired(backends=("a", "b", "c", "d", "e")))
        self.r.step(0, {})
        self.assertEqual(self.r.pending, {"e"})


if __name__ == "__main__":
    unittest.main()
