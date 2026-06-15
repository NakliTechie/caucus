"""``caucus bench`` — a CONSUMER of the running daemon.

It is **not** part of the serving core: it talks to the running daemon over its API exactly
like any other client, and does not count against the one-serving-process rule.

Caucus's value is an empirical claim — *synth produces better output*. This proves it by
running benchmarks **through** Caucus and comparing against a single-model baseline, reporting
**accuracy AND cost** side by side (never a score without its cost).

**Reuse existing harnesses; never reimplement a benchmark.** The real benches run against
Caucus with a base-URL swap, because Caucus is an OpenAI/Anthropic-compatible endpoint:

* Reasoning / single-turn → ``lm-evaluation-harness`` (MMLU-Pro, GPQA), ``evalplus``
  (HumanEval+, MBPP+) pointed at ``ANTHROPIC_BASE_URL``/``OPENAI_BASE_URL`` = the daemon.
* Coding / agentic → SWE-bench Verified via an existing scaffold (SWE-agent / mini-swe-agent /
  Aider) + the official ``swebench`` scorer; Aider polyglot.

The A/B is the point: **Baseline** = the combo's primary model alone (one completion);
**Caucus** = the full combo through the daemon. This module provides the A/B *runner* + cost
accounting; the benchmark *content* comes from those harnesses. The small ``SMOKE`` set below
is a connectivity/plumbing check — NOT a benchmark.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

# A connectivity SMOKE set (NOT a benchmark — see the module docstring). Just enough to prove
# the consumer talks to the daemon and the A/B + scoring + cost plumbing works end to end.
SMOKE = [
    ("What is 2+2? Reply with only the number.", "4"),
    ("What is the capital of France? Reply with one word.", "paris"),
    ("Is the sky blue on a clear day? Reply yes or no.", "yes"),
]


@dataclass
class Result:
    label: str
    correct: int
    total: int
    output_tokens: int
    cost_usd: float
    seconds: float

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    @property
    def cost_per_correct(self) -> float:
        return self.cost_usd / self.correct if self.correct else float("inf")


def _ask(base_url: str, question: str, model: str | None, max_tokens: int = 256) -> str:
    body = {"max_tokens": max_tokens, "messages": [{"role": "user", "content": question}]}
    if model:
        body["model"] = model
    r = httpx.post(f"{base_url}/v1/messages", json=body, timeout=120)
    r.raise_for_status()
    data = r.json()
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


def _ledger(base_url: str) -> dict:
    try:
        return httpx.get(f"{base_url}/health", timeout=5).json().get("ledger", {})
    except Exception:
        return {}


def run_config(base_url: str, label: str, items: list[tuple[str, str]],
               model: str | None = None) -> Result:
    """Run a set of (question, expected) items through the daemon; score by substring match.

    Cost is read from the daemon's ledger delta (LiteLLM's bundled price map — no network).
    """
    before = _ledger(base_url)
    started = time.monotonic()
    correct = 0
    for question, expected in items:
        try:
            text = _ask(base_url, question, model)
        except Exception:
            text = ""
        if expected.lower() in text.lower():
            correct += 1
    after = _ledger(base_url)
    return Result(
        label=label, correct=correct, total=len(items),
        output_tokens=after.get("total_tokens", 0) - before.get("total_tokens", 0),
        cost_usd=round(after.get("total_cost_usd", 0.0) - before.get("total_cost_usd", 0.0), 6),
        seconds=round(time.monotonic() - started, 1),
    )


def format_table(results: list[Result]) -> str:
    """Accuracy AND cost, side by side (never a score without its cost)."""
    rows = ["", f"{'config':16} {'accuracy':>10} {'correct':>9} {'tokens':>9} {'cost($)':>10} "
                f"{'$/correct':>11} {'secs':>7}", "-" * 76]
    for r in results:
        cpc = "—" if r.cost_per_correct == float("inf") else f"{r.cost_per_correct:.6f}"
        rows.append(f"{r.label:16} {r.accuracy:>9.0%} {r.correct:>4}/{r.total:<4} "
                    f"{r.output_tokens:>9} {r.cost_usd:>10.6f} {cpc:>11} {r.seconds:>7}")
    return "\n".join(rows)
