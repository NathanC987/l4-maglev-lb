"""cloud/rrstats.py against stats lines recorded from a real bridge/load_client.py
run (200 req/s for ~5 s against a local service_server.py, no LB)."""
import json
import pathlib
import subprocess
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import rrstats  # noqa: E402

FIXTURE = HERE / "fixtures" / "load_client_stats.jsonl"


def lines():
    return rrstats.read_stats(FIXTURE.read_text().splitlines() + ["not json", "{broken"])


class RrstatsTest(unittest.TestCase):
    def test_one_process(self):
        s = rrstats.summarize([lines()], skip_s=1)
        self.assertEqual(s["processes"], 1)
        self.assertAlmostEqual(s["offeredRps"], 200, delta=20)
        self.assertEqual(s["failed"], 0)
        self.assertEqual(sum(s["repliesBySlot"].values()), s["ok"])
        p = s["latencyMs"]
        self.assertTrue(0 < p["p50"] <= p["p90"] <= p["p99"], p)

    def test_two_processes_add_up_and_keep_the_same_percentiles(self):
        one = rrstats.summarize([lines()], skip_s=1)
        two = rrstats.summarize([lines(), lines()], skip_s=1)
        self.assertEqual(two["ok"], 2 * one["ok"])
        self.assertAlmostEqual(two["offeredRps"], 2 * one["offeredRps"], delta=0.5)
        self.assertEqual(two["latencyMs"], one["latencyMs"])

    def test_warm_up_longer_than_the_run_is_an_error(self):
        self.assertIn("error", rrstats.summarize([lines()], skip_s=100))
        self.assertIn("error", rrstats.summarize([lines(), []], skip_s=1))

    def test_cli(self):
        out = subprocess.run([sys.executable, str(HERE.parent / "rrstats.py"), str(FIXTURE),
                              "--skip-s", "1"], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(out.stdout)["failed"], 0)


if __name__ == "__main__":
    unittest.main()
