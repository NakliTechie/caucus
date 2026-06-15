"""M7 — sandbox-and-test selection: apply candidate edits, run tests, select the survivor."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from caucus.combos import Combo
from caucus.sandbox import get_sandbox
from caucus.selection import (
    CandidateEdit,
    apply_edit,
    edit_from_tool_use,
    run_action_selection,
    select_survivor,
)

_EDIT_TEST_CMD = "python3 -c \"import sys; sys.exit(0 if 'NEW' in open('data.txt').read() else 1)\""


def _action_body():
    return {
        "model": "x", "max_tokens": 128, "tools": [{"name": "str_replace_editor"}],
        "messages": [
            {"role": "user", "content": "change OLD to NEW in data.txt"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t", "name": "str_replace_editor",
                 "input": {"command": "view", "path": "data.txt"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t", "content": "OLD"}]},
        ],
    }

_SB = get_sandbox()
requires_sandbox = pytest.mark.skipif(_SB is None, reason="no sandbox backend on this host")

# A plain-python test command (pytest may not be on the sandbox's scrubbed PATH).
_TEST_CMD = ["python3", "-c",
             "import sys; sys.path.insert(0,'.'); from mathutil import add; "
             "sys.exit(0 if add(2,3)==5 else 1)"]


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mathutil.py").write_text("def add(a, b):\n    return a - b\n")  # BUG: subtracts
    return repo


def _cand(i, edit):
    return SimpleNamespace(index=i, text=f"candidate {i}", edit=edit)


# ---- edit application ----------------------------------------------------------------

def test_apply_str_replace_create_insert(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    assert apply_edit(tmp_path, CandidateEdit("a.py", "str_replace", old="x = 1", new="x = 2"))
    assert "x = 2" in f.read_text()
    assert apply_edit(tmp_path, CandidateEdit("new.py", "create", new="hello\n"))
    assert (tmp_path / "new.py").read_text() == "hello\n"
    assert apply_edit(tmp_path, CandidateEdit("a.py", "insert", new="y = 3", line=1))
    assert "y = 3" in (tmp_path / "a.py").read_text()
    # a non-matching str_replace does not apply
    assert not apply_edit(tmp_path, CandidateEdit("a.py", "str_replace", old="nope", new="z"))


def test_edit_from_tool_use():
    e = edit_from_tool_use({"name": "str_replace_editor", "input": {
        "command": "str_replace", "path": "setup.py", "old_str": "1.2.3", "new_str": "1.2.4"}})
    assert e.op == "str_replace" and e.path == "setup.py" and e.new == "1.2.4"
    assert edit_from_tool_use({"input": {"command": "view", "path": "x"}}) is None


# ---- the select trace on a real repo (the M7 gate) -----------------------------------

@requires_sandbox
def test_select_survivor_picks_the_passing_candidate(tmp_path):
    repo = _make_repo(tmp_path)
    candidates = [
        _cand(0, CandidateEdit("mathutil.py", "str_replace", old="return a - b", new="return a + b")),  # passes
        _cand(1, CandidateEdit("mathutil.py", "str_replace", old="return a - b", new="return a * b")),  # fails
    ]
    sel = select_survivor(repo, candidates, _TEST_CMD, sandbox=_SB)
    assert sel.survivor == 0
    assert sel.reason == "tests-pass"
    assert [r.passed for r in sel.results] == [True, False]
    # the real repo is untouched (selection only mutated disposable copies)
    assert "return a - b" in (repo / "mathutil.py").read_text()


@requires_sandbox
def test_select_survivor_tiebreak_when_multiple_pass(tmp_path):
    repo = _make_repo(tmp_path)
    candidates = [
        _cand(0, CandidateEdit("mathutil.py", "str_replace", old="return a - b", new="return a + b")),
        _cand(1, CandidateEdit("mathutil.py", "str_replace", old="return a - b",
                               new="return b + a")),  # also correct
    ]
    sel = select_survivor(repo, candidates, _TEST_CMD, sandbox=_SB)
    assert sel.survivor in (0, 1)
    assert sel.reason == "judge-tiebreak"
    assert all(r.passed for r in sel.results)


@requires_sandbox
def test_select_survivor_least_bad_when_none_pass(tmp_path):
    repo = _make_repo(tmp_path)
    candidates = [
        _cand(0, CandidateEdit("mathutil.py", "str_replace", old="return a - b", new="return a * b")),
        _cand(1, CandidateEdit("mathutil.py", "str_replace", old="return a - b", new="return a - b")),
    ]
    sel = select_survivor(repo, candidates, _TEST_CMD, sandbox=_SB)
    assert sel.survivor is not None
    assert "least-bad" in sel.reason


@requires_sandbox
def test_select_survivor_degrades_when_no_edit_applies(tmp_path):
    repo = _make_repo(tmp_path)
    candidates = [_cand(0, CandidateEdit("mathutil.py", "str_replace", old="NONEXISTENT", new="x")),
                  _cand(1, None)]
    sel = select_survivor(repo, candidates, _TEST_CMD, sandbox=_SB)
    assert sel.survivor is None
    assert "degrade" in sel.reason


@requires_sandbox
async def test_run_action_selection_returns_survivor_with_edit(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "data.txt").write_text("OLD\n")
    combo = Combo("sbx", panel=["caucus-mock/edit"], judge="caucus-mock/edit",
                  strategy="sandbox-and-test")
    resp = await run_action_selection(_action_body(), combo, workspace=str(repo),
                                      test_command=_EDIT_TEST_CMD, n=3)
    assert resp is not None
    assert resp["id"].startswith("msg_")
    assert resp["stop_reason"] == "tool_use"
    tool_use = [b for b in resp["content"] if b["type"] == "tool_use"][0]
    assert tool_use["input"]["new_str"] == "NEW"  # the survivor's edit, never merged
    assert (repo / "data.txt").read_text() == "OLD\n"  # real repo untouched


async def test_run_action_selection_degrades_without_workspace():
    combo = Combo("sbx", panel=["caucus-mock/edit"], judge="caucus-mock/edit",
                  strategy="sandbox-and-test")
    resp = await run_action_selection(_action_body(), combo, workspace="", test_command="")
    assert resp is None  # no workspace/test command → fail closed → pass-through
