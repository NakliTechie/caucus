"""Per-turn cost ledger — status ledger + console cost rollup.

In-memory only — a sovereign daemon keeps no telemetry and writes no usage to disk.
Records carry token counts, an estimated cost (from LiteLLM's *bundled* cost map, no network),
and timing — never any message content.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import asdict, dataclass, field


def turn_cost(model: str, completion_tokens: int, prompt_tokens: int = 0) -> float:
    """Estimate USD cost for a turn from LiteLLM's bundled price map. 0.0 if unknown (e.g. mock)."""
    try:
        import litellm

        prompt_cost, completion_cost = litellm.cost_per_token(
            model=model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
        )
        return float(prompt_cost + completion_cost)
    except Exception:
        return 0.0


@dataclass
class TurnRecord:
    rid: str
    combo: str
    turn: str       # plan | action
    mode: str       # synth | passthrough | synth→passthrough(degraded)
    model: str
    tokens: int
    cost_usd: float
    ms: int
    calls: list = field(default_factory=list)  # per sub-call trace (model/kind/tokens/cost) — content-free metadata


class Ledger:
    def __init__(self, cap: int = 200) -> None:
        self._items: deque[TurnRecord] = deque(maxlen=cap)
        self._lock = threading.Lock()
        self._total_cost = 0.0
        self._total_tokens = 0

    def record(self, rec: TurnRecord) -> None:
        with self._lock:
            self._items.append(rec)
            self._total_cost += rec.cost_usd
            self._total_tokens += rec.tokens

    def recent(self, n: int = 50) -> list[dict]:
        with self._lock:
            return [asdict(r) for r in list(self._items)[-n:]][::-1]  # newest first

    def summary(self) -> dict:
        with self._lock:
            return {
                "turns": len(self._items),
                "total_tokens": self._total_tokens,
                "total_cost_usd": round(self._total_cost, 6),
            }
