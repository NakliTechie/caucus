"""Action-turn candidate generation (M6): N candidates from a real action-turn request."""

from caucus.combos import Combo
from caucus.engine import generate_candidates, score_candidate
from caucus.selection import generate_for_action


def test_generate_candidates_returns_n():
    texts, tokens = generate_candidates(
        "You are a coding assistant.", "Add a --verbose flag.", "caucus-mock/echo", n=3)
    assert len(texts) == 3
    assert all(t for t in texts)
    assert tokens > 0


async def test_generate_for_action_on_a_real_action_turn():
    # A real action-turn shape: a tool_result continuation, tools present.
    body = {
        "model": "claude", "max_tokens": 256, "tools": [{"name": "str_replace_editor"}],
        "messages": [
            {"role": "user", "content": "Update the version string in setup.py to 1.2.4."},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "str_replace_editor",
                 "input": {"command": "view", "path": "setup.py"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "version='1.2.3'"}]},
        ],
    }
    combo = Combo("m", panel=["caucus-mock/echo"], judge="caucus-mock/echo")
    candidates, tokens = await generate_for_action(body, combo, n=4)
    assert len(candidates) == 4
    assert [c.index for c in candidates] == [0, 1, 2, 3]
    assert all(c.text for c in candidates)


def test_score_candidate_returns_a_number():
    # The mock's echo isn't a number, so scoring falls back to 0.0 — exercises the path safely.
    score = score_candidate("sys", "q", "some candidate", "caucus-mock/echo")
    assert isinstance(score, float)


def test_apply_edit_rejects_path_traversal(tmp_path):
    # F8: apply_edit runs on the HOST before the sandboxed test — a model-supplied "../" or absolute
    # path must never write outside the workspace copy.
    from caucus.selection import CandidateEdit, apply_edit
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "escaped.txt"
    assert apply_edit(work, CandidateEdit(path="../escaped.txt", op="create", new="pwned")) is False
    assert not outside.exists()
    assert apply_edit(work, CandidateEdit(path="/tmp/caucus-evil-xyz.txt", op="create", new="x")) is False
    # an in-tree create still works
    assert apply_edit(work, CandidateEdit(path="sub/ok.py", op="create", new="ok")) is True
    assert (work / "sub" / "ok.py").read_text() == "ok"
