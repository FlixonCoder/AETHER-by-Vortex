"""
LLM client factory.
Returns a real Anthropic client, an OpenRouter-backed adapter matching the
same `.messages.create(...)` interface, or a scenario-aware offline mock.
"""
import asyncio
import re
import time

import httpx
from config import (
    ANTHROPIC_API_KEY,
    LLM_PROVIDER,
    LLM_REASONING_EFFORT,
    OFFLINE_MODE,
    OPENROUTER_BASE_URL,
    OPENROUTER_MODEL_MAP,
)


def _strip_reasoning(text: str) -> str:
    """Drop any <think>...</think> block a reasoning model leaks into the reply."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


class _Content:
    def __init__(self, text: str):
        self.text = text


class _Response:
    def __init__(self, text: str):
        self.content = [_Content(text)]


class _OpenRouterMessages:
    def __init__(self, api_key: str):
        self._api_key = api_key

    def create(self, model: str, max_tokens: int, messages: list, timeout: float = 90.0) -> _Response:
        or_model = OPENROUTER_MODEL_MAP.get(model, f"anthropic/{model}")
        body = {"model": or_model, "max_tokens": max_tokens, "messages": messages}
        if LLM_REASONING_EFFORT:
            body["reasoning_effort"] = LLM_REASONING_EFFORT

        # Free-tier inference endpoints throttle and time out intermittently.
        # Without a retry a single blip drops the agent into its canned
        # fallback, which looks like a working pipeline producing bad content.
        last_err = None
        for attempt in range(3):
            try:
                resp = httpx.post(
                    f"{OPENROUTER_BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                    json=body,
                    timeout=timeout,
                )
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise httpx.HTTPStatusError(f"retryable HTTP {resp.status_code}",
                                                request=resp.request, response=resp)
                resp.raise_for_status()
                data = resp.json()
                return _Response(_strip_reasoning(data["choices"][0]["message"]["content"]))
            except Exception as e:
                last_err = e
                if attempt < 2:
                    print(f"[LLM] {type(e).__name__} on attempt {attempt + 1}/3, retrying…", flush=True)
                    time.sleep(1.5 * (attempt + 1))

        raise last_err


class OpenRouterClient:
    def __init__(self, api_key: str):
        self.messages = _OpenRouterMessages(api_key)


async def acreate(client, **kwargs) -> _Response:
    """Run a blocking `.messages.create(...)` off the event loop.

    Every provider client here (Anthropic SDK, the httpx adapter, the mock) is
    synchronous. Calling one directly inside a coroutine stalls the whole event
    loop for the duration of the request, which freezes telemetry ticks and
    WebSocket broadcasts while an agent is thinking. Off-loading to a worker
    thread keeps the dashboard live and lets independent calls overlap.
    """
    # Only the httpx adapter understands a per-call timeout; the mock and the
    # Anthropic SDK take their own, so drop it rather than raising TypeError.
    if not isinstance(client, OpenRouterClient):
        kwargs.pop("timeout", None)
    return await asyncio.to_thread(lambda: client.messages.create(**kwargs))


def get_client():
    if OFFLINE_MODE:
        from .mock_llm import MockAnthropic
        return MockAnthropic()

    if LLM_PROVIDER == "openrouter":
        return OpenRouterClient(ANTHROPIC_API_KEY)

    import anthropic
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
