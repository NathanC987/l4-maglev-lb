import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from schema import ValidationError, loads, validate_action, validate_desired  # noqa: E402


def base(**over):
    d = {"schema": "l4mlb-live/1",
         "nodes": {"lb": "lb", "clients": [{"id": "c1", "rps": 100}],
                   "backends": [{"id": "s1"}, {"id": "s2", "capacity": 4}]},
         "edges": [{"from": "c1", "to": "lb"}, {"from": "lb", "to": "s1"}],
         "load": {"enabled": True}}
    d.update(over)
    return d


class TestDesired(unittest.TestCase):
    def test_valid_normalizes_with_defaults(self):
        d = validate_desired(base())
        self.assertEqual(d["lb"], "lb")
        self.assertEqual(d["clients"][0]["rps"], 100.0)
        self.assertEqual(d["clients"][0]["timeoutMs"], 1000)
        self.assertTrue(d["clients"][0]["connected"])
        self.assertTrue(d["backends"][0]["attached"])
        self.assertFalse(d["backends"][1]["attached"])  # s2 has no lb edge: detached
        self.assertEqual(d["backends"][1]["capacity"], 4)

    def assertRejects(self, doc, fragment):
        with self.assertRaises(ValidationError) as cm:
            validate_desired(doc)
        self.assertTrue(any(fragment in e["path"] or fragment in e["msg"]
                            for e in cm.exception.errors), cm.exception.errors)

    def test_bounds_bools_unknown_keys_ids(self):
        d = base()
        d["nodes"]["clients"][0]["rps"] = 1e9
        self.assertRejects(d, "rps")
        d = base()
        d["nodes"]["backends"][0]["capacity"] = True
        self.assertRejects(d, "capacity")
        d = base()
        d["nodes"]["backends"][0]["capacity"] = 2.5
        self.assertRejects(d, "integer")
        d = base()
        d["nodes"]["backends"][0]["evil"] = 1
        self.assertRejects(d, "unknown key")
        d = base()
        d["nodes"]["backends"][0]["id"] = "../../etc"
        self.assertRejects(d, "must match")
        self.assertRejects(base(extra=1), "unknown key")
        self.assertRejects(base(schema="v0"), "schema")

    def test_graph_rules(self):
        d = base()
        d["edges"].append({"from": "c1", "to": "s1"})
        self.assertRejects(d, "only knows the VIP")
        d = base()
        d["edges"].append({"from": "s1", "to": "c1"})
        self.assertRejects(d, "only client->lb")
        d = base()
        d["nodes"]["backends"].append({"id": "lb"})
        self.assertRejects(d, "unique")
        d = base()
        d["nodes"]["backends"] = [{"id": f"s{i}"} for i in range(17)]
        self.assertRejects(d, "at most 16")
        d = base()
        d["nodes"]["clients"][0]["maxConnAgeMs"] = 500
        self.assertRejects(d, "maxConnAgeMs")

    def test_total_rps_cap(self):
        d = base()
        d["nodes"]["clients"] = [{"id": f"c{i}", "rps": 2000} for i in range(3)]
        d["edges"] = []
        self.assertRejects(d, "total rps")

    def test_strict_json(self):
        with self.assertRaises(ValueError):
            loads('{"a": NaN}')
        with self.assertRaises(ValueError):
            loads('{"a": 1, "a": 2}')


class TestAction(unittest.TestCase):
    def test_actions(self):
        self.assertEqual(validate_action({"action": "slow", "target": "s1", "factor": 3})["factor"], 3.0)
        self.assertEqual(validate_action({"action": "errors", "target": "s1"})["rate"], 0.3)
        for bad in ({"action": "rm -rf", "target": "s1"},
                    {"action": "crash", "target": "s 1"},
                    {"action": "crash", "target": "s1", "factor": 2},
                    {"action": "slow", "target": "s1", "factor": 99}):
            with self.assertRaises(ValidationError):
                validate_action(bad)


if __name__ == "__main__":
    unittest.main()
