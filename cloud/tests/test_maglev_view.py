"""cloud/maglev_view.py: the terminal view of maglev-lb's tables (AWS console,
Session Manager). No AWS and no maglev-lb needed: a stand-in serves /tables."""
import base64
import contextlib
import io
import json
import os
import re
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import maglev_view as mv  # noqa: E402

M = 1009
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def table(ids):
    """A table spreading slots over backend ids like Maglev does (round robin is
    enough here); 0 stays the empty marker."""
    return bytes(ids[i % len(ids)] + 1 for i in range(M))


def doc(gen, ids, rows=(), flows=None):
    t = table(ids)
    return {
        "generation": gen, "ageMs": 5, "m": M,
        "backends": [{"id": i, "addr": f"10.99.1.{31 + i}", "port": 9000,
                      "slots": sum(1 for v in t if v == i + 1)} for i in ids],
        "table": base64.b64encode(t).decode(),
        "conntrack": {"active": len(rows), "scanUs": 900, "rowsPerBackend": 16,
                      "flows": flows or [{"id": i, "n": 3} for i in ids], "rows": list(rows)},
    }


def row(sport, bid, slot, idle):
    return {"src": "10.99.1.10", "sport": sport, "dport": 8080, "proto": "tcp", "id": bid, "slot": slot,
            "idleMs": idle}


def grid_of(lines):
    """The grid's lines: between its caption and BACKENDS."""
    first = next(i for i, ln in enumerate(lines) if ln.startswith("Slots "))
    last = next(i for i, ln in enumerate(lines) if ln.startswith("BACKENDS"))
    return lines[first + 1:last]


class LogicTest(unittest.TestCase):
    def test_a_removal_and_what_mod_n_would_have_done(self):
        before, after = table([0, 1, 2]), table([0, 1])
        c = mv.diff(before, after)
        self.assertEqual(c["removed"], [2])
        self.assertEqual(c["added"], [])
        self.assertEqual(c["count"], sum(1 for x, y in zip(before, after) if x != y))
        c["modulo"] = mv.modulo_moved([0, 1, 2], [0, 1], M)
        text = mv.describe(c, M, 4, 5, lambda i: f"be{i + 1}")
        self.assertTrue(text.startswith("Generation 4 → 5: be3 left. "))
        self.assertIn("Picking a backend by hash mod N instead would have moved", text)
        self.assertIsNone(mv.diff(before, after[:-1]))

    def test_same_words_as_the_canvas(self):
        # breakscale src/live/tables.test.ts: 3 of 9 slots, mod N 55.6%.
        before = bytes([1, 2, 3, 1, 2, 3, 1, 2, 3])
        after = bytes([1, 2, 1, 1, 2, 2, 1, 2, 1])
        c = mv.diff(before, after)
        c["modulo"] = mv.modulo_moved([0, 1, 2], [0, 1], 9)
        text = mv.describe(c, 9, 4, 5, lambda i: f"b{i + 1}")
        self.assertIn("b3 left. 3 of 9 slots (33.3%) changed owner", text)
        self.assertIn("No other slot moved.", text)
        self.assertIn("hash mod N instead would have moved 55.6%", text)

    def test_canvas_backend_names(self):
        self.assertEqual(mv.host_of("10.99.1.33"), "be3")
        self.assertEqual(mv.host_of("192.0.2.1"), "")
        self.assertEqual(mv.host_of(None), "")


class FrameTest(unittest.TestCase):
    def view(self, color):
        v = mv.View(color=color)
        v.update(doc(1, [0, 1, 2], rows=[row(40001, 0, 0, 100)]))
        # be3 leaves; one connection stays pinned to it, and is the least recent.
        v.update(doc(2, [0, 1], rows=[row(40001, 0, 0, 100), row(40002, 2, 2, 1500)],
                     flows=[{"id": 0, "n": 1}, {"id": 2, "n": 1}]))
        return v

    def test_plain_frame(self):
        v = self.view(color=False)
        lines = v.frame(100, 40)
        text = "\n".join(lines)
        self.assertNotRegex(text, ANSI)
        self.assertIn(f"Slots 0-{M - 1:,} of {M:,}, one square each", text)
        self.assertIn("be3 10.99.1.33", text)
        self.assertIn("left the table", text)
        self.assertIn("Generation 1 → 2: be3 10.99.1.33 left.", text.replace("\n", " "))
        # The grid: slots 0 and 1 kept their owners; slot 2 was be3's and moved.
        self.assertEqual(grid_of(lines)[0][2:6], "12**")
        # The connection still pinned to be3 comes first, though it is the least recent.
        ct = lines[[i for i, ln in enumerate(lines) if ln.startswith("CONNECTION TRACKING")][0] + 2]
        self.assertIn(":40002", ct)
        self.assertIn("! the pin wins", ct)

    def test_colour_frame_fits_the_terminal(self):
        v = self.view(color=True)
        for cols, rows in ((80, 24), (100, 40), (160, 50), (40, 16)):
            lines = v.frame(cols, rows)
            self.assertLessEqual(len(lines), rows, f"{cols}x{rows}")
            for ln in lines:
                self.assertLessEqual(len(ANSI.sub("", ln)), max(cols, 40), f"{cols}x{rows}: {ln!r}")
        self.assertIn("\x1b[38;5;231m", "\n".join(v.frame(100, 40)), "moved slots are drawn white")
        v.show_moved = False
        self.assertNotIn("\x1b[38;5;231m▀", "".join(v.frame(100, 40)[3:8]))

    def test_focus_and_pages(self):
        v = self.view(color=False)
        v.focus = 1
        grid = "".join(grid_of(v.frame(60, 20)))
        self.assertNotIn("1", grid.replace("*", ""), "only backend 2's slots are drawn")
        self.assertIn("2", grid)
        v.focus, v.page = None, 99
        self.assertRegex("\n".join(v.frame(120, 20)), r"page (\d+) of \1")

    def test_before_the_first_copy(self):
        v = mv.View(color=False)
        v.update({"pending": True})
        self.assertIn("first copy", "\n".join(v.frame(80, 24)))


class Stand(BaseHTTPRequestHandler):
    docs = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps(Stand.docs.pop(0) if len(Stand.docs) > 1 else Stand.docs[0]).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class OnceTest(unittest.TestCase):
    def test_once_asks_again_after_pending(self):
        Stand.docs = [{"pending": True}, doc(3, [0, 1])]
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Stand)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = mv.main(["--url", f"http://127.0.0.1:{srv.server_port}/tables", "--once",
                                "--no-color", "--size", "100x40"])
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(code, 0)
        self.assertIn("Generation 3", out.getvalue())

    def test_once_says_when_nothing_answers(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = mv.main(["--url", "http://127.0.0.1:9/tables", "--once"])
        self.assertEqual(code, 1)
        self.assertIn("cannot read", err.getvalue())


class TerminalTest(unittest.TestCase):
    def test_keys_then_q_restores_the_terminal(self):
        import pty
        import select
        import time
        Stand.docs = [doc(1, [0, 1, 2], rows=[row(40001, 0, 0, 100)])]
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Stand)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "maglev_view.py")
        pid, fd = pty.fork()
        if pid == 0:
            os.environ.update(TERM="xterm-256color", COLUMNS="100", LINES="40")
            os.execv(sys.executable, [sys.executable, script, "--url",
                                      f"http://127.0.0.1:{srv.server_port}/tables"])
        out = b""

        def read_for(seconds):
            nonlocal out
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                r, _, _ = select.select([fd], [], [], 0.1)
                if r:
                    try:
                        out += os.read(fd, 65536)
                    except OSError:
                        return
        try:
            read_for(2.0)
            self.assertIn(b"BACKENDS", out)
            self.assertIn(b"\x1b[?1049h", out, "it draws on the alternate screen")
            for key in b"m2]0[":
                os.write(fd, bytes([key]))
                read_for(0.2)
            self.assertIn("›2".encode(), out, "2 focuses the second backend")
            os.write(fd, b"q")
            read_for(1.5)
            _, status = os.waitpid(pid, 0)
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        self.assertTrue(out.rstrip().endswith(b"\x1b[?25h\x1b[?1049l"), "cursor and screen restored")


if __name__ == "__main__":
    unittest.main()
