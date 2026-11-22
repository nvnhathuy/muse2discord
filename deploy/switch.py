import json
import os
import sys
import time
import urllib.parse
from pathlib import Path

DEPLOY_DIR = Path(__file__).resolve().parent
REPO_ROOT = DEPLOY_DIR.parent
sys.path.insert(0, str(DEPLOY_DIR))
sys.path.insert(0, str(REPO_ROOT / "hub"))

from portainer import call, EP, demux, dexec
from bridgehub.config import Config

REQUIRED_VOLUMES = ("bridge-v2-data", "bridge-v2-run")


def parse_env_file(path):
    path = os.path.expanduser(str(path))
    values = {}
    with open(path, encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip()
    return values


def validate_hub_env(env_file):
    env_file = os.path.expanduser(str(env_file))
    if not os.path.isfile(env_file):
        raise RuntimeError(f"Preflight failed: hub env file not found: {env_file}")
    env_vars = parse_env_file(env_file)
    old_env = os.environ.copy()
    try:
        os.environ.clear()
        os.environ.update(env_vars)
        return Config.from_env()
    finally:
        os.environ.clear()
        os.environ.update(old_env)


def preflight(image_tag=None, env_file=None, volumes=REQUIRED_VOLUMES):
    image_tag = image_tag or os.environ.get("DEPLOY_HUB_IMAGE", "discord-hub-v2:trial")
    env_file = os.path.expanduser(str(env_file or os.environ.get("DEPLOY_HUB_ENV_FILE", "").strip() or "hub.env"))

    # 1. Validate hub.env configuration
    cfg = validate_hub_env(env_file)
    print(f"Preflight agents: active=[{', '.join(cfg.agents)}] disabled=[{', '.join(cfg.disabled_agents) or 'none'}]")

    # 2. Verify v2 image exists in Portainer/Docker
    encoded_image = urllib.parse.quote(image_tag, safe="")
    st, body = call("GET", f"{EP}/images/{encoded_image}/json")
    if st != 200:
        raise RuntimeError(f"Preflight failed: image {image_tag} not found (HTTP {st}: {body.decode('utf-8', 'replace')[:200]})")

    # 3. Verify required volumes exist
    for vol in volumes:
        st, body = call("GET", f"{EP}/volumes/{vol}")
        if st != 200:
            raise RuntimeError(f"Preflight failed: volume {vol} not ready (HTTP {st}: {body.decode('utf-8', 'replace')[:200]})")

    return cfg


def _configure_ssh_host_keys(client, paramiko_module):
    client.load_system_host_keys()
    known_hosts = os.environ.get("DEPLOY_KNOWN_HOSTS", "").strip()
    if known_hosts:
        client.load_host_keys(os.path.expanduser(known_hosts))
    client.set_missing_host_key_policy(paramiko_module.RejectPolicy())


def open_vm_ssh():
    import paramiko
    vm_host = os.environ.get("DEPLOY_VM_HOST", "").strip()
    if not vm_host:
        raise RuntimeError("DEPLOY_VM_HOST must be set in environment")
    vm_port = int(os.environ.get("DEPLOY_VM_PORT", "22"))
    vm_user = os.environ.get("DEPLOY_VM_USER", "").strip() or "debian"
    ssh_key = os.path.expanduser(os.environ.get("DEPLOY_SSH_KEY", "").strip() or "~/.ssh/id_ed25519")
    jump_host = os.environ.get("DEPLOY_JUMP_HOST", "").strip()
    proxy_cmd = os.environ.get("DEPLOY_PROXY_COMMAND", "").strip()

    jump_client = None
    sock = None
    if jump_host:
        jump_port = int(os.environ.get("DEPLOY_JUMP_PORT", "22"))
        jump_user = os.environ.get("DEPLOY_JUMP_USER", "").strip()
        if not jump_user:
            raise RuntimeError("DEPLOY_JUMP_USER must be set when DEPLOY_JUMP_HOST is used")
        last = None
        for _ in range(4):
            try:
                j = paramiko.SSHClient()
                _configure_ssh_host_keys(j, paramiko)
                j_sock = paramiko.ProxyCommand(proxy_cmd) if proxy_cmd else None
                j.connect(jump_host, port=jump_port, username=jump_user,
                          key_filename=ssh_key, sock=j_sock, timeout=30)
                jump_client = j
                break
            except Exception as exc:
                last = exc
                time.sleep(12)
        if jump_client is None:
            raise last
        sock = jump_client.get_transport().open_channel(
            "direct-tcpip", (vm_host, vm_port), ("127.0.0.1", 0), timeout=25)
    elif proxy_cmd:
        sock = paramiko.ProxyCommand(proxy_cmd)

    vm_client = paramiko.SSHClient()
    _configure_ssh_host_keys(vm_client, paramiko)
    vm_client.connect(vm_host, port=vm_port, username=vm_user,
                      key_filename=ssh_key, sock=sock, timeout=30)
    return vm_client, jump_client


def run_vm_command(cmd, timeout=90):
    vm_client, jump_client = open_vm_ssh()
    try:
        _, out, err = vm_client.exec_command(cmd, timeout=timeout)
        exit_status = out.channel.recv_exit_status()
        output = (out.read() + err.read()).decode(errors="replace").strip()
        if exit_status != 0:
            raise RuntimeError(f"VM command failed ({exit_status}): {cmd}\n{output}")
        return output
    finally:
        vm_client.close()
        if jump_client:
            jump_client.close()


def _check_v2_status_once(container_name, expected_bots):
    st, body = call("GET", f"{EP}/containers/{container_name}/json")
    if st != 200:
        raise RuntimeError(f"Health check failed: cannot inspect {container_name} (HTTP {st})")
    info = json.loads(body)
    state = info.get("State", {})
    if not state.get("Running"):
        raise RuntimeError(f"Health check failed: container {container_name} is not running (status={state.get('Status')})")

    st, out = dexec(container_name, ["bridge", "status"], timeout=15)
    if st not in (200, 201):
        raise RuntimeError(f"Health check failed: bridge status exec returned HTTP {st}: {out}")
    try:
        resp = json.loads(out.strip().splitlines()[-1])
    except Exception as exc:
        raise RuntimeError(f"Health check failed: bridge status did not return valid JSON: {out!r}") from exc
    if "error" in resp:
        raise RuntimeError(f"Health check failed: bridge status returned error: {resp['error']}")
    if not resp.get("journal_healthy", False):
        raise RuntimeError(f"Health check failed: hub journal is unhealthy: {resp}")
    if not resp.get("ready", False):
        raise RuntimeError(f"Health check failed: hub is not ready (recovering={resp.get('recovering')})")
    online = set(resp.get("online_bots", ()))
    missing = set(expected_bots) - online
    if missing:
        raise RuntimeError(f"Health check failed: bots not online: {sorted(missing)} (online={sorted(online)})")
    return resp


def verify_v2_health(container_name, expected_bots=None, timeout=30, poll_interval=2):
    if isinstance(expected_bots, str):
        expected_bots = (expected_bots,)
    elif expected_bots is None:
        expected_bots = ()
    else:
        expected_bots = tuple(expected_bots)
    deadline = time.monotonic() + max(0, timeout)
    last_exc = None
    while True:
        try:
            return _check_v2_status_once(container_name, expected_bots)
        except Exception as exc:
            last_exc = exc
            if time.monotonic() >= deadline or poll_interval <= 0:
                raise last_exc
            time.sleep(min(poll_interval, max(0.01, deadline - time.monotonic())))


def _is_v2_running(container_name):
    st, body = call("GET", f"{EP}/containers/{container_name}/json")
    if st != 200:
        return False
    try:
        return bool(json.loads(body).get("State", {}).get("Running"))
    except Exception:
        return False


def _detect_running_v1(v1_candidates):
    if not v1_candidates:
        return []
    out = run_vm_command("docker ps --format '{{.Names}}'").strip()
    if not out:
        return []
    running_on_vm = set(out.split())
    return [c for c in v1_candidates if c in running_on_vm]


def switch(wait_seconds=18, health_timeout=30, health_interval=2):
    container_name = os.environ.get("DEPLOY_HUB_CONTAINER", "discord-hub-v2")
    v1_raw = os.environ.get("DEPLOY_V1_CONTAINERS", "discord-bridge discord-bridge-lu discord-bridge-chim").strip()
    v1_candidates = [c for c in v1_raw.split() if c]

    # (a) Preflight BEFORE touching v1
    cfg = preflight()
    expected_bots = [*cfg.agents, "hub"] if cfg.hub_bot_id else list(cfg.agents)
    print("PREFLIGHT OK")

    # (b) Stop running v1 containers, start v2 and verify health; rollback stopped v1 on any failure
    v2_running_before = _is_v2_running(container_name)
    v2_started_by_this_run = False
    stopped_v1 = []
    try:
        running_v1 = _detect_running_v1(v1_candidates)
        if running_v1:
            targets = " ".join(running_v1)
            stopped_v1 = list(running_v1)
            print("STOP v1:", run_vm_command(f"docker stop {targets}"))
        print("STATE:", run_vm_command("docker ps --filter name=discord-bridge --format '{{.Names}} {{.Status}}'"))

        st, body = call("POST", f"{EP}/containers/{container_name}/start", data=b"")
        print("hub start ->", st, body.decode("utf-8", "replace")[:120])
        if st not in (200, 204, 304):
            raise RuntimeError(f"Failed to start {container_name} (HTTP {st})")
        if st in (200, 204) and not v2_running_before:
            v2_started_by_this_run = True
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        st, raw = call("GET", f"{EP}/containers/{container_name}/logs?stdout=1&stderr=1&tail=60")
        if st == 200:
            print("--- hub logs ---")
            print(demux(raw)[-2500:])
        verify_v2_health(container_name, expected_bots, timeout=health_timeout, poll_interval=health_interval)
        print("V2 HEALTH CHECK OK")
    except Exception as exc:
        print(f"ERROR: v2 startup/verification failed: {exc}", file=sys.stderr)
        if v2_started_by_this_run:
            try:
                call("POST", f"{EP}/containers/{container_name}/stop", data=b"")
            except Exception:
                pass
        if stopped_v1:
            try:
                rollback_out = run_vm_command(f"docker start {' '.join(stopped_v1)}")
                print("ROLLBACK v1 restarted:", rollback_out, file=sys.stderr)
            except Exception as rb_exc:
                print(f"ERROR: rollback docker start failed: {rb_exc}", file=sys.stderr)
        raise RuntimeError(f"Switch to v2 failed and rolled back to v1: {exc}") from exc


if __name__ == "__main__":
    switch()
