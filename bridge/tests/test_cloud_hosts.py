"""HostManager with EC2 and SSH faked and the clock driven by hand: which instances
are started and stopped, in what order, and when it gives up."""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cloud_hosts import HostManager  # noqa: E402


def info(role, state="stopped", typ="t3.micro"):
    return {"role": role, "id": f"i-{role}", "privateIp": "x", "mac": "m", "type": typ, "state": state,
            "publicIp": "1.2.3.4" if state == "running" else None}


class FakeAws:
    def __init__(self, roles, state="stopped"):
        self.state = {r: state for r in roles}
        self.calls = []
        self.fail = False

    def describe(self):
        return [info(r, s) for r, s in self.state.items()]

    def _set(self, verb, ids, new):
        self.calls.append((verb, sorted(i[2:] for i in ids)))
        if self.fail:
            raise RuntimeError("boom")
        for i in ids:
            self.state[i[2:]] = new

    def start(self, ids):
        self._set("start", ids, "running")

    def stop(self, ids):
        self._set("stop", ids, "stopped")


class FakePrep:
    def __init__(self):
        self.prepared, self.leases, self.disconnected = [], [], []
        self.fail = False

    def prepare(self, host, lease_min):
        if self.fail:
            raise RuntimeError("no ssh yet")
        self.prepared.append(host.role)

    def renew_lease(self, host, lease_min):
        self.leases.append(host.role)

    def disconnect(self, host):
        self.disconnected.append(host.role)


class Base(unittest.IsolatedAsyncioTestCase):
    def setup_mgr(self, roles=("lb", "be1", "be2"), state="stopped", grace=60.0):
        self.aws = FakeAws(roles, state)
        self.prep = FakePrep()
        self.events = []
        self.m = HostManager(self.aws, self.prep, [info(r, state) for r in roles],
                             lambda msg, level="info": self.events.append(msg), stop_grace_s=grace)
        self.t = 1000.0

    async def tick(self, wanted, dt=1.0):
        self.t += dt
        self.m.tick(self.t, set(wanted))
        for _ in range(20):
            await asyncio.sleep(0)
        await asyncio.sleep(0.01)
        for _ in range(20):
            await asyncio.sleep(0)

    async def settle(self, wanted, n=8, dt=10.0):
        for _ in range(n):
            await self.tick(wanted, dt)


class Lifecycle(Base):
    async def test_starts_nothing_unwanted(self):
        self.setup_mgr()
        await self.settle(())
        self.assertEqual([c for c in self.aws.calls if c[0] == "start"], [])

    async def test_starts_wanted_in_one_batch_then_becomes_ready(self):
        self.setup_mgr()
        await self.tick(("lb", "be1"))
        self.assertEqual(self.aws.calls, [("start", ["be1", "lb"])])
        self.assertFalse(self.m.ready("be1"))
        await self.settle(("lb", "be1"))
        self.assertTrue(self.m.ready("be1"))
        self.assertTrue(self.m.ready("lb"))
        self.assertFalse(self.m.ready("be2"))
        self.assertCountEqual(self.prep.prepared, ["lb", "be1"])

    async def test_not_ready_until_prepared(self):
        self.setup_mgr()
        self.prep.fail = True
        await self.settle(("be1",))
        self.assertEqual(self.aws.state["be1"], "running")
        self.assertFalse(self.m.ready("be1"))
        self.prep.fail = False
        await self.settle(("be1",))
        self.assertTrue(self.m.ready("be1"))

    async def test_unwanted_host_stops_only_after_the_grace_period(self):
        self.setup_mgr(grace=60.0)
        await self.settle(("be1",))
        await self.tick((), 1.0)
        await self.tick((), 30.0)
        self.assertEqual(self.aws.state["be1"], "running")
        await self.tick((), 31.0)
        self.assertEqual(self.aws.state["be1"], "stopped")
        self.assertIn("be1", self.prep.disconnected)
        self.assertFalse(self.m.ready("be1"))

    async def test_wanting_it_again_cancels_the_stop(self):
        self.setup_mgr(grace=60.0)
        await self.settle(("be1",))
        await self.tick((), 1.0)
        await self.tick((), 30.0)
        await self.tick(("be1",), 1.0)
        await self.tick(("be1",), 100.0)
        self.assertEqual([c for c in self.aws.calls if c[0] == "stop"], [])

    async def test_restart_after_stop_prepares_again(self):
        self.setup_mgr(grace=1.0)
        await self.settle(("be1",))
        await self.settle((), n=3)
        self.assertEqual(self.aws.state["be1"], "stopped")
        await self.settle(("be1",))
        self.assertTrue(self.m.ready("be1"))
        self.assertEqual(self.prep.prepared.count("be1"), 2)

    async def test_leftover_running_host_is_stopped_if_unwanted(self):
        self.setup_mgr(state="running", grace=5.0)
        await self.settle((), n=3)
        self.assertEqual(set(self.aws.state.values()), {"stopped"})

    async def test_leftover_running_host_is_reused_if_wanted(self):
        self.setup_mgr(roles=("be1",), state="running")
        await self.settle(("be1",), n=2)
        self.assertTrue(self.m.ready("be1"))
        self.assertEqual(self.aws.calls, [])

    async def test_host_that_stopped_by_itself_is_noticed_and_restarted(self):
        self.setup_mgr(roles=("be1",))
        await self.settle(("be1",))
        self.aws.state["be1"] = "stopped"
        self.m.next_poll = 0
        await self.tick(("be1",))
        self.assertFalse(self.m.ready("be1"))
        await self.settle(("be1",))
        self.assertEqual([c[0] for c in self.aws.calls].count("start"), 2)
        self.assertTrue(self.m.ready("be1"))

    async def test_an_aws_failure_is_retried_not_fatal(self):
        self.setup_mgr(roles=("be1",))
        self.aws.fail = True
        await self.tick(("be1",))
        self.assertFalse(self.m.ready("be1"))
        await self.tick(("be1",), 1.0)
        self.assertEqual(len(self.aws.calls), 1, "backs off rather than hammering AWS")
        self.aws.fail = False
        await self.settle(("be1",))
        self.assertTrue(self.m.ready("be1"))

    async def test_lease_is_renewed_while_ready_and_not_when_stopped(self):
        self.setup_mgr(roles=("be1",), grace=1.0)
        await self.settle(("be1",))
        self.prep.leases.clear()
        await self.tick(("be1",), 301.0)
        self.assertEqual(self.prep.leases, ["be1"])
        await self.settle((), n=3)
        self.prep.leases.clear()
        await self.tick((), 301.0)
        self.assertEqual(self.prep.leases, [])

    async def test_mark_unreachable_prepares_again(self):
        self.setup_mgr(roles=("be1",))
        await self.settle(("be1",))
        self.m.mark_unreachable("be1")
        self.assertFalse(self.m.ready("be1"))
        await self.settle(("be1",))
        self.assertTrue(self.m.ready("be1"))
        self.assertEqual(self.prep.prepared.count("be1"), 2)


class Cost(Base):
    async def test_estimate_counts_only_running_compute_plus_disks(self):
        self.setup_mgr(roles=("lb", "be1"))
        idle = self.m.usd_per_hour()
        self.assertLess(idle, 0.002, "stopped instances cost only their disks")
        await self.settle(("be1",))
        self.assertAlmostEqual(self.m.usd_per_hour() - idle, 0.0154, places=4)

    async def test_spend_accumulates_with_time(self):
        self.setup_mgr(roles=("be1",))
        await self.settle(("be1",), n=3)
        before = self.m.spent_usd
        await self.tick(("be1",), 3600.0)
        self.assertAlmostEqual(self.m.spent_usd - before, self.m.usd_per_hour(), places=4)


class StopAll(Base):
    async def test_stops_what_runs_and_reports_nothing_left(self):
        self.setup_mgr(state="running")
        left = await self.m.stop_all(sleep=lambda s: asyncio.sleep(0))
        self.assertEqual(left, [])
        self.assertEqual(set(self.aws.state.values()), {"stopped"})
        self.assertCountEqual(self.prep.disconnected, ["lb", "be1", "be2"])

    async def test_reports_what_it_could_not_stop(self):
        self.setup_mgr(state="running")
        self.aws.fail = True
        clock = iter(range(0, 100000, 1000))
        left = await self.m.stop_all(sleep=lambda s: asyncio.sleep(0), clock=lambda: next(clock))
        self.assertCountEqual(left, ["lb", "be1", "be2"])


if __name__ == "__main__":
    unittest.main()
