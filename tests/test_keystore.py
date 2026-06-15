"""Local key store: local-only, fingerprint-not-key, 600 perms, env injection.

Default store is a chmod-600 dotenv ``.env`` keyed by provider env-var name
(``DEEPSEEK_API_KEY=sk-...``); these tests exercise that backend.
"""

import os
import stat

from caucus.keystore import EnvFileKeyStore, env_var_for, fingerprint, inject_env


def test_env_keystore_roundtrip(tmp_path):
    store = EnvFileKeyStore(tmp_path / ".env")
    assert store.get("openai") is None
    store.set("openai", "sk-secret-123")
    assert store.get("openai") == "sk-secret-123"
    assert "openai" in store.providers()
    assert store.delete("openai") is True
    assert store.get("openai") is None
    assert store.delete("openai") is False  # idempotent


def test_env_file_is_dotenv_format(tmp_path):
    path = tmp_path / ".env"
    store = EnvFileKeyStore(path)
    store.set("deepseek", "sk-deepseek-xyz")
    text = path.read_text()
    # Standard dotenv line, keyed by the provider's ENV VAR NAME (not the provider name).
    assert "DEEPSEEK_API_KEY=sk-deepseek-xyz" in text
    assert "deepseek=" not in text  # never the bare provider name


def test_env_keystore_holds_many_providers(tmp_path):
    store = EnvFileKeyStore(tmp_path / ".env")
    store.set("openai", "sk-openai")
    store.set("deepseek", "sk-deepseek")
    store.set("anthropic", "sk-anthropic")
    assert store.get("openai") == "sk-openai"
    assert store.get("deepseek") == "sk-deepseek"
    assert store.get("anthropic") == "sk-anthropic"
    assert store.providers() == ["anthropic", "deepseek", "openai"]
    text = (tmp_path / ".env").read_text()
    assert "OPENAI_API_KEY=sk-openai" in text
    assert "DEEPSEEK_API_KEY=sk-deepseek" in text
    assert "ANTHROPIC_API_KEY=sk-anthropic" in text
    # Deleting one leaves the others intact.
    assert store.delete("deepseek") is True
    assert store.providers() == ["anthropic", "openai"]


def test_env_file_is_chmod_600_in_a_700_dir(tmp_path):
    path = tmp_path / "sub" / ".env"
    EnvFileKeyStore(path).set("openai", "sk-x")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700


def test_unknown_api_key_var_reverse_maps_to_provider(tmp_path):
    # A provider with no override uses the generic <PREFIX>_API_KEY rule, and providers()
    # must reverse-map it back: GROQ_API_KEY -> "groq".
    path = tmp_path / ".env"
    store = EnvFileKeyStore(path)
    store.set("groq", "sk-groq")
    assert "GROQ_API_KEY=sk-groq" in path.read_text()
    assert store.providers() == ["groq"]
    assert store.get("groq") == "sk-groq"


def test_fingerprint_is_not_the_key(tmp_path):
    store = EnvFileKeyStore(tmp_path / ".env")
    store.set("openai", "sk-supersecretvalue")
    fp = fingerprint(store, "openai")
    assert "supersecret" not in fp
    assert fp.startswith("sha256:")
    assert fingerprint(store, "absent") == "none"


def test_env_var_mapping():
    assert env_var_for("openai") == "OPENAI_API_KEY"
    assert env_var_for("anthropic") == "ANTHROPIC_API_KEY"
    assert env_var_for("openrouter") == "OPENROUTER_API_KEY"
    assert env_var_for("deepseek") == "DEEPSEEK_API_KEY"
    assert env_var_for("ollama") is None       # local — no key
    assert env_var_for("caucus-mock") is None


def test_inject_env_loads_keys_in_process_only(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    store = EnvFileKeyStore(tmp_path / ".env")
    store.set("openai", "sk-injected")
    injected = inject_env(store)
    assert injected == ["openai"]
    assert os.environ.get("OPENAI_API_KEY") == "sk-injected"
    # inject_env returns names only, never values
    assert "sk-injected" not in injected
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
