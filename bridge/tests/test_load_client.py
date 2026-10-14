import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import Child, free_port  # noqa: E402
from test_service_server import start_server  # noqa: E402


def start_client(port, **kw):
    args = ["--vip", "127.0.0.1", "--port", str(port), "--src-ip", "127.0.0.1",
            "--stats-interval-ms", "100", "--seed", "2"]
    for k, v in kw.items():
        args += ["--" + k.replace("_", "-"), str(v)]
    c = Child("load_client.py", args)
    c.wait_ready()
    return c


def stats(m):
    return m.get("type") == "stats"


class TestLoadClient(unittest.TestCase):
    def test_per_request_open_loop_rate(self):
        port = free_port()
        srv = start_server(port, capacity=64, service_ms=5, service_cv=0)
        cli = start_client(port, rps=200, timeout_ms=1000)
        try:
            st = cli.wait_for(lambda m: stats(m) and m["counters"]["scheduled"] >= 400,
                              timeout=10)
            c = st["counters"]
            self.assertGreater(c["ok"], 300)
            self.assertEqual(c["failed"], 0, st["failedByReason"])
            self.assertEqual(st["respBySlot"].get("3", 0), c["ok"])
            self.assertTrue(st["lastTrace"]["ok"])
        finally:
            cli.close()
            srv.close()

    def test_shed_is_classified(self):
        port = free_port()
        srv = start_server(port, capacity=1, service_ms=200, service_cv=0, queue_limit=0)
        cli = start_client(port, rps=100, timeout_ms=1000, retries=0)
        try:
            # capacity 1 + no queue: one request is served every 200ms, the rest shed
            cli.wait_for(lambda m: stats(m) and m["failedByReason"].get("shed", 0) >= 10
                         and m["counters"]["ok"] >= 1, timeout=10)
        finally:
            cli.close()
            srv.close()

    def test_timeout_and_crashed_are_classified(self):
        port = free_port()
        srv = start_server(port, capacity=64, service_ms=500, service_cv=0)
        cli = start_client(port, rps=50, timeout_ms=100, retries=0)
        try:
            cli.wait_for(lambda m: stats(m) and m["failedByReason"].get("timeout", 0) >= 5)
            srv.close()  # listener gone -> connection refused
            cli.wait_for(lambda m: stats(m) and m["failedByReason"].get("crashed", 0) >= 5)
        finally:
            cli.close()
            srv.close()

    def test_pooled_mode_holds_k_connections(self):
        port = free_port()
        srv = start_server(port, capacity=64, service_ms=5, service_cv=0)
        cli = start_client(port, rps=200, timeout_ms=1000, pool_size=3)
        try:
            st = cli.wait_for(lambda m: stats(m) and m["counters"]["ok"] >= 200, timeout=10)
            self.assertEqual(st["openConns"], 3)
            self.assertEqual(st["counters"]["failed"], 0, st["failedByReason"])
            sst = srv.wait_for(lambda m: stats(m) and m["counters"]["ok"] >= 200)
            self.assertEqual(sst["openConns"], 3)  # 200+ requests, still only 3 connections
        finally:
            cli.close()
            srv.close()

    def test_live_rps_change_and_pause(self):
        port = free_port()
        srv = start_server(port, capacity=64, service_ms=1)
        cli = start_client(port, rps=10)
        try:
            cli.send({"op": "config", "rps": 300, "rps_bogus": 1})
            cli.wait_for(lambda m: stats(m) and m["config"]["rps"] == 300)
            cli.send({"op": "load", "enabled": False})
            st = cli.wait_for(lambda m: stats(m) and not m["loadEnabled"])
            before = st["counters"]["scheduled"]
            st2 = cli.wait_for(lambda m: stats(m) and m["tMs"] > st["tMs"] + 500)
            self.assertEqual(st2["counters"]["scheduled"], before)
            cli.send({"op": "config", "rps": 1e9})  # out of bounds: ignored
            st3 = cli.wait_for(lambda m: stats(m) and m["tMs"] > st2["tMs"] + 200)
            self.assertEqual(st3["config"]["rps"], 300)
        finally:
            cli.close()
            srv.close()


if __name__ == "__main__":
    unittest.main()
