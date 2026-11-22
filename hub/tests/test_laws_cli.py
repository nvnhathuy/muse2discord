"""`bridge laws` CLI: agents read the room hard laws from laws.json in
the registry dir (BRIDGE_REGISTRY_DIR) - the file the admin page owns.
No hub socket is involved, so these tests run the script standalone.
"""

import json
import os
import subprocess
import sys

BRIDGE = os.path.join(os.path.dirname(__file__), "..", "bridge")


def run_laws(registry_dir, *extra):
    env = dict(os.environ)
    env["BRIDGE_REGISTRY_DIR"] = str(registry_dir)
    return subprocess.run(
        [sys.executable, BRIDGE, "laws", *extra],
        capture_output=True, text=True, env=env, timeout=30,
    )


def _rule(rule_id, text):
    return {"id": rule_id, "text": text, "created_by": "admin",
            "created_at": 1700000000.0, "updated_at": 1700000000.0}


def test_laws_prints_version_and_numbered_rules(tmp_path):
    payload = {"version": 3, "updated_at": 1700000000.0,
               "rules": [_rule("a1", "Luật một"), _rule("b2", "Luật hai")]}
    (tmp_path / "laws.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    proc = run_laws(tmp_path)
    assert proc.returncode == 0
    lines = proc.stdout.strip().splitlines()
    assert lines[0].startswith("Luật cứng — phiên bản 3, cập nhật ")
    assert lines[1] == "1. Luật một"
    assert lines[2] == "2. Luật hai"


def test_laws_without_file_is_version_zero(tmp_path):
    proc = run_laws(tmp_path)
    assert proc.returncode == 0
    assert proc.stdout.strip().splitlines() == [
        "Luật cứng — phiên bản 0, cập nhật chưa có"]


def test_laws_accepts_any_agent_key(tmp_path):
    payload = {"version": 1, "updated_at": 1700000000.0,
               "rules": [_rule("a1", "Luật duy nhất")]}
    (tmp_path / "laws.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    proc = run_laws(tmp_path, "--agent", "bat-ky-khoa-nao")
    assert proc.returncode == 0
    assert "1. Luật duy nhất" in proc.stdout
