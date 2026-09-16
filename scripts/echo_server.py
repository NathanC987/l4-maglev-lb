#!/usr/bin/env python3
"""Tiny labeled TCP+UDP echo server used by the backend netns in the test
topology. Prefixes every reply with LABEL: so integration tests can tell
which backend answered, and (for the DSR check) can inspect the reply's
source IP independently of the payload.

Usage: echo_server.py BIND_IP PORT LABEL
"""
import socket
import struct
import sys
import threading

# Linux's IP_PKTINFO; not always exposed as socket.IP_PKTINFO depending on
# the Python build, so fall back to the well-known Linux value.
IP_PKTINFO = getattr(socket, "IP_PKTINFO", 8)
_PKTINFO_FMT = "=I4s4s"  # struct in_pktinfo { int ifindex; in_addr spec_dst; in_addr addr; }


def handle_tcp_conn(conn, label):
    with conn:
        while True:
            data = conn.recv(4096)
            if not data:
                return
            conn.sendall(f"{label}:".encode() + data)


def tcp_server(ip, port, label):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((ip, port))
    s.listen(16)
    while True:
        conn, _ = s.accept()
        threading.Thread(target=handle_tcp_conn, args=(conn, label), daemon=True).start()


def udp_server(ip, port, label):
    # A single UDP socket bound to 0.0.0.0 (matching the TCP listener, so
    # both the VIP and the backend's own real IP are reachable) does NOT by
    # itself remember which local address an incoming datagram was
    # addressed to - unlike TCP, where accept() hands back a socket already
    # bound to the exact address the SYN targeted. A plain sendto() reply
    # would therefore pick whatever address the routing table's default
    # source selection prefers (the backend's real IP), breaking DSR (the
    # reply must appear to come from the VIP). IP_PKTINFO ancillary data
    # makes the destination address of each received datagram available,
    # and lets a reply explicitly request that same address as its source.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.IPPROTO_IP, IP_PKTINFO, 1)
    s.bind((ip, port))
    anc_bufsize = socket.CMSG_SPACE(struct.calcsize(_PKTINFO_FMT))
    while True:
        data, ancdata, _flags, addr = s.recvmsg(4096, anc_bufsize)
        dst_ip = None
        for level, cmsg_type, cmsg_data in ancdata:
            if level == socket.IPPROTO_IP and cmsg_type == IP_PKTINFO:
                _ifindex, _spec_dst, orig_dst = struct.unpack(_PKTINFO_FMT, cmsg_data)
                dst_ip = orig_dst

        reply = f"{label}:".encode() + data
        if dst_ip is not None:
            pktinfo = struct.pack(_PKTINFO_FMT, 0, dst_ip, b"\x00\x00\x00\x00")
            s.sendmsg([reply], [(socket.IPPROTO_IP, IP_PKTINFO, pktinfo)], 0, addr)
        else:
            s.sendto(reply, addr)


def main():
    if len(sys.argv) != 4:
        print(f"usage: {sys.argv[0]} BIND_IP PORT LABEL", file=sys.stderr)
        return 2
    ip, port, label = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    threading.Thread(target=tcp_server, args=(ip, port, label), daemon=True).start()
    threading.Thread(target=udp_server, args=(ip, port, label), daemon=True).start()
    threading.Event().wait()


if __name__ == "__main__":
    sys.exit(main())
