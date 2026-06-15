"""Caucus configuration.

Config lives at ``~/.config/caucus/config.toml`` (combo definitions + bind/port —
**never secrets**). Environment variables overlay the file so a
keyless smoke run / CI needs no file at all. Keys are handled by the credential
store, not here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # py3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - we pin >=3.10 but be safe
    tomllib = None  # type: ignore

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8787

# The explicit, labelled mock provider: never *silently* fall back to
# nothing — the mock is opt-in and clearly not real inference.
MOCK_PROVIDER = "caucus-mock"
MOCK_MODEL = "caucus-mock/echo"

LOCAL_BINDS = {"127.0.0.1", "localhost", "::1"}


class ConfigError(RuntimeError):
    """Raised for a configuration the daemon must refuse to start with."""


def config_path() -> Path:
    """Resolve the config file path (``CAUCUS_CONFIG`` > ``XDG_CONFIG_HOME`` > ~/.config)."""
    override = os.environ.get("CAUCUS_CONFIG")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    root = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return root / "caucus" / "config.toml"


@dataclass
class CaucusConfig:
    """Resolved runtime configuration. No secrets ever live here."""

    bind: str = DEFAULT_BIND
    port: int = DEFAULT_PORT
    # Active primary model — empty string means "no provider configured yet".
    model: str = ""
    # Cloud fallback (availability honesty rule): escalate here if the primary errors or
    # a deliberation misses the planning-quality floor. Empty = no escalation.
    fallback_model: str = ""
    # v1.1 sandbox-and-test: the repo to test candidates against + its test command.
    # Both empty → action-turn selection degrades to pass-through.
    workspace: str = ""
    test_command: str = ""
    # Network exposure is opt-in: non-local bind requires --expose AND a token.
    expose: bool = False
    auth_token: str = ""
    source: str = "defaults"

    @property
    def is_local_bind(self) -> bool:
        return self.bind in LOCAL_BINDS

    @property
    def base_url(self) -> str:
        host = self.bind if ":" not in self.bind else f"[{self.bind}]"
        return f"http://{host}:{self.port}"

    def validate(self) -> None:
        """Enforce the bind-safety hard rule. Refuse unsafe exposure."""
        if not self.is_local_bind:
            if not self.expose:
                raise ConfigError(
                    f"Refusing to bind to non-local address {self.bind!r}. "
                    "Network exposure is opt-in: pass --expose explicitly."
                )
            if not self.auth_token:
                raise ConfigError(
                    f"Refusing to expose {self.bind}:{self.port} without an auth token. "
                    "Set CAUCUS_AUTH_TOKEN (or --auth-token) before exposing."
                )
        # Debug logging streams request/response content previews into the Logs tab — never serve
        # that on an exposed daemon. Caught here (not just in the CLI) so a stray CAUCUS_DEBUG env
        # var can't slip past with --expose.
        if self.expose and os.environ.get("CAUCUS_DEBUG"):
            raise ConfigError(
                "Refusing to start: CAUCUS_DEBUG streams content previews, which must never be "
                "served on an exposed daemon. Unset CAUCUS_DEBUG, or drop --expose."
            )


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


# --- Minimal TOML writer (stdlib tomllib is read-only) --------------------------------
# Scoped to our config schema: str / int / bool / list[str], flat + dotted tables.

def _toml_value(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def dump_toml(tables: dict[str, dict]) -> str:
    """Serialize {table_path: {key: value}} to TOML. Table paths may be dotted (combos.quality)."""
    out: list[str] = []
    for table, kv in tables.items():
        out.append(f"[{table}]")
        for key, value in kv.items():
            out.append(f"{key} = {_toml_value(value)}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def load_combos(path: Optional[Path] = None):
    """Default combos overlaid with any defined/edited in config.toml ([combos.<name>])."""
    from .combos import DEFAULT_COMBOS, Combo

    combos = {n: Combo(c.name, list(c.panel), c.judge, c.strategy) for n, c in DEFAULT_COMBOS.items()}
    path = path or config_path()
    if path.is_file() and tomllib is not None:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
        for name, tbl in (raw.get("combos") or {}).items():
            if isinstance(tbl, dict):
                combos[name] = Combo(name, list(tbl.get("panel", [])),
                                     str(tbl.get("judge", "")), str(tbl.get("strategy", "passthrough")))
    return combos


def save_config(cfg: "CaucusConfig", combos: dict, path: Optional[Path] = None) -> Path:
    """Write server settings + combo definitions to config.toml (never keys)."""
    path = Path(path) if path else config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tables: dict[str, dict] = {
        "server": {"bind": cfg.bind, "port": cfg.port, "model": cfg.model,
                   "fallback_model": cfg.fallback_model, "workspace": cfg.workspace,
                   "test_command": cfg.test_command},
    }
    for name, combo in combos.items():
        tables[f"combos.{name}"] = {"panel": list(combo.panel), "judge": combo.judge,
                                    "strategy": combo.strategy}
    path.write_text(dump_toml(tables))
    try:
        os.chmod(path, 0o600)  # no secrets, but it reveals the user's workspace path + test command
    except OSError:
        pass
    return path


def load_config(overrides: dict | None = None) -> CaucusConfig:
    """Load config from file (if present), then overlay env vars, then explicit overrides."""
    data: dict = {}
    source = "defaults"

    path = config_path()
    if path.is_file() and tomllib is not None:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
        server = raw.get("server", {}) if isinstance(raw, dict) else {}
        for key in ("bind", "port", "model", "fallback_model", "workspace", "test_command",
                    "expose", "auth_token"):
            if key in server:
                data[key] = server[key]
        source = str(path)

    # Environment overlay — keyless CI / smoke runs configure entirely from env.
    env_map = {
        "bind": "CAUCUS_BIND",
        "port": "CAUCUS_PORT",
        "model": "CAUCUS_MODEL",
        "fallback_model": "CAUCUS_FALLBACK_MODEL",
        "workspace": "CAUCUS_WORKSPACE",
        "test_command": "CAUCUS_TEST_COMMAND",
        "expose": "CAUCUS_EXPOSE",
        "auth_token": "CAUCUS_AUTH_TOKEN",
    }
    for key, env in env_map.items():
        if env in os.environ:
            data[key] = os.environ[env]
            source = "env" if source == "defaults" else source

    if overrides:
        data.update({k: v for k, v in overrides.items() if v is not None})

    cfg = CaucusConfig(
        bind=str(data.get("bind", DEFAULT_BIND)),
        port=int(data.get("port", DEFAULT_PORT)),
        model=str(data.get("model", "")),
        fallback_model=str(data.get("fallback_model", "")),
        workspace=str(data.get("workspace", "")),
        test_command=str(data.get("test_command", "")),
        expose=_coerce_bool(data.get("expose", False)),
        auth_token=str(data.get("auth_token", "")),
        source=source,
    )
    return cfg
