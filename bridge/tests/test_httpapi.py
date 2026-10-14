"""HTTP-layer security and routing, against a fake bridge (no root, no topology)."""
import asyncio
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import free_port  # noqa: E402
from httpapi import BridgeHTTPServer, Publisher  # noqa: E402

TOKEN = "t" * 32


class FakeBridge:
    def __init__(self):
        self.desired = None
        self.actions = []

    async def apply_desired(self, d):
        self.desired = d
        return {"accepted": True, "plan": {}}

    async def apply_action(self, a):
        self.actions.append(a)
        return 200, {"ok": True}

    async def reset_session(self):
        return {"sessionId": "x"}

    async def request_lb_restart(self):
        return {"accepted": True}


VALID = {"schema": "l4mlb-live/1",
         "nodes": {"lb": "lb", "clients": [{"id": "c1", "rps": 10}], "backends": [{"id": "s1"}]},
         "edges": [{"from": "c1", "to": "lb"}, {"from": "lb", "to": "s1"}],
         "load": {"enabled": True}}


class TestHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.loop = asyncio.new_event_loop()
        threading.Thread(target=cls.loop.run_forever, daemon=True).start()
        cls.port = free_port()
        cls.static = tempfile.mkdtemp()
        with open(os.path.join(cls.static, "index.html"), "w") as f:
            f.write("<html>live</html>")
        cls.bridge = FakeBridge()
        cls.pub = Publisher()
        cls.pub.publish(b'{"seq":1}')
        cls.httpd = BridgeHTTPServer(cls.port, TOKEN, cls.loop, cls.bridge, cls.pub, cls.static,
                                     ["http://localhost:5173"])
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.pub.close()
        cls.httpd.shutdown()
        cls.loop.call_soon_threadsafe(cls.loop.stop)

    def req(self, method, path, body=None, headers=None, token=True, host=None):
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        if token:
            h["Authorization"] = "Bearer " + TOKEN
        if body is not None and not isinstance(body, (bytes, str)):
            body = json.dumps(body)
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data, r

    def test_token_required(self):
        self.assertEqual(self.req("GET", "/api/v1/state", token=False)[0], 401)
        self.assertEqual(self.req("GET", "/api/v1/state",
                                  headers={"Authorization": "Bearer wrong"}, token=False)[0], 401)
        st, body, _ = self.req("GET", "/api/v1/state")
        self.assertEqual(st, 200)
        self.assertEqual(body, self.pub.body)

    def test_host_and_origin(self):
        self.assertEqual(self.req("GET", "/api/v1/state", host="evil.example:8790")[0], 403)
        self.assertEqual(self.req("GET", "/api/v1/state",
                                  headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.req("GET", "/api/v1/state",
                                  headers={"Origin": "http://localhost:5173"})[0], 200)

    def test_body_rules(self):
        st, _, _ = self.req("PUT", "/api/v1/desired", body=json.dumps(VALID),
                            headers={"Content-Type": "text/plain"})
        self.assertEqual(st, 415)
        big = json.dumps({**VALID, "pad": "x" * 70000})
        st, _, _ = self.req("PUT", "/api/v1/desired", body=big,
                            headers={"Content-Type": "application/json"})
        self.assertEqual(st, 413)
        st, _, _ = self.req("PUT", "/api/v1/desired", body='{"schema": NaN}',
                            headers={"Content-Type": "application/json"})
        self.assertEqual(st, 400)

    def test_validation_and_apply(self):
        bad = json.loads(json.dumps(VALID))
        bad["nodes"]["clients"][0]["rps"] = 1e9
        st, body, _ = self.req("PUT", "/api/v1/desired", body=bad)
        self.assertEqual(st, 422)
        self.assertIn("rps", body.decode())
        bad["nodes"]["clients"][0]["rps"] = 10
        bad["nodes"]["backends"][0]["capacity"] = True
        self.assertEqual(self.req("PUT", "/api/v1/desired", body=bad)[0], 422)
        st, _, _ = self.req("PUT", "/api/v1/desired", body=VALID)
        self.assertEqual(st, 200)
        self.assertEqual(self.bridge.desired["clients"][0]["rps"], 10)
        st, _, _ = self.req("POST", "/api/v1/actions", body={"action": "crash", "target": "s1"})
        self.assertEqual(st, 200)
        self.assertEqual(self.req("POST", "/api/v1/actions",
                                  body={"action": "nuke", "target": "s1"})[0], 422)

    def test_methods_and_routes(self):
        self.assertEqual(self.req("DELETE", "/api/v1/desired")[0], 405)
        self.assertEqual(self.req("GET", "/api/v1/nope")[0], 404)
        self.assertEqual(self.req("POST", "/api/v1/session/reset", body={})[0], 200)

    def test_static_and_traversal(self):
        st, body, _ = self.req("GET", "/", token=False)
        self.assertEqual((st, body), (200, b"<html>live</html>"))
        st, body, _ = self.req("GET", "/some/spa/route", token=False)
        self.assertEqual(body, b"<html>live</html>")
        st, _, _ = self.req("GET", "/%2e%2e/%2e%2e/etc/passwd", token=False)
        self.assertEqual(st, 403)

    def test_stream_delivers_snapshots(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/api/v1/stream", headers={"Host": f"127.0.0.1:{self.port}",
                                                    "Authorization": "Bearer " + TOKEN})
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Content-Type"), "text/event-stream")
        first = r.fp.readline() + r.fp.readline()
        self.assertIn(b"event: snapshot", first)
        self.pub.publish(b'{"seq":2}')
        r.fp.readline()
        nxt = r.fp.readline() + r.fp.readline()
        self.assertIn(b'{"seq":2}', nxt)
        c.close()


if __name__ == "__main__":
    unittest.main()
