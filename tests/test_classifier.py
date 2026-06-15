"""Turn classifier: per-case labels + overall accuracy on the labeled fixture set (M1 gate)."""

import json
from pathlib import Path

import pytest

from caucus.classifier import Turn, classify

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "turn_fixtures.json").read_text())


@pytest.mark.parametrize("case", FIXTURES, ids=[c["name"] for c in FIXTURES])
def test_each_fixture(case):
    result = classify(case["body"])
    assert result.turn.value == case["expected"], (
        f"{case['name']}: expected {case['expected']}, got {result.turn.value} "
        f"(reason: {result.reason})"
    )


def test_overall_accuracy():
    correct = sum(classify(c["body"]).turn.value == c["expected"] for c in FIXTURES)
    accuracy = correct / len(FIXTURES)
    assert accuracy >= 0.9, f"accuracy {accuracy:.0%} below 0.9 ({correct}/{len(FIXTURES)})"


def test_reason_cascade():
    # plan-intent wins even mid-loop
    body = {"messages": [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t", "content": "boom"},
        {"type": "text", "text": "let's rethink the approach"}]}],
        "tools": [{"name": "bash"}]}
    r = classify(body)
    assert r.turn is Turn.PLAN and r.reason == "plan-intent"

    # pure continuation → action
    body = {"messages": [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t", "content": "11 passed"}]}],
        "tools": [{"name": "bash"}]}
    r = classify(body)
    assert r.turn is Turn.ACTION and r.reason == "continuation (tool_result)"

    # no tools → plan (prose)
    r = classify({"messages": [{"role": "user", "content": "summarize this"}]})
    assert r.turn is Turn.PLAN and r.reason == "no-tools (prose/reasoning)"
