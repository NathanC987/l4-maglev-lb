#!/usr/bin/env python3
"""Run inside the client netns: sudo ip netns exec l4mlb-client python3 \
check_backend.py VIP PORT

Opens one fresh, short-lived TCP connection and prints only the label of
the backend that answered. Used by run-flow-regen-test.sh to sample which
backend(s) new flows land on after a Maglev table regeneration.
"""
import socket
import sys


def main():
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} VIP PORT", file=sys.stderr)
        return 2
    vip, port = sys.argv[1], int(sys.argv[2])

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3)
    s.connect((vip, port))
    s.sendall(b"probe")
    reply = s.recv(4096)
    s.close()
    print(reply.split(b":", 1)[0].decode())
    return 0


if __name__ == "__main__":
    sys.exit(main())
