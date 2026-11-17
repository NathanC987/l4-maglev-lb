"""cloud/referee.py against synthetic flow-log records (no AWS needed).

    python3 -m unittest discover -s cloud/tests
"""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import referee  # noqa: E402

LB, CLI, BE1, BE2, VIP = "10.99.1.20", "10.99.1.10", "10.99.1.31", "10.99.1.32", "10.200.0.10"
ROLES = {"i-lb": "lb", "i-cli": "client", "i-be1": "be1", "i-be2": "be2"}
T0, T1 = 1_000_000, 1_000_300


def rec(inst, direction, src, dst, psrc, pdst, proto, packets, nbytes,
        start=T0 + 10, end=T0 + 70, sport=0, dport=0, status="OK", action="ACCEPT"):
    eni = "eni-" + inst[2:]
    return {"message": f"5 {eni} {inst} {direction} {src} {dst} {psrc} {pdst} {sport} {dport} "
                       f"{proto} {packets} {nbytes} {start} {end} {action} {status}"}


def dsr_events(gre_packets=1000, reply_bytes=5_000_000):
    """A well-behaved run: client -> VIP via the LB, GRE to two backends, replies
    from the VIP straight back to the client."""
    half = gre_packets // 2
    return [
        rec("i-cli", "egress", CLI, VIP, CLI, VIP, 6, gre_packets, gre_packets * 60, sport=40000, dport=9000),
        rec("i-lb", "ingress", VIP, LB, CLI, VIP, 6, gre_packets, gre_packets * 60, sport=40000, dport=9000),
        rec("i-lb", "egress", LB, BE1, LB, BE1, 47, half, half * 84),
        rec("i-lb", "egress", LB, BE2, LB, BE2, 47, gre_packets - half, (gre_packets - half) * 84),
        # health checks from the LB's kernel: TCP, from the LB's own address
        rec("i-lb", "egress", LB, BE1, LB, BE1, 6, 40, 2400, sport=50000, dport=9000),
        rec("i-be1", "ingress", LB, BE1, LB, BE1, 47, half, half * 84),
        rec("i-be1", "egress", BE1, CLI, VIP, CLI, 6, 3000, reply_bytes // 2, sport=9000, dport=40000),
        rec("i-be2", "egress", BE2, CLI, VIP, CLI, 6, 3000, reply_bytes // 2, sport=9000, dport=40000),
        rec("i-cli", "ingress", VIP, CLI, VIP, CLI, 6, 6000, reply_bytes, sport=9000, dport=40000),
    ]


WINDOW = {"start": {"epoch": T0, "txPackets": 500.0}, "end": {"epoch": T1, "txPackets": 1500.0}}
# CloudWatch packets out per instance: the backends sent the 6000 reply packets
# plus some health-check replies; the LB sent its 1000 GRE packets plus a little.
CW = {("be1", "NetworkPacketsOut"): 3100.0, ("be2", "NetworkPacketsOut"): 3080.0,
      ("lb", "NetworkPacketsOut"): 1150.0}
SKIP_BE1 = {"message": f"5 eni-be1 i-be1 - - - - - - - - - - {T0 + 10} {T0 + 70} - SKIPDATA"}


def aws_observed_events():
    """What AWS actually logs: the backends' VIP-sourced egress is missing, and
    their interfaces get SKIPDATA records for those intervals instead."""
    return [e for e in dsr_events() if not ("egress" in e["message"] and f" {VIP} {CLI} " in e["message"]
                                            and "i-be" in e["message"])] + [SKIP_BE1]


def run_checks(events, window=WINDOW, cw=CW):
    records, skipped = referee.load_records(events, ROLES, T0, T1)
    return {name: (ok, detail) for name, ok, detail in
            referee.checks(referee.summarize(records), window, skipped, cw)}


class ParseTest(unittest.TestCase):
    def test_parses_the_stack_format(self):
        r = referee.parse_record(rec("i-be1", "egress", BE1, CLI, VIP, CLI, 6, 3, 300)["message"])
        self.assertEqual((r["pktsrc"], r["protocol"], r["packets"], r["bytes"]), (VIP, "6", 3, 300))

    def test_rejects_wrong_field_count_and_skips_nodata(self):
        self.assertIsNone(referee.parse_record("5 eni-1 i-1 egress"))
        nodata = "5 eni-1 i-lb - - - - - - - - - - 1000 1060 - NODATA"
        self.assertEqual(referee.parse_record(nodata)["status"], "NODATA")
        records, skipped = referee.load_records([{"message": nodata}], ROLES, T0, T1)
        self.assertEqual((records, dict(skipped)), ([], {}))

    def test_records_stamped_late_stay_in_and_older_traffic_stays_out(self):
        def gre(start, end):
            return rec("i-lb", "egress", LB, BE1, LB, BE1, 47, 1, 84, start=start, end=end)
        late = gre(T1 + 30, T1 + 59)
        overlaps_start = gre(T0 - 9, T0 + 29)
        after = gre(T1 + 200, T1 + 260)
        before = gre(T0 - 69, T0 - 30)
        at_start = gre(T0 - 38, T0)
        records, _ = referee.load_records([late, overlaps_start, after, before, at_start], ROLES, T0, T1)
        self.assertEqual(sorted(r["start"] for r in records), [T0 - 9, T1 + 30])


class ChecksTest(unittest.TestCase):
    def test_a_correct_dsr_run_passes_everything(self):
        res = run_checks(dsr_events())
        self.assertTrue(all(ok is True or ok == "info" for ok, _ in res.values()), res)
        ratio = res["Reply bytes per byte the LB sent (the DSR ratio)"][1]
        self.assertTrue(float(ratio.rstrip("x")) > 50, ratio)

    def test_what_aws_logs_passes_on_cloudwatch_counts(self):
        res = run_checks(aws_observed_events())
        self.assertTrue(all(ok is True or ok == "info" for ok, _ in res.values()), res)
        ok, detail = res["Backends' own flow logs of their VIP-sourced replies"]
        self.assertEqual(ok, "info")
        self.assertIn("1 SKIPDATA", detail)

    def test_backends_sending_too_little_fails(self):
        name = "The backends sent at least as many packets as the client got from the VIP"
        cw = {**CW, ("be1", "NetworkPacketsOut"): 100.0, ("be2", "NetworkPacketsOut"): 120.0}
        self.assertFalse(run_checks(aws_observed_events(), cw=cw)[name][0])
        self.assertIsNone(run_checks(aws_observed_events(), cw={})[name][0])

    def test_replies_through_the_lb_fail(self):
        ev = dsr_events() + [rec("i-lb", "egress", LB, CLI, VIP, CLI, 6, 10, 15000)]
        ok, _ = run_checks(ev)["The LB sends no reply traffic: nothing from the VIP, nothing to the client"]
        self.assertFalse(ok)

    def test_counter_mismatch_fails_and_skipdata_is_inconclusive(self):
        name = "maglev-lb's count of packets it forwarded matches AWS's GRE count"
        off = {"start": {"epoch": T0, "txPackets": 0.0}, "end": {"epoch": T1, "txPackets": 1200.0}}
        self.assertFalse(run_checks(dsr_events(), off)[name][0])
        skip = {"message": f"5 eni-lb i-lb - - - - - - - - - - {T0 + 10} {T0 + 70} - SKIPDATA"}
        self.assertIsNone(run_checks(dsr_events() + [skip], off)[name][0])
        # SKIPDATA on another interface, or long before the window, leaves it decided
        old = {"message": "5 eni-lb i-lb - - - - - - - - - - 1000 1060 - SKIPDATA"}
        self.assertFalse(run_checks(dsr_events() + [SKIP_BE1, old], off)[name][0])

    def test_client_backend_mismatch_fails(self):
        ev = [e for e in dsr_events() if "i-cli ingress" not in e["message"]]
        ev.append(rec("i-cli", "ingress", VIP, CLI, VIP, CLI, 6, 6000, 4_000_000))
        ok, _ = run_checks(ev)["What the client received from the VIP is what the backends' flow logs show sent"]
        self.assertFalse(ok)


class CloudWatchTest(unittest.TestCase):
    def test_empty_metrics_are_missing_not_zero(self):
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "cw-be1-NetworkPacketsOut.json").write_text(json.dumps({"Datapoints": []}))
        (d / "cw-lb-NetworkPacketsOut.json").write_text(
            json.dumps({"Datapoints": [{"Sum": 10.0}, {"Sum": 5.0}]}))
        self.assertEqual(referee.cw_sums(d), {("lb", "NetworkPacketsOut"): 15.0})


class MainTest(unittest.TestCase):
    def _dir(self, events, window=WINDOW):
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "window.json").write_text(json.dumps(window))
        (d / "roles.json").write_text(json.dumps(ROLES))
        (d / "flowlogs.json").write_text(json.dumps({"events": events}))
        (d / "cw-lb-NetworkOut.json").write_text(json.dumps({"Datapoints": [{"Sum": 84000.0}]}))
        return d

    def _run(self, d):
        return subprocess.run([sys.executable, str(pathlib.Path(referee.__file__)), str(d)],
                              capture_output=True, text=True)

    def test_exit_codes(self):
        self.assertEqual(self._run(self._dir(dsr_events())).returncode, 0)
        bad = dsr_events() + [rec("i-lb", "egress", LB, CLI, VIP, CLI, 6, 10, 15000)]
        p = self._run(self._dir(bad))
        self.assertEqual(p.returncode, 1)
        self.assertIn("FAIL", p.stdout)
        self.assertEqual(self._run(self._dir([])).returncode, 2)


if __name__ == "__main__":
    unittest.main()
