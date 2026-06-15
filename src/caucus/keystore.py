"""Local key store — keys persist locally, set once.

The guarantee is **local-only**: a key is stored on the user's machine, under the user's
control, and never transmitted off-machine except to the chosen provider (LiteLLM sends it
straight to that provider), never logged, never written to ``config.toml``, never returned to
the browser. Not "never stored" — stored locally.

Two backends:

* **env file** (the default): keys persist to ``~/.config/caucus/.env`` as standard dotenv
  lines (``DEEPSEEK_API_KEY=sk-...``), keyed by the provider's environment-variable name. The
  file is ``chmod 600`` inside a ``chmod 700`` directory and written atomically (temp + replace)
  so it is never even briefly world-readable. Plaintext-but-permission-restricted, exactly as
  the spec sanctions — and directly consumable by anything that loads a ``.env``.
* **keyring** (OS keychain — macOS Keychain / Windows Credential Manager / Secret Service):
  opt-in, encrypted at rest. Selected with ``CAUCUS_KEYSTORE=keyring`` and only iff ``keyring``
  is importable (``pip install keyring``).

Only a non-reversible **fingerprint** of a key is ever surfaced (for recognition); the key
itself is returned solely to the routing layer to hand to the provider.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional, Protocol

from .config import config_path
from .log import key_fingerprint

# Provider → environment variable LiteLLM reads. Generic rule is ``{PROVIDER}_API_KEY``;
# a few providers differ. Ollama (local) needs no key.
_ENV_OVERRIDES = {
    "gemini": "GEMINI_API_KEY",
    "vertex_ai": "VERTEX_PROJECT",
    "azure": "AZURE_API_KEY",
}
_NO_KEY_PROVIDERS = {"ollama", "caucus-mock"}

# Reverse map for env-var → provider, derived from the overrides above.
_PROVIDER_FOR_ENV = {env: provider for provider, env in _ENV_OVERRIDES.items()}


def env_var_for(provider: str) -> Optional[str]:
    p = provider.lower()
    if p in _NO_KEY_PROVIDERS:
        return None
    return _ENV_OVERRIDES.get(p, f"{p.upper()}_API_KEY")


def _provider_for_env(env_var: str) -> Optional[str]:
    """Reverse-map a dotenv var name back to a provider name.

    Honours the explicit overrides (``GEMINI_API_KEY`` → ``gemini``); otherwise, for a generic
    ``<PREFIX>_API_KEY`` var, derives the provider as the lower-cased prefix. Anything that does
    not look like an API-key var is ignored (returns ``None``).
    """
    if env_var in _PROVIDER_FOR_ENV:
        return _PROVIDER_FOR_ENV[env_var]
    if env_var.endswith("_API_KEY"):
        prefix = env_var[: -len("_API_KEY")]
        if prefix:
            return prefix.lower()
    return None


class KeyStore(Protocol):
    backend: str

    def set(self, provider: str, key: str) -> None: ...
    def get(self, provider: str) -> Optional[str]: ...
    def delete(self, provider: str) -> bool: ...
    def providers(self) -> list[str]: ...


def _parse_dotenv(text: str) -> dict[str, str]:
    """Parse standard dotenv lines into a ``{VAR: value}`` dict.

    Tolerant of blank lines, ``#`` comments, an optional ``export`` prefix, and single/double
    quoted values. Lines without ``=`` are skipped.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if not name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[name] = value
    return out


def _format_dotenv(data: dict[str, str]) -> str:
    """Serialize ``{VAR: value}`` to dotenv lines, deterministically ordered."""
    return "".join(f"{name}={data[name]}\n" for name in sorted(data))


class EnvFileKeyStore:
    """chmod-600 dotenv file (``~/.config/caucus/.env``) — the default store.

    Keys are stored by their provider env-var name (``DEEPSEEK_API_KEY=sk-...``), one per line,
    so the file is a standard ``.env`` and holds many providers at once.
    """

    backend = "env"

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or (config_path().parent / ".env")

    def _read(self) -> dict[str, str]:
        if not self.path.is_file():
            return {}
        try:
            return _parse_dotenv(self.path.read_text())
        except Exception:
            return {}

    def _write(self, data: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        # Write via a 600 temp file then replace, so the key is never briefly world-readable.
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, _format_dotenv(data).encode())
        finally:
            os.close(fd)
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    def set(self, provider: str, key: str) -> None:
        env = env_var_for(provider)
        if not env:
            # Keyless providers (ollama, caucus-mock) have nothing to store.
            return
        data = self._read()
        # Strip CR/LF so a malformed paste can't inject an extra dotenv line into the .env file.
        data[env] = key.replace("\r", "").replace("\n", "")
        self._write(data)

    def get(self, provider: str) -> Optional[str]:
        env = env_var_for(provider)
        if not env:
            return None
        return self._read().get(env)

    def delete(self, provider: str) -> bool:
        env = env_var_for(provider)
        if not env:
            return False
        data = self._read()
        if env in data:
            del data[env]
            self._write(data)
            return True
        return False

    def providers(self) -> list[str]:
        out = []
        for env_var in self._read():
            provider = _provider_for_env(env_var)
            if provider:
                out.append(provider)
        return sorted(set(out))


class KeyringKeyStore:
    """OS keychain backend (encrypted at rest). Opt-in via ``CAUCUS_KEYSTORE=keyring``."""

    backend = "keyring"
    _SERVICE = "caucus"
    _INDEX = "__providers__"

    def __init__(self, keyring_mod) -> None:
        self._kr = keyring_mod

    def _index(self) -> list[str]:
        raw = self._kr.get_password(self._SERVICE, self._INDEX)
        return json.loads(raw) if raw else []

    def _set_index(self, providers: list[str]) -> None:
        self._kr.set_password(self._SERVICE, self._INDEX, json.dumps(sorted(set(providers))))

    def set(self, provider: str, key: str) -> None:
        self._kr.set_password(self._SERVICE, provider.lower(), key)
        self._set_index(self._index() + [provider.lower()])

    def get(self, provider: str) -> Optional[str]:
        return self._kr.get_password(self._SERVICE, provider.lower())

    def delete(self, provider: str) -> bool:
        if provider.lower() not in self._index():
            return False
        try:
            self._kr.delete_password(self._SERVICE, provider.lower())
        except Exception:
            pass
        self._set_index([p for p in self._index() if p != provider.lower()])
        return True

    def providers(self) -> list[str]:
        return sorted(self._index())


def get_keystore() -> KeyStore:
    """The chmod-600 ``.env`` file by default; the OS keychain only when opted into.

    Set ``CAUCUS_KEYSTORE=keyring`` to use the OS keychain (requires the ``keyring`` extra and a
    usable backend). Any other value — or the default — uses the dotenv file store.
    """
    if os.environ.get("CAUCUS_KEYSTORE", "").lower() == "keyring":
        try:
            import keyring  # optional extra

            backend = keyring.get_keyring()
            # A usable backend — not the "fail" placeholder keyring installs when none exists.
            if backend and "fail" not in type(backend).__name__.lower():
                return KeyringKeyStore(keyring)
        except Exception:
            pass
    return EnvFileKeyStore()


def fingerprint(store: KeyStore, provider: str) -> str:
    return key_fingerprint(store.get(provider))


def inject_env(store: KeyStore) -> list[str]:
    """Load stored keys into this process's env so LiteLLM/engine calls find them.

    In-process only — keys reach the daemon's environment, never disk (beyond the secured
    store) and never a log. v1.1 sandbox subprocesses must scrub these.
    Returns the list of providers injected (names only, never values).
    """
    injected = []
    for provider in store.providers():
        env = env_var_for(provider)
        if not env:
            continue
        key = store.get(provider)
        if key and not os.environ.get(env):
            os.environ[env] = key
            injected.append(provider)
    return injected
