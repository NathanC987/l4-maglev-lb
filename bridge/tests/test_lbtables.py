"""The bridge's half of the live tables view: caching the Maglev table and translating
maglev-lb's ids into canvas nodes (no LB needed)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import lbtables  # noqa: E402

DOC = {"generation": 3, "ageMs": 40, "m": 7, "table": "AQIBAgECAQ==",
       "backends": [{"id": 0, "addr": "10.0.0.1", "port": 8080, "slots": 4},
                    {"id": 1, "addr": "10.0.0.2", "port": 8080, "slots": 3}],
       "conntrack": {"active": 2, "scanUs": 7, "rowsPerBackend": 16,
                     "flows": [{"id": 0, "n": 1}, {"id": 5, "n": 1}],
                     "rows": [{"src": "10.99.1.10", "sport": 4000, "dport": 8080, "proto": "tcp",
                               "id": 0, "slot": 2, "idleMs": 1},
                              {"src": "10.9.9.9", "sport": 1, "dport": 8080, "proto": "udp",
                               "id": 5, "slot": 6, "idleMs": 900}]}}


class TestCache(unittest.TestCase):
    def test_reuses_a_table_only_for_the_same_process_and_generation(self):
        c = lbtables.TablesCache()
        self.assertIsNone(c.known(100))
        self.assertIsNone(c.merge(100, {"pending": True}))
        self.assertIsNone(c.merge(100, None))
        self.assertEqual(c.merge(100, DOC)["table"], DOC["table"])
        self.assertEqual(c.known(100), 3)
        slim = {k: v for k, v in DOC.items() if k != "table"}
        self.assertEqual(c.merge(100, slim)["table"], DOC["table"])
        # A restarted maglev-lb counts generations from 1 again: never mix the two.
        self.assertIsNone(c.known(200))
        self.assertIsNone(c.merge(200, slim))
        self.assertIsNone(c.merge(100, {**slim, "generation": 4}))


class TestToCanvas(unittest.TestCase):
    def test_ids_become_canvas_nodes(self):
        out = lbtables.to_canvas(DOC, {0: "b1", 1: "b2"}, {"10.99.1.10": "c1"}, None, {"source": {"where": "x"}})
        self.assertEqual([b["node"] for b in out["backends"]], ["b1", "b2"])
        self.assertEqual(out["conntrack"]["flows"], [{"lbId": 0, "node": "b1", "n": 1},
                                                     {"lbId": 5, "node": None, "n": 1}])
        rows = out["conntrack"]["rows"]
        self.assertEqual((rows[0]["client"], rows[0]["node"], rows[0]["slot"]), ("c1", "b1", 2))
        self.assertEqual((rows[1]["client"], rows[1]["node"]), (None, None))
        self.assertEqual(out["source"], {"where": "x"})
        self.assertEqual(out["table"], DOC["table"])

    def test_table_left_out_when_the_canvas_has_it(self):
        self.assertNotIn("table", lbtables.to_canvas(DOC, {}, {}, 3, {}))
        self.assertIn("table", lbtables.to_canvas(DOC, {}, {}, 2, {}))


if __name__ == "__main__":
    unittest.main()
