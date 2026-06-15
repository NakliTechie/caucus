"""Combos — the user-chosen panel + judge + selection strategy.

A combo is defined by **role + slug** so it doesn't rot: a list of *panel* member models, a
*judge*/synthesizer model, and a *strategy* (v1.0: pass-through action turns; v1.1:
sandbox-and-test). The agent never picks the panel — it sends a vanilla request; the combo is
chosen Caucus-side (config) or per-session by the alias the agent targets
(`caucus-quality` / `-budget` / `-local` / `-balanced`).

Combos live in `config.toml` (definitions only — never keys). The four shipped combos are
editable defaults.
"""

from __future__ import annotations

from dataclasses import dataclass, field

STRATEGY_PASSTHROUGH = "passthrough"  # v1.0 action turns
STRATEGY_SANDBOX = "sandbox-and-test"  # v1.1 action turns


@dataclass
class Combo:
    name: str
    panel: list[str] = field(default_factory=list)
    judge: str = ""
    strategy: str = STRATEGY_PASSTHROUGH

    @property
    def primary(self) -> str:
        """The single model action turns pass through to — the judge (the combo's strongest)."""
        return self.judge or (self.panel[0] if self.panel else "")

    def to_toml_table(self) -> dict:
        return {"panel": self.panel, "judge": self.judge, "strategy": self.strategy}


# Shipped, editable defaults. Slugs are sensible BYOK/Ollama choices; users edit freely.
DEFAULT_COMBOS: dict[str, Combo] = {
    "quality": Combo(
        "quality",
        panel=["openai/gpt-4o", "anthropic/claude-3-5-sonnet-latest",
               "openrouter/google/gemini-2.0-flash-001"],
        judge="anthropic/claude-3-5-sonnet-latest",
    ),
    "budget": Combo(
        "budget",
        panel=["openai/gpt-4o-mini", "openrouter/meta-llama/llama-3.1-8b-instruct"],
        judge="openai/gpt-4o-mini",
    ),
    "local": Combo(
        "local",
        panel=["ollama/llama3.2:1b", "ollama/qwen2.5:0.5b"],
        judge="ollama/llama3.2:1b",
    ),
    "balanced": Combo(
        "balanced",
        panel=["ollama/llama3.2:1b", "ollama/qwen2.5:0.5b"],
        judge="anthropic/claude-3-5-sonnet-latest",
    ),
}

# Aliases the agent can target to switch combos per-session without editing config.
ALIASES: dict[str, str] = {
    "caucus-quality": "quality",
    "caucus-budget": "budget",
    "caucus-local": "local",
    "caucus-balanced": "balanced",
}


def normalize(name: str) -> str:
    """Map an alias (caucus-quality) to its combo name (quality); pass others through."""
    return ALIASES.get(name, name)


def resolve(requested: str, active: str, combos: dict[str, Combo]) -> Combo | None:
    """Pick the combo for a request.

    Precedence: the alias/combo the *agent* targeted (per-session) > the configured *active*
    combo/model > a single-model ad-hoc combo from whichever slug we have. Returns None when no
    provider is configured at all (the empty-state).
    """
    req = normalize(requested or "")
    if req in combos:
        return combos[req]
    act = normalize(active or "")
    if act in combos:
        return combos[act]
    slug = active or requested
    if not slug:
        return None
    return Combo(slug, panel=[slug], judge=slug)  # single-model: panel == judge
