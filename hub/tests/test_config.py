import os
from unittest.mock import patch

import pytest

from bridgehub.config import Config


def environment():
    values = {"GUILD_ID": "10", "CHANNEL_ID": "20", "OWNER_ID": "1"}
    for name, mid in [("LEAD", "101"), ("WORKER", "102"), ("DEPUTY", "103")]:
        values[f"{name}_BOT_ID"] = mid
        values[f"{name}_BOT_TOKEN"] = "offline-placeholder"
    return values


def test_config_defaults_and_custom_names_tokens():
    env = environment()
    env.update(LEAD_DISPLAY_NAME="Custom", LEAD_CALL_TOKENS='["call-lead"]', CONTEXT_COUNT="0")
    with patch.dict(os.environ, env, clear=True):
        cfg = Config.from_env()
    assert cfg.agents["lead"].display_name == "Custom"
    assert cfg.agents["lead"].call_tokens == ("call-lead",)
    assert cfg.context_count == 0
    assert cfg.socket_path == "/run/bridge.sock"


@pytest.mark.parametrize("key,value", [("LEAD_BOT_TOKEN", "replace-lead-bot-token"),
    ("LEAD_BOT_ID", "102"), ("OWNER_ID", "101"), ("GUILD_ID", "0"),
    ("CONTEXT_COUNT", "-1"), ("LEAD_CALL_TOKENS", '"lead"'),
    ("LEAD_CALL_TOKENS", "[]"), ("LEAD_CALL_TOKENS", '[""]')])
def test_invalid_environment_rejected(key, value):
    env = environment()
    env[key] = value
    with patch.dict(os.environ, env, clear=True), pytest.raises(ValueError):
        Config.from_env()
