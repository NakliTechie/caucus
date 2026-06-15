"""Caucus's native, tool-aware mixture-of-agents engine.

(It's a clean in-process MoA, not the project's moat — the turn-aware assembly and the repo-aware
sandbox-and-test selection are. A text judge reading candidates helps open-ended reasoning more
than code, where only execution verifies; see selection.py for the execution-based path.)

Two jobs, both over OpenAI-shaped messages so a panel member can return a real ``tool_call``:

* ``synthesize`` (plan turns) — the panel answers IN PARALLEL, then:
  - **tool turn**: if the model wants to call a tool, that's structured output a critique/synthesis
    can't improve — we return the tool call as-is (this is the fix for "synth flattened the tool
    call into text"). Only *text* candidates go through critique + synthesis.
  - **text turn**: the judge critiques the candidates and synthesizes one answer.
* ``best_of_n`` (action selection, v1.1) — sample N in parallel, rate each, pick the best.

Token budgets derive from the request's ``max_tokens`` (no hardcoded cap that truncated reasoning
models). Every provider call goes through ``providers.call`` so the inspector trace + availability
fallback are preserved; the panel runs in threads, so each call is launched inside a copy of the
request context and the inspector's ``call_trace`` contextvar (a shared list) stays visible.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import providers
from .providers import call_trace

_CRITIQUE_SYS = ("You are a meticulous reviewer. Briefly critique each candidate answer's strengths "
                 "and weaknesses against the user's request. Be specific and concise.")
_SYNTH_SYS = ("Synthesize the single best final answer to the user's request, drawing on the candidate "
              "answers and the critique. Return only the final answer — no preamble, no meta-commentary.")


@dataclass
class Candidate:
    content: str = ""
    tool_calls: list = field(default_factory=list)
    finish_reason: str = "stop"
    tokens: int = 0


@dataclass
class SynthResult:
    content: str = ""
    tool_calls: list = field(default_factory=list)
    finish_reason: str = "end_turn"
    tokens: int = 0
    model: str = ""
    is_tool_turn: bool = False


def _candidate(resp) -> Candidate:
    ch = resp.choices[0]
    msg = ch.message
    tcs = []
    for tc in (getattr(msg, "tool_calls", None) or []):
        tcs.append(tc.model_dump() if hasattr(tc, "model_dump") else dict(tc))
    return Candidate(content=msg.content or "", tool_calls=tcs,
                     finish_reason=ch.finish_reason or "stop",
                     tokens=int(getattr(resp.usage, "completion_tokens", 0) or 0))


def _last_user_text(messages: list[dict]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, list):
                return " ".join(b.get("text", "") for b in content if isinstance(b, dict)).strip()
            return str(content or "").strip()
    return ""


def _parallel(fn, items: list) -> list:
    """Run fn over items in threads. ThreadPoolExecutor workers start with an EMPTY context, so we
    capture the request's inspector trace list here (main thread) and bind it inside each worker —
    that way every parallel panel call's _record lands in the same shared list (CPython
    list.append is atomic under the GIL)."""
    trace = call_trace.get()

    def runner(it):
        if trace is not None:
            call_trace.set(trace)
        return fn(it)

    with ThreadPoolExecutor(max_workers=max(1, len(items))) as ex:
        return list(ex.map(runner, items))


def synthesize(messages: list[dict], panel: list[str], judge: str, *, tools: list | None = None,
               max_tokens: int = 4096, fallback: str | None = None) -> SynthResult:
    panel = panel or [judge]

    def one(model: str):
        try:
            return _candidate(providers.call(model, messages, kind="panel", max_tokens=max_tokens,
                                             temperature=1.0, tools=tools, fallback=fallback))
        except Exception:
            return None

    cands = [c for c in _parallel(one, panel) if c]
    if not cands:
        raise RuntimeError("all panel members failed")
    total = sum(c.tokens for c in cands)

    tool_cands = [c for c in cands if c.tool_calls]
    if tool_cands:  # tool turn — return the structured call, do NOT synthesize it into text
        best = tool_cands[0]
        return SynthResult(content=best.content, tool_calls=best.tool_calls, finish_reason="tool_calls",
                           tokens=total, model=panel[0], is_tool_turn=True)

    texts = [c.content for c in cands if c.content.strip()]
    if not texts:
        return SynthResult(content=cands[0].content, tokens=total, model=judge)
    if len(texts) == 1:
        return SynthResult(content=texts[0], tokens=total, model=judge)

    user_q = _last_user_text(messages)
    numbered = "\n\n".join(f"Candidate {i + 1}:\n{t}" for i, t in enumerate(texts))
    crit = providers.call(judge, [
        {"role": "system", "content": _CRITIQUE_SYS},
        {"role": "user", "content": f"User request:\n{user_q}\n\n{numbered}\n\nCritique each candidate:"},
    ], kind="judge", max_tokens=max_tokens, temperature=0.2, fallback=fallback)
    critique = crit.choices[0].message.content or ""
    total += int(getattr(crit.usage, "completion_tokens", 0) or 0)

    fin = providers.call(judge, [
        {"role": "system", "content": _SYNTH_SYS},
        {"role": "user", "content": f"User request:\n{user_q}\n\n{numbered}\n\nCritique:\n{critique}\n\nFinal answer:"},
    ], kind="judge", max_tokens=max_tokens, temperature=0.2, fallback=fallback)
    final = fin.choices[0].message.content or texts[0]
    total += int(getattr(fin.usage, "completion_tokens", 0) or 0)
    return SynthResult(content=final, finish_reason="end_turn", tokens=total, model=judge)


def best_of_n(messages: list[dict], model: str, *, n: int = 3, max_tokens: int = 4096,
              fallback: str | None = None) -> SynthResult:
    def one(_i: int):
        try:
            return _candidate(providers.call(model, messages, kind="panel", max_tokens=max_tokens,
                                             temperature=1.0, fallback=fallback))
        except Exception:
            return None

    cands = [c for c in _parallel(one, list(range(n))) if c and (c.content or c.tool_calls)]
    if not cands:
        raise RuntimeError("best_of_n: no candidates")
    total = sum(c.tokens for c in cands)
    if len(cands) == 1:
        return SynthResult(content=cands[0].content, tool_calls=cands[0].tool_calls,
                           is_tool_turn=bool(cands[0].tool_calls), tokens=total, model=model)

    def rate(c: Candidate) -> float:
        try:
            r = providers.call(model, messages + [
                {"role": "assistant", "content": c.content or "(tool call)"},
                {"role": "user", "content": "Rate the response above from 0 to 10 (only the number)."},
            ], kind="judge", max_tokens=16, temperature=0.1, fallback=fallback)
            return float((r.choices[0].message.content or "0").strip().split()[0])
        except Exception:
            return 0.0

    best = max(cands, key=rate)
    return SynthResult(content=best.content, tool_calls=best.tool_calls,
                       is_tool_turn=bool(best.tool_calls), tokens=total, model=model)


def parse_conversation(messages: list[dict]) -> tuple[str, str, None]:
    """Fold a message list into (system_prompt, transcript, None) — for the text-only callers that
    still want a flattened query. Multimodal content blocks collapse to their text parts."""
    system_prompt, conversation = "", []
    for message in messages:
        role, content = message.get("role"), message.get("content")
        if isinstance(content, list):
            text = " ".join(b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text")
        else:
            text = content or ""
        if role == "system":
            system_prompt = text
        elif role == "user":
            conversation.append(f"User: {text}")
        elif role == "assistant":
            conversation.append(f"Assistant: {text}")
    return system_prompt, "\n".join(conversation), None
