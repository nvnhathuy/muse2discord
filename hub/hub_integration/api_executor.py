"""In-hub executor for API agents (issue #22, Đơn B).

An agent with kind="api" has no machine and no runner: when the hub
routes a message to it, THIS component - inside the hub process,
no extra container, service or process - produces the reply:

  working -> part "<name> đã nhận, đang trả lời..." -> race every
  configured model in parallel -> final with the first good answer
  (or a short error final when every model fails - never silence).

Hard locks, enforced by code rather than by prompt wording
(contract 1.5a):

1. No tool surface: the model receives plain text and its answer is
   used VERBATIM as the reply text. Nothing is interpreted or run.
2. Pinned destinations: the provider URL is chosen by the provider
   branch in this file - never from profile or message text. The
   openai branch may use a per-agent base_url from the agent config,
   but only one the admin store validated against its host allowlist
   (api.openai.com, api.groq.com); anything else keeps the pinned
   default. The API key is placed only in that provider's header.
3. Hard budget: at most ``daily_budget`` provider calls per agent
   per day (default 500), counted in code before each call and
   persisted across restarts; past the cap the executor stops and
   answers a short "out of calls" final instead.
4. The room's hard laws are injected from the real laws file on
   EVERY execution (text + version) - the model is never trusted
   to remember them.
5. The key never reaches a log line; failures log agent + provider
   + lengths only. Every step is wrapped so an executor failure can
   never kill the hub or another agent's bot.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import threading
import time
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Optional

log = logging.getLogger(__name__)

GEMINI_HOST = "https://generativelanguage.googleapis.com"
OPENAI_HOST = "https://api.openai.com"
# Reply-token budget for the openai branch. Reasoning models such as
# openai/gpt-oss-120b spend tokens on reasoning first; with a small or
# missing max_tokens the reasoning eats the whole budget and the
# returned content comes back empty. 4096 leaves room for the
# reasoning plus a full ~1900-character answer (spec floor: 2500).
OPENAI_MAX_TOKENS = 4096
# Browser-style User-Agent for every provider call. urllib's default
# ("Python-urllib/3.x") is treated as a bot by Cloudflare: Groq
# answers it with HTTP 403 error 1010 (found live 2026-11-23 on the
# Groq seat); the same key + model answers 200 with a browser UA.
BROWSER_USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36")
MODEL_TIMEOUT = 40.0     # seconds per model call (contract 1.4)
TOTAL_TIMEOUT = 45.0     # seconds for the whole race
PROMPT_LIMIT = 6000      # characters (contract 1.4)
ANSWER_LIMIT = 1900      # characters (contract 1.4)
DEFAULT_DAILY_BUDGET = 500


# ----------------------------------------------------------------------
# provider adapters (contract 1.3) - the ONLY place provider URLs exist
# ----------------------------------------------------------------------
def _openai_base(base_url):
    """Bare host form of an OpenAI-compatible base URL: the admin
    store already normalizes on write, but a hand-edited config file
    may still carry a trailing /v1 - strip it here too so the path
    below is appended exactly once."""
    if not base_url:
        return OPENAI_HOST
    base = str(base_url).strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3].rstrip("/")
    return base or OPENAI_HOST


def build_request(provider: str, model: str, api_key: str, prompt: str,
                  base_url=None):
    """(url, headers, body) for one provider call. The URL comes from
    the provider branch alone; the key only enters that provider's
    auth header. For openai, an optional per-agent base_url (validated
    against the host allowlist by the admin store) replaces the
    default host - e.g. Groq's OpenAI-compatible endpoint."""
    if provider == "gemini":
        url = f"{GEMINI_HOST}/v1beta/models/{model}:generateContent"
        headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
        body = {"contents": [{"parts": [{"text": prompt}]}]}
        return url, headers, body
    if provider == "openai":
        url = f"{_openai_base(base_url)}/v1/chat/completions"
        headers = {"Authorization": "Bearer " + api_key,
                   "Content-Type": "application/json"}
        body = {"model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": OPENAI_MAX_TOKENS}
        return url, headers, body
    raise ValueError(f"unsupported provider: {provider!r}")


def parse_response(provider: str, payload: dict) -> str:
    """Extract the answer text; missing/empty fields are an error for
    that model (the race moves on to the next one)."""
    if provider == "gemini":
        candidates = payload.get("candidates") or []
        if not candidates:
            raise ValueError("gemini response has no candidates")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts).strip()
        if not text:
            raise ValueError("gemini response has no text")
        return text
    if provider == "openai":
        choices = payload.get("choices") or []
        if not choices:
            raise ValueError("openai response has no choices")
        text = ((choices[0].get("message") or {}).get("content") or "").strip()
        if not text:
            raise ValueError("openai response has no content")
        return text
    raise ValueError(f"unsupported provider: {provider!r}")


def urllib_transport(provider: str, model: str, api_key: str,
                     prompt: str, timeout: float, base_url=None) -> str:
    url, headers, body = build_request(provider, model, api_key, prompt,
                                       base_url=base_url)
    headers["User-Agent"] = BROWSER_USER_AGENT
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers=headers,
        method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return parse_response(provider, payload)


# ----------------------------------------------------------------------
# daily budget (contract 1.5a item 3)
# ----------------------------------------------------------------------
class DailyBudget:
    """Per-agent provider-call counter, persisted as JSON so a hub
    restart cannot reset the day's spend."""

    def __init__(self, path: str, limit: int = DEFAULT_DAILY_BUDGET,
                 clock: Callable[[], float] = time.time):
        self.path = path
        self.limit = limit
        self._clock = clock
        self._lock = threading.Lock()
        self._state: dict = {}
        self._load()

    def _today(self) -> str:
        return datetime.fromtimestamp(self._clock(), timezone.utc).date().isoformat()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                self._state = raw.get("agents") or {}
        except (OSError, ValueError):
            self._state = {}

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"agents": self._state}, fh, ensure_ascii=False)
        os.replace(tmp, self.path)

    def spend(self, agent_key: str) -> bool:
        """Count one provider call. False = budget exhausted, the
        call must NOT happen."""
        with self._lock:
            today = self._today()
            entry = self._state.get(agent_key) or {}
            if entry.get("date") != today:
                entry = {"date": today, "count": 0}
            if entry["count"] >= self.limit:
                self._state[agent_key] = entry
                return False
            entry["count"] += 1
            self._state[agent_key] = entry
            try:
                self._save()
            except OSError:
                log.warning("api budget save failed agent=%s", agent_key)
            return True

    def count(self, agent_key: str) -> int:
        with self._lock:
            entry = self._state.get(agent_key) or {}
            return int(entry.get("count", 0)) if entry.get("date") == self._today() else 0


# ----------------------------------------------------------------------
# the executor
# ----------------------------------------------------------------------
class ApiExecutor:
    def __init__(self, hub, store, agents_provider, laws_path: str,
                 transport: Optional[Callable] = None,
                 budget: Optional[DailyBudget] = None,
                 clock: Callable[[], float] = time.time):
        self.hub = hub
        self.store = store
        self.agents_provider = agents_provider
        self.laws_path = laws_path
        self.transport = transport or urllib_transport
        self.budget = budget or DailyBudget(
            os.path.join(store.registry_dir, "api_budget.json"), clock=clock)
        self._clock = clock
        self._inflight: set = set()

    # ------------------------------------------------------------------
    # entry point: called synchronously by the hub on every enqueue
    # ------------------------------------------------------------------
    def schedule(self, agent_key: str, message) -> None:
        agent = self.agents_provider().get(agent_key)
        if agent is None or getattr(agent, "kind", "ssh") != "api":
            return
        marker = (agent_key, message.id)
        if marker in self._inflight:
            return
        self._inflight.add(marker)
        self.hub.spawn(self._run(agent_key, message))

    # ------------------------------------------------------------------
    def _laws(self):
        try:
            with open(self.laws_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            rules = [str(rule.get("text", "")).strip()
                     for rule in raw.get("rules", [])]
            return int(raw.get("version", 0)), [t for t in rules if t]
        except (OSError, ValueError):
            return 0, []

    def _build_prompt(self, agent, message) -> str:
        display = getattr(agent, "display_name", agent.key)
        head = (f"Bạn là {display}, một agent trong phòng chat của đội. "
                "Trả lời trực tiếp, ngắn gọn, bằng ngôn ngữ của tin nhắn.")
        version, rules = self._laws()
        if rules:
            head += (f"\n\nLuật cứng của phòng (phiên bản {version})"
                     " — tuân thủ tuyệt đối:\n"
                     + "\n".join(f"{i + 1}. {t}" for i, t in enumerate(rules)))
        tail = (f"\n\nTin nhắn cần trả lời"
                f" (từ {message.author_name or message.author_id}):\n"
                f"{message.content}")
        prompt = head + tail
        try:
            context = self.hub._context(agent.key)
        except Exception:
            context = []
        lines = [f"[{m.get('author_name') or m.get('author_id')}] {m.get('content', '')}"
                 for m in context]
        while lines:
            block = "\n\nNgữ cảnh phòng gần đây:\n" + "\n".join(lines)
            if len(head) + len(block) + len(tail) <= PROMPT_LIMIT:
                prompt = head + block + tail
                break
            lines.pop(0)  # drop the oldest context first
        return prompt[:PROMPT_LIMIT]

    # ------------------------------------------------------------------
    def _race(self, agent_key: str, cfg: dict, api_key: str, prompt: str):
        """Run every model in parallel; first success wins.
        Returns (status, text): status is "ok" | "budget" | "error"."""
        provider = cfg["provider"]
        models = list(cfg["models"])

        def call(model):
            if not self.budget.spend(agent_key):
                return ("budget", model, None)
            try:
                text = self.transport(provider, model, api_key, prompt,
                                      MODEL_TIMEOUT,
                                      base_url=cfg.get("base_url"))
                return ("ok", model, text)
            except Exception as exc:
                log.warning("api model failed agent=%s provider=%s model=%s (%s)",
                            agent_key, provider, model, type(exc).__name__)
                return ("error", model, None)

        deadline = time.monotonic() + TOTAL_TIMEOUT
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=max(1, len(models))) as pool:
            futures = [pool.submit(call, model) for model in models]
            pending = set(futures)
            while pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                done, pending = concurrent.futures.wait(
                    pending, timeout=remaining,
                    return_when=concurrent.futures.FIRST_COMPLETED)
                if not done:
                    break
                for future in done:
                    status, _model, text = future.result()
                    if status == "ok":
                        return ("ok", text)
            # no success: budget only if every finished call said so
            results = [f.result() for f in futures if f.done()]
            if results and all(r[0] == "budget" for r in results):
                return ("budget", None)
            return ("error", None)

    # ------------------------------------------------------------------
    async def _run(self, agent_key: str, message) -> None:
        marker = (agent_key, message.id)
        try:
            agent = self.agents_provider().get(agent_key)
            if agent is None or getattr(agent, "kind", "ssh") != "api":
                return
            if not getattr(agent, "enabled", False):
                return  # suspended: leave the message in the hub queue
            cfg = self.store.get_config(agent_key)
            if not cfg or not cfg.get("models"):
                return  # no provider config: same, stays queued
            api_key = self.store.read_key(agent_key)
            if not api_key:
                return  # no key: stays queued
            display = getattr(agent, "display_name", agent_key)
            hub = self.hub
            hub._deliver(agent_key, message)
            await hub.working(agent_key, message.id)
            part_text = f"{display} đã nhận, đang trả lời..."
            part_sent = False
            try:
                await hub.say(agent_key, message.id, part_text,
                              part=1, final=False)
                part_sent = True
            except Exception as exc:
                log.warning("api part failed agent=%s (%s)",
                            agent_key, type(exc).__name__)
            prompt = self._build_prompt(agent, message)
            status, answer = await asyncio.to_thread(
                self._race, agent_key, cfg, api_key, prompt)
            if status == "ok":
                limit = min(ANSWER_LIMIT, 1950 - len(part_text))
                final_text = (answer or "")[:max(1, limit)]
            elif status == "budget":
                log.warning("api budget exhausted agent=%s provider=%s",
                            agent_key, cfg["provider"])
                final_text = (f"{display} đã hết lượt gọi hôm nay "
                              "(giới hạn ngày), mai trả lời tiếp.")
            else:
                final_text = (f"{display} đang gặp lỗi nên chưa trả lời "
                              "được tin này.")
            if part_sent:
                await hub.say(agent_key, message.id, final_text,
                              part=2, final=True)
            else:
                await hub.say(agent_key, message.id, final_text)
        except Exception as exc:
            # The executor must never take the hub down with it.
            log.error("api executor failed agent=%s id=%s (%s)",
                      agent_key, message.id, type(exc).__name__)
        finally:
            self._inflight.discard(marker)
