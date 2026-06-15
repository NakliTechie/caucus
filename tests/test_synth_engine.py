"""The native synth engine: parallel panel, tool-awareness, inspector trace across threads."""

from caucus import synth_engine as se
from caucus.providers import call_trace, register_mock


def test_text_turn_runs_panel_in_parallel_then_judge():
    register_mock()
    call_trace.set([])
    r = se.synthesize([{"role": "user", "content": "How should I design a cache?"}],
                      panel=["caucus-mock/echo", "caucus-mock/echo"], judge="caucus-mock/echo",
                      max_tokens=128)
    assert not r.is_tool_turn and r.content
    # the inspector trace survives the worker threads: 2 panel calls + critique + synthesis
    assert [c["kind"] for c in call_trace.get()] == ["panel", "panel", "judge", "judge"]


def test_tool_turn_is_preserved_not_flattened():
    # The bug we are fixing: a turn whose answer is a tool call must NOT be synthesized into text.
    register_mock()
    r = se.synthesize([{"role": "user", "content": "change OLD to NEW in data.txt"}],
                      panel=["caucus-mock/edit"], judge="caucus-mock/echo",
                      tools=[{"type": "function", "function": {"name": "str_replace_editor", "parameters": {}}}],
                      max_tokens=128)
    assert r.is_tool_turn and r.finish_reason == "tool_calls"
    assert r.tool_calls and r.tool_calls[0]["function"]["name"] == "str_replace_editor"


def test_best_of_n_returns_a_candidate():
    register_mock()
    r = se.best_of_n([{"role": "user", "content": "do the thing"}], "caucus-mock/echo", n=3, max_tokens=64)
    assert r.content
