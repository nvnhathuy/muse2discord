import json, os, ssl, sys, urllib.request, urllib.error, urllib.parse

BASE = "https://127.0.0.1:9443/api"
EP = "/endpoints/1/docker"
CTX = ssl._create_unverified_context()
# Token Portainer đọc từ file bí mật (không bao giờ ghi token vào code).
# Đặt PORTAINER_TOKEN_FILE nếu file nằm ở vị trí khác.
TOKEN = open(os.environ.get("PORTAINER_TOKEN_FILE",
             os.path.expanduser("~/.secrets/portainer_token"))).read().strip()

def call(method, path, data=None, ctype="application/json", timeout=120):
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("X-API-Key", TOKEN)
    if data is not None:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()

def demux(raw):
    out, i = b"", 0
    while i + 8 <= len(raw):
        ln = int.from_bytes(raw[i+4:i+8], "big")
        out += raw[i+8:i+8+ln]
        i += 8 + ln
    return out.decode("utf-8", "replace")

def dexec(container, cmd, timeout=60):
    payload = json.dumps({"AttachStdout": True, "AttachStderr": True, "Tty": False, "Cmd": cmd}).encode()
    st, body = call("POST", f"{EP}/containers/{container}/exec", data=payload)
    if st not in (200, 201):
        return st, body.decode("utf-8", "replace")[:300]
    eid = json.loads(body)["Id"]
    st, raw = call("POST", f"{EP}/exec/{eid}/start", data=json.dumps({"Detach": False, "Tty": False}).encode(), timeout=timeout)
    return st, demux(raw)

if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "containers":
        st, body = call("GET", EP + "/containers/json?all=1")
        for c in json.loads(body):
            print(c["Names"][0], "|", c["Image"], "|", c["State"], "|", c["Status"])
    elif mode == "info":
        st, body = call("GET", EP + "/info")
        d = json.loads(body)
        print("NCPU:", d.get("NCPU"), "MemTotal GB:", round(d.get("MemTotal", 0) / 1e9, 1),
              "Docker:", d.get("ServerVersion"), "Driver:", d.get("Driver"))
    elif mode == "exec":
        st, out = dexec(sys.argv[2], sys.argv[3:])
        print("status:", st)
        print(out[:1500])
