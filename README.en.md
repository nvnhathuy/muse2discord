Bản tiếng Việt: [README.md](README.md)

# Discord Bridge v2 — a central hub for an agent room

A single **hub** (a Python/asyncio process) holds the connections of multiple Discord bots — each bot is one agent — routes messages by a name-calling rule, keeps per-agent queues plus a durable journal, and wakes each agent through an agent-side sentinel. The reference deployment runs for real: the `#ai-agent` room with 3 agents **Lead – Deputy – Worker**, the hub running in a Docker container on a Docker host.

```
Discord ⇄ [hub: bridgehub/] ⇄ socket /run/bridge.sock ⇄ [agent-side sentinel] ⇄ agent
```

Repository layout:

| Directory | Contents |
|---|---|
| `hub/` | Hub code: the `bridgehub/` package, the `bridge` CLI, `tests/`, `Dockerfile`, `compose.yaml` |
| `agent/` | Sample agent-side package: `sentinel.sh`, `say.sh`, `portainer_bridge.py`, `connect_via_proxy.py`, `AGENT_SIDE.md` |
| `deploy/` | Portainer-based deployment scripts: `portainer.py`, `build_image.py`, `create_container.py`, and the `hub.env.example` template |
| `docs/` | Full operations documentation (full operations documentation) + `SELF_WAKE_PROPOSAL.md` |

---

## Prerequisites

1. **A Docker host** for the hub — the reference deployment runs on a Docker host (Docker via Portainer). Any Linux machine running Docker works; if you do not use Portainer, use `hub/compose.yaml` instead of the scripts in `deploy/`.
2. **One Discord bot application per agent** (3 bots for 3 agents), each with **MESSAGE CONTENT INTENT enabled** in the Discord Developer Portal and invited to the server, granted these permissions in the room channel: View Channel, Read Message History, Send Messages, Add Reactions (Administrator is not needed).
3. **One room channel** on that server — where the room owner gives orders and the agents report.
4. **Tailscale (mandatory)** — see the dedicated section below. Without Tailscale this design does not work when agents live outside the home network.
5. **Each agent's machine** must be able to SSH to the host that runs the hub/Portainer (over Tailscale), with Python 3 and bash.

### Tailscale — the only way into the house

The system's entire access path runs over **Tailscale**:

- The hub sits inside the home network (on a Docker host); the agents are **outside the home network**. There is no other way in: an agent enters the house over Tailscale first, and only then SSHes / calls Portainer to reach the hub.
- The host that runs the host-side bridge scripts (the hub host) has **its own Tailscale address** — and that address is exactly the SSH destination in the sentinel script (`operator@<TAILSCALE_IP>`).
- If an agent's runtime cannot reach Tailscale directly (for example it is blocked by the provider's proxy), use the **proxy connector** `agent/connect_via_proxy.py` as the SSH `ProxyCommand` — it relays CONNECT through the runtime's proxy; the proxy address and credentials are read from the `HTTPS_PROXY` environment variable at run time and are not stored in the file.

**Tailscale setup steps (do this before building any agent):**

1. Install Tailscale on **the host/VM that holds the bridge scripts** (where agents will SSH to) and on **each agent's machine**.
2. Sign all of them into **the same tailnet**.
3. Note the **Tailscale address of the VM/host** (the `100.x.x.x` form; check with `tailscale ip -4`) — this address goes into each agent's sentinel (the `HOST` variable).
4. Verify from the agent's machine: `ssh <user>@<tailscale-address>` must work **before** you run the sentinel. If SSH over Tailscale does not pass, do not debug anything else yet.

---

## Step 1 — Create the bots and collect IDs/tokens

For each agent, in the [Discord Developer Portal](https://discord.com/developers/applications):

1. New Application → **Bot** section → Reset Token, save the token (this is `<AGENT>_BOT_TOKEN`, shown only once).
2. Enable **MESSAGE CONTENT INTENT** under Bot → Privileged Gateway Intents.
3. **OAuth2 → URL Generator**: select the `bot` scope and the permissions View Channel / Read Message History / Send Messages / Add Reactions → open the generated link to invite the bot to the server.
4. Record: the bot's **Application ID** (= `<AGENT>_BOT_ID`), the **Server ID** (`GUILD_ID` — enable Developer Mode in Discord, right-click the server → Copy Server ID), the room **Channel ID** (`CHANNEL_ID`), and the **room owner's User ID** (`OWNER_ID`).

## Step 2 — Build the hub image

From the `hub/` directory:

```sh
docker build -t discord-hub-v2:trial .
```

Or through the Portainer API with the provided script (reference flow): pack the `hub/` directory into `ctx.tgz` and run `deploy/build_image.py` — it POSTs the build to the Portainer endpoint and prints the build log.

Verify the code before building (no Discord, no tokens needed):

```sh
cd hub && python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

## Step 3 — Create `hub.env`

```sh
cp deploy/hub.env.example hub.env
chmod 600 hub.env
```

Fill in the real values from Step 1:

- `<AGENT>_BOT_TOKEN`, `<AGENT>_BOT_ID` for each agent (LEAD / DEPUTY / WORKER).
- `<AGENT>_CALL_TOKENS`: a JSON list of the name forms the calling rule recognizes, e.g. `LEAD_CALL_TOKENS=["lead"]`, `WORKER_CALL_TOKENS=["worker"]`, `DEPUTY_CALL_TOKENS=["deputy"]`.
- `<AGENT>_DISPLAY_NAME`: the display name used in logs.
- `<AGENT>_RECEIVE_DEADLINE`: the initial receive deadline (seconds), minimum 180 — example values: Lead 180, Deputy 180, Worker 300 (an agent located farther away may set a longer deadline).
- `GUILD_ID`, `CHANNEL_ID`, `OWNER_ID`; `CONTEXT_COUNT` (how many context messages accompany each delivery batch, default 5).
- Keep `BRIDGE_SOCKET=/run/bridge.sock` and `BRIDGE_JOURNAL=/data/bridge.jsonl` as in the template.

## Step 4 — Create the hub container

Reference parameters (matching `hub/compose.yaml`):

- Name: `discord-hub-v2`, image `discord-hub-v2:trial`.
- User: `10001:10001`.
- Bind two volumes: `bridge-v2-data:/data` (the durable journal) and `bridge-v2-run:/run` (the socket).
- Restart policy: `unless-stopped`.
- Env: all variables from `hub.env` (compose loads them via `env_file`; the Portainer script `deploy/create_container.py` reads `hub.env` and passes them straight into the container config).

Run with compose: `docker compose up -d` in `hub/` (set `BRIDGE_ENV_FILE` to point at `hub.env` if the file is not next to the compose file). Run with Portainer: run `deploy/create_container.py` (creates volumes + container), then start the container and read the logs to verify.

## Step 5 — Verify the hub

```sh
# Inside the container:
docker exec discord-hub-v2 sh -c 'BRIDGE_SOCKET=/run/bridge.sock bridge --help'
# Check the journal: all 3 bots logged in, the hub starts writing bridge.jsonl
docker exec discord-hub-v2 sh -c 'tail -n 50 /data/bridge.jsonl'
docker logs discord-hub-v2 --tail 60
```

Pass criteria: the container is running, all 3 bots are online in Discord, and a correctly formed call typed in the room (e.g. `"lead điểm danh`) produces `message` + `delivery` lines in the journal for that agent.

## Step 6 — Build a sample agent (Deputy) end-to-end

The agent side needs three things: the host-side bridge script, the sentinel, and the agent-side conduct rules (`agent/AGENT_SIDE.md`).

1. **On the VM/host** (where agents SSH to): place `agent/portainer_bridge.py` in the home directory. It reads the Portainer token in place (the file `~/.secrets/portainer_token` on the host — the token never leaves the host) and execs into the hub container via Portainer over the LAN. Adjust the constants in the script to your infrastructure (Portainer address, endpoint, container name).
2. **On the agent's machine**: copy `agent/sentinel.sh`, `agent/say.sh`, `agent/connect_via_proxy.py` into the agent's working directory (example: `~/workspace/discord-bridge-agent/`).
3. **Re-identify** in `sentinel.sh` and `say.sh` (change only these, never the mechanism):
   - `DIR`: the agent's working directory.
   - `KEY`: the path to the agent's own SSH key.
   - `HOST`: `<user>@<the VM's Tailscale address>` (the Tailscale step in Prerequisites).
   - Every `--agent deputy` → your agent's name; the temp error file `agent_sentinel_ssh_err.txt` may be kept as is or renamed to match.
   - If SSH connects directly, drop the `ProxyCommand`; if you must go through the runtime's proxy, keep `connect_via_proxy.py`.
4. **Run the sentinel**: `bash sentinel.sh` as a background process. The sentinel holds one SSH connection; the remote loop parks on `wait --timeout 30`; when a message arrives it calls `working` for the first message, prints the JSON, and exits — the agent processes it, replies via `say.sh` (`bridge say --final`), and **relaunches the sentinel in the same turn**.
5. **Roll-call test**: in the room, the owner types `"deputy điểm danh`. Pass criteria: the sentinel exits and wakes the agent within a few seconds, the agent posts a final reply in the room, and a new sentinel has been relaunched.

## Step 7 — Clone for the remaining agents

Repeat Step 6 for each agent, each with its own directory, its own SSH key, and its own `--agent` name. Each copy differs only in the identity lines listed in Step 6 — apart from those, the copies are identical.

## Step 8 — Room rules (summary)

Full text in `docs/02-luat-phong.md`. The core:

- **FIX-6**: a message is a CALL only when it has a `"` immediately before the agent's name, or an @mention, or the word "tất cả" ("everyone"). Mentioning a name without the `"` = merely referring to them; the agent must not reply. When referring to another agent without directly assigning them work, write the plain name, not the call form.
- When called, read carefully, observe the order on the chat board, act in the specified order.
- Agents address each other by name; at most 8 consecutive bot-to-bot messages (the hub has a circuit breaker); when the work is done, go silent.
- For work that takes a while: post a **receipt message first**, then do the work.
- An owner message containing the standalone command word **"now"** = urgent: use the fastest approach.
- Never post code on the chat board — code and long content go through files.

## Step 9 — Operations

- **Journal**: `/data/bridge.jsonl` inside the container — one JSON object per line, types: `message`, `delivery`, `working`, `reply`, `warning`. Filter/aggregate inside the container before pulling output out (exec output via Portainer is truncated at about 4KB).
- **Two-level ack**: `working` = received (level 1), extends the lease by +5 minutes per call, capped at 30 minutes. `say`/`ack` finalizes = level 2. Unfinalized messages are **redelivered up to 3 times**.
- **Escalation / recovered**: after 5 minutes unfinished, the hub posts a warning into the room; after 30 minutes it @mentions the owner. "Recovered" means an agent that was flagged as stalled has just finalized its exchange — it is a symptom of letting exchanges hang, not a hub fault.
- **Known traps**: (1) `--part` chunks after part 1 only *edit* the same message and are not re-routed — a call must be a new message; (2) holding an exchange open for a long time as an "anchor" gets the agent flagged as stalled; (3) each part number can be sent only once.
- **Rollback**: if you still keep the v1 bridges (one file-based bridge container per agent), the rollback is to stop the v2 hub and start the v1 containers again.

## Step 10 — Scheduling / self-waking

Full text in `docs/05-hen-gio-tu-thuc.md` and `docs/SELF_WAKE_PROPOSAL.md`.

- **Option A — cron inside the agent's runtime**: a run-once job opens a new agent turn at the appointed time; the job body carries the original order's message id + the content to post + where to report. Discipline: after creating a job, **read it back to confirm immediately**; before any important appointment, run a **+2-minute test**; more than 2 minutes past the appointment with no message = a miss, and it must be **reported proactively**.
- **Option B — a clock on the VM (recommended backbone)**: a cron on the always-on VM scans a JSON schedule table; at the appointed time it posts a message routed to the target agent via the **independent posting path** (the bot posts directly through the Discord API — the hub still ingests and routes it by call form), and the agent's sentinel catches it within about a second.
- **Option C — scheduling inside the hub**: the long-term direction; requires hub changes, not done yet.

## Security

- Bot tokens live only in **`hub.env` (chmod 600)** on the host and in the hub's memory; the Portainer token lives only in the secret file on the VM. No token is in this repository.
- `.gitignore` already blocks: `hub.env`, `__pycache__/`, `*.pyc`, `hub_inbox.jsonl`, `.secrets/`, `*.log`. Before pushing, re-scan the repo tree yourself for token-like strings (40+ character strings, the `ptr_` prefix, the word `PASSWORD`…) — see `deploy/hub.env.example` for what the template should contain: variable names only.
- The scripts in `deploy/` are the ones actually used for the reference infrastructure (Docker host via Portainer, host via Tailscale) — paths and addresses in them belong to the reference deployment and must be adjusted to yours.
