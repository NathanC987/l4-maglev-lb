"""cloud/render_dashboard.py and the dashboard templates it fills."""
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "cloud"))
import render_dashboard  # noqa: E402

TEMPLATES = ROOT / "monitoring" / "grafana-cloud" / "dashboards"
ENV = {"LB": "i-0lb", "CLIENT": "i-0cli", "BE1": "i-0be1", "BE2": "i-0be2", "BE3": "i-0be3",
       "BE4": "i-0be4", "REGION": "us-east-1", "ACCOUNT": "123456789012",
       "LOG_GROUP": "/l4mlb/vpc-flow-logs"}


class RenderTest(unittest.TestCase):
    def test_every_placeholder_is_filled_and_the_json_stays_valid(self):
        subs = render_dashboard.substitutions(ENV)
        files = sorted(TEMPLATES.glob("*.json"))
        self.assertEqual([f.name for f in files],
                         ["maglev-lb-aws-referee.json", "maglev-lb-cloud.json"])
        for f in files:
            out = render_dashboard.render(f.read_text(), subs)
            self.assertNotRegex(out, r"__[A-Z0-9_]+__", f.name)
            json.loads(out)

    def test_log_group_arn_is_not_clobbered_by_the_shorter_placeholder(self):
        out = render_dashboard.render("__LOG_GROUP_ARN__|__LOG_GROUP__",
                                      render_dashboard.substitutions(ENV))
        self.assertEqual(out, "arn:aws:logs:us-east-1:123456789012:log-group:/l4mlb/vpc-flow-logs"
                              "|/l4mlb/vpc-flow-logs")

    def test_missing_backends_become_none(self):
        env = {k: v for k, v in ENV.items() if k not in ("BE2", "BE3", "BE4")}
        subs = render_dashboard.substitutions(env)
        self.assertEqual(subs["__BE4__"], "none")

    def test_dashboards_use_only_the_cloud_datasources(self):
        for f in TEMPLATES.glob("*.json"):
            uids = set()

            def walk(o):
                if isinstance(o, dict):
                    ds = o.get("datasource")
                    if isinstance(ds, dict) and ds.get("uid"):
                        uids.add(ds["uid"])
                    for v in o.values():
                        walk(v)
                elif isinstance(o, list):
                    for v in o:
                        walk(v)
            walk(json.loads(f.read_text()))
            self.assertLessEqual(uids, {"prometheus-cloud", "cloudwatch", "-- Mixed --"}, f.name)


if __name__ == "__main__":
    unittest.main()
