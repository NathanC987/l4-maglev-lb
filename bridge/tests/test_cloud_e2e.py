"""The whole AWS canvas with only AWS itself faked: the real cloud_bridge.py, HTTP API,
reconciler, lb_agent.py and workload processes, behind the fake `aws` and `ssh` in fakes/.
This is what catches a mistake before it costs an EC2 run."""
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

TESTS = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.dirname(TESTS)
FAKES = os.path.join(TESTS, "fakes")
TOKEN = "e2etoken0123456789abcdef"
ROLE = "arn:aws:iam::123456789012:role/test"
ROLES = ["client", "client2", "lb", "be1", "be2", "be3", "be4", "be5"]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ip_of(role):
    return {"client": "10.99.1.10", "client2": "10.99.1.11", "lb": "10.99.1.20"}.get(role) \
        or f"10.99.1.{30 + int(role[2:])}"


def doc(backends=("b1", "b2"), clients=("c1",), lb="lb1", attached=None):
    attached = list(backends) if attached is None else attached
    edges = [{"from": c, "to": lb} for c in clients] + [{"from": lb, "to": b} for b in attached]
    return {"schema": "l4mlb-live/1",
            "nodes": {"lb": lb, "clients": [{"id": c, "rps": 5, "timeoutMs": 200, "retries": 0} for c in clients],
                      "backends": [{"id": b} for b in backends]},
            "edges": edges, "load": {"enabled": True}}


class Rig:
    def __init__(self, extra=()):
        self.dir = tempfile.TemporaryDirectory()
        self.home = self.dir.name
        self.port = free_port()
        self.metrics_port = free_port()
        # Room for slots 1..5 above the base, which the fake client "connects" to.
        self.port_base = free_port() + 100
        run = os.path.join(self.home, "run")
        os.makedirs(run)
        with open(os.path.join(self.home, "ssh.json"), "w") as f:
            json.dump({"port_base": self.port_base, "bridge_dir": BRIDGE, "run_dir": run,
                       "fake_lb": os.path.join(FAKES, "fake_lb.py"), "metrics_port": free_port()}, f)
        insts = {f"i-{n:017x}": {"role": r, "n": n, "type": "t3.micro", "state": "stopped", "ip": None,
                                 "private": ip_of(r), "at": 0}
                 for n, r in enumerate(ROLES, 1)}
        self.ids = {i["role"]: k for k, i in insts.items()}
        with open(os.path.join(self.home, "ec2.json"), "w") as f:
            json.dump({"delay": 0.3, "instances": insts}, f)
        key = os.path.join(self.home, "key.pem")
        open(key, "w").close()
        self.err = open(os.path.join(self.home, "bridge.err"), "w")
        self.p = subprocess.Popen(
            [sys.executable, os.path.join(BRIDGE, "cloud_bridge.py"), "--stack", "t", "--region", "us-east-1",
             "--role-arn", ROLE, "--ssh-key", key, "--cloud-run-dir", os.path.join(self.home, "c"),
             "--port", str(self.port), "--metrics-port", str(self.metrics_port),
             "--token", TOKEN, "--stop-grace-s", "1",
             "--aws-bin", os.path.join(FAKES, "aws"), "--ssh-bin", os.path.join(FAKES, "ssh"), *extra],
            env={"PATH": os.environ["PATH"], "HOME": self.home}, stdout=self.err, stderr=self.err)
        self.wait(lambda: self._ok("/health"), 10, "the API")

    def _ok(self, path):
        try:
            self.api("GET", path)
            return True
        except OSError:
            return False

    def api(self, method, path, body=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/v1{path}", method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())

    def state(self):
        return self.api("GET", "/state")

    def wait(self, pred, timeout, what):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return
            time.sleep(0.1)
        st = self.state()
        phases = {k: v.get("phase") or v.get("state") for k, v in {**st.get("backends", {}), **st.get("clients", {})}.items()}
        hosts = {h["role"]: h["state"] for h in st.get("cloud", {}).get("hosts", [])}
        log = "\n".join(x for x in self.logs().splitlines() if "bridge http:" not in x)
        raise AssertionError(f"timed out waiting for {what}\nphases {phases}\nhosts {hosts}\nec2 {self.ec2()}\n{log[-2000:]}")

    def logs(self):
        self.err.flush()
        with open(os.path.join(self.home, "bridge.err")) as f:
            return f.read()[-3000:]

    def aws_calls(self):
        path = os.path.join(self.home, "aws.log")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(x) for x in f]

    def started(self):
        return self._roles("ec2 start-instances")

    def stopped(self):
        return self._roles("ec2 stop-instances")

    def _roles(self, call):
        by_id = {v: k for k, v in self.ids.items()}
        out = set()
        for c in self.aws_calls():
            if c["call"] == call:
                out |= {by_id[a] for a in c["args"] if a in by_id}
        return out

    def ec2(self):
        with open(os.path.join(self.home, "ec2.json")) as f:
            return {i["role"]: i["state"] for i in json.load(f)["instances"].values()}

    def ssh_log(self):
        with open(os.path.join(self.home, "ssh.log")) as f:
            return [json.loads(x) for x in f]

    def close(self):
        if self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.p.kill()
        self.err.close()
        self.dir.cleanup()


class CanvasDrivesEc2(unittest.TestCase):
    def setUp(self):
        self.rig = Rig()
        self.addCleanup(self.rig.close)

    def serving(self, *ids):
        def check():
            b = self.rig.state().get("backends", {})
            return all(b.get(i, {}).get("phase") == "serving" for i in ids)
        return check

    def test_nothing_starts_until_a_design_arrives(self):
        time.sleep(1.5)
        self.assertEqual(self.rig.started(), set())
        self.assertEqual(self.rig.state()["cloud"]["running"], 0)

    def test_the_canvas_starts_only_what_is_on_it(self):
        r = self.rig
        r.api("PUT", "/desired", doc())
        st = r.state()
        r.wait(self.serving("b1", "b2"), 25, "both backends serving")
        self.assertEqual(r.started(), {"client", "lb", "be1", "be2"})
        self.assertEqual(r.ec2()["be3"], "stopped")
        st = r.state()
        self.assertEqual(st["backends"]["b1"]["host"]["id"], r.ids["be1"])
        self.assertEqual(st["backends"]["b1"]["addr"], "10.99.1.31:9000")
        self.assertEqual(st["cloud"]["running"], 4)
        self.assertGreater(st["cloud"]["usdPerHour"], 0)
        self.assertEqual(st["lb"]["state"], "running")
        self.assertEqual(st["clients"]["c1"]["ip"], "10.99.1.10")

    def test_removing_a_backend_stops_its_instance_after_the_grace_period(self):
        r = self.rig
        r.api("PUT", "/desired", doc())
        r.wait(self.serving("b1", "b2"), 25, "both backends serving")
        slot = r.state()["backends"]["b2"]["slot"]
        r.api("PUT", "/desired", doc(backends=("b1",)))
        r.wait(lambda: f"be{slot}" in r.stopped(), 25, "its instance to stop")
        self.assertNotIn("be1" if slot == 2 else "be2", r.stopped())
        r.wait(lambda: r.ec2()[f"be{slot}"] == "stopped", 10, "EC2 to report it stopped")
        self.assertIn(r.state()["backends"]["b1"]["phase"], ("serving",))

    def test_undo_inside_the_grace_period_keeps_the_instance(self):
        r = self.rig
        r.api("PUT", "/desired", doc(backends=("b1",)))
        r.wait(self.serving("b1"), 25, "the backend serving")
        r.api("PUT", "/desired", doc(backends=()))
        r.api("PUT", "/desired", doc(backends=("b1",)))
        time.sleep(2.5)
        self.assertEqual(r.stopped(), set())
        self.assertEqual(r.state()["backends"]["b1"]["phase"], "serving")

    def test_a_sixth_backend_never_starts_anything(self):
        r = self.rig
        ids = tuple(f"b{i}" for i in range(1, 7))
        r.api("PUT", "/desired", doc(backends=ids))
        r.wait(lambda: sum(b["phase"] == "serving" for b in r.state()["backends"].values()) == 5,
               60, "five backends serving")
        st = r.state()
        self.assertEqual(sum(b["phase"] == "pending" for b in st["backends"].values()), 1)
        self.assertEqual(st["cloud"]["maxBackends"], 5)
        self.assertEqual(r.started(), {"client", "lb", "be1", "be2", "be3", "be4", "be5"})

    def test_each_client_is_its_own_instance_and_a_third_starts_nothing(self):
        r = self.rig
        r.api("PUT", "/desired", doc(backends=("b1",), clients=("c1", "c2", "c3")))
        r.wait(lambda: all(r.state()["clients"].get(c, {}).get("state") == "running" for c in ("c1", "c2")),
               25, "two clients running")
        st = r.state()
        self.assertEqual(st["clients"]["c3"]["state"], "pending")
        self.assertEqual({st["clients"][c]["host"]["role"] for c in ("c1", "c2")}, {"client", "client2"})
        self.assertEqual({st["clients"][c]["ip"] for c in ("c1", "c2")}, {"10.99.1.10", "10.99.1.11"})
        self.assertEqual(st["cloud"]["maxClients"], 2)
        self.assertEqual(r.started(), {"client", "client2", "lb", "be1"})
        # Removing a client stops its instance, and only that one.
        gone = st["clients"]["c2"]["host"]["role"]
        r.api("PUT", "/desired", doc(backends=("b1",), clients=("c1",)))
        r.wait(lambda: gone in r.stopped(), 20, "the removed client's instance to stop")
        self.assertEqual(r.stopped(), {gone})

    def test_every_ready_instance_reports_its_own_cpu(self):
        r = self.rig
        r.api("PUT", "/desired", doc(backends=("b1",)))
        r.wait(self.serving("b1"), 25, "serving")
        r.wait(lambda: "cpu" in (r.state()["backends"]["b1"]["host"] or {}), 10, "the host's CPU")
        st = r.state()
        for h in (st["backends"]["b1"]["host"], st["clients"]["c1"]["host"], st["lb"]["host"]):
            self.assertGreaterEqual(h["cpu"], 0.0)
            self.assertLessEqual(h["cpu"], 1.0)

    def test_crash_kills_the_remote_process_for_real(self):
        r = self.rig
        r.api("PUT", "/desired", doc(backends=("b1",)))
        r.wait(self.serving("b1"), 25, "the backend serving")
        slot = r.state()["backends"]["b1"]["slot"]
        port = r.port_base + slot
        r.api("POST", "/actions", {"action": "crash", "target": "b1"})
        r.wait(lambda: r.state()["backends"]["b1"]["phase"] == "crashed", 5, "the crashed phase")
        kills = [e for e in r.ssh_log() if "ran" in e and e["ran"].startswith("kill -9 ")]
        self.assertEqual(len(kills), 1, "one remote SIGKILL, not a polite stdin close")

        def closed():
            with socket.socket() as s:
                s.settimeout(0.2)
                return s.connect_ex(("127.0.0.1", port)) != 0
        r.wait(closed, 5, "the port to close")
        r.wait(lambda: r.state()["backends"]["b1"]["proc"]["alive"] is False, 5, "the channel to end")
        # Its EC2 instance is still up: a crash is the process, not the machine.
        self.assertEqual(r.stopped(), set())
        r.api("POST", "/actions", {"action": "restart", "target": "b1"})
        r.wait(self.serving("b1"), 15, "the restart to serve")

    def test_a_drain_reaches_the_lb(self):
        r = self.rig
        r.api("PUT", "/desired", doc(backends=("b1", "b2")))
        r.wait(self.serving("b1", "b2"), 25, "both serving")
        r.api("POST", "/actions", {"action": "drain", "target": "b1"})
        r.wait(lambda: r.state()["backends"]["b1"]["lb"]["draining"], 5, "the drain to show in LB metrics")

    def test_the_lb_tables_come_from_the_lb_host(self):
        r = self.rig
        r.api("PUT", "/desired", doc(backends=("b1", "b2")))
        r.wait(self.serving("b1", "b2"), 25, "both serving")
        got = {}

        def ready():
            got.clear()
            got.update(r.api("GET", "/lb/tables"))
            return not got.get("pending")
        r.wait(ready, 10, "the tables")
        self.assertEqual((got["source"]["where"], got["source"]["instance"]), ("aws", r.ids["lb"]))
        self.assertEqual({b["node"] for b in got["backends"]}, {"b1", "b2"})
        self.assertEqual(got["conntrack"]["rows"][0]["client"], "c1")
        self.assertIn("table", got)
        # Asked with the generation it holds, the canvas gets everything but the table.
        again = r.api("GET", f"/lb/tables?gen={got['generation']}")
        self.assertNotIn("table", again)
        self.assertEqual(again["generation"], got["generation"])

    def scrape(self, host=None):
        r = self.rig
        req = urllib.request.Request(f"http://127.0.0.1:{r.metrics_port}/metrics")
        if host:
            req.add_header("Host", host)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.read().decode()

    def test_grafana_gets_the_canvas_and_cloudwatch(self):
        r = self.rig
        r.api("PUT", "/desired", doc(backends=("b1", "b2")))
        r.wait(self.serving("b1", "b2"), 25, "both serving")
        text = self.scrape()
        self.assertIn('canvas_requests_per_second{result="offered"}', text)
        self.assertIn(f'canvas_aws_instance_ready{{role="lb",instance_id="{r.ids["lb"]}",type="t3.micro"}} 1',
                      text)
        self.assertRegex(text, r'canvas_backend_lb_up\{backend="b1",host="be\d",addr="10\.99\.1\.3\d"\} 1')
        # CloudWatch is read only once Grafana is scraping, through the role, about
        # running instances only: the LB's 1-minute metrics and everyone's 5-minute ones.
        r.wait(lambda: "canvas_cloudwatch_cpu_percent" in self.scrape(), 15, "CloudWatch")
        text = self.scrape()
        self.assertIn(f'canvas_cloudwatch_cpu_percent{{role="lb",instance_id="{r.ids["lb"]}"}} 42', text)
        self.assertIn(f'canvas_cloudwatch_network_out_bytes_per_second{{role="lb",instance_id="{r.ids["lb"]}"}} 6000',
                      text)
        self.assertIn('canvas_cloudwatch_ok{state="ok"} 1', text)
        cw = [c for c in r.aws_calls() if c["call"] == "cloudwatch get-metric-data"]
        self.assertTrue(cw and all(c["token"] == "tok" for c in cw))
        asked = {d["Value"] for c in cw for q in json.loads(c["args"][c["args"].index("--metric-data-queries") + 1])
                 for d in q["MetricStat"]["Metric"]["Dimensions"]}
        self.assertNotIn(r.ids["be5"], asked, "a stopped instance is never asked about")
        # Read-only, and only for a Host naming it.
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.scrape(host="evil.example:80")
        self.assertEqual(e.exception.code, 421)

    def test_load_waits_for_the_lb(self):
        r = self.rig
        r.api("PUT", "/desired", doc())
        r.wait(self.serving("b1", "b2"), 25, "both serving")
        r.wait(lambda: r.state()["clients"]["c1"]["state"] == "running", 10, "the client running")
        ran = [e["ran"] for e in r.ssh_log() if "ran" in e and "load_client.py" in e["ran"]]
        self.assertTrue(ran)
        self.assertIn("--paused", ran[0], "the first client must start paused while the LB boots")

    def test_stop_from_the_canvas_stops_every_instance_and_exits(self):
        r = self.rig
        r.api("PUT", "/desired", doc())
        r.wait(self.serving("b1", "b2"), 25, "both serving")
        self.assertEqual(r.api("POST", "/cloud/stop", {})["accepted"], True)
        self.assertEqual(r.p.wait(timeout=30), 0, r.logs())
        self.assertEqual(set(r.ec2().values()), {"stopped"})

    def test_sigterm_also_stops_every_instance(self):
        r = self.rig
        r.api("PUT", "/desired", doc(backends=("b1",)))
        r.wait(self.serving("b1"), 25, "serving")
        r.p.send_signal(signal.SIGTERM)
        self.assertEqual(r.p.wait(timeout=30), 0, r.logs())
        self.assertEqual(set(r.ec2().values()), {"stopped"})

    def test_kill_9_leaves_no_process_behind(self):
        r = self.rig
        r.api("PUT", "/desired", doc(backends=("b1",)))
        r.wait(self.serving("b1"), 25, "serving")
        port = r.port_base + r.state()["backends"]["b1"]["slot"]
        r.p.kill()
        r.p.wait()

        def closed():
            with socket.socket() as s:
                s.settimeout(0.2)
                return s.connect_ex(("127.0.0.1", port)) != 0
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not closed():
            time.sleep(0.2)
        self.assertTrue(closed(), "the remote process must exit when its channel closes")

    def test_every_ec2_call_uses_the_role_not_the_operators_credentials(self):
        r = self.rig
        r.api("PUT", "/desired", doc(backends=("b1",)))
        r.wait(lambda: r.started(), 10, "a start")
        calls = r.aws_calls()
        self.assertEqual(calls[0]["call"], "sts assume-role")
        self.assertIsNone(calls[0]["token"])
        for c in calls[1:]:
            self.assertEqual(c["token"], "tok", c["call"])
            self.assertIn(c["call"], ("ec2 describe-instances", "ec2 start-instances",
                                      "ec2 stop-instances", "sts assume-role"))

    def test_api_stays_locked_and_canvas_ids_never_reach_a_command_line(self):
        r = self.rig
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"http://127.0.0.1:{r.port}/api/v1/state", timeout=5)
        self.assertEqual(e.exception.code, 401)
        marker = "zzCanvasIdMarker"
        with self.assertRaises(urllib.error.HTTPError) as e:
            r.api("PUT", "/desired", doc(backends=("x; touch /tmp/pwned",)))
        self.assertEqual(e.exception.code, 422)
        r.api("PUT", "/desired", doc(backends=(marker,)))
        r.wait(self.serving(marker), 25, "serving")
        for e in r.ssh_log():
            self.assertNotIn(marker, json.dumps(e))
        for c in r.aws_calls():
            self.assertNotIn(marker, json.dumps(c))


class SpendingLimits(unittest.TestCase):
    def rig(self, *extra):
        r = Rig(extra)
        self.addCleanup(r.close)
        return r

    def test_budget_stops_everything(self):
        r = self.rig("--budget-usd", "0.00001")
        r.api("PUT", "/desired", doc(backends=("b1",)))
        self.assertEqual(r.p.wait(timeout=40), 0, r.logs())
        self.assertIn("budget", r.logs())
        self.assertEqual(set(r.ec2().values()), {"stopped"})

    def test_idle_browser_stops_everything(self):
        r = self.rig("--idle-stop-min", "1", "--max-minutes", "5")
        r.api("PUT", "/desired", doc(backends=("b1",)))
        self.assertEqual(r.p.wait(timeout=80), 0, r.logs())
        self.assertIn("no browser connected for 1 minutes", r.logs())
        self.assertEqual(set(r.ec2().values()), {"stopped"})

    def test_session_limit_stops_everything(self):
        r = self.rig("--idle-stop-min", "5", "--max-minutes", "1")
        r.api("PUT", "/desired", doc(backends=("b1",)))
        self.assertEqual(r.p.wait(timeout=80), 0, r.logs())
        self.assertIn("1 minute limit", r.logs())
        self.assertEqual(set(r.ec2().values()), {"stopped"})


if __name__ == "__main__":
    unittest.main()
