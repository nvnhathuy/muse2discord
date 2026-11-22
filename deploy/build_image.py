import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from portainer import call, EP

data = open('/tmp/ctx.tgz', 'rb').read()
st, body = call("POST", EP + "/build?t=discord-hub-v2:trial&dockerfile=Dockerfile&forcerm=1",
                data=data, ctype="application/x-tar", timeout=900)
print("build status:", st)
tail = body.decode("utf-8", "replace").strip().splitlines()
for line in tail[-6:]:
    try:
        d = json.loads(line)
        print((d.get("stream") or d.get("error") or str(d))[:200].strip())
    except Exception:
        print(line[:200])
