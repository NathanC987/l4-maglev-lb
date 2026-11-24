#!/usr/bin/env python3
"""Stands in for maglev-lb in test_cloud_e2e.py: serves /metrics, with a backend up
exactly while something accepts on its port, and takes DRAIN/UNDRAIN on the admin FIFO.
/tables answers like src/ui/inspector.c: pending at first, then a 7-slot table."""
import argparse
import base64
import http.server
import json
import os
import socket
import threading
import time

p = argparse.ArgumentParser()
for name in ("iface", "vip", "director-ip", "admin-fifo", "hc-interval-ms", "hc-timeout-ms",
             "hc-rise", "hc-fall", "ct-tcp-timeout-ms", "ct-udp-timeout-ms"):
    p.add_argument("--" + name)
p.add_argument("--metrics-port", type=int)
p.add_argument("--backend", action="append")
args = p.parse_args()
with open(os.path.join(os.environ.get("HOME", "/tmp"), "ssh.json")) as f:
    base = json.load(f)["port_base"]
n = len(args.backend)
up, draining = [False] * n, [False] * n
tables_asked = False


def probe():
    while True:
        for i in range(n):
            with socket.socket() as s:
                s.settimeout(0.1)
                up[i] = s.connect_ex(("127.0.0.1", base + i + 1)) == 0
        time.sleep(0.2)


def admin():
    while True:
        with open(args.admin_fifo) as f:
            for line in f:
                verb, bid = line.split()
                draining[int(bid)] = verb == "DRAIN"


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def tables(self):
        global tables_asked
        if not tables_asked:
            tables_asked = True
            return {"pending": True}
        live = [i for i in range(n) if up[i]]
        table = bytes((live[s % len(live)] + 1) if live else 0 for s in range(7))
        doc = {"generation": 1, "ageMs": 5, "m": 7,
               "backends": [{"id": i, "addr": f"10.99.1.{31 + i}", "port": 8080,
                             "slots": table.count(i + 1)} for i in live],
               "conntrack": {"active": 1, "scanUs": 9, "rowsPerBackend": 16,
                             "flows": [{"id": live[0], "n": 1}] if live else [],
                             "rows": [{"src": "10.99.1.10", "sport": 40000, "dport": 8080, "proto": "tcp",
                                       "id": live[0], "slot": 0, "idleMs": 3}] if live else []}}
        if "gen=1" not in self.path:
            doc["table"] = base64.b64encode(table).decode()
        return doc

    def do_GET(self):
        if self.path.startswith("/tables"):
            body = json.dumps(self.tables()).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        lines = ["maglev_lb_rx_packets_total 1"]
        for i in range(n):
            lab = f'{{backend_id="{i}",backend_addr="a"}}'
            lines += [f"maglev_lb_backend_up{lab} {int(up[i])}",
                      f"maglev_lb_backend_admin_enabled{lab} {int(not draining[i])}",
                      f"maglev_lb_backend_draining{lab} {int(draining[i])}"]
        body = ("\n".join(lines) + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


srv = http.server.ThreadingHTTPServer(("127.0.0.1", args.metrics_port), H)
threading.Thread(target=probe, daemon=True).start()
threading.Thread(target=admin, daemon=True).start()
print("starting datapath", flush=True)
srv.serve_forever()
