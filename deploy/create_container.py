import os, sys, json
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from portainer import call, EP

for vol in ["bridge-v2-data", "bridge-v2-run"]:
    st, body = call("POST", EP + "/volumes/create", data=json.dumps({"Name": vol}).encode())
    print("volume", vol, "->", st)

env = []
for line in open(os.path.join(HERE, 'hub.env')):
    line = line.strip()
    if line and "=" in line:
        env.append(line)

cfg = {
    "Image": "discord-hub-v2:trial",
    "Env": env,
    "User": "10001:10001",
    "StopTimeout": 20,
    "HostConfig": {
        "Binds": ["bridge-v2-data:/data", "bridge-v2-run:/run"],
        "RestartPolicy": {"Name": "unless-stopped"},
    },
}
st, body = call("POST", EP + "/containers/create?name=discord-hub-v2", data=json.dumps(cfg).encode())
print("create ->", st)
print(body.decode("utf-8", "replace")[:300])
