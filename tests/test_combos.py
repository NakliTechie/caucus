"""Combos: resolution + alias selection + config.toml round-trip (never keys) + ledger."""

import tomllib

from caucus.combos import DEFAULT_COMBOS, Combo, normalize, resolve
from caucus.config import CaucusConfig, dump_toml, load_combos, save_config
from caucus.ledger import Ledger, TurnRecord, turn_cost


def test_default_combos_present():
    assert set(DEFAULT_COMBOS) == {"quality", "budget", "local", "balanced"}
    # Balanced is genuinely heterogeneous: local panel, cloud judge.
    bal = DEFAULT_COMBOS["balanced"]
    assert all(m.startswith("ollama/") for m in bal.panel)
    assert bal.judge.startswith("anthropic/")


def test_resolve_precedence():
    combos = {n: c for n, c in DEFAULT_COMBOS.items()}
    # agent targets an alias → that combo wins
    assert resolve("caucus-budget", "quality", combos).name == "budget"
    # else the configured active
    assert resolve("claude-3-5-sonnet", "caucus-local", combos).name == "local"
    # else a single-model ad-hoc combo (panel == judge)
    c = resolve("", "ollama/llama3.2:1b", combos)
    assert c.panel == ["ollama/llama3.2:1b"] and c.judge == "ollama/llama3.2:1b"
    # nothing configured → no provider
    assert resolve("", "", combos) is None


def test_combo_primary_is_judge():
    assert DEFAULT_COMBOS["balanced"].primary == DEFAULT_COMBOS["balanced"].judge


def test_config_toml_roundtrip_has_no_keys(tmp_path):
    cfg = CaucusConfig(bind="127.0.0.1", port=8787, model="caucus-quality",
                       fallback_model="anthropic/claude-3-5-sonnet-latest",
                       auth_token="should-not-be-written")
    path = save_config(cfg, DEFAULT_COMBOS, tmp_path / "config.toml")
    raw = tomllib.loads(path.read_text())
    assert raw["server"]["model"] == "caucus-quality"
    assert raw["combos"]["balanced"]["judge"].startswith("anthropic/")
    # workspace + test_command survive the save→load round-trip (regression: they were dropped)
    cfg2 = CaucusConfig(model="x", workspace="/tmp/repo", test_command="pytest -q")
    p2 = save_config(cfg2, DEFAULT_COMBOS, tmp_path / "c2.toml")
    from caucus.config import load_config
    import os
    os.environ["CAUCUS_CONFIG"] = str(p2)
    try:
        loaded = load_config()
        assert loaded.workspace == "/tmp/repo" and loaded.test_command == "pytest -q"
    finally:
        os.environ.pop("CAUCUS_CONFIG", None)
    # §5: no secrets in config.toml — not even the exposure token.
    assert "should-not-be-written" not in path.read_text()
    assert "auth_token" not in raw["server"]


def test_load_combos_overlays_file(tmp_path):
    cfg = CaucusConfig(model="local")
    edited = {n: Combo(c.name, list(c.panel), c.judge, c.strategy) for n, c in DEFAULT_COMBOS.items()}
    edited["quality"] = Combo("quality", panel=["openai/gpt-4o"], judge="openai/gpt-4o")
    p = save_config(cfg, edited, tmp_path / "config.toml")
    loaded = load_combos(p)
    assert loaded["quality"].panel == ["openai/gpt-4o"]
    assert "balanced" in loaded  # defaults still present


def test_dump_toml_types():
    out = dump_toml({"server": {"port": 8787, "model": "x", "expose": False},
                     "combos.q": {"panel": ["a", "b"], "judge": "c"}})
    raw = tomllib.loads(out)
    assert raw["server"]["port"] == 8787 and raw["server"]["expose"] is False
    assert raw["combos"]["q"]["panel"] == ["a", "b"]


def test_ledger_records_and_summarises():
    led = Ledger()
    led.record(TurnRecord("r1", "local", "plan", "synth", "ollama/llama3.2:1b", 120, 0.0, 15))
    led.record(TurnRecord("r2", "local", "action", "passthrough", "ollama/llama3.2:1b", 30, 0.0, 2))
    s = led.summary()
    assert s["turns"] == 2 and s["total_tokens"] == 150
    assert led.recent()[0]["rid"] == "r2"  # newest first


def test_turn_cost_unknown_model_is_zero():
    assert turn_cost("caucus-mock/echo", 100) == 0.0
