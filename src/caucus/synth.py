"""Plan-turn synth (M2) — convene the panel, run the judge, return one synthesized answer.

A turn the classifier labels ``plan`` is routed here instead of passing through. Caucus's native
synth engine (``synth_engine``) runs the combo's panel **in parallel** and the judge synthesizes —
and, crucially, if the model's answer is a TOOL CALL, the tool call is returned intact (a tool call
can't be "synthesized" into prose). The Anthropic request is converted to OpenAI for the engine and
the result converted back via LiteLLM's adapter (``frontdoor``).

The engine is synchronous (blocking provider calls), so it runs in a worker thread — the event loop
and every concurrent pass-through turn stay responsive. Streaming delivers the already-deliberated
answer — text *or* tool_use — over a correct Anthropic SSE sequence.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import AsyncIterator

from . import frontdoor, synth_engine
from .combos import Combo
from .routing import meets_floor


def moa_inputs(body: dict) -> tuple[str, str]:
    """Anthropic request → (system_prompt, transcript) for the text-based candidate generators
    (action selection, v1.1). The Anthropic ``system`` field becomes a system message that
    ``parse_conversation`` folds in; user/assistant turns become the transcript."""
    messages: list[dict] = []
    system = body.get("system")
    if system:
        if isinstance(system, list):
            system = " ".join(b.get("text", "") for b in system if isinstance(b, dict))
        messages.append({"role": "system", "content": str(system)})
    messages.extend(body.get("messages", []))
    system_prompt, initial_query, _ = synth_engine.parse_conversation(messages)
    return system_prompt, initial_query


def _result_to_anthropic(result: "synth_engine.SynthResult", tool_map: dict, prompt_chars: int) -> dict:
    """Turn a SynthResult into an Anthropic response — reusing LiteLLM's OpenAI→Anthropic adapter so
    a tool turn becomes a real ``tool_use`` block (and any sanitized tool names map back)."""
    from litellm.types.utils import (
        ChatCompletionMessageToolCall, Choices, Function, Message, ModelResponse, Usage,
    )
    tcs = None
    if result.tool_calls:
        tcs = []
        for i, tc in enumerate(result.tool_calls):
            fn = tc.get("function", {})
            tcs.append(ChatCompletionMessageToolCall(
                id=tc.get("id") or f"call_{i}", type="function",
                function=Function(name=fn.get("name", ""), arguments=fn.get("arguments", "{}"))))
    in_tok, out_tok = max(1, prompt_chars // 4), max(1, result.tokens or 1)
    mr = ModelResponse(
        id=f"chatcmpl-{uuid.uuid4().hex[:24]}", created=int(time.time()),
        model=result.model or "caucus", object="chat.completion",
        choices=[Choices(index=0, message=Message(role="assistant", content=(result.content or None),
                                                  tool_calls=tcs),
                         finish_reason=("tool_calls" if result.is_tool_turn else "stop"))],
        usage=Usage(prompt_tokens=in_tok, completion_tokens=out_tok, total_tokens=in_tok + out_tok))
    anthropic = dict(frontdoor.to_anthropic_response(mr, tool_map))
    anthropic["id"] = f"msg_{uuid.uuid4().hex[:24]}"  # a synthesized msg_, not a passthrough id
    anthropic.setdefault("usage", {"input_tokens": in_tok, "output_tokens": out_tok})
    return anthropic


async def synthesize(body: dict, combo: Combo, fallback: str = "") -> dict:
    """Run plan-turn synth over a combo → an Anthropic response dict (text or tool_use).

    The panel members generate candidates in parallel; if the answer is a tool call it's returned
    as-is, otherwise the judge critiques + synthesizes. Availability fallback: each provider
    call escalates to ``fallback`` on an outage, and a sub-floor *text* synthesis re-runs the whole
    deliberation on the cloud ``fallback`` (a tool turn isn't prose to floor-check)."""
    openai_req, tool_map = frontdoor.to_openai_request(body)
    messages = openai_req.get("messages", [])
    tools = openai_req.get("tools")
    max_tokens = int(body.get("max_tokens", 4096))
    result = await asyncio.to_thread(
        synth_engine.synthesize, messages, combo.panel, combo.judge,
        tools=tools, max_tokens=max_tokens, fallback=fallback)
    if (fallback and result.model != fallback and not result.is_tool_turn
            and not meets_floor(result.content)):
        result = await asyncio.to_thread(
            synth_engine.synthesize, messages, [fallback], fallback,
            tools=tools, max_tokens=max_tokens, fallback=fallback)
    return _result_to_anthropic(result, tool_map, len(str(messages)))


def _sse(event_name: str, data: dict) -> bytes:
    return f"event: {event_name}\ndata: {json.dumps(data)}\n\n".encode()


def _text_chunks(text: str, size: int = 48) -> list[str]:
    words, chunks, cur = text.split(" "), [], ""
    for w in words:
        piece = (" " if cur else "") + w
        if len(cur) + len(piece) > size and cur:
            chunks.append(cur)
            cur = w
        else:
            cur += piece
    if cur:
        chunks.append(cur)
    return chunks or [text]


async def message_to_sse(message: dict) -> AsyncIterator[bytes]:
    """Emit an already-synthesized Anthropic message as a correct SSE sequence — text blocks stream
    as text_delta, tool_use blocks stream as a tool_use block + input_json_delta."""
    content = message.get("content") or []
    msg_open = {k: v for k, v in message.items() if k != "content"}
    msg_open["content"] = []
    yield _sse("message_start", {"type": "message_start", "message": msg_open})
    for i, block in enumerate(content):
        btype = block.get("type")
        if btype == "tool_use":
            yield _sse("content_block_start", {"type": "content_block_start", "index": i,
                "content_block": {"type": "tool_use", "id": block.get("id"),
                                  "name": block.get("name"), "input": {}}})
            yield _sse("content_block_delta", {"type": "content_block_delta", "index": i,
                "delta": {"type": "input_json_delta", "partial_json": json.dumps(block.get("input") or {})}})
            yield _sse("content_block_stop", {"type": "content_block_stop", "index": i})
        else:
            yield _sse("content_block_start", {"type": "content_block_start", "index": i,
                "content_block": {"type": "text", "text": ""}})
            for chunk in _text_chunks(block.get("text", "")):
                yield _sse("content_block_delta", {"type": "content_block_delta", "index": i,
                    "delta": {"type": "text_delta", "text": chunk}})
            yield _sse("content_block_stop", {"type": "content_block_stop", "index": i})
    yield _sse("message_delta",
               {"type": "message_delta",
                "delta": {"stop_reason": message.get("stop_reason") or "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": (message.get("usage") or {}).get("output_tokens", 1)}})
    yield _sse("message_stop", {"type": "message_stop"})
