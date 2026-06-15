"""The ``caucus`` CLI — the headless human surface.

Ships ``start``, ``status``, ``stop``, ``config``, ``combo``, and ``logs``. The proxy
endpoint remains the agent's API; this is for humans.
"""

from __future__ import annotations

import json
import sys

import click

from . import __version__
from .config import MOCK_MODEL, ConfigError, load_config
from .engine import engine_status


@click.group(help="Caucus — a local, sovereign turn-aware synth proxy.")
@click.version_option(__version__, prog_name="caucus")
def main() -> None:  # pragma: no cover - thin click entry
    pass


@main.command(help="Start the Caucus daemon (front door + console, in-process).")
@click.option("--bind", default=None, help="Bind address (default 127.0.0.1).")
@click.option("--port", default=None, type=int, help="Port (default 8787).")
@click.option("--model", default=None, help="Active model/combo, e.g. ollama/llama3.2:1b.")
@click.option("--fallback", default=None, help="Cloud fallback for the availability honesty rule.")
@click.option("--workspace", default=None, help="Repo to sandbox-and-test candidates against (v1.1).")
@click.option("--test-command", default=None, help="The repo's test command (v1.1).")
@click.option("--expose", is_flag=True, default=False, help="Opt in to a non-local bind.")
@click.option("--auth-token", default=None, help="Required token when exposed.")
@click.option("--log-file", default=None, help="Also write redacted logs here (for `caucus logs`).")
@click.option("--mock", is_flag=True, default=False,
              help=f"Use the keyless mock provider ({MOCK_MODEL}) for a handshake with no key.")
@click.option("--debug", is_flag=True, default=False,
              help="Local exploration: surface LiteLLM request/response logs + the MOA "
                   "pipeline trace. NOT for an exposed daemon (debug logs carry content previews).")
def start(bind, port, model, fallback, workspace, test_command, expose, auth_token, log_file, mock, debug) -> None:
    import os

    import uvicorn

    if debug and expose:
        raise click.ClickException(
            "Refusing to start: --debug streams request/response content previews into the Logs "
            "tab, which must never be served on an exposed daemon. Drop --debug, or drop --expose.")

    if debug:
        os.environ["CAUCUS_DEBUG"] = "1"
        os.environ["CAUCUS_LOG_LEVEL"] = "DEBUG"

    from .server import create_app

    if log_file:
        os.environ["CAUCUS_LOG_FILE"] = log_file
    if mock and not model:
        model = MOCK_MODEL
    overrides = {"bind": bind, "port": port, "model": model, "fallback_model": fallback,
                 "workspace": workspace, "test_command": test_command,
                 "expose": expose or None, "auth_token": auth_token}
    try:
        cfg = load_config({k: v for k, v in overrides.items() if v is not None})
        cfg.validate()
        app = create_app(cfg)
    except ConfigError as exc:
        click.echo(f"caucus: {exc}", err=True)
        sys.exit(2)

    if debug:
        import logging

        from . import providers
        providers.set_debug(True)
        logging.getLogger("caucus").setLevel(logging.DEBUG)

    _write_pid(cfg.port)
    click.echo(f"caucus {__version__} → {cfg.base_url}  (model: {cfg.model or 'agent-supplied'})")
    click.echo(f"  point your agent at:  ANTHROPIC_BASE_URL={cfg.base_url}")
    click.echo(f"  console:              {cfg.base_url}/")
    if debug:
        click.echo("  debug:                ON — LiteLLM logs + the synth (MOA) trace are live "
                   "(local only; don't expose this daemon)")
    try:
        uvicorn.run(app, host=cfg.bind, port=cfg.port, log_level="info" if debug else "warning")
    finally:
        _pid_path(cfg.port).unlink(missing_ok=True)


def _pid_path(port: int):
    from .config import config_path

    return config_path().parent / f"caucus-{port}.pid"


def _write_pid(port: int) -> None:
    import os

    path = _pid_path(port)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():     # never follow a planted symlink — write a real file in our own dir
        path.unlink()
    path.write_text(str(os.getpid()))


@main.command(help="Stop a running Caucus daemon (by port).")
@click.option("--port", default=None, type=int, help="Port of the daemon to stop.")
def stop(port) -> None:
    import os
    import signal

    cfg = load_config({"port": port} if port else {})
    path = _pid_path(cfg.port)
    if not path.is_file():
        click.echo(f"no running daemon found for port {cfg.port}")
        return
    try:
        os.kill(int(path.read_text().strip()), signal.SIGTERM)
        click.echo(f"sent SIGTERM to caucus on port {cfg.port}")
    except ProcessLookupError:
        click.echo("daemon not running (stale pid file removed)")
    path.unlink(missing_ok=True)


@main.command(help="Tail the daemon log file (set via --log-file / CAUCUS_LOG_FILE).")
@click.option("-n", "lines", default=40, help="Number of trailing lines.")
def logs(lines) -> None:
    import os

    log_file = os.environ.get("CAUCUS_LOG_FILE")
    if not log_file or not os.path.isfile(log_file):
        click.echo("No log file. Start with `caucus start --log-file <path>` (logs also go to stderr).")
        return
    with open(log_file) as fh:
        for line in fh.readlines()[-lines:]:
            click.echo(line.rstrip())


@main.command(help="Run benchmarks (DRACO + curated probe) through the daemon vs a baseline → HTML.")
@click.option("--bind", default=None, help="Daemon bind address.")
@click.option("--port", default=None, type=int, help="Daemon port.")
@click.option("--draco/--no-draco", default=True, help="Run the DRACO deep-research benchmark.")
@click.option("--draco-n", default=8, type=int, help="Number of DRACO tasks (domain-balanced subset).")
@click.option("--domains", default=None, help="Comma-separated DRACO domains to restrict to.")
@click.option("--probe/--no-probe", default=True, help="Also run the quick curated reasoning probe.")
@click.option("--baseline", "baselines", multiple=True,
              help="Baseline model(s), a single completion each. Repeatable.")
@click.option("--caucus", "caucus_alias", default="deepseek", help="Caucus combo alias to test.")
@click.option("--judge", "judge_model", default="deepseek/deepseek-v4-pro", help="LLM judge for DRACO.")
@click.option("--html", "html_path", default=None, help="Write the HTML report here (default ./caucus-bench.html).")
@click.option("--open/--no-open", "open_html", default=True, help="Open the HTML report when done.")
@click.option("--smoke", is_flag=True, default=False, help="Just the connectivity smoke set (no key needed).")
def bench(bind, port, draco, draco_n, domains, probe, baselines, caucus_alias, judge_model,
          html_path, open_html, smoke) -> None:
    import datetime
    import os
    import webbrowser

    import httpx

    cfg = load_config({k: v for k, v in {"bind": bind, "port": port}.items() if v is not None})
    base_url = cfg.base_url
    try:
        httpx.get(f"{base_url}/health", timeout=3)
    except Exception:
        click.echo(f"caucus: no daemon at {base_url} — start one first (`caucus start`).", err=True)
        sys.exit(2)

    if smoke:  # keyless connectivity check
        from .benchmarks.bench import SMOKE, format_table, run_config
        results = [run_config(base_url, "caucus", SMOKE)]
        click.echo(format_table(results))
        return

    from .benchmarks.run import run

    base_list = list(baselines) or ["deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-pro"]
    html_path = html_path or os.path.abspath("caucus-bench.html")
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    dom = [d.strip() for d in domains.split(",")] if domains else None

    click.echo(f"caucus bench → {base_url}  (caucus={caucus_alias}, baselines={base_list}, "
               f"draco_n={draco_n if draco else 0}, judge={judge_model})")
    summary = run(base_url, do_probe=probe, do_draco=draco, draco_n=draco_n,
                  baselines=base_list, caucus_alias=caucus_alias, judge_model=judge_model,
                  domains=dom, html_path=html_path, title="Caucus benchmark — DeepSeek V4",
                  generated_at=now, log=lambda m: click.echo("  " + m))

    if summary.get("draco"):
        click.echo("\nDRACO normalized scores:")
        for c in summary["draco"]["configs"]:
            click.echo(f"  {c['label']:18s} {c['normalized']:5.1f}%  (pass {c['pass_rate']:.1f}%)  "
                       f"gen ${c['gen_cost_usd']:.4f}")
    click.echo(f"\nHTML report: {html_path}")
    if open_html:
        webbrowser.open(f"file://{html_path}")


@main.group(help="List and switch combos (panel + judge + strategy).")
def combo() -> None:  # pragma: no cover - thin click group
    pass


@combo.command("list", help="List combos and the active one.")
def combo_list() -> None:
    from .combos import ALIASES, resolve
    from .config import load_combos

    cfg = load_config()
    combos = load_combos()
    alias_for = {n: a for a, n in ALIASES.items()}
    active = resolve("", cfg.model, combos)
    for name, c in combos.items():
        mark = "*" if active and active.name == name else " "
        alias = f" ({alias_for[name]})" if name in alias_for else ""
        click.echo(f" {mark} {name}{alias}: panel={', '.join(c.panel)} | judge={c.judge}")


@combo.command("use", help="Set the active combo (writes config.toml; never keys).")
@click.argument("name")
def combo_use(name) -> None:
    from .config import load_combos, save_config

    cfg = load_config()
    combos = load_combos()
    cfg.model = name
    save_config(cfg, combos)
    click.echo(f"active combo set to {name}")


@main.command(help="Show daemon + engine + provider status.")
@click.option("--bind", default=None, help="Bind address to query.")
@click.option("--port", default=None, type=int, help="Port to query.")
def status(bind, port) -> None:
    import httpx

    from .keystore import fingerprint, get_keystore

    cfg = load_config({k: v for k, v in {"bind": bind, "port": port}.items() if v is not None})
    store = get_keystore()
    info: dict = {
        "configured_bind": f"{cfg.bind}:{cfg.port}",
        "config_source": cfg.source,
        "model": cfg.model or "(agent-supplied)",
        "fallback_model": cfg.fallback_model or "(none)",
        "keystore_backend": store.backend,
        "keys": {p: fingerprint(store, p) for p in store.providers()},
        "engine": engine_status(),
    }
    try:
        resp = httpx.get(f"{cfg.base_url}/health", timeout=2.0)
        info["daemon"] = "running"
        info["health"] = resp.json()
    except Exception:
        info["daemon"] = "not running"
    click.echo(json.dumps(info, indent=2))


@main.group(help="Manage config and provider keys (keys live in a secured local store).")
def config() -> None:  # pragma: no cover - thin click group
    pass


@config.command("set-key", help="Store a BYOK provider key locally (set once). e.g. openai, anthropic, openrouter.")
@click.argument("provider")
@click.option("--key", default=None, help="The key (omit to be prompted without echo).")
def set_key(provider, key) -> None:
    from .keystore import fingerprint, get_keystore

    if not key:
        key = click.prompt(f"{provider} API key", hide_input=True)
    store = get_keystore()
    store.set(provider, key)
    # Never echo the key — only its fingerprint.
    click.echo(f"stored {provider} key in {store.backend} store · {fingerprint(store, provider)}")


@config.command("keys", help="List stored providers and key fingerprints (never the keys).")
def keys() -> None:
    from .keystore import fingerprint, get_keystore

    store = get_keystore()
    providers = store.providers()
    if not providers:
        click.echo(f"no keys stored ({store.backend} store). Add one: caucus config set-key openai")
        return
    for p in providers:
        click.echo(f"  {p:14s} {fingerprint(store, p)}")


@config.command("clear-key", help="Forget a stored provider key.")
@click.argument("provider")
def clear_key(provider) -> None:
    from .keystore import get_keystore

    store = get_keystore()
    click.echo(f"cleared {provider}" if store.delete(provider) else f"no stored key for {provider}")


if __name__ == "__main__":  # pragma: no cover
    main()
