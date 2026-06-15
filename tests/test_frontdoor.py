"""Front door: native Anthropic pass-through + LiteLLM's Anthropic⇄OpenAI translation."""

from caucus import frontdoor


async def test_passthrough_returns_anthropic_shape():
    body = {
        "model": "caucus-mock/echo",
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
    }
    resp = await frontdoor.passthrough(body, stream=False)
    assert resp["type"] == "message"
    assert resp["role"] == "assistant"
    assert resp["content"][0]["type"] == "text"
    assert "usage" in resp


def test_translate_anthropic_to_openai_and_back():
    body = {
        "model": "gpt-4o",
        "max_tokens": 32,
        "system": "be terse",
        "messages": [{"role": "user", "content": "hello"}],
    }
    openai_req, mapping = frontdoor.to_openai_request(body)
    assert "messages" in openai_req
    roles = [m["role"] for m in openai_req["messages"]]
    assert "user" in roles
    # system prompt is carried over (as a system message or system field)
    assert "system" in roles or any("terse" in str(m.get("content", "")) for m in openai_req["messages"])
    assert isinstance(mapping, dict)


def test_provider_of():
    assert frontdoor.provider_of("ollama/llama3.2:1b") == "ollama"
    assert frontdoor.provider_of("caucus-mock/echo") == "caucus-mock"
    assert frontdoor.provider_of("gpt-4o") is None
