"""Availability-fallback routing policy.

Panel members and pass-through targets are **L1 local** (Ollama, reached directly) or
**C1 BYOK cloud** — LiteLLM is agnostic to which. Two escalation rules:

* **Availability** (everywhere): if a provider call errors, escalate to the cloud fallback.
  Implemented at the provider-call level (``providers.LiteLLMClient``) so it covers every
  engine panel/critique/synthesis call, and at the pass-through call.
* **Honesty rule** (deliberation turns): if the local model can't clear the *planning-quality
  floor*, escalate the whole synth to the cloud fallback. ``meets_floor`` is a
  v1.0 heuristic; a learned floor is v3 roadmap.

The C2 keyless relay rung is **not** v1 — escalation only ever targets a model the user
configured with their own key (BYOK).
"""

from __future__ import annotations

MIN_FLOOR_CHARS = 24
_REFUSAL_PREFIXES = (
    "i cannot", "i can't help", "i am unable", "i'm unable", "as an ai",
    "error:", "i do not have", "i don't have enough",
)


def ladder(primary: str, fallback: str = "") -> list[str]:
    """Ordered, de-duplicated escalation ladder: [primary, cloud-fallback]."""
    out: list[str] = []
    for model in (primary, fallback):
        if model and model not in out:
            out.append(model)
    return out


def meets_floor(text: str) -> bool:
    """The planning-quality floor (honesty rule). Conservative: only obvious misses fail."""
    if not text:
        return False
    stripped = text.strip()
    if len(stripped) < MIN_FLOOR_CHARS:
        return False
    low = stripped.lower()
    if any(low.startswith(prefix) for prefix in _REFUSAL_PREFIXES):
        return False
    return True
