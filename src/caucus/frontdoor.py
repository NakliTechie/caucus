"""The Anthropic front door — in-process, via LiteLLM.

Two responsibilities:

* **Pass-through** (v1.0 action turns) — hand the Anthropic ``/v1/messages``
  request to ``litellm.anthropic_messages``, LiteLLM's own in-process Anthropic endpoint.
  Streaming is preserved end-to-end. No translator is written here.

* **Translate** (used by plan-turn synth) — convert Anthropic⇄OpenAI with LiteLLM's
  ``LiteLLMAnthropicMessagesAdapter`` so the synth engine (which speaks OpenAI chat) can run the
  panel, then convert the synthesized result back. Again — LiteLLM's translator, not ours.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Optional

import litellm
from litellm.llms.anthropic.experimental_pass_through.adapters.transformation import (
    LiteLLMAnthropicMessagesAdapter,
)
from litellm.types.llms.anthropic import AnthropicMessagesRequest

_adapter = LiteLLMAnthropicMessagesAdapter()

# Fields of an Anthropic /v1/messages request that map straight onto
# litellm.anthropic_messages(**kwargs).
_PASSTHROUGH_FIELDS = (
    "messages",
    "max_tokens",
    "system",
    "metadata",
    "stop_sequences",
    "temperature",
    "thinking",
    "tool_choice",
    "tools",
    "top_k",
    "top_p",
)


def provider_of(model: str) -> Optional[str]:
    """Provider prefix of a ``provider/model`` slug, or None to let LiteLLM infer it."""
    return model.split("/", 1)[0] if "/" in model else None


def _passthrough_kwargs(body: dict, *, stream: bool) -> dict:
    model = body.get("model", "")
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": int(body.get("max_tokens", 1024)),
        "messages": body.get("messages", []),
        "stream": stream,
    }
    for field in _PASSTHROUGH_FIELDS:
        if field in ("messages", "max_tokens"):
            continue
        if body.get(field) is not None:
            kwargs[field] = body[field]
    provider = provider_of(model)
    if provider:
        kwargs["custom_llm_provider"] = provider
    return kwargs


async def passthrough(body: dict, *, stream: bool, fallback: str = "") -> Any:
    """Pass-through turn: Anthropic in, Anthropic out, via LiteLLM. Returns a dict
    (non-streaming) or an async byte iterator (SSE, streaming).

    Availability escalation: if the primary provider errors, retry once on the cloud
    ``fallback``. (Floor escalation is a deliberation-turn concern — see synth; buffering a
    stream to score it would break pass-through streaming.)"""
    try:
        return await litellm.anthropic_messages(**_passthrough_kwargs(body, stream=stream))
    except Exception:
        if fallback and body.get("model") != fallback:
            return await litellm.anthropic_messages(
                **_passthrough_kwargs({**body, "model": fallback}, stream=stream)
            )
        raise


def to_openai_request(body: dict) -> tuple[dict, dict]:
    """Anthropic request → (OpenAI chat request dict, tool-name mapping). For synth.

    The adapter requires a ``model`` key; Caucus ignores the agent-sent model (the combo decides),
    so inject a placeholder when absent — the panel/judge models are chosen by the synth engine."""
    request = AnthropicMessagesRequest(**{**body, "model": body.get("model") or "caucus"})
    openai_request, tool_name_mapping = _adapter.translate_anthropic_to_openai(request)
    return dict(openai_request), dict(tool_name_mapping or {})


def to_anthropic_response(model_response: Any, tool_name_mapping: dict | None = None) -> dict:
    """OpenAI ModelResponse → Anthropic /v1/messages response dict. For synth."""
    anthropic_response = _adapter.translate_openai_response_to_anthropic(
        model_response, tool_name_mapping=tool_name_mapping or {}
    )
    return dict(anthropic_response)
