"""Availability-fallback routing: ladder, planning-quality floor, availability + honesty escalation."""

import pytest

from caucus.providers import make_client, register_mock
from caucus.routing import ladder, meets_floor


def test_ladder_dedup_and_skip_empty():
    assert ladder("a", "b") == ["a", "b"]
    assert ladder("a", "a") == ["a"]
    assert ladder("a", "") == ["a"]
    assert ladder("", "") == []


def test_meets_floor():
    assert meets_floor("This is a sufficiently long planning answer.") is True
    assert meets_floor("ok") is False          # too short
    assert meets_floor("") is False
    assert meets_floor("Error: boom happened here") is False
    assert meets_floor("I cannot help with that, sorry there") is False


def test_client_availability_escalation_to_fallback():
    register_mock()
    client = make_client(fallback="caucus-mock/echo")
    # primary 'fail' raises → escalate to the echo fallback
    resp = client.chat.completions.create(
        model="caucus-mock/fail", messages=[{"role": "user", "content": "hi"}], max_tokens=32
    )
    assert "caucus-mock" in resp.choices[0].message.content


def test_client_without_fallback_propagates_error():
    register_mock()
    client = make_client()  # no fallback
    with pytest.raises(Exception):
        client.chat.completions.create(
            model="caucus-mock/fail", messages=[{"role": "user", "content": "hi"}], max_tokens=32
        )
