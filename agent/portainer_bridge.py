#!/usr/bin/env python3
"""Run the hub `bridge` CLI inside container discord-hub-v2 via the Portainer
Docker-proxy API on the Docker host. The Portainer token is read in place from
/home/operator/.secrets/portainer_token and never printed.

Usage: portainer_bridge.py <bridge args...> [--content-b64 <base64>]
  --content-b64: if present (anywhere), it is removed from the bridge args and
  the decoded bytes are piped to the bridge command's stdin inside the
  container (used by `bridge say`).
"""
import base64
import json
import shlex
import ssl
import struct
import sys
import urllib.request

BASE = "https://192.0.2.10:9443/api/endpoints/1/docker"
CONTAINER = "discord-hub-v2"
TOKEN_FILE = "/home/operator/.secrets/portainer_token"


def api(ctx, tok, method, path, body=None, timeout=120):
    data = None
    headers = {"X-API-Key": tok}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    return urllib.request.urlopen(req, context=ctx, timeout=timeout)


def main():
    args = sys.argv[1:]
    content = None
    if "--content-b64" in args:
        i = args.index("--content-b64")
        content = base64.b64decode(args[i + 1])
        del args[i:i + 2]
    if not args:
        print("no bridge args", file=sys.stderr)
        return 2

    # Overall HTTP timeout must exceed any `bridge wait --timeout N`.
    http_timeout = 120
    if "--timeout" in args:
        try:
            http_timeout = int(args[args.index("--timeout") + 1]) + 90
        except Exception:
            pass

    bridge_cmd = "bridge " + " ".join(shlex.quote(a) for a in args)
    if content is not None:
        b64 = base64.b64encode(content).decode()
        bridge_cmd = "printf %s %s | base64 -d | %s" % (shlex.quote(b64), "", bridge_cmd)
    inner = "export BRIDGE_SOCKET=/run/bridge.sock; " + bridge_cmd

    tok = open(TOKEN_FILE).read().strip()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    with api(ctx, tok, "POST", "/containers/%s/exec" % CONTAINER, {
        "AttachStdin": content is not None,
        "AttachStdout": True,
        "AttachStderr": True,
        "Tty": False,
        "Cmd": ["sh", "-c", inner],
    }, timeout=30) as r:
        exec_id = json.load(r)["Id"]

    with api(ctx, tok, "POST", "/exec/%s/start" % exec_id,
             {"Detach": False, "Tty": False}, timeout=http_timeout) as r:
        raw = r.read()

    # Demultiplex docker stream: frames of [stream(1), 0,0,0, size(4 BE)].
    out, err = bytearray(), bytearray()
    i = 0
    while i + 8 <= len(raw):
        stream = raw[i]
        size = struct.unpack(">I", raw[i + 4:i + 8])[0]
        chunk = raw[i + 8:i + 8 + size]
        (out if stream == 1 else err).extend(chunk)
        i += 8 + size
    if i < len(raw):  # trailing non-framed bytes: treat as stdout
        out.extend(raw[i:])
    sys.stdout.buffer.write(bytes(out))
    sys.stdout.buffer.flush()
    if err:
        sys.stderr.buffer.write(bytes(err))
        sys.stderr.buffer.flush()

    try:
        with api(ctx, tok, "GET", "/exec/%s/json" % exec_id, timeout=30) as r:
            code = json.load(r).get("ExitCode")
        return int(code) if code is not None else 0
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
