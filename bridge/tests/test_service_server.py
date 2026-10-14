import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import Child, free_port  # noqa: E402


def start_server(port, **kw):
    args = ["--bind", "127.0.0.1", "--port", str(port), "--slot", "3",
            "--lb-ip", "127.0.0.1", "--stats-interval-ms", "100", "--seed", "1"]
    for k, v in kw.items():
        args += ["--" + k.replace("_", "-"), str(v)]
    c = Child("service_server.py", args)
    c.wait_ready()
    return c


def request(port, req_id, mode="close", timeout=5.0):
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    s.sendall(f"REQ {req_id} {mode}\n".encode())
    data = b""
    while b"\n" not in data:
        chunk = s.recv(256)
        if not chunk:
            break
        data += chunk
    return s, data.decode().strip()


class TestAdmission(unittest.TestCase):
    def test_capacity_queue_and_shed(self):
        port = free_port()
        srv = start_server(port, capacity=2, service_ms=150, service_cv=0, queue_limit=3)
        try:
            results = [None] * 10
            barrier = threading.Barrier(10)

            def one(i):
                barrier.wait()
                s, line = request(port, i)
                s.close()
                results[i] = line

            threads = [threading.Thread(target=one, args=(i,)) for i in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            verbs = sorted(r.split()[0] for r in results)
            self.assertEqual(verbs.count("OK"), 5, results)
            self.assertEqual(verbs.count("SHED"), 5, results)
            queued = sorted(int(r.split()[3]) for r in results if r.startswith("OK"))
            # 2 served immediately, 3 waited behind them (>= ~1 service time)
            self.assertTrue(all(q < 50_000 for q in queued[:2]), queued)
            self.assertTrue(all(q > 100_000 for q in queued[2:]), queued)
            st = srv.wait_for(lambda m: m.get("type") == "stats"
                              and m["counters"]["ok"] == 5)
            self.assertEqual(st["counters"]["shed"], 5)
            self.assertEqual(st["counters"]["arrivals"], 10)
        finally:
            srv.close()

    def test_error_rate_one_fails_everything(self):
        port = free_port()
        srv = start_server(port, capacity=4, service_ms=1, error_rate=1.0)
        try:
            for i in range(5):
                s, line = request(port, i)
                s.close()
                self.assertTrue(line.startswith("ERR "), line)
        finally:
            srv.close()

    def test_live_reconfig_and_fault(self):
        port = free_port()
        srv = start_server(port, capacity=4, service_ms=1)
        try:
            srv.send({"op": "fault", "errorRate": 1.0})
            time.sleep(0.2)
            s, line = request(port, 1)
            s.close()
            self.assertTrue(line.startswith("ERR "), line)
            srv.send({"op": "clear"})
            srv.send({"op": "config", "capacity": 7, "queueLimit": 9})
            time.sleep(0.2)
            s, line = request(port, 2)
            s.close()
            self.assertTrue(line.startswith("OK "), line)
            st = srv.wait_for(lambda m: m.get("type") == "stats"
                              and m["config"]["capacity"] == 7)
            self.assertEqual(st["config"]["queueLimit"], 9)
        finally:
            srv.close()


class TestConnections(unittest.TestCase):
    def test_probe_is_not_a_request(self):
        port = free_port()
        srv = start_server(port)
        try:
            socket.create_connection(("127.0.0.1", port)).close()
            st = srv.wait_for(lambda m: m.get("type") == "stats"
                              and m["counters"]["probes"] == 1)
            self.assertEqual(st["counters"]["arrivals"], 0)
            self.assertEqual(st["counters"]["accepts"], 0)
        finally:
            srv.close()

    def test_server_closes_first_in_close_mode(self):
        port = free_port()
        srv = start_server(port, service_ms=1)
        try:
            s, line = request(port, 9, "close")
            self.assertTrue(line.startswith("OK 9 3 "), line)
            s.settimeout(2)
            self.assertEqual(s.recv(16), b"")  # EOF without us closing first
            s.close()
        finally:
            srv.close()

    def test_keep_mode_multiplexes_out_of_order(self):
        port = free_port()
        srv = start_server(port, capacity=32, service_ms=40, service_cv=2.0)
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=5)
            s.sendall(b"".join(f"REQ {i} keep\n".encode() for i in range(20)))
            data = b""
            while data.count(b"\n") < 20:
                chunk = s.recv(4096)
                self.assertTrue(chunk, "connection closed early")
                data += chunk
            ids = [int(l.split()[1]) for l in data.decode().strip().split("\n")]
            self.assertEqual(sorted(ids), list(range(20)))
            self.assertNotEqual(ids, list(range(20)))  # replies came back out of order
            s.close()
        finally:
            srv.close()

    def test_malformed_line_closes(self):
        port = free_port()
        srv = start_server(port)
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=2)
            s.sendall(b"GET / HTTP/1.0\n")
            self.assertEqual(s.recv(16), b"")
            s.close()
        finally:
            srv.close()

    def test_stdin_eof_exits(self):
        port = free_port()
        srv = start_server(port)
        srv.proc.stdin.close()
        self.assertEqual(srv.proc.wait(timeout=5), 0)
        srv.close()


if __name__ == "__main__":
    unittest.main()
