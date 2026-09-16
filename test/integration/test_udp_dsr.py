#!/usr/bin/env python3
"""Run inside the client netns: sudo ip netns exec l4mlb-client python3 \
test_udp_dsr.py VIP PORT

Sends one UDP datagram to VIP:PORT and inspects the reply's source address
as reported by recvfrom() - for a connectionless UDP socket this is the
actual IP-level source address the kernel saw on the wire, so asserting it
equals VIP is a direct, automatable proof that DSR is working (the backend
replied as the VIP, not through the LB with some other source).
"""
import socket
import sys


def main():
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} VIP PORT", file=sys.stderr)
        return 2
    vip, port = sys.argv[1], int(sys.argv[2])

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(3)
    s.sendto(b"udp-hello", (vip, port))
    data, (src_ip, src_port) = s.recvfrom(4096)

    print(f"reply from {src_ip}:{src_port}: {data!r}")
    if src_ip != vip:
        print(f"FAIL: reply source IP {src_ip} != VIP {vip} (DSR not working)")
        return 1
    if not data.startswith(b"BACKEND"):
        print(f"FAIL: unexpected reply payload: {data!r}")
        return 1

    print("PASS: test_udp_dsr")
    return 0


if __name__ == "__main__":
    sys.exit(main())
