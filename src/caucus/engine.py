"""The synth engine surface — Caucus's own mixture-of-agents / best-of-N (``synth_engine``).

Plan-turn synth lives in ``synth_engine.synthesize`` (called from ``synth``). This module keeps the
action-selection *generation* helpers (v1.1): sample N candidates from a model and rate one,
used by ``selection`` to pick a sandbox-tested survivor. Everything routes through the LiteLLM
client in ``providers`` — no external engine.
"""

from __future__ import annotations

from typing import Optional

from . import __version__, synth_engine
from .providers import make_client


def engine_status() -> dict:
    """Proof the native synth engine is reachable in-process — surfaced by /health + `caucus status`."""
    return {
        "engine": "caucus-synth",
        "version": __version__,
        "in_process": True,
        "approaches": ["moa", "bon"],
        "reachable": callable(getattr(synth_engine, "synthesize", None))
        and callable(getattr(synth_engine, "best_of_n", None)),
    }


def generate_candidates(
    system_prompt: str,
    query: str,
    model: str,
    *,
    n: int = 3,
    fallback: str = "",
    request_config: Optional[dict] = None,
) -> tuple[list[str], int]:
    """Generate N action-turn candidates (best-of-N's *generation* half). Returns (texts, tokens).

    Selection is NOT done here — selection picks by sandbox-test-pass (Caucus's moat), with the synth
    engine's rating as the tiebreak. This exposes the n-sampling so every candidate can be tested.
    """
    client = make_client(fallback or None)
    max_tokens = (request_config or {}).get("max_tokens", 4096)
    messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": query}]
    base = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 1}
    texts: list[str] = []
    tokens = 0
    try:
        resp = client.chat.completions.create(**{**base, "n": n})
        texts = [c.message.content for c in resp.choices if c.message.content is not None]
        tokens += getattr(resp.usage, "completion_tokens", 0) or 0
    except Exception:
        for _ in range(n):  # provider without n support → sample one at a time
            try:
                resp = client.chat.completions.create(**{**base, "n": 1})
                if resp.choices and resp.choices[0].message.content is not None:
                    texts.append(resp.choices[0].message.content)
                    tokens += getattr(resp.usage, "completion_tokens", 0) or 0
            except Exception:
                continue
    return texts, tokens


def generate_candidate_messages(
    system_prompt: str, query: str, model: str, *, n: int = 3, fallback: str = "",
    request_config: Optional[dict] = None,
) -> tuple[list[dict], int]:
    """Like generate_candidates but keeps each candidate's tool_calls (so edits survive).

    Returns (messages, tokens) where each message is {"content": str, "tool_calls": list}.
    """
    client = make_client(fallback or None)
    max_tokens = (request_config or {}).get("max_tokens", 4096)
    messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": query}]
    base = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 1}

    def _extract(resp) -> tuple[list[dict], int]:
        out = []
        for ch in resp.choices:
            msg = ch.message
            tcs = []
            for tc in (getattr(msg, "tool_calls", None) or []):
                fn = getattr(tc, "function", None)
                if fn is not None:
                    tcs.append({"name": fn.name, "arguments": fn.arguments})
            out.append({"content": msg.content or "", "tool_calls": tcs})
        return out, getattr(resp.usage, "completion_tokens", 0) or 0

    try:
        msgs, tokens = _extract(client.chat.completions.create(**{**base, "n": n}))
        return msgs, tokens
    except Exception:
        collected, total = [], 0
        for _ in range(n):
            try:
                m, t = _extract(client.chat.completions.create(**{**base, "n": 1}))
                collected += m
                total += t
            except Exception:
                continue
        return collected, total


def score_candidate(system_prompt: str, query: str, candidate: str, model: str,
                    *, fallback: str = "") -> float:
    """Judge a candidate 0–10 (the synth engine's rating, used as the selection tiebreak)."""
    client = make_client(fallback or None)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": query},
        {"role": "assistant", "content": candidate},
        {"role": "system", "content": "Rate the previous response 0-10 (0 poor, 10 excellent) "
                                       "for relevance, coherence, and helpfulness. Reply with only a number."},
        {"role": "user", "content": "Rate the above response:"},
    ]
    try:
        resp = client.chat.completions.create(model=model, messages=messages, max_tokens=16,
                                              n=1, temperature=0.1)
        return float(resp.choices[0].message.content.strip())
    except Exception:
        return 0.0
