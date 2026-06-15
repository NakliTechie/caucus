"""Action-turn selection (v1.1).

* generate N candidate responses for an action turn (best-of-N generation).
* apply each candidate's edit in the sandbox, run the repo's test command, and select
  the survivor (tests-pass primary, judge-score tiebreak). Added on top of this module.

This is the heavy, inherently-ours part: it needs repo + sandbox + test-runner
state the engine does not have. The candidate *generation* reuses the engine's best-of-N
sampling; the *selection* by test-pass is Caucus's own.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import json
import shlex
import uuid

from .combos import Combo
from .engine import generate_candidate_messages, generate_candidates, score_candidate
from .synth import moa_inputs
from .sandbox import discard, ephemeral_copy, get_sandbox, run_tests


@dataclass
class Candidate:
    index: int
    text: str


async def generate_for_action(body: dict, combo: Combo, *, n: int = 3,
                              fallback: str = "") -> tuple[list[Candidate], int]:
    """Generate N candidates for an action turn. Returns (candidates, completion_tokens).

    Candidates come from the combo's primary (the execution model), sampled N times. The engine is
    synchronous, so generation runs in a worker thread — the event loop stays responsive.
    """
    system_prompt, query = moa_inputs(body)
    texts, tokens = await asyncio.to_thread(
        generate_candidates, system_prompt, query, combo.primary, n=n, fallback=fallback,
        request_config={"max_tokens": int(body.get("max_tokens", 4096))},
    )
    return [Candidate(i, t) for i, t in enumerate(texts)], tokens


# --- apply each candidate's edit in the sandbox, run tests, select the survivor ----

@dataclass
class CandidateEdit:
    """A structured file edit extracted from a candidate (str_replace / create / insert)."""
    path: str
    op: str          # "str_replace" | "create" | "insert"
    old: str = ""
    new: str = ""
    line: int = 0


@dataclass
class CandidateResult:
    index: int
    applied: bool
    passed: bool
    returncode: int
    output: str
    score: float = 0.0


@dataclass
class Selection:
    survivor: Optional[int]      # candidate index, or None → degrade to pass-through
    reason: str
    results: list[CandidateResult] = field(default_factory=list)


def apply_edit(workdir: Path, edit: CandidateEdit) -> bool:
    """Apply one structured edit to the sandbox copy. Never merges; one edit, one file."""
    # edit.path comes from the model's tool call, and apply_edit runs on the HOST (before the
    # sandboxed test runs), so an unchecked "../../.ssh/authorized_keys" with op=create would be an
    # arbitrary host write. Reject null bytes / empty paths, then keep the resolved target strictly
    # inside the workspace copy.
    if not edit.path or "\x00" in edit.path:
        return False
    target = (workdir / edit.path).resolve()
    root = workdir.resolve()
    if target != root and root not in target.parents:
        return False
    try:
        if edit.op == "create":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(edit.new)
            return True
        if not target.is_file():
            return False
        text = target.read_text()
        if edit.op == "str_replace":
            if edit.old == "" or edit.old not in text:
                return False
            target.write_text(text.replace(edit.old, edit.new, 1))
            return True
        if edit.op == "insert":
            lines = text.splitlines(keepends=True)
            idx = max(0, min(edit.line, len(lines)))
            lines.insert(idx, edit.new if edit.new.endswith("\n") else edit.new + "\n")
            target.write_text("".join(lines))
            return True
    except Exception:
        return False
    return False


def edit_from_tool_use(tool_use: dict) -> Optional[CandidateEdit]:
    """Map a Claude-Code edit tool_use (str_replace_editor / text_editor) to a CandidateEdit."""
    inp = tool_use.get("input", {}) if isinstance(tool_use, dict) else {}
    cmd = inp.get("command")
    path = inp.get("path") or inp.get("file_path")
    if not path:
        return None
    if cmd in ("str_replace", "replace"):
        return CandidateEdit(path, "str_replace", old=inp.get("old_str", ""), new=inp.get("new_str", ""))
    if cmd in ("create", "write"):
        return CandidateEdit(path, "create", new=inp.get("file_text", inp.get("new_str", "")))
    if cmd == "insert":
        return CandidateEdit(path, "insert", new=inp.get("new_str", inp.get("insert_line_text", "")),
                             line=int(inp.get("insert_line", 0)))
    return None


def select_survivor(workspace: Path, candidates: list, test_command: list[str], *, sandbox,
                    judge_model: str = "", fallback: str = "", query: str = "",
                    timeout: int = 60) -> Selection:
    """Apply each candidate's edit to an isolated copy, run the test command, select the survivor.

    Primary selector: tests pass. Tiebreak among passers: judge score. If none pass: the
    least-bad applied candidate, flagged. If none even applied: None → caller degrades.
    Each ``candidate`` is an object with ``.index``, ``.text``, and ``.edit`` (a CandidateEdit
    or None). Never merges edits — one candidate's edit is what survives.
    """
    results: list[CandidateResult] = []
    for cand in candidates:
        edit = getattr(cand, "edit", None)
        copy = ephemeral_copy(workspace)
        try:
            if edit is None or not apply_edit(copy, edit):
                results.append(CandidateResult(cand.index, False, False, -1, "edit did not apply"))
                continue
            outcome = run_tests(sandbox, copy, test_command, timeout=timeout)
            results.append(CandidateResult(cand.index, True, outcome.passed,
                                           outcome.returncode, outcome.output[-2000:]))
        finally:
            discard(copy)

    passers = [r for r in results if r.passed]
    if len(passers) == 1:
        return Selection(passers[0].index, "tests-pass", results)
    if len(passers) > 1:
        by_index = {c.index: c for c in candidates}
        for r in passers:
            r.score = score_candidate("", query, getattr(by_index[r.index], "text", ""),
                                      judge_model, fallback=fallback) if judge_model else 0.0
        best = max(passers, key=lambda r: r.score)
        return Selection(best.index, "judge-tiebreak", results)
    applied = [r for r in results if r.applied]
    if applied:  # none passed → least-bad (closest returncode to 0), flagged
        best = min(applied, key=lambda r: abs(r.returncode))
        return Selection(best.index, "least-bad (no candidate passed — flagged)", results)
    return Selection(None, "no-candidate-applied (degrade to pass-through)", results)


@dataclass
class ActionCandidate:
    index: int
    text: str
    edit: Optional[CandidateEdit] = None
    tool_call: Optional[dict] = None  # the raw tool_use to return to the agent if selected


async def generate_action_candidates(body: dict, combo: Combo, *, n: int = 3,
                                     fallback: str = "") -> tuple[list[ActionCandidate], int]:
    """Generate N action candidates, each parsed for its edit (from the model's tool_call)."""
    system_prompt, query = moa_inputs(body)
    msgs, tokens = await asyncio.to_thread(
        generate_candidate_messages, system_prompt, query, combo.primary, n=n, fallback=fallback,
        request_config={"max_tokens": int(body.get("max_tokens", 4096))})
    candidates: list[ActionCandidate] = []
    for i, m in enumerate(msgs):
        edit = tool_use = None
        for tc in m.get("tool_calls", []):
            try:
                args = json.loads(tc.get("arguments") or "{}")
            except Exception:
                args = {}
            tu = {"name": tc.get("name"), "input": args}
            parsed = edit_from_tool_use(tu)
            if parsed is not None:
                edit, tool_use = parsed, tu
                break
        candidates.append(ActionCandidate(i, m.get("content", ""), edit, tool_use))
    return candidates, tokens


def _survivor_response(cand: ActionCandidate, model: str, tokens: int) -> dict:
    """Build the Anthropic response for the selected survivor (its edit tool_use, never merged)."""
    content: list[dict] = []
    if cand.text:
        content.append({"type": "text", "text": cand.text})
    if cand.tool_call:
        content.append({"type": "tool_use", "id": f"toolu_{uuid.uuid4().hex[:20]}",
                        "name": cand.tool_call["name"], "input": cand.tool_call["input"]})
    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}", "type": "message", "role": "assistant",
        "model": model, "content": content or [{"type": "text", "text": ""}],
        "stop_reason": "tool_use" if cand.tool_call else "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": tokens or 1},
    }


def _sse(event_name: str, data: dict) -> bytes:
    return f"event: {event_name}\ndata: {json.dumps(data)}\n\n".encode()


async def action_message_to_sse(message: dict):
    """Emit a selected survivor (text and/or tool_use blocks) as an Anthropic SSE sequence."""
    opener = {k: v for k, v in message.items() if k != "content"}
    opener["content"] = []
    yield _sse("message_start", {"type": "message_start", "message": opener})
    for idx, block in enumerate(message["content"]):
        if block["type"] == "text":
            yield _sse("content_block_start", {"type": "content_block_start", "index": idx,
                                               "content_block": {"type": "text", "text": ""}})
            yield _sse("content_block_delta", {"type": "content_block_delta", "index": idx,
                                               "delta": {"type": "text_delta", "text": block["text"]}})
        elif block["type"] == "tool_use":
            yield _sse("content_block_start", {"type": "content_block_start", "index": idx,
                       "content_block": {"type": "tool_use", "id": block["id"],
                                         "name": block["name"], "input": {}}})
            yield _sse("content_block_delta", {"type": "content_block_delta", "index": idx,
                       "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}})
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": idx})
    yield _sse("message_delta", {"type": "message_delta",
               "delta": {"stop_reason": message["stop_reason"], "stop_sequence": None},
               "usage": {"output_tokens": message["usage"]["output_tokens"]}})
    yield _sse("message_stop", {"type": "message_stop"})


async def run_action_selection(body: dict, combo: Combo, *, workspace: str, test_command: str,
                               n: int = 3, fallback: str = "") -> Optional[dict]:
    """Full v1.1 action path: generate → apply each in the sandbox → test → select survivor.

    Returns the survivor's Anthropic response, or None to degrade to pass-through:
    no sandbox available, no workspace/test command, or no candidate produced an applicable edit.
    """
    sandbox = get_sandbox()
    ws = Path(workspace).expanduser()
    if sandbox is None or not workspace or not test_command or not ws.is_dir():
        return None  # fail closed → caller passes through
    candidates, tokens = await generate_action_candidates(body, combo, n=n, fallback=fallback)
    if not any(c.edit for c in candidates):
        return None  # nothing to apply (e.g. text-only candidates) → pass through
    _, query = moa_inputs(body)
    sel = await asyncio.to_thread(
        select_survivor, ws, candidates, shlex.split(test_command), sandbox=sandbox,
        judge_model=combo.judge, fallback=fallback, query=query)
    body.setdefault("_selection", sel)  # surfaced for logging / the console view
    if sel.survivor is None:
        return None
    winner = next(c for c in candidates if c.index == sel.survivor)
    return _survivor_response(winner, combo.primary, tokens)
