"""Plan-turn synth (M2): MOA inputs, synthesized Anthropic message, SSE emission."""

from caucus import synth
from caucus.combos import Combo
from caucus.routing import meets_floor


def _combo(model: str) -> Combo:
    return Combo(model, panel=[model], judge=model)


def test_moa_inputs_extracts_system_and_query():
    body = {
        "system": "You are terse.",
        "messages": [
            {"role": "user", "content": "How should I design the cache?"},
            {"role": "assistant", "content": "Let me think."},
            {"role": "user", "content": "Focus on invalidation."},
        ],
    }
    system, query = synth.moa_inputs(body)
    assert "terse" in system
    assert "design the cache" in query
    assert "invalidation" in query  # full transcript, not just the last turn


async def test_synthesize_returns_anthropic_message():
    body = {
        "model": "caucus-mock/echo", "max_tokens": 256, "system": "be helpful",
        "messages": [{"role": "user", "content": "How should I design a cache?"}],
    }
    msg = await synth.synthesize(body, _combo("caucus-mock/echo"))
    assert msg["type"] == "message" and msg["role"] == "assistant"
    assert msg["content"][0]["type"] == "text" and msg["content"][0]["text"]
    assert msg["id"].startswith("msg_")  # synth mints its own id
    assert msg["usage"]["output_tokens"] > 0


async def test_synth_honesty_floor_escalation():
    # The 'weak' model returns a sub-floor synthesized result → the whole synth escalates to cloud.
    body = {"max_tokens": 128, "messages": [{"role": "user", "content": "How should I design X?"}]}
    msg = await synth.synthesize(body, _combo("caucus-mock/weak"), fallback="caucus-mock/echo")
    assert msg["model"] == "caucus-mock/echo", "should have escalated past the sub-floor primary"
    assert meets_floor(msg["content"][0]["text"])


async def test_synth_no_escalation_when_floor_met():
    body = {"max_tokens": 128, "messages": [{"role": "user", "content": "How should I design X?"}]}
    msg = await synth.synthesize(body, _combo("caucus-mock/echo"), fallback="caucus-mock/cloud")
    assert msg["model"] == "caucus-mock/echo"  # primary cleared the floor; no escalation


async def test_synthesize_heterogeneous_panel():
    # A real multi-model panel: members generate, judge synthesises (all mock here).
    combo = Combo("mix", panel=["caucus-mock/echo", "caucus-mock/alpha", "caucus-mock/beta"],
                  judge="caucus-mock/echo")
    body = {"max_tokens": 256, "messages": [{"role": "user", "content": "How should I shard the DB?"}]}
    msg = await synth.synthesize(body, combo)
    assert msg["type"] == "message" and msg["content"][0]["text"]
    assert msg["model"] == "caucus-mock/echo"  # the judge model is reported


async def test_message_to_sse_is_a_valid_anthropic_sequence():
    msg = {
        "id": "msg_x", "type": "message", "role": "assistant", "model": "x",
        "content": [{"type": "text", "text": "hello world this is a synthesized answer"}],
        "usage": {"output_tokens": 7}, "stop_reason": "end_turn", "stop_sequence": None,
    }
    body = b"".join([chunk async for chunk in synth.message_to_sse(msg)]).decode()
    for ev in ("message_start", "content_block_start", "content_block_delta",
               "content_block_stop", "message_delta", "message_stop"):
        assert f"event: {ev}" in body
