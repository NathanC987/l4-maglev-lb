#!/usr/bin/env python3
"""Run inside the client netns: sudo ip netns exec l4mlb-client python3 \
open_flow.py VIP PORT

Opens one TCP connection and holds it open across an external event
orchestrated by the caller (scripts/run-flow-regen-test.sh): sends msg1 and
reports which backend answered, then blocks waiting for a line on stdin
before sending msg2 and reporting the second reply's backend. Used to prove
a flow stays pinned to its original backend across an unrelated Maglev
table regeneration (conntrack pinning surviving M2's health-checker-driven
churn).
"""
import socket
import sys


def main():
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} VIP PORT", file=sys.stderr)
        return 2
    vip, port = sys.argv[1], int(sys.argv[2])

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect((vip, port))

    s.sendall(b"msg1")
    reply1 = s.recv(4096)
    label1 = reply1.split(b":", 1)[0].decode()
    print(f"ANSWERED_BY {label1}", flush=True)

    sys.stdin.readline()  # wait for the orchestrator's go-ahead

    s.sendall(b"msg2")
    reply2 = s.recv(4096)
    label2 = reply2.split(b":", 1)[0].decode()
    print(f"REPLY2 {label2}", flush=True)
    s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
