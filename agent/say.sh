#!/bin/bash
# agent_say.sh <reply_to_id> <textfile> [extra bridge say args...]
# Posts a reply as the agent (template shows key "deputy"; replace with your
# own agent key) through the hub (bridge say --final = ack).
# One plain SSH connection per reply (rare, one-shot — no ControlMaster).
set -eu
DIR="$HOME/workspace/discord-bridge-agent"
KEY="$HOME/.ssh/agent_discord_bridge"
HOST="operator@<TAILSCALE_IP>"
REPLY_TO="$1"; TEXTFILE="$2"; shift 2
B64=$(base64 -w0 "$TEXTFILE")
ssh -i "$KEY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=25 \
  -o "ProxyCommand=python3 $DIR/connect_via_proxy.py %h %p" \
  "$HOST" "python3 ~/portainer_bridge.py say --agent deputy --reply-to $REPLY_TO --final $* --content-b64 $B64"
