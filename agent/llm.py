"""Model providers for the buyer agent: one tool loop, three wire formats.

The harness in buyer.py talks to a Chat: send the request, get back text and tool calls, send the tool
results, repeat. Which model sits behind it is a setting. What a model must not hold (the mandate, the
signing key, the approval link) never passes through this file.

* Claude: Anthropic SDK through a local model proxy (any Anthropic-compatible endpoint).
* GPT:    the same proxy's OpenAI-compatible endpoint.
* Gemini: Google's OpenAI-compatible endpoint. It receives the purchasing request and tool data only.
"""
import asyncio
import json
import os
import re
import subprocess
from dataclasses import dataclass, field

import anthropic
import httpx

# Frontier models only. id is what goes into run["model"]. pick: offered in the console, where a person waits for the answer.
MODELS = [
    {"id": "claude-opus-5-5", "label": "Claude Opus 5.5", "provider": "anthropic", "pick": True},
    {"id": "gpt-6.1-sol", "label": "GPT-6.1 Sol", "provider": "openai", "pick": True},
    {"id": "gemini-4-hydrogen", "label": "Gemini 4 Hydrogen", "provider": "google", "pick": True},  # the Gemini a person waits for
    {"id": "gemini-4-carbon", "label": "Gemini 4 Carbon", "provider": "google", "pick": False},     # the Gemini for work nobody waits on (the test runs)
    # The one model here that is not a frontier model, kept for one reason: a buyer that must run its agent on its own hardware uses open weights.
    {"id": "gemma-4-31b-it", "label": "Gemma 4 31B (open weights)", "provider": "google", "pick": True},
]
# Models in earlier recorded runs, so their records still read well. None of them is used any more.
PAST = {"claude-sonnet-5-5": "Claude Sonnet 5.5", "claude-haiku-5-5": "Claude Haiku 5.5", "gpt-6-sol": "GPT-6 Sol", "gpt-6-luna": "GPT-6 Luna", "gpt-5.6-luna": "GPT-5.6 Luna", "gemini-3.8-flash": "Gemini 3.8 Flash"}
DEFAULT_MODEL = os.environ.get("AGENT_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("AGENT_EFFORT", "low")
CALL_TIMEOUT_S = float(os.environ.get("AGENT_CALL_TIMEOUT_S", "40"))
RETRY_PAUSE_S = float(os.environ.get("AGENT_RETRY_PAUSE_S", "3"))  # wait this long, once, after a rate limit or a 5xx
MAX_TOKENS = 8000
MODEL_PROXY = os.environ.get("AGENT_BASE_URL", "http://127.0.0.1:8317")

# Where each provider lives and which vault entry holds its key.
PROVIDERS = {
    "anthropic": {"name": "Anthropic", "base": MODEL_PROXY, "key_env": "AGENT_API_KEY", "key_hsec": "CLIPROXY_API_KEY"},
    "openai": {"name": "OpenAI", "base": MODEL_PROXY + "/v1", "key_env": "AGENT_API_KEY", "key_hsec": "CLIPROXY_API_KEY"},
    "google": {"name": "Google", "base": os.environ.get("AGENT_GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai"),
               "key_env": "AGENT_GEMINI_API_KEY", "key_hsec": "JUNCTION_GEMINI_API_KEY"},
}
# The model a run switches to when its own stops answering. Only a frontier model stands in for a frontier model:
# Claude and GPT have one each, so they have none inside their provider; the two Gemini models cover each other.
FALLBACK = {"gemini-4-hydrogen": "gemini-4-carbon", "gemini-4-carbon": "gemini-4-hydrogen"}
_keys: dict[str, str] = {}


def provider_of(model: str) -> str:
    known = next((m["provider"] for m in MODELS if m["id"] == model), None)
    return known or ("openai" if model.startswith("gpt-") else "google" if model.startswith("gemini") else "anthropic")


def stand_in(model: str) -> str | None:
    """A frontier model at another provider that can take a request when this one's provider does not answer."""
    return "gpt-6.1-sol" if provider_of(model) == "google" and not model.startswith("gemma") else "gemini-4-hydrogen"


def label_of(model: str) -> str:
    return next((m["label"] for m in MODELS if m["id"] == model), PAST.get(model, model))


def _key(provider: str) -> str:
    """The provider's key from the environment or the local vault. Kept in memory, never written to disk here."""
    p = PROVIDERS[provider]
    if p["key_hsec"] not in _keys:
        key = os.environ.get(p["key_env"]) or subprocess.run(["hsec", "get", p["key_hsec"]], capture_output=True, text=True, timeout=10).stdout.strip()
        if not key:
            raise RuntimeError(f"No model key: set {p['key_env']} or unlock hsec ({p['key_hsec']}).")
        _keys[p["key_hsec"]] = key
    return _keys[p["key_hsec"]]


@dataclass
class Call:
    id: str
    name: str
    args: dict
    bad_args: str | None = None  # set when the model sent arguments that are not a JSON object


@dataclass
class Reply:
    text: str
    calls: list[Call] = field(default_factory=list)
    stop: str = "end"  # "tools" (run the calls and come back), "end" (final answer) or "refusal"
    model: str = ""


class Chat:
    """One conversation with one model. Subclasses keep the history in their provider's own format."""

    def __init__(self, model: str, system: str, tools: list[dict], note=None):
        self.provider = provider_of(model)
        self.model, self.system, self.tools = model, system, tools  # tools: [{name, description, schema}]
        fb = FALLBACK.get(model)
        self.models = list(dict.fromkeys(m for m in (model, fb) if m))  # first is the one in use; a failing model is dropped for the rest of the run
        self.note = note or (lambda title, text: None)
        self.patient = True  # a test run waits out a rate limit; a person watching the console gets a clear answer in seconds instead
        self.give_up = False
        self.calls, self.used, self.tokens = 0, [], {"in": 0, "out": 0}

    async def user(self, text: str) -> Reply | None:
        """Send the buyer's request. None means no model answered."""
        raise NotImplementedError

    async def results(self, results: list[dict]) -> Reply | None:
        """Send tool results: [{id, name, content, error}]."""
        raise NotImplementedError

    async def close(self):
        pass

    def stats(self) -> dict:
        return {"provider": self.provider, "model": self.model, "calls": self.calls, "answered_by": list(dict.fromkeys(self.used)),
                "fallback": any(m != self.model for m in self.used), "tokens_in": self.tokens["in"], "tokens_out": self.tokens["out"]}

    def _failed(self, model: str, why: str):
        """A slow or failing model must not stall a run: drop it and stay on the next one."""
        nxt = next((m for m in self.models if m != model), None)
        if nxt:
            self.models.remove(model)
        # A person is watching and the first call failed: do not make them wait for the same provider's second model too.
        self.give_up = not self.patient and self.calls == 0
        if why == "HTTP 429" and model == self.model:
            self.note("Model is over its allowance", f"{label_of(model)} is over its per-minute allowance at {PROVIDERS[self.provider]['name']} right now.")
        else:
            self.note("Model call failed", f"{model}: {why}." + (f" Switching to {nxt}." if nxt and not self.give_up else ""))

    def _count(self, model: str, tokens_in, tokens_out):
        self.calls += 1
        self.used.append(model)
        self.tokens["in"] += tokens_in or 0
        self.tokens["out"] += tokens_out or 0


class ClaudeChat(Chat):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.client = anthropic.AsyncAnthropic(base_url=PROVIDERS["anthropic"]["base"], api_key=_key("anthropic"), max_retries=0, timeout=CALL_TIMEOUT_S)
        self.messages = []
        self.wire_tools = [{"name": t["name"], "description": t["description"], "input_schema": t["schema"]} for t in self.tools]

    async def user(self, text):
        self.messages.append({"role": "user", "content": text})
        return await self._ask()

    async def results(self, results):
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"], **({"is_error": True} if r.get("error") else {})} for r in results]})
        return await self._ask()

    async def _ask(self):
        for model in list(self.models):
            # Thinking blocks are tied to the model that wrote them, so the fallback model gets the history without them.
            msgs = self.messages if model == self.model else [
                {**m, "content": [b for b in m["content"] if getattr(b, "type", "") not in ("thinking", "redacted_thinking")]} if m["role"] == "assistant" else m for m in self.messages]
            resp, why = None, ""
            for attempt in (1, 2):
                try:
                    resp = await self.client.messages.create(model=model, max_tokens=MAX_TOKENS, system=self.system, tools=self.wire_tools, messages=msgs, output_config={"effort": EFFORT})
                    break
                except anthropic.APIStatusError as e:
                    why = f"HTTP {e.status_code}"
                    if attempt == 2 or not (e.status_code == 429 or e.status_code >= 500):
                        break
                    await asyncio.sleep(RETRY_PAUSE_S)  # rate limit or a provider error: wait once before giving up on this model
                except anthropic.APIError as e:  # timeout or no connection
                    why = type(e).__name__
                    break
            if resp is None:
                self._failed(model, why)
                if self.give_up:
                    break
                continue
            u = resp.usage
            self._count(model, sum(getattr(u, k, 0) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")), getattr(u, "output_tokens", 0))
            text = "\n".join(b.text for b in resp.content if b.type == "text").strip()
            if resp.stop_reason == "refusal":
                return Reply(text, stop="refusal", model=model)
            if resp.stop_reason != "tool_use":
                return Reply(text, model=model)
            self.messages.append({"role": "assistant", "content": resp.content})
            return Reply(text, [Call(b.id, b.name, dict(b.input)) for b in resp.content if b.type == "tool_use"], "tools", model)
        return None

    async def close(self):
        await self.client.close()


class OpenAIChat(Chat):
    """Chat Completions with function calling over plain HTTP. Serves GPT (the model proxy) and Gemini (Google)."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        p = PROVIDERS[self.provider]
        self.http = httpx.AsyncClient(base_url=p["base"], headers={"Authorization": f"Bearer {_key(self.provider)}"}, timeout=CALL_TIMEOUT_S)
        self.messages = [{"role": "system", "content": self.system}]
        self.wire_tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["schema"]}} for t in self.tools]

    async def user(self, text):
        self.messages.append({"role": "user", "content": text})
        return await self._ask()

    async def results(self, results):
        self.messages += [{"role": "tool", "tool_call_id": r["id"], "content": r["content"]} for r in results]
        return await self._ask()

    async def _post(self, model):
        """One call, with one retry when the provider is briefly unavailable. Returns (body, error)."""
        why = ""
        slow = model.startswith("gemma")  # the hosted open-weights model has a small per-minute token allowance
        tries = 5 if slow and self.patient else 2
        for attempt in range(1, tries + 1):
            try:
                req = {"model": model, "messages": self.messages, "tools": self.wire_tools, "reasoning_effort": EFFORT, "max_completion_tokens": MAX_TOKENS}
                if model.startswith("gemma"):
                    del req["reasoning_effort"]  # open-weights Gemma has no thinking level
                r = await self.http.post("/chat/completions", json=req)
            except httpx.HTTPError as e:
                why = type(e).__name__
            else:
                if r.status_code == 200:
                    try:
                        body = r.json()
                        if body.get("choices"):
                            return body, ""
                        why = "empty answer"
                    except ValueError:
                        why = "unreadable answer"
                else:
                    why = f"HTTP {r.status_code}"
                    if r.status_code not in (408, 409, 429) and r.status_code < 500:
                        break  # our request is wrong; trying again will not help
            if attempt < tries:
                await asyncio.sleep(25 if slow and why == "HTTP 429" and self.patient else RETRY_PAUSE_S)
        return None, why

    async def _ask(self):
        for model in list(self.models):
            body, why = await self._post(model)
            if body is None:
                self._failed(model, why)
                if self.give_up:
                    break
                continue
            u = body.get("usage") or {}
            self._count(model, u.get("prompt_tokens"), u.get("completion_tokens"))
            choice = body["choices"][0]
            msg = choice.get("message") or {}
            text = (msg.get("content") or "").strip() if isinstance(msg.get("content"), str) else ""
            text = re.sub(r"<thought>.*?</thought>", "", text, flags=re.S).strip()  # Gemma puts its reasoning inline
            if msg.get("refusal") or choice.get("finish_reason") == "content_filter":
                return Reply(text, stop="refusal", model=model)
            raw = msg.get("tool_calls") or []
            if not raw:
                return Reply(text, model=model)
            calls = []
            for n, tc in enumerate(raw):
                tc.setdefault("id", f"call_{self.calls}_{n}")
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    bad = None if isinstance(args, dict) else "arguments must be a JSON object"
                except ValueError as e:
                    args, bad = {}, f"arguments are not valid JSON ({e})"
                calls.append(Call(tc["id"], fn.get("name") or "", args if isinstance(args, dict) else {}, bad))
            # Keep the model's own message as it came (Gemini attaches a signature to each tool call that it wants back).
            self.messages.append({"role": "assistant", **{k: v for k, v in msg.items() if k != "role" and v is not None}})
            return Reply(text, calls, "tools", model)
        return None

    async def close(self):
        await self.http.aclose()


def open_chat(model: str | None, system: str, tools: list[dict], note=None) -> Chat:
    model = model or DEFAULT_MODEL
    return (ClaudeChat if provider_of(model) == "anthropic" else OpenAIChat)(model, system, tools, note)
