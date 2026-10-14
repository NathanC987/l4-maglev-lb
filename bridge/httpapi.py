"""The bridge's HTTP surface: a stdlib ThreadingHTTPServer, bound to 127.0.0.1
only, in front of a process that runs as root and spawns other processes.

Security model (see docs/live-mode.md):
  - loopback only, never configurable;
  - a random 128-bit token, handed to the browser in the URL *fragment* (which
    never reaches the server or its logs), required as `Authorization: Bearer`
    on every /api request - including the snapshot stream, which is why the
    frontend reads it with fetch() rather than EventSource (no custom headers);
  - Host must be this server (defeats DNS rebinding); Origin, if sent, must be
    allow-listed (defeats cross-site requests); writes must be JSON;
  - body size, concurrent streams and socket timeouts are capped;
  - no CORS headers are ever sent: the UI is served same-origin (--static-dir)
    or through the Vite dev proxy;
  - query strings are never logged.
Domain state is never touched from these threads: commands are handed to the
asyncio loop thread and their result awaited (with a timeout).
"""
import asyncio
import hmac
import json
import mimetypes
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import schema

MAX_BODY = 64 * 1024
MAX_STREAMS = 4
KEEPALIVE_S = 15.0


class Publisher:
    """Latest-snapshot mailbox: the loop thread publishes bytes, stream threads
    wait for a newer sequence number."""

    def __init__(self):
        self.cond = threading.Condition()
        self.seq = 0
        self.body = b"{}"
        self.streams = 0
        self.last_stream_seen = time.monotonic()
        self.closing = False

    def publish(self, body):
        with self.cond:
            self.seq += 1
            self.body = body
            self.cond.notify_all()

    def wait_newer(self, seq, timeout):
        with self.cond:
            self.cond.wait_for(lambda: self.seq != seq or self.closing, timeout=timeout)
            return self.seq, self.body

    def stream_count(self, delta):
        with self.cond:
            self.streams += delta
            self.last_stream_seen = time.monotonic()

    def seconds_without_streams(self):
        with self.cond:
            if self.streams > 0:
                return 0.0
            return time.monotonic() - self.last_stream_seen

    def close(self):
        with self.cond:
            self.closing = True
            self.cond.notify_all()


class BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port, token, loop, bridge, publisher, static_dir, dev_origins):
        self.token = token
        self.loop = loop
        self.bridge = bridge
        self.publisher = publisher
        self.static_dir = os.path.realpath(static_dir) if static_dir else None
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}",
                                *dev_origins}
        self.stream_slots = threading.BoundedSemaphore(MAX_STREAMS)
        super().__init__(("127.0.0.1", port), Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "l4mlb-bridge"
    sys_version = ""
    timeout = 5  # socket timeout for every read/write
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        path = urllib.parse.urlsplit(self.path).path
        sys.stderr.write(f"bridge http: {self.command} {path} -> {getattr(self, '_status', '?')}\n")

    # --- responses -------------------------------------------------------------
    def _send(self, status, body=b"", ctype="application/json"):
        self._status = status
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj, separators=(",", ":")).encode())

    def _error(self, status, msg):
        # The request body may be unread at this point; don't let leftover bytes
        # be parsed as the next request on a kept-alive connection.
        self.close_connection = True
        self._json(status, {"error": msg})

    # --- checks ------------------------------------------------------------------
    def _host_ok(self):
        if self.headers.get("Host") not in self.server.allowed_hosts:
            self._error(403, "bad Host")
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in self.server.allowed_origins:
            self._error(403, "bad Origin")
            return False
        return True

    def _auth_ok(self):
        got = self.headers.get("Authorization", "")
        want = "Bearer " + self.server.token
        if not hmac.compare_digest(got.encode(), want.encode()):
            self._error(401, "missing or bad token")
            return False
        return True

    def _read_json(self):
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._error(415, "Content-Type must be application/json")
            return None
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._error(411, "Content-Length required")
            return None
        if length < 0 or length > MAX_BODY:
            self._error(413, f"body must be <= {MAX_BODY} bytes")
            self.close_connection = True
            return None
        try:
            return schema.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            self._error(400, f"invalid JSON: {e}")
            return None

    def _call(self, coro_fn, *args):
        fut = asyncio.run_coroutine_threadsafe(coro_fn(*args), self.server.loop)
        return fut.result(timeout=5)

    # --- routing -----------------------------------------------------------------
    def _route(self):
        path = urllib.parse.urlsplit(self.path).path
        if not self._host_ok():
            return
        if not path.startswith("/api/"):
            if self.command in ("GET", "HEAD"):
                self._static(path)
            else:
                self._error(405, "method not allowed")
            return
        if not self._auth_ok():
            return
        route = (self.command, path)
        try:
            if route == ("GET", "/api/v1/health"):
                self._json(200, {"ok": True, "schema": schema.SCHEMA})
            elif route == ("GET", "/api/v1/state"):
                self._send(200, self.server.publisher.body)
            elif route == ("GET", "/api/v1/stream"):
                self._stream()
            elif route == ("PUT", "/api/v1/desired"):
                doc = self._read_json()
                if doc is not None:
                    desired = schema.validate_desired(doc)
                    self._json(200, self._call(self.server.bridge.apply_desired, desired))
            elif route == ("POST", "/api/v1/actions"):
                doc = self._read_json()
                if doc is not None:
                    act = schema.validate_action(doc)
                    status, body = self._call(self.server.bridge.apply_action, act)
                    self._json(status, body)
            elif route == ("POST", "/api/v1/session/reset"):
                self._json(200, self._call(self.server.bridge.reset_session))
            elif route == ("POST", "/api/v1/lb/restart"):
                self._json(202, self._call(self.server.bridge.request_lb_restart))
            else:
                self._error(404, "no such endpoint")
        except schema.ValidationError as e:
            self._json(422, {"errors": e.errors})
        except TimeoutError:
            self._error(503, "bridge busy")

    do_GET = do_PUT = do_POST = do_HEAD = _route

    def do_DELETE(self):
        if self._host_ok():
            self._error(405, "method not allowed")

    do_PATCH = do_OPTIONS = do_DELETE

    # --- streaming -----------------------------------------------------------------
    def _stream(self):
        if not self.server.stream_slots.acquire(blocking=False):
            self._error(503, f"at most {MAX_STREAMS} streams")
            return
        pub = self.server.publisher
        pub.stream_count(+1)
        self._status = 200
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.close_connection = True
            seq = -1
            while not pub.closing:
                new_seq, body = pub.wait_newer(seq, KEEPALIVE_S)
                if pub.closing:
                    break
                if new_seq == seq:
                    self.wfile.write(b": keepalive\n\n")
                else:
                    seq = new_seq
                    self.wfile.write(b"event: snapshot\ndata: " + body + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass
        finally:
            pub.stream_count(-1)
            self.server.stream_slots.release()

    # --- static bundle -------------------------------------------------------------
    def _static(self, path):
        root = self.server.static_dir
        if root is None:
            self._send(404, b"no --static-dir configured; use the Vite dev server\n",
                       "text/plain; charset=utf-8")
            return
        rel = urllib.parse.unquote(path).lstrip("/")
        target = os.path.realpath(os.path.join(root, rel))
        if os.path.commonpath([root, target]) != root:
            self._error(403, "forbidden")
            return
        if not os.path.isfile(target):
            target = os.path.join(root, "index.html")  # SPA fallback
        try:
            with open(target, "rb") as f:
                body = f.read()
        except OSError:
            self._error(404, "not found")
            return
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        self._send(200, body, ctype)
