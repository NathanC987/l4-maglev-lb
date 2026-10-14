#!/usr/bin/env python3
"""End-to-end smoke test for the live-mode bridge, driven purely over its HTTP
API (the same API the breakscale fork uses). Run via run-live-smoke-test.sh."""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PORT = 8790
TOKEN = "smoketest" + "0123456789abcdef"
BASE = f"http://127.0.0.1:{PORT}"
LB_METRICS = "http://10.98.0.1:9105/metrics"

failures = []


def check(cond, msg):
    print(("    OK: " if cond else "    FAIL: ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def call(method, path, body=None, headers=None, token=True, raw=None):
    h = {"Host": f"127.0.0.1:{PORT}"}
    if token:
        h["Authorization"] = "Bearer " + TOKEN
    data = raw
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def state():
    return call("GET", "/api/v1/state")[1]


def wait(pred, timeout, what):
    deadline = time.time() + timeout
    s = None
    while time.time() < deadline:
        s = state()
        try:
            if pred(s):
                return s
        except (KeyError, TypeError):
            pass
        time.sleep(0.25)
    check(False, f"timed out waiting for: {what}")
    return s


def avg(key_fn, seconds=3.0):
    vals, deadline = [], time.time() + seconds
    while time.time() < deadline:
        try:
            vals.append(key_fn(state()))
        except (KeyError, TypeError):
            pass
        time.sleep(0.25)
    return sum(vals) / len(vals) if vals else 0.0


def desired(backends=("s1", "s2", "s3"), rps=100, timeout_ms=1000, pool=0, extra=0):
    bs = [{"id": b, "capacity": 8, "serviceMs": 20, "serviceCv": 0.5, "queueLimit": 64}
          for b in backends] + [{"id": f"x{i}"} for i in range(extra)]
    return {"schema": "l4mlb-live/1",
            "nodes": {"lb": "lb", "clients": [{"id": "c1", "rps": rps, "timeoutMs": timeout_ms,
                                               "retries": 0, "poolSize": pool}],
                      "backends": bs},
            "edges": [{"from": "c1", "to": "lb"}] + [{"from": "lb", "to": b["id"]} for b in bs],
            "load": {"enabled": True}}


def put(d):
    return call("PUT", "/api/v1/desired", body=d)


def act(action, target, **kw):
    return call("POST", "/api/v1/actions", body={"action": action, "target": target, **kw})


def lb_metric_line(name, lb_id):
    text = urllib.request.urlopen(LB_METRICS, timeout=2).read().decode()
    for line in text.splitlines():
        if line.startswith(name + "{") and f'backend_id="{lb_id}"' in line:
            return float(line.rsplit(" ", 1)[1])
    return None


def start_bridge():
    p = subprocess.Popen([sys.executable, os.path.join(REPO, "bridge", "live_bridge.py"),
                          "--token", TOKEN, "--no-auto-pause"],
                         stdout=subprocess.PIPE, stderr=open(os.path.join(REPO, "bridge", ".run",
                                                                          "smoke-bridge.log"), "ab"),
                         text=True)
    deadline = time.time() + 90
    while time.time() < deadline:
        line = p.stdout.readline()
        if not line:
            break
        if "live bridge ready" in line:
            return p
    raise SystemExit("bridge did not start; see bridge/.run/smoke-bridge.log and bridge/.run/logs/")


def children_left():
    out = subprocess.run(["pgrep", "-f", "service_server.py|load_client.py|build/maglev-lb"],
                         capture_output=True, text=True).stdout.split()
    return [pid for pid in out if int(pid) != os.getpid()]


def main():
    os.makedirs(os.path.join(REPO, "bridge", ".run"), exist_ok=True)
    print("==> Starting the bridge (topology + maglev-lb + convergence)", flush=True)
    bridge = start_bridge()
    try:
        run_checks()
    finally:
        print("==> kill -9 the bridge: every child must die with it", flush=True)
        bridge.kill()
        bridge.wait()
        time.sleep(3)
        left = children_left()
        check(not left, f"no orphaned children after kill -9 (left: {left})")
        subprocess.run([os.path.join(REPO, "scripts", "bench", "teardown-bench-netns.sh")],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print("==> SIGTERM path: clean shutdown tears the topology down", flush=True)
    bridge = start_bridge()
    bridge.send_signal(signal.SIGTERM)
    bridge.wait(timeout=30)
    netns = subprocess.run(["ip", "netns", "list"], capture_output=True, text=True).stdout
    check("l4mlb-bench" not in netns, "SIGTERM removed every bench namespace")
    check(not children_left(), "SIGTERM left no children")

    print()
    if failures:
        print(f"SOME LIVE SMOKE TESTS FAILED ({len(failures)})")
        for f in failures:
            print("  - " + f)
        return 1
    print("ALL LIVE SMOKE TESTS PASSED")
    return 0


def run_checks():
    print("==> Security: auth, Host, Origin, content type, size, bounds", flush=True)
    check(call("GET", "/api/v1/health")[0] == 200, "health with token -> 200")
    check(call("GET", "/api/v1/state", token=False)[0] == 401, "no token -> 401")
    check(call("GET", "/api/v1/state", headers={"Host": "evil.example:8790"})[0] == 403,
          "foreign Host -> 403")
    check(call("GET", "/api/v1/state", headers={"Origin": "http://evil.example"})[0] == 403,
          "foreign Origin -> 403")
    check(call("PUT", "/api/v1/desired", raw=json.dumps(desired()).encode(),
               headers={"Content-Type": "text/plain"})[0] == 415, "text/plain -> 415")
    check(call("PUT", "/api/v1/desired", raw=b"{" + b" " * 70000 + b"}",
               headers={"Content-Type": "application/json"})[0] == 413, "70KB body -> 413")
    bad = desired()
    bad["nodes"]["clients"][0]["rps"] = 1e9
    check(put(bad)[0] == 422, "rps 1e9 -> 422")
    bad = desired()
    bad["nodes"]["backends"][0]["capacity"] = True
    check(put(bad)[0] == 422, "capacity=true -> 422")
    check(put(desired(extra=14))[0] == 422, "17 backends -> 422")

    print("==> Baseline: 1 client @100 rps -> lb -> 3 backends", flush=True)
    check(put(desired())[0] == 200, "PUT desired -> 200")
    s = wait(lambda s: all(s["backends"][b]["phase"] == "serving" for b in ("s1", "s2", "s3"))
             and s["lb"]["backendsEligible"] == 3, 20, "3 backends serving, 3 eligible")
    time.sleep(2)
    ok = avg(lambda s: s["clients"]["c1"]["rates"]["ok"])
    fail = avg(lambda s: s["clients"]["c1"]["rates"]["failed"])
    check(85 <= ok <= 115, f"client ok/s ~100 (got {ok:.1f})")
    check(fail < 2, f"client failed/s ~0 (got {fail:.2f})")
    s = state()
    arrivals = sum(s["backends"][b]["rates"]["arrivals"] for b in ("s1", "s2", "s3"))
    attempts = s["clients"]["c1"]["rates"]["attempts"]
    check(attempts > 0 and abs(arrivals - attempts) / attempts < 0.25,
          f"sum of backend arrivals ~ client attempts ({arrivals:.0f} vs {attempts:.0f})")
    check(all(s["backends"][b]["lb"]["flowsAssignedPs"] > 0 for b in ("s1", "s2", "s3")),
          "maglev-lb assigned new flows to every backend")
    check(s["lb"]["cpuDatapath"] < 0.5, f"LB datapath CPU low ({s['lb']['cpuDatapath']:.3f})")
    check(any(v > 0 for v in s["backends"]["s1"]["respPsToClient"].values()),
          "DSR return flow attributed to the client")

    print("==> Crash s1 (SIGKILL)", flush=True)
    check(act("crash", "s1")[0] == 200, "crash action accepted")
    wait(lambda s: s["clients"]["c1"]["totals"]["failedByReason"].get("crashed", 0) > 0, 5,
         "client sees 'crashed' failures (RST from a dead listener)")
    wait(lambda s: not s["backends"]["s1"]["lb"]["up"] and s["lb"]["backendsEligible"] == 2, 10,
         "LB marks s1 down, 2 eligible")
    dead = state()["backends"]["s1"]
    check(not dead["proc"]["alive"] and dead["utilization"] == 0 and dead["inFlight"] == 0
          and dead["latencyMs"]["n"] == 0 and not dead["respPsToClient"],
          "a dead backend reports no work and no replies (not its last numbers)")
    time.sleep(2)
    check(state()["backends"]["s1"]["lb"]["hcFailuresPs"] > 0,
          "failed health checks are a current rate against the dead backend")
    crashed_ps = avg(lambda s: s["clients"]["c1"]["failedPsByReason"].get("crashed", 0), 2)
    check(crashed_ps < 1, f"crash failures stop once the table regenerates ({crashed_ps:.2f}/s)")
    check(act("restart", "s1")[0] == 200, "restart action accepted")
    wait(lambda s: s["backends"]["s1"]["phase"] == "serving", 10, "s1 serving again")

    print("==> Slow s2 x10 with a 200ms client timeout", flush=True)
    put(desired(timeout_ms=200))
    act("slow", "s2", factor=10)
    wait(lambda s: s["clients"]["c1"]["totals"]["failedByReason"].get("timeout", 0) > 5, 8,
         "timeouts appear")
    check(state()["backends"]["s2"]["lb"]["up"], "health stays green while slow (TCP connect works)")
    act("clear", "s2")
    put(desired())

    print("==> Partition s3 (gre1 down): a gray failure", flush=True)
    before = state()["clients"]["c1"]["totals"]["failedByReason"].get("timeout", 0)
    act("partition", "s3")
    time.sleep(3)
    s = state()
    check(s["backends"]["s3"]["lb"]["up"], "LB still reports s3 UP (probes don't use GRE)")
    check(avg(lambda s: s["backends"]["s3"]["rates"]["arrivals"], 1.5) < 1,
          "but s3 receives nothing")
    check(s["clients"]["c1"]["totals"]["failedByReason"].get("timeout", 0) > before,
          "clients see timeouts")
    act("clear", "s3")
    wait(lambda s: s["backends"]["s3"]["rates"]["arrivals"] > 5, 8, "s3 receives traffic again")

    print("==> Drain s2: new flows stop, nothing crashes", flush=True)
    crashed_before = state()["clients"]["c1"]["totals"]["failedByReason"].get("crashed", 0)
    act("drain", "s2")
    wait(lambda s: s["backends"]["s2"]["lb"]["flowsAssignedPs"] < 1, 6, "s2 gets no new flows")
    s = state()
    check(s["backends"]["s2"]["lb"]["draining"], "LB reports s2 draining")
    check(s["clients"]["c1"]["totals"]["failedByReason"].get("crashed", 0) == crashed_before,
          "no 'crashed' failures from a drain")
    check(s["backends"]["s1"]["rates"]["arrivals"] > 5, "others keep serving")
    act("undrain", "s2")

    print("==> Higher load: 1000 rps, connection per request", flush=True)
    put(desired(rps=1000))
    time.sleep(3)
    ok = avg(lambda s: s["clients"]["c1"]["rates"]["ok"])
    check(850 <= ok <= 1150, f"client ok/s ~1000 (got {ok:.0f})")
    check(not state()["clients"]["c1"]["behind"], "client generator keeps up")

    print("==> Pooled connections: 4 connections, the LB balances connections not requests",
          flush=True)
    put(desired(rps=200, pool=4))
    time.sleep(4)
    s = state()
    check(s["clients"]["c1"]["openConns"] == 4, f"4 pooled connections ({s['clients']['c1']['openConns']})")
    check(sum(s["backends"][b]["lb"]["flowsAssignedPs"] for b in ("s1", "s2", "s3")) < 2,
          "LB sees ~no new flows while requests keep flowing")
    check(s["clients"]["c1"]["rates"]["ok"] > 150, "requests still succeed")

    print("==> Remove s3: drain, stop, free the slot", flush=True)
    lb_id = state()["backends"]["s3"]["lbId"]
    put(desired(backends=("s1", "s2")))
    wait(lambda s: "s3" not in s["backends"] and not s["retiring"], 40, "s3 retired and slot freed")
    check(lb_metric_line("maglev_lb_backend_up", lb_id) == 0, "freed slot is DOWN in maglev-lb")
    check(lb_metric_line("maglev_lb_backend_admin_enabled", lb_id) == 1,
          "freed slot is UNDRAINed (admin enabled) for reuse")

    print("==> Stream delivers snapshots", flush=True)
    req = urllib.request.Request(BASE + "/api/v1/stream", headers={
        "Host": f"127.0.0.1:{PORT}", "Authorization": "Bearer " + TOKEN})
    with urllib.request.urlopen(req, timeout=5) as r:
        got = b""
        while b"event: snapshot" not in got:
            got += r.readline()
    check(True, "SSE stream sent a snapshot event")


if __name__ == "__main__":
    sys.exit(main())
