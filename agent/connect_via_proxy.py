#!/usr/bin/env python3
"""HTTP CONNECT relay through the Muse runtime proxy on its Tailscale port (3130).

Used as an ssh ProxyCommand: python3 connect_via_proxy.py <host> <port>
Proxy address and credentials come from the HTTPS_PROXY environment variable
at runtime only; nothing secret is stored on disk.
"""
import base64
import os
import socket
import sys
import threading
from urllib.parse import urlparse


def main() -> int:
    dest_host, dest_port = sys.argv[1], int(sys.argv[2])
    raw = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not raw:
        print("no HTTPS_PROXY in environment", file=sys.stderr)
        return 1
    # Same proxy as the runtime, but the port selects Tailscale: 3130.
    proxy = urlparse(raw.rsplit(":", 1)[0] + ":3130")
    sock = socket.create_connection((proxy.hostname, proxy.port))
    headers = [f"CONNECT {dest_host}:{dest_port} HTTP/1.1",
               f"Host: {dest_host}:{dest_port}"]
    if proxy.username:
        creds = f"{proxy.username}:{proxy.password or ''}".encode()
        headers.append("Proxy-Authorization: Basic "
                       + base64.b64encode(creds).decode())
    sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(4096)
        if not chunk:
            print("proxy closed during CONNECT", file=sys.stderr)
            return 1
        resp += chunk
    if b" 200 " not in resp.split(b"\r\n", 1)[0] + b" ":
        print(f"CONNECT failed: {resp.splitlines()[0]!r}", file=sys.stderr)
        return 1
    # Any bytes after the CONNECT headers belong to the destination already
    # (e.g. the SSH banner) and must not be discarded.
    leftover = resp.split(b"\r\n\r\n", 1)[1]
    if leftover:
        sys.stdout.buffer.write(leftover)
        sys.stdout.buffer.flush()

    def pump(src, dst):
        try:
            while True:
                if hasattr(src, "recv"):
                    data = src.recv(65536)
                elif hasattr(src, "read1"):
                    data = src.read1(65536)  # return after one raw read; read() would wait to fill
                else:
                    data = src.read(65536)
                if not data:
                    break
                (dst.sendall if hasattr(dst, "sendall") else dst.write)(data)
                if hasattr(dst, "flush"):
                    dst.flush()
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except Exception:
                pass

    t = threading.Thread(target=pump, args=(sock, sys.stdout.buffer), daemon=True)
    t.start()
    pump(sys.stdin.buffer, sock)
    t.join(timeout=5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
