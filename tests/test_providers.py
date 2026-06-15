"""The mock provider routes through LiteLLM's real CustomLLM path; the client the engine uses
is OpenAI-shaped."""

import litellm

from caucus.config import MOCK_MODEL
from caucus.providers import make_client, register_mock


def test_mock_completion_supports_n_for_panel():
    register_mock()
    resp = litellm.completion(
        model=MOCK_MODEL,
        messages=[{"role": "user", "content": "hi"}],
        n=3,
        temperature=1,
        max_tokens=32,
    )
    assert len(resp.choices) == 3
    assert all(c.message.content for c in resp.choices)
    # Variation across the panel so a judge has distinct candidates.
    assert resp.choices[0].message.content != resp.choices[1].message.content


def test_litellm_client_is_openai_shaped():
    client = make_client()
    resp = client.chat.completions.create(
        model=MOCK_MODEL, messages=[{"role": "user", "content": "hi"}], max_tokens=16
    )
    assert resp.choices[0].message.content
    assert hasattr(resp.usage, "completion_tokens")
    assert resp.model_dump()  # the engine logs via model_dump()
