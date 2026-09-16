#!/usr/bin/env python3
"""Run inside the client netns: sudo ip netns exec l4mlb-client python3 \
test_tcp_stickiness.py VIP PORT

Opens many separate TCP connections to VIP:PORT and checks two things:
  1. Within a single connection, multiple exchanges always land on the same
     backend (the conntrack pinning property).
  2. Across many separate connections, traffic splits across both backends
     roughly evenly (the Maglev distribution property).

A successful TCP exchange with a socket connected to VIP is itself proof
that replies are arriving with source IP == VIP: the kernel only accepts
segments for an established connection from the exact peer address it
connected to, so a backend replying with a different source address would
just look like a stalled/reset connection here, not a successful one.
"""
import socket
import sys
from collections import Counter

N_CONNECTIONS = 40


def one_connection(vip, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3)
    s.connect((vip, port))
    s.sendall(b"hello-1")
    reply1 = s.recv(4096)
    s.sendall(b"hello-2")
    reply2 = s.recv(4096)
    s.close()
    return reply1.split(b":", 1)[0], reply2.split(b":", 1)[0]


def main():
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} VIP PORT", file=sys.stderr)
        return 2
    vip, port = sys.argv[1], int(sys.argv[2])

    counts = Counter()
    for i in range(N_CONNECTIONS):
        label1, label2 = one_connection(vip, port)
        if label1 != label2:
            print(f"FAIL: connection {i} saw inconsistent backends within "
                  f"one connection: {label1!r} then {label2!r}")
            return 1
        counts[label1] += 1

    print(f"backend distribution over {N_CONNECTIONS} connections: {dict(counts)}")
    if len(counts) < 2:
        print("FAIL: only one backend ever answered; expected traffic split across both")
        return 1

    values = list(counts.values())
    ratio = min(values) / max(values)
    if ratio < 0.25:
        print(f"FAIL: distribution too skewed (min/max={ratio:.2f})")
        return 1

    print("PASS: test_tcp_stickiness")
    return 0


if __name__ == "__main__":
    sys.exit(main())
