"""The native synth engine must be reachable in-process (M0) and run a real MOA synth on the mock."""

from caucus import synth_engine as se
from caucus.engine import engine_status
from caucus.providers import register_mock


def test_engine_reachable_in_process():
    status = engine_status()
    assert status["engine"] == "caucus-synth"
    assert status["in_process"] is True
    assert status["reachable"] is True
    assert "moa" in status["approaches"]
    assert "bon" in status["approaches"]


def test_synth_runs_panel_then_judge_against_mock():
    register_mock()
    r = se.synthesize([{"role": "user", "content": "What is 2+2? Answer briefly."}],
                      panel=["caucus-mock/echo", "caucus-mock/echo"], judge="caucus-mock/echo",
                      max_tokens=128)
    assert isinstance(r.content, str) and r.content
    assert not r.content.startswith("Error")
    assert r.tokens > 0 and not r.is_tool_turn
