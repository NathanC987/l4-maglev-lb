#!/usr/bin/env python3
"""Tiny labeled TCP+UDP echo server used by the backend netns in the test
topology. Prefixes every reply with LABEL: so integration tests can tell
which backend answered, and (for the DSR check) can inspect the reply's
source IP independently of the payload.

Usage: echo_server.py BIND_IP PORT LABEL
"""
import socket
import sys
import threading


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
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((ip, port))
    while True:
        data, addr = s.recvfrom(4096)
        s.sendto(f"{label}:".encode() + data, addr)


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
