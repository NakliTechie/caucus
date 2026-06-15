"""Turn classifier — plan-turn (deliberate) vs action-turn (pass through).

It runs per ``/v1/messages`` request and decides whether the model is at a *decision point*
(where a panel earns its cost) or *executing* (where streaming speed matters and merging edits is
ill-defined).

This is a **regex heuristic over the latest turn**, not a learned or repo-aware model — it reads
the message shape and a few prompt signals, nothing about the repository or the loop history. It
routes the obvious cases well but can misread intent (a "rethink why the test failed" continuation
looks like an action turn); a learned classifier is the roadmap. The signals — tool-call presence,
message shape, prompt signals — resolve to one explainable cascade:

  1. explicit planning intent in the human's latest text  → PLAN   (a decision point, even mid-loop)
  2. a pure tool_result continuation (no new human ask)   → ACTION (mid-execution; keep streaming)
  3. explicit do-it-now action intent                     → ACTION
  4. no tools available at all                            → PLAN   (the model can only reason)
  5. a fresh, substantive human instruction (default)     → PLAN   (start-of-task decision point)

Default-to-PLAN on a *fresh* instruction but ACTION on *continuations* keeps synth scoped to
roughly the start of each human task — exactly the "deliberate at decision points, execute on
one model" shape. The bulk of a coding loop is continuations, so it passes through.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class Turn(str, Enum):
    PLAN = "plan"
    ACTION = "action"


# Planning / reasoning intent — "help me decide / understand", checked first so it can
# override a mid-execution loop.
_PLAN_RE = re.compile(
    r"\b(plan|design|architect\w*|approach|strateg\w+|trade-?offs?|compare|comparison|"
    r"explain|why|rationale|pros and cons|high[- ]level|overview|brainstorm|propose|"
    r"options|recommend\w*|evaluate|think through|reason about|walk me through|"
    r"figure out|come up with|best way|should (i|we|it)|how should|what.{0,15}best)\b",
    re.IGNORECASE,
)

# Do-it-now action intent — a fresh imperative that wants execution, not deliberation.
_ACTION_RE = re.compile(
    r"\b(run|re-?run|execute|lint|re-?format|format|commit|install|rebuild|re-?build|"
    r"apply the|go ahead|just do it|make it so)\b",
    re.IGNORECASE,
)


@dataclass
class ClassifierResult:
    turn: Turn
    reason: str
    signals: dict = field(default_factory=dict)

    @property
    def is_plan(self) -> bool:
        return self.turn is Turn.PLAN


def _last_user_message(messages: list[dict]) -> dict | None:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return msg
    return None


def _human_text_and_tool_result(message: dict | None) -> tuple[str, bool]:
    """Return (human-authored text, has_tool_result) for a user message.

    Tool-result blocks are model/tooling output, not the human's words — they must not
    trigger plan-intent, and a message that is *only* tool results is a continuation.
    """
    if message is None:
        return "", False
    content = message.get("content")
    if isinstance(content, str):
        return content, False
    if not isinstance(content, list):
        return "", False
    texts: list[str] = []
    has_tool_result = False
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "tool_result":
            has_tool_result = True
        elif btype == "text" and block.get("text"):
            texts.append(str(block["text"]))
    return " ".join(texts).strip(), has_tool_result


def classify(body: dict) -> ClassifierResult:
    """Classify a single Anthropic /v1/messages request as a plan or action turn."""
    messages = body.get("messages") or []
    tools = body.get("tools") or []
    last_user = _last_user_message(messages)
    human_text, has_tool_result = _human_text_and_tool_result(last_user)
    is_continuation = has_tool_result and not human_text

    signals = {
        "has_tools": bool(tools),
        "has_tool_result": has_tool_result,
        "is_continuation": is_continuation,
        "plan_intent": bool(_PLAN_RE.search(human_text)),
        "action_intent": bool(_ACTION_RE.search(human_text)),
    }

    if signals["plan_intent"]:
        return ClassifierResult(Turn.PLAN, "plan-intent", signals)
    if is_continuation:
        return ClassifierResult(Turn.ACTION, "continuation (tool_result)", signals)
    if signals["action_intent"]:
        return ClassifierResult(Turn.ACTION, "action-intent", signals)
    if not tools:
        return ClassifierResult(Turn.PLAN, "no-tools (prose/reasoning)", signals)
    return ClassifierResult(Turn.PLAN, "fresh-instruction (decision point)", signals)
