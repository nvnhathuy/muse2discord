#!/bin/bash
# Hub v2 sentinel (template — shown for agent key "deputy"; replace it with
# your own agent key): ONE persistent SSH connection to the hub host running a
# remote loop of `bridge wait --agent deputy --timeout 30` (via
# ~/portainer_bridge.py on the hub host, which execs into the discord-hub-v2
# container through Portainer). No ControlMaster (a stuck pre-auth master
# refuses all mux requests).
# The remote loop stays silent on empty waits; on the first non-empty wait it
# calls `bridge working` for the first message, prints the JSON and ends the
# SSH session — the local process exit is what wakes the agent.
# The sentinel must NEVER die on hub or transport errors: it has to survive
# hub restarts and migrations of any length. (Incident 2026-11-22: a hub
# conversion of ~2 minutes made a sentinel exit for good after 3 failed
# waits, and the agent stayed deaf until a human relaunched it.) A failed
# wait — and likewise a dropped SSH session — only backs the retry off
# (15s -> 30s -> 60s, capped); the first successful wait resets the backoff.
# The only planned exit is SENTINEL_MAX_LIFE (2h), so the agent opens a
# fresh round; message delivery ends the round the same way, with the JSON.
set -u
DIR="$HOME/workspace/discord-bridge-agent"
KEY="$HOME/.ssh/agent_discord_bridge"
HOST="operator@<TAILSCALE_IP>"

REMOTE=$(cat <<'EOF'
START=$(date +%s); FAILS=0
while :; do
  NOW=$(date +%s)
  if [ $((NOW - START)) -gt 7200 ]; then echo "SENTINEL_MAX_LIFE"; exit 0; fi
  OUT=$(python3 ~/portainer_bridge.py wait --agent deputy --timeout 30 2>/dev/null)
  RC=$?
  if [ $RC -ne 0 ] || [ -z "$OUT" ]; then
    FAILS=$((FAILS + 1))
    if [ $FAILS -eq 1 ]; then sleep 15; elif [ $FAILS -eq 2 ]; then sleep 30; else sleep 60; fi
    continue
  fi
  FAILS=0
  HAS=$(printf '%s' "$OUT" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('messages',[])))" 2>/dev/null || echo 0)
  if [ "$HAS" != "0" ]; then
    FIRST=$(printf '%s' "$OUT" | python3 -c "import sys,json; print(json.load(sys.stdin)['messages'][0]['id'])" 2>/dev/null)
    if [ -n "$FIRST" ]; then python3 ~/portainer_bridge.py working --agent deputy "$FIRST" >/dev/null 2>&1; fi
    echo "$OUT"
    exit 0
  fi
done
EOF
)
B64=$(printf '%s' "$REMOTE" | base64 -w0)

# Reconnect forever: a dropped SSH session (hub host reboot, network blip)
# is retried with the same 15s -> 30s -> 60s backoff instead of exiting.
FAILS=0
while :; do
  OUT=$(timeout 7260 ssh -i "$KEY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
    -o ConnectTimeout=25 -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
    -o "ProxyCommand=python3 $DIR/connect_via_proxy.py %h %p" \
    "$HOST" "echo $B64 | base64 -d | bash" 2>/tmp/agent_sentinel_ssh_err.txt)
  case "$OUT" in
    *'"messages"'*)
      printf '%s\n' "$OUT" >> "$DIR/hub_inbox.jsonl"
      echo "$OUT"
      exit 0
      ;;
    *SENTINEL_MAX_LIFE*)
      echo "SENTINEL_MAX_LIFE"
      exit 0
      ;;
    *)
      FAILS=$((FAILS + 1))
      if [ $FAILS -eq 1 ]; then sleep 15; elif [ $FAILS -eq 2 ]; then sleep 30; else sleep 60; fi
      ;;
  esac
done
