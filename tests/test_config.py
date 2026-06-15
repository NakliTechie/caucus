"""Config: env overlay + the bind-safety hard rule (§7.7)."""

import pytest

from caucus.config import CaucusConfig, ConfigError, load_config


def test_local_bind_validates():
    CaucusConfig(bind="127.0.0.1").validate()  # must not raise


def test_nonlocal_bind_requires_expose():
    with pytest.raises(ConfigError):
        CaucusConfig(bind="0.0.0.0").validate()


def test_exposed_bind_requires_auth_token():
    with pytest.raises(ConfigError):
        CaucusConfig(bind="0.0.0.0", expose=True).validate()
    # With a token it is allowed.
    CaucusConfig(bind="0.0.0.0", expose=True, auth_token="secret").validate()


def test_env_overlay(monkeypatch, tmp_path):
    monkeypatch.setenv("CAUCUS_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("CAUCUS_PORT", "9999")
    monkeypatch.setenv("CAUCUS_MODEL", "ollama/llama3.2:1b")
    cfg = load_config()
    assert cfg.port == 9999
    assert cfg.model == "ollama/llama3.2:1b"
    assert cfg.is_local_bind
