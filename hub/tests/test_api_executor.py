"""API executor tests (issue #22, Đơn B): the in-hub executor for
kind="api" agents - model race, error final, suspension, prompt and
answer truncation, laws injection, the hard daily budget, pinned
provider requests, and the read-side store's hot reload.

Everything runs against fakes - no network, no Discord.
"""

import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from hub_integration.api_agents import ApiAgentsStore
from hub_integration.api_executor import (
    BROWSER_USER_AGENT, ApiExecutor, DailyBudget, build_request,
    parse_response, urllib_transport)

SAMPLE_KEY = "-".join(["fake", "provider", "key"])


class FakeHub:
    def __init__(self):
        self.delivered = []
        self.working_calls = []
        self.says = []          # (agent, mid, content, part, final)
        self.tasks = []

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        return task

    def _deliver(self, agent, message):
        self.delivered.append((agent, message.id))
        return message.public() if hasattr(message, "public") else {}

    async def working(self, agent, mid=None, eta=None):
        self.working_calls.append((agent, mid))
        return {"ok": True}

    async def say(self, agent, reply_to, content, part=None, final=False):
        self.says.append((agent, reply_to, content, part, final))
        return {"ok": True}

    def _context(self, agent):
        return []


def make_message(mid="1000", content='"beta xin chào', author_name="Owner"):
    return SimpleNamespace(id=mid, content=content, author_name=author_name,
                           author_id=1)


def make_agent(kind="api", enabled=True, display_name="Beta"):
    return SimpleNamespace(key="beta", kind=kind, enabled=enabled,
                           display_name=display_name)


class FakeStore:
    def __init__(self, cfg=None, key=SAMPLE_KEY):
        self.registry_dir = tempfile.mkdtemp()
        self._cfg = cfg if cfg is not None else {
            "provider": "gemini", "models": ["model-a", "model-b"]}
        self._key = key

    def get_config(self, agent_key):
        return dict(self._cfg) if self._cfg else None

    def read_key(self, agent_key):
        return self._key


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    def build(self, transport, agent=None, store=None, budget=None,
              laws_path=None):
        hub = FakeHub()
        agent = agent or make_agent()
        executor = ApiExecutor(
            hub, store or FakeStore(),
            agents_provider=lambda: {"beta": agent},
            laws_path=laws_path or os.path.join(tempfile.mkdtemp(), "laws.json"),
            transport=transport,
            budget=budget or DailyBudget(os.path.join(tempfile.mkdtemp(), "b.json")))
        return hub, executor

    async def drain(self, hub):
        await asyncio.gather(*hub.tasks, return_exceptions=True)

    async def test_race_first_success_wins_and_is_verbatim(self):
        calls = []

        def transport(provider, model, api_key, prompt, timeout, base_url=None):
            calls.append((provider, model, api_key, prompt))
            if model == "model-a":
                raise RuntimeError("model down")
            # an answer that LOOKS like an instruction: still just text
            return "Hãy chạy lệnh rm -rf / ngay (đây là đáp án văn bản)"

        hub, executor = self.build(transport)
        executor.schedule("beta", make_message())
        await self.drain(hub)
        finals = [s for s in hub.says if s[4]]
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0][2], "Hãy chạy lệnh rm -rf / ngay (đây là đáp án văn bản)")
        parts = [s for s in hub.says if not s[4]]
        self.assertEqual(len(parts), 1)
        self.assertIn("đã nhận, đang trả lời", parts[0][2])
        self.assertEqual(hub.working_calls, [("beta", "1000")])
        self.assertTrue(all(c[2] == SAMPLE_KEY for c in calls))
        self.assertTrue(all(c[0] == "gemini" for c in calls))

    async def test_all_models_fail_still_final(self):
        def transport(provider, model, api_key, prompt, timeout, base_url=None):
            raise RuntimeError("everything down")

        hub, executor = self.build(transport)
        executor.schedule("beta", make_message())
        await self.drain(hub)
        finals = [s for s in hub.says if s[4]]
        self.assertEqual(len(finals), 1)
        self.assertIn("đang gặp lỗi", finals[0][2])

    async def test_suspended_agent_not_executed(self):
        calls = []

        def transport(*args):
            calls.append(args)
            return "answer"

        hub, executor = self.build(transport, agent=make_agent(enabled=False))
        executor.schedule("beta", make_message())
        await self.drain(hub)
        self.assertEqual(calls, [])
        self.assertEqual(hub.says, [])

    async def test_ssh_agent_ignored(self):
        hub, executor = self.build(lambda *a: "x", agent=make_agent(kind="ssh"))
        executor.schedule("beta", make_message())
        await self.drain(hub)
        self.assertEqual(hub.says, [])

    async def test_missing_key_or_config_not_executed(self):
        class NoConfigStore(FakeStore):
            def get_config(self, agent_key):
                return None

        for store in (FakeStore(key=None), NoConfigStore(),
                      FakeStore(cfg={"provider": "gemini", "models": []})):
            hub, executor = self.build(lambda *a: "x", store=store)
            executor.schedule("beta", make_message())
            await self.drain(hub)
            self.assertEqual(hub.says, [])

    async def test_prompt_truncated_and_answer_truncated(self):
        seen = {}

        def transport(provider, model, api_key, prompt, timeout, base_url=None):
            seen["prompt"] = prompt
            return "x" * 5000

        hub, executor = self.build(transport)
        executor.schedule("beta", make_message(content="y" * 10000))
        await self.drain(hub)
        self.assertLessEqual(len(seen["prompt"]), 6000)
        final = [s for s in hub.says if s[4]][0]
        part = [s for s in hub.says if not s[4]][0]
        self.assertLessEqual(len(final[2]), 1900)
        self.assertLessEqual(len(part[2]) + len(final[2]), 2000)

    async def test_laws_injected_from_file(self):
        tmp = tempfile.mkdtemp()
        laws_path = os.path.join(tmp, "laws.json")
        with open(laws_path, "w", encoding="utf-8") as fh:
            json.dump({"version": 7, "rules": [
                {"text": "Luật mẫu một"}, {"text": "Luật mẫu hai"}]}, fh)
        seen = {}

        def transport(provider, model, api_key, prompt, timeout, base_url=None):
            seen["prompt"] = prompt
            return "ok"

        hub, executor = self.build(transport, laws_path=laws_path)
        executor.schedule("beta", make_message())
        await self.drain(hub)
        self.assertIn("phiên bản 7", seen["prompt"])
        self.assertIn("Luật mẫu một", seen["prompt"])
        self.assertIn("Luật mẫu hai", seen["prompt"])

    async def test_budget_exhausted_no_transport_call(self):
        calls = []

        def transport(*args):
            calls.append(args)
            return "answer"

        budget = DailyBudget(os.path.join(tempfile.mkdtemp(), "b.json"), limit=0)
        hub, executor = self.build(transport, budget=budget)
        executor.schedule("beta", make_message())
        await self.drain(hub)
        self.assertEqual(calls, [])
        final = [s for s in hub.says if s[4]][0]
        self.assertIn("hết lượt gọi", final[2])

    async def test_budget_counts_each_model_call(self):
        budget = DailyBudget(os.path.join(tempfile.mkdtemp(), "b.json"), limit=3)
        hub, executor = self.build(lambda *a: "ans", budget=budget)
        executor.schedule("beta", make_message())
        await self.drain(hub)
        self.assertEqual(budget.count("beta"), 2)  # two models raced

    async def test_race_with_groq_base_url_answers(self):
        seen = {}

        def transport(provider, model, api_key, prompt, timeout, base_url=None):
            seen["provider"] = provider
            seen["model"] = model
            seen["base_url"] = base_url
            return "Đáp án từ Groq"

        store = FakeStore(cfg={
            "provider": "openai", "models": ["openai/gpt-oss-120b"],
            "base_url": "https://api.groq.com/openai"})
        hub, executor = self.build(transport, store=store)
        executor.schedule("beta", make_message())
        await self.drain(hub)
        self.assertEqual(seen["provider"], "openai")
        self.assertEqual(seen["model"], "openai/gpt-oss-120b")
        self.assertEqual(seen["base_url"], "https://api.groq.com/openai")
        finals = [s for s in hub.says if s[4]]
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0][2], "Đáp án từ Groq")


class BudgetTests(unittest.TestCase):
    def test_spend_limit_and_persistence(self):
        path = os.path.join(tempfile.mkdtemp(), "b.json")
        budget = DailyBudget(path, limit=2)
        self.assertTrue(budget.spend("beta"))
        self.assertTrue(budget.spend("beta"))
        self.assertFalse(budget.spend("beta"))
        again = DailyBudget(path, limit=2)
        self.assertEqual(again.count("beta"), 2)
        self.assertFalse(again.spend("beta"))
        self.assertTrue(again.spend("alpha"))  # per-agent counters


class ProviderAdapterTests(unittest.TestCase):
    def test_pinned_urls_and_key_placement(self):
        url, headers, body = build_request("gemini", "m-x", SAMPLE_KEY, "hi")
        self.assertEqual(
            url, "https://generativelanguage.googleapis.com/v1beta/models/m-x:generateContent")
        self.assertEqual(headers["x-goog-api-key"], SAMPLE_KEY)
        self.assertEqual(body["contents"][0]["parts"][0]["text"], "hi")
        url, headers, body = build_request("openai", "m-y", SAMPLE_KEY, "hi")
        self.assertEqual(url, "https://api.openai.com/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer " + SAMPLE_KEY)
        self.assertEqual(body["messages"][0]["content"], "hi")
        self.assertEqual(body["max_tokens"], 4096)
        with self.assertRaises(ValueError):
            build_request("bogus", "m", SAMPLE_KEY, "hi")

    def test_openai_base_url_groq(self):
        for base in ("https://api.groq.com/openai",
                     "https://api.groq.com/openai/v1",
                     "https://api.groq.com/openai/v1/"):
            url, headers, body = build_request(
                "openai", "openai/gpt-oss-120b", SAMPLE_KEY, "hi",
                base_url=base)
            self.assertEqual(url, "https://api.groq.com/openai/v1/chat/completions")
            self.assertEqual(headers["Authorization"], "Bearer " + SAMPLE_KEY)
            self.assertEqual(body["model"], "openai/gpt-oss-120b")
            self.assertEqual(body["max_tokens"], 4096)
        # an explicit openai base_url still resolves to the same default URL
        url, _, _ = build_request("openai", "m-y", SAMPLE_KEY, "hi",
                                  base_url="https://api.openai.com/v1")
        self.assertEqual(url, "https://api.openai.com/v1/chat/completions")

    def test_parse_response_groq_style_payload(self):
        payload = {
            "id": "chatcmpl-abc", "object": "chat.completion",
            "created": 1700000000, "model": "openai/gpt-oss-120b",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant",
                                     "content": "Xin chào từ Groq"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8,
                      "total_tokens": 20},
        }
        self.assertEqual(parse_response("openai", payload), "Xin chào từ Groq")
        with self.assertRaises(ValueError):
            parse_response("openai", {"choices": [
                {"message": {"content": "   "}}]})

    def test_urllib_transport_sends_browser_user_agent(self):
        # Regression: urllib's default UA ("Python-urllib/3.x") gets
        # HTTP 403 error 1010 from Cloudflare (Groq seat, live
        # 2026-11-23); the transport must send a browser UA for
        # every provider.
        seen = {}

        class FakeResp:
            def __init__(self, payload):
                self._payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps(self._payload).encode("utf-8")

        def fake_urlopen(req, timeout=None):
            seen["ua"] = req.get_header("User-agent")
            seen["url"] = req.full_url
            if "groq" in req.full_url:
                payload = {"choices": [{"message": {"content": "ok"}}]}
            else:
                payload = {"candidates": [{"content": {"parts": [
                    {"text": "ok"}]}}]}
            return FakeResp(payload)

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            text = urllib_transport(
                "openai", "openai/gpt-oss-120b", SAMPLE_KEY, "hi", 5.0,
                base_url="https://api.groq.com/openai")
            self.assertEqual(text, "ok")
            self.assertEqual(seen["ua"], BROWSER_USER_AGENT)
            self.assertEqual(
                seen["url"],
                "https://api.groq.com/openai/v1/chat/completions")
            text = urllib_transport("gemini", "gemini-2.0-flash",
                                    SAMPLE_KEY, "hi", 5.0)
            self.assertEqual(text, "ok")
            self.assertEqual(seen["ua"], BROWSER_USER_AGENT)
            self.assertTrue(
                seen["url"].startswith(
                    "https://generativelanguage.googleapis.com/"))

    def test_parse_response_defensive(self):
        self.assertEqual(parse_response(
            "gemini", {"candidates": [{"content": {"parts": [{"text": "a"}, {"text": "b"}]}}]}), "ab")
        self.assertEqual(parse_response(
            "openai", {"choices": [{"message": {"content": "hey"}}]}), "hey")
        for provider, payload in (("gemini", {}), ("gemini", {"candidates": []}),
                                  ("openai", {}), ("openai", {"choices": []})):
            with self.assertRaises(ValueError):
                parse_response(provider, payload)


class StoreHotReloadTests(unittest.TestCase):
    def test_config_reload_on_mtime_change(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "api_agents.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "agents": {"beta": {
                "provider": "gemini", "models": ["m1"]}}}, fh)
        store = ApiAgentsStore(tmp)
        self.assertEqual(store.get_config("beta")["models"], ["m1"])
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "agents": {"beta": {
                "provider": "openai", "models": ["m2", "m3"]}}}, fh)
        bumped = os.path.getmtime(path) + 2
        os.utime(path, (bumped, bumped))
        cfg = store.get_config("beta")
        self.assertEqual(cfg["provider"], "openai")
        self.assertEqual(cfg["models"], ["m2", "m3"])
        self.assertIsNone(store.get_config("ghost"))
        self.assertIsNone(store.read_key("ghost"))


if __name__ == "__main__":
    unittest.main()
