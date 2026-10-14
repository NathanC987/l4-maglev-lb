"""Test helpers: run a live-mode child process with piped stdin/stdout, the way
the bridge does, and read its JSON-line output on a background thread."""
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time

BRIDGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Child:
    def __init__(self, script, args):
        self.proc = subprocess.Popen(
            [sys.executable, "-I", os.path.join(BRIDGE_DIR, script), *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1)
        self.lines = queue.Queue()
        self.last_stats = None
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.proc.stdout:
            try:
                self.lines.put(json.loads(line))
            except ValueError:
                pass

    def wait_for(self, pred, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = self.lines.get(timeout=0.1)
            except queue.Empty:
                continue
            if msg.get("type") == "stats":
                self.last_stats = msg
            if pred(msg):
                return msg
        raise AssertionError(f"timed out; last stats: {self.last_stats}")

    def wait_ready(self):
        return self.wait_for(lambda m: m.get("type") == "ready")

    def send(self, obj):
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def close(self):
        if self.proc.poll() is None:
            try:
                self.proc.stdin.close()  # EOF on stdin is the shutdown signal
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc.stdout.close()
        self.proc.stderr.close()
