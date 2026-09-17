#!/usr/bin/env python3
"""Smoke-tests --tui under a real pty (ncurses needs one): launches
maglev-lb --tui, waits for it to produce visible output, then checks it
exits cleanly (code 0) both when the user presses 'q' and when it receives
SIGTERM (the Ctrl-C-equivalent path, exercising the datapath-thread/TUI
signal handling in main.c's run_with_tui()).

Usage: tui_smoke_test.py BIN IFACE VIP DIRECTOR_IP BACKEND1 BACKEND2
"""
import os
import pty
import select
import signal
import sys
import time


def run_case(argv, send_q, timeout=10):
    pid, fd = pty.fork()
    if pid == 0:
        env = dict(os.environ)
        env["TERM"] = "xterm"
        os.execve(argv[0], argv, env)
        os._exit(127)

    start = time.time()
    output = b""
    started = False
    started_at = None
    try:
        while time.time() - start < timeout:
            r, _, _ = select.select([fd], [], [], 0.5)
            if fd in r:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output += chunk
                if not started and (b"maglev-lb" in output or b"HEALTH" in output):
                    started = True
                    started_at = time.time()
            if started and time.time() - started_at > 2:
                break

        if not started:
            print("FAIL: TUI never produced visible output")
            print(f"    captured: {output[:500]!r}")
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            return False

        if send_q:
            os.write(fd, b"q")
        else:
            os.kill(pid, signal.SIGTERM)

        deadline = time.time() + timeout
        while time.time() < deadline:
            wpid, status = os.waitpid(pid, os.WNOHANG)
            if wpid != 0:
                break
            time.sleep(0.1)
        else:
            print("FAIL: process did not exit in time")
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            return False

        exited_cleanly = os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
        if not exited_cleanly:
            print(f"FAIL: process did not exit cleanly, status={status}")
            return False
        print("OK")
        return True
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def main():
    if len(sys.argv) != 7:
        print(f"usage: {sys.argv[0]} BIN IFACE VIP DIRECTOR_IP BACKEND1 BACKEND2", file=sys.stderr)
        return 2
    bin_path, iface, vip, director_ip, be1, be2 = sys.argv[1:7]
    argv = [bin_path, "--iface", iface, "--vip", vip, "--director-ip", director_ip,
            "--backend", be1, "--backend", be2, "--tui"]

    print("case 1: press q to quit")
    ok1 = run_case(argv, send_q=True)

    print("case 2: SIGTERM (Ctrl-C equivalent)")
    ok2 = run_case(argv, send_q=False)

    if ok1 and ok2:
        print("PASS: tui_smoke_test")
        return 0
    print("FAIL: tui_smoke_test")
    return 1


if __name__ == "__main__":
    sys.exit(main())
