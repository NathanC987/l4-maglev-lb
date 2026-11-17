"""The local bridge's snapshot without its topology: Bridge shares code with the AWS
bridge, and its real test (scripts/live/run-live-smoke-test.sh) needs root."""
import asyncio
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from live_bridge import Bridge  # noqa: E402
from schema import validate_desired  # noqa: E402


class LocalSnapshot(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        args = types.SimpleNamespace(drop_uid=os.getuid(), drop_gid=os.getgid(), slots=16,
                                     ct_timeout_ms=15000, record=None)
        self.b = Bridge(args, self.loop)
        self.b.r.set_desired(validate_desired({
            "schema": "l4mlb-live/1",
            "nodes": {"lb": "lb", "clients": [{"id": "c1"}], "backends": [{"id": "b1"}]},
            "edges": [], "load": {"enabled": False}}))

    def test_local_shape_is_unchanged(self):
        snap = self.b.build_snapshot()
        self.assertNotIn("cloud", snap)
        self.assertEqual(snap["lb"]["config"]["measuredCeilingPps"], 520000)
        self.assertEqual(snap["lb"]["state"], "stopped")
        self.assertEqual(snap["status"]["phase"], "degraded")
        self.assertIn("maglev-lb is not running", snap["status"]["errors"])
        self.assertEqual(snap["backends"]["b1"]["phase"], "detached")
        self.assertEqual(snap["clients"]["c1"]["state"], "disconnected")

    def test_local_addresses(self):
        self.assertEqual(self.b.ex.backend_addr(1), "10.98.0.11:9000")
        self.assertEqual(self.b.ex.client_ip(2), "10.98.0.202")
        self.assertTrue(self.b.ex.host_ready(1))
        self.assertTrue(self.b.ex.client_host_ready(0))
        self.assertFalse(self.b.ex.lost(None))


if __name__ == "__main__":
    unittest.main()
