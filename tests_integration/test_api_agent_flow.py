"""Integration test (issue #22, Đơn B): a message routed to an
API agent is answered end-to-end by the in-hub executor - real Hub,
fake Discord transport, fake provider transport.

Run: python3 -m pytest tests_integration/test_api_agent_flow.py
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.abspath(os.path.join(ROOT, "hub")))
sys.path.insert(0, os.path.abspath(os.path.join(ROOT, "admin")))

from bridgehub.config import Agent, Config
from bridgehub.core import Hub, Message
from hub_integration.api_agents import ApiAgentsStore
from hub_integration.api_executor import ApiExecutor
from tests.helpers import FakeTransport, empty_history, eventually


def make_config(path):
    return Config(10, 20, 1, {
        "lead": Agent("lead", 101, "offline-placeholder", "Lead", ("lead",)),
        "beta": Agent("beta", 102, "offline-placeholder", "Beta", ("beta",)),
    }, str(path / "journal.jsonl"), str(path / "bridge.sock"))


class ApiAgentFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_message_to_api_agent_is_answered_by_executor(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry_dir = os.path.join(tmp, "registry")
            os.makedirs(os.path.join(registry_dir, "api-keys"))
            with open(os.path.join(registry_dir, "api_agents.json"), "w",
                      encoding="utf-8") as fh:
                json.dump({"version": 1, "agents": {"beta": {
                    "provider": "gemini", "models": ["model-a"]}}}, fh)
            with open(os.path.join(registry_dir, "api-keys", "beta.key"),
                      "w", encoding="utf-8") as fh:
                fh.write("fake-provider-key")
            with open(os.path.join(registry_dir, "laws.json"), "w",
                      encoding="utf-8") as fh:
                json.dump({"version": 3, "rules": [{"text": "Luật mẫu"}]}, fh)

            prompts = []

            def provider_transport(provider, model, api_key, prompt, timeout,
                                   base_url=None):
                prompts.append(prompt)
                return "Đáp án từ model"

            transport = FakeTransport()
            hub = Hub(make_config(Path(tmp)), transport)
            agents = {
                "lead": SimpleNamespace(key="lead", kind="ssh", enabled=True,
                                        display_name="Lead"),
                "beta": SimpleNamespace(key="beta", kind="api", enabled=True,
                                        display_name="Beta"),
            }
            executor = ApiExecutor(
                hub, ApiAgentsStore(registry_dir),
                agents_provider=lambda: agents,
                laws_path=os.path.join(registry_dir, "laws.json"),
                transport=provider_transport)
            hub.api_delivery_hook = executor.schedule
            await hub.recover(empty_history)

            message = Message("2000", 10, 20, 1, '"beta bạn có đó không',
                              author_name="Owner")
            await hub.ingest(message)

            def answered():
                return ("beta", "2000") in hub.replies and \
                    hub.replies[("beta", "2000")]["final"]

            await eventually(answered, timeout=5)
            sent_texts = [content for (agent, _mid, content, _edit, _id)
                          in transport.sent if agent == "beta"]
            self.assertTrue(any("đã nhận, đang trả lời" in t for t in sent_texts))
            self.assertTrue(any("Đáp án từ model" in t for t in sent_texts))
            self.assertIn("phiên bản 3", prompts[0])
            self.assertIn("Luật mẫu", prompts[0])
            # the delivery completed: nothing left queued for beta
            self.assertNotIn("2000", hub.queues["beta"])
            await hub.close()


if __name__ == "__main__":
    unittest.main()
