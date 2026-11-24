"""The Grafana feed's parts: CloudWatch polling (bridge/cloudwatch.py) and the
Prometheus text (bridge/promexport.py)."""
import calendar
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cloudwatch  # noqa: E402
import promexport  # noqa: E402

TARGETS = (("be1", "i-0b1"), ("lb", "i-0lb"))


class FakeAws:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def metric_data(self, qs, start, end):
        self.calls.append(qs)
        if self.fail:
            raise RuntimeError(self.fail)
        return {"MetricDataResults": [
            {"Id": q["Id"], "Timestamps": ["2026-11-25T01:00:00+00:00", "2026-11-25T00:59:00+00:00"],
             "Values": [600.0, 1.0]} for q in qs]}


class Clock:
    def __init__(self):
        self.t = float(calendar.timegm((2026, 11, 25, 1, 1, 0)))

    def __call__(self):
        return self.t


def poller(aws, watched=True):
    clock = Clock()
    return cloudwatch.CloudWatchPoller(aws, lambda: TARGETS, lambda: watched, clock), clock


def asked(qs):
    return sorted((q["MetricStat"]["Metric"]["Dimensions"][0]["Value"], q["MetricStat"]["Metric"]["MetricName"],
                   q["MetricStat"]["Period"]) for q in qs)


class CloudWatchTest(unittest.TestCase):
    def test_nothing_is_asked_while_nobody_watches(self):
        aws = FakeAws()
        p, _ = poller(aws, watched=False)
        self.assertFalse(p.poll_once())
        self.assertEqual(aws.calls, [])

    def test_lb_every_minute_the_rest_every_five(self):
        aws = FakeAws()
        p, clock = poller(aws)
        self.assertTrue(p.poll_once())
        first = asked(aws.calls[0])
        self.assertIn(("i-0lb", "CPUUtilization", 60), first)
        self.assertIn(("i-0b1", "NetworkPacketsOut", 300), first)
        self.assertNotIn(("i-0lb", "CPUUtilization", 300), first, "the LB's CPU is asked once, at 1 minute")
        clock.t += 30
        self.assertFalse(p.poll_once(), "nothing is due yet")
        clock.t += 31
        self.assertTrue(p.poll_once())
        self.assertEqual({i for i, _, _ in asked(aws.calls[1])}, {"i-0lb"}, "only the LB's 1-minute metrics")
        clock.t += 240
        p.poll_once()
        self.assertIn(("i-0b1", "CPUUtilization", 300), asked(aws.calls[2]))
        self.assertEqual(len(aws.calls), 3)

    def test_sums_become_rates_and_the_newest_point_wins(self):
        p, _ = poller(FakeAws())
        p.poll_once()
        o = promexport._Out()
        p.render_into(o)
        text = o.text()
        self.assertIn('canvas_cloudwatch_network_out_bytes_per_second{role="lb",instance_id="i-0lb"} 10\n', text)
        self.assertIn('canvas_cloudwatch_network_out_bytes_per_second{role="be1",instance_id="i-0b1"} 2\n', text)
        self.assertIn('canvas_cloudwatch_cpu_percent{role="lb",instance_id="i-0lb"} 600\n', text)
        self.assertIn('canvas_cloudwatch_age_seconds{role="lb",instance_id="i-0lb",metric="CPUUtilization"} 60\n',
                      text)
        self.assertIn('canvas_cloudwatch_ok{state="ok"} 1', text)

    def test_denied_backs_off_and_says_how_to_fix_it(self):
        aws = FakeAws(fail="An error occurred (AccessDenied) when calling the GetMetricData operation")
        p, clock = poller(aws)
        self.assertFalse(p.poll_once())
        self.assertEqual(p.state, "denied")
        self.assertIn("canvas.sh up", p.error)
        clock.t += 300
        p.poll_once()
        self.assertEqual(len(aws.calls), 1, "a denied read is not retried for 10 minutes")
        o = promexport._Out()
        p.render_into(o)
        self.assertIn('canvas_cloudwatch_ok{state="denied"} 0', o.text())

    def test_the_cli_call(self):
        t = calendar.timegm((2026, 11, 25, 1, 1, 0))
        args = cloudwatch.metric_data_args([{"Id": "q0"}], t, t + 60)
        self.assertEqual(args[:3], ["cloudwatch", "get-metric-data", "--metric-data-queries"])
        self.assertIn("2026-11-25T01:01:00Z", args)


class ExportTest(unittest.TestCase):
    SNAP = {
        "status": {"loadEnabled": True, "errors": []},
        "clients": {"c1": {"ip": "10.99.1.10", "rates": {"scheduled": 100, "ok": 99, "failed": 1},
                           "latencyMs": {"p99": 12.5}}},
        "backends": {
            'we"ird\\id': {"slot": 1, "addr": "10.99.1.31:9000", "phase": "serving",
                           "rates": {"arrivals": 50}, "latencyMs": {"p99": 3}, "lb": {"up": True}},
            "b9": {"slot": None, "phase": "pending"},
        },
        "lb": {"id": "lb1", "state": "running", "rxPps": 300, "cpuDatapath": None, "cpuProcess": 0.02,
               "dropsPps": {"no_backend": 0.5}, "conntrack": {"activeFlows": 7}},
        "system": {"latencyMs": {"p50": 1, "p95": 2, "p99": float("nan")}},
        "cloud": {"stack": "l4mlb", "region": "us-east-1", "running": 1, "usdPerHour": 0.09,
                  "hosts": [{"role": "lb", "id": "i-0lb", "type": "c7i-flex.large", "state": "ready",
                             "cpu": 0.1, "rxBps": 10, "txBps": 20}, None]},
    }

    def test_valid_text(self):
        text = promexport.render(self.SNAP)
        line = re.compile(r'^[a-z_0-9]+(\{([a-z_]+="([^"\\]|\\.)*",?)+\})? -?[0-9.e+-]+$')
        seen = set()
        for ln in text.splitlines():
            if ln.startswith("# HELP "):
                name = ln.split()[2]
                self.assertNotIn(name, seen, f"HELP twice for {name}")
                seen.add(name)
            elif not ln.startswith("# TYPE "):
                self.assertRegex(ln, line)
        self.assertIn('canvas_requests_per_second{result="offered"} 100', text)
        self.assertIn('backend="we\\"ird\\\\id",host="be1",addr="10.99.1.31"', text)
        self.assertIn('canvas_backend_info{backend="b9",phase="pending"} 1', text)
        self.assertNotIn('quantile="p99"', text, "NaN is left out")
        self.assertIn('canvas_lb_cpu_ratio{part="process"} 0.02', text)
        # Its HELP survives the first sample being missing.
        self.assertIn("# HELP canvas_lb_cpu_ratio Share of one core", text)
        self.assertIn('canvas_aws_instance_cpu_ratio{role="lb",instance_id="i-0lb",type="c7i-flex.large"} 0.1', text)


if __name__ == "__main__":
    unittest.main()
