"""The Caucus daemon — one process, one codebase.

Serves three of the five surfaces in-process and same-origin:
* the proxy endpoint ``POST /v1/messages`` — the agent's API (the front door);
* ``GET /health`` + the ``/v1/{combos,ledger,keys}`` console API;
* ``GET /`` — the local web console (combo editor + live synth proof + cost ledger).

``handle_messages`` resolves the active combo, classifies the turn, then routes: PLAN → synth
(panel + judge), ACTION → pass-through. Streaming on pass-through is preserved.
"""

from __future__ import annotations

import asyncio
import hmac
import re
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from . import __version__, frontdoor, synth, selection
from .classifier import classify
from .combos import ALIASES, STRATEGY_SANDBOX, Combo, resolve
from .config import CaucusConfig, MOCK_MODEL, load_combos, load_config, save_config
from .engine import engine_status
from .keystore import env_var_for, fingerprint, get_keystore, inject_env
from .ledger import Ledger, TurnRecord, turn_cost
from .log import attach_ring, event, get_logger, ring_clear, ring_tail
from .providers import call_trace, register_mock

log = get_logger("server")
_CONSOLE_DIR = Path(__file__).parent / "console"

# Strict, same-origin-only CSP for the console. Scripts are served files ('self'),
# never inline; the page talks to nothing but this same process.
_CONSOLE_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; "
    "form-action 'self'; frame-ancestors 'none'"
)

# A cheap, representative model per provider for the Settings "test connection" affordance — used
# only when no combo references the provider yet. The test does a 1-token completion: it proves the
# stored key authenticates, nothing more. Keyless providers (ollama/caucus-mock) never need a test.
_TEST_MODELS = {
    "deepseek": "deepseek/deepseek-chat",
    "openai": "openai/gpt-4o-mini",
    "anthropic": "anthropic/claude-3-5-haiku-latest",
    "gemini": "gemini/gemini-2.5-flash",
    "openrouter": "openrouter/openai/gpt-4o-mini",
    "groq": "groq/llama-3.1-8b-instant",
    "mistral": "mistral/mistral-small-latest",
    "nvidia_nim": "nvidia_nim/meta/llama-3.1-8b-instruct",
    "xai": "xai/grok-2-latest",
    "together": "together_ai/meta-llama/Llama-3-8b-chat-hf",
    "fireworks": "fireworks_ai/accounts/fireworks/models/llama-v3p1-8b-instruct",
    "perplexity": "perplexity/llama-3.1-sonar-small-128k-online",
}

_MAX_BODY_BYTES = 10 * 1024 * 1024   # reject request bodies larger than this (resource guard)

# A combo's panel/judge entries are LiteLLM model slugs ("provider/model", or a bare model name
# LiteLLM infers the provider for). Validate them on save so a crafted string can't smuggle a URL
# into LiteLLM's routing (SSRF): letters/digits/_.- with optional "/"-segments and ":" tags
# (ollama/llama3.2:1b), rejecting protocols, spaces, and anything URL-shaped.
_MODEL_RE = re.compile(r"^[A-Za-z0-9_.\-]+(/[A-Za-z0-9_.\-:]+)*$")


def _valid_model(m: object) -> bool:
    return isinstance(m, str) and 0 < len(m) <= 200 and "://" not in m and bool(_MODEL_RE.match(m))


def _no_provider_error() -> JSONResponse:
    # State it plainly, point at the one-line fix. Never silently fall back to nothing.
    return JSONResponse(
        status_code=400,
        content={"type": "error", "error": {"type": "invalid_request_error", "message": (
            "No provider configured. Set a model, e.g. `CAUCUS_MODEL=ollama/llama3.2:1b` "
            f"(local), a BYOK cloud model, a combo (`caucus-quality`), or `{MOCK_MODEL}` "
            "for a keyless mock handshake.")}},
    )


def _authorized(request: Request, cfg: CaucusConfig) -> bool:
    """Auth model: if a token is configured it is required on EVERY request, including a local
    bind — defense in depth, so a local SSH tunnel or local malware reaching 127.0.0.1 must still
    present it. With no token, a local bind is open to the local user; a non-local bind without a
    token is refused at startup (Config.validate)."""
    if cfg.auth_token:
        token = request.headers.get("x-caucus-token") or ""
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            token = token or auth[7:].strip()
        return hmac.compare_digest(token, cfg.auth_token)   # constant-time — no length/char leak
    return cfg.is_local_bind


def create_app(cfg: Optional[CaucusConfig] = None) -> FastAPI:
    cfg = cfg or load_config()
    cfg.validate()  # bind-safety hard rule — refuse unsafe exposure at construction
    combos = load_combos()
    ledger = Ledger()
    selections: deque = deque(maxlen=50)  # recent action-selection traces (console view)
    # Cap concurrent deliberation turns (each fans out a panel + judge / sandbox runs) so a flood
    # can't spawn unbounded provider calls and worker threads. Pass-through turns are unaffected.
    turn_sem = asyncio.Semaphore(8)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        register_mock()
        # Tee LiteLLM's loggers into the ring buffer so the console "Logs" tab can show the
        # per-call LiteLLM stream (the caucus + synth_engine loggers are teed in log.configure()).
        attach_ring("LiteLLM", "LiteLLM Proxy", "LiteLLM Router")
        # Load BYOK keys from the secured store into this process's env (in-process only) so
        # LiteLLM calls find them. Only provider *names* are logged.
        injected = inject_env(get_keystore())
        status = engine_status()
        if not status["reachable"]:  # fail loud if the engine can't start
            raise RuntimeError("Synth engine not reachable in-process — refusing to start.")
        event(log, "caucus.start", version=__version__, bind=cfg.bind, port=cfg.port,
              model=cfg.model or "(agent-supplied)", fallback=cfg.fallback_model or "(none)",
              keys=",".join(injected) or "(none)", combos=",".join(combos),
              engine=f"{status['engine']}/{status['version']}", exposed=cfg.expose)
        yield

    app = FastAPI(title="Caucus", version=__version__, lifespan=lifespan)
    app.state.config = cfg
    app.state.combos = combos
    app.state.ledger = ledger

    @app.middleware("http")
    async def auth_gate(request: Request, call_next):
        # Reject oversized bodies up front (resource-exhaustion guard) before anything reads them.
        clen = request.headers.get("content-length")
        if clen and clen.isdigit() and int(clen) > _MAX_BODY_BYTES:
            return JSONResponse(status_code=413, content={"type": "error", "error": {
                "type": "invalid_request_error", "message": "Request body too large."}})
        # On a local bind everything is open to the local user. When a token is set it gates EVERY
        # surface — proxy, console, and the key/combo management endpoints — so exposure never hands
        # a remote party the keys or the combo config.
        if not _authorized(request, cfg):
            return JSONResponse(status_code=401, content={"type": "error", "error": {
                "type": "authentication_error", "message": "Missing or invalid Caucus auth token."}})
        return await call_next(request)

    # ---- proxy endpoint (the agent's API) --------------------------------------------
    @app.post("/v1/messages")
    async def handle_messages(request: Request):
        rid = uuid.uuid4().hex[:12]
        started = time.monotonic()
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"type": "error", "error": {
                "type": "invalid_request_error", "message": "Request body must be valid JSON."}})

        combo = resolve(body.get("model", ""), cfg.model, combos)
        if combo is None:
            return _no_provider_error()
        stream = bool(body.get("stream", False))
        call_trace.set([])  # collect each provider sub-call (model/tokens/cost) for the inspector
        decision = classify(body)
        turn = decision.turn.value
        mode = turn

        def record(model: str, tokens: int) -> None:
            ms = round((time.monotonic() - started) * 1000)
            calls = list(call_trace.get() or [])
            ledger.record(TurnRecord(rid, combo.name, turn, mode, model, tokens,
                                     turn_cost(model, tokens), ms, calls))
            event(log, "caucus.turn", rid=rid, combo=combo.name, turn=turn,
                  reason=decision.reason, mode=mode, model=model, stream=stream,
                  tokens=tokens, calls=len(calls), ms=ms)

        degraded = ""   # set when a synth/selection silently downgrades → surfaced as a header (F13)
        try:
            if decision.is_plan:
                try:
                    async with turn_sem:   # concurrency cap on the heavy panel fan-out
                        synthesized = await synth.synthesize(body, combo, fallback=cfg.fallback_model)
                    mode = "synth"
                    record(synthesized["model"], synthesized["usage"]["output_tokens"])
                    if stream:
                        return StreamingResponse(synth.message_to_sse(synthesized),
                                                 media_type="text/event-stream")
                    return JSONResponse(synthesized)
                except Exception as exc:  # judge/panel failed → best-of-1 pass-through
                    mode = "synth→passthrough(degraded)"
                    degraded = "synth-failed"
                    event(log, "caucus.turn.degrade", rid=rid, combo=combo.name,
                          error=type(exc).__name__)

            elif combo.strategy == STRATEGY_SANDBOX:
                # v1.1 action selection — sandbox-and-test. Fails closed → pass-through.
                try:
                    async with turn_sem:
                        survivor = await selection.run_action_selection(
                            body, combo, workspace=cfg.workspace, test_command=cfg.test_command,
                            fallback=cfg.fallback_model)
                except Exception as exc:
                    survivor = None
                    event(log, "caucus.turn.degrade", rid=rid, combo=combo.name,
                          error=type(exc).__name__)
                sel = body.pop("_selection", None)
                if sel is not None:
                    selections.appendleft({
                        "rid": rid, "combo": combo.name, "survivor": sel.survivor,
                        "reason": sel.reason,
                        "results": [{"index": r.index, "applied": r.applied, "passed": r.passed,
                                     "returncode": r.returncode} for r in sel.results]})
                if survivor is not None:
                    mode = "select"
                    record(survivor["model"], survivor["usage"]["output_tokens"])
                    if stream:
                        return StreamingResponse(selection.action_message_to_sse(survivor),
                                                 media_type="text/event-stream")
                    return JSONResponse(survivor)
                mode = "select→passthrough(degraded)"
                # The agent asked for sandbox-and-test but it silently fell back — tell it (F13).
                degraded = "sandbox-and-test-unavailable"

            body["model"] = combo.primary  # action turns pass through to the combo's primary
            result = await frontdoor.passthrough(body, stream=stream, fallback=cfg.fallback_model)
        except Exception as exc:  # degrade gracefully, never hang the agent
            event(log, "caucus.turn.error", rid=rid, combo=combo.name, error=type(exc).__name__)
            return JSONResponse(status_code=502, content={"type": "error", "error": {
                "type": "api_error", "message": f"Upstream provider error ({type(exc).__name__})."}})

        hdrs = {"X-Caucus-Degraded": degraded} if degraded else None
        if stream:
            async def sse():
                async for chunk in result:
                    yield chunk if isinstance(chunk, (bytes, bytearray)) else str(chunk).encode()
            record(combo.primary, 0)
            return StreamingResponse(sse(), media_type="text/event-stream", headers=hdrs)

        out = dict(result)
        record(combo.primary, (out.get("usage") or {}).get("output_tokens", 0))
        return JSONResponse(out, headers=hdrs)

    # ---- OpenAI-compatible endpoint (for OpenAI clients + tool-calling benchmarks) ----
    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        # Caucus also speaks the OpenAI Chat Completions shape so OpenAI-native clients/agents and
        # benchmarks can point at it. TOOL PASSTHROUGH: when tools are present we keep the request in
        # OpenAI shape and route it via litellm.completion to the combo's primary — no Anthropic
        # translation, so tool_calls survive intact (the synth produces text and can't emit a tool
        # call; structured tool turns must pass through). Tool-aware synth is the follow-up.
        import asyncio
        import json as _json

        import litellm
        rid = uuid.uuid4().hex[:12]
        started = time.monotonic()
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"error": {
                "message": "Request body must be valid JSON.", "type": "invalid_request_error"}})
        combo = resolve(body.get("model", ""), cfg.model, combos)
        if combo is None:
            return JSONResponse(status_code=400, content={"error": {
                "message": "No provider configured (set CAUCUS_MODEL / a combo).", "type": "invalid_request_error"}})
        stream = bool(body.get("stream", False))
        call_trace.set([])
        kwargs = {"model": combo.primary, "messages": body.get("messages") or []}
        for k in ("max_tokens", "temperature", "top_p", "tool_choice", "tools", "parallel_tool_calls"):
            if body.get(k) is not None:
                kwargs[k] = body[k]

        def record(model: str, tokens: int) -> None:
            ms = round((time.monotonic() - started) * 1000)
            calls = list(call_trace.get() or [])
            ledger.record(TurnRecord(rid, combo.name, "openai", "passthrough-openai", model,
                                     tokens, turn_cost(model, tokens), ms, calls))
            event(log, "caucus.openai", rid=rid, combo=combo.name, model=model, stream=stream,
                  tools=bool(kwargs.get("tools")), tokens=tokens, ms=ms)

        try:
            if stream:
                async def sse():
                    for chunk in litellm.completion(**{**kwargs, "stream": True}):
                        data = chunk.model_dump() if hasattr(chunk, "model_dump") else chunk
                        yield f"data: {_json.dumps(data)}\n\n".encode()
                    yield b"data: [DONE]\n\n"
                record(combo.primary, 0)
                return StreamingResponse(sse(), media_type="text/event-stream")
            resp = await asyncio.to_thread(lambda: litellm.completion(**kwargs))
            out = resp.model_dump() if hasattr(resp, "model_dump") else dict(resp)
            record(combo.primary, (out.get("usage") or {}).get("completion_tokens", 0))
            return JSONResponse(out)
        except Exception as exc:
            event(log, "caucus.openai.error", rid=rid, combo=combo.name, error=type(exc).__name__)
            return JSONResponse(status_code=502, content={"error": {
                "message": f"Upstream provider error ({type(exc).__name__}).", "type": "api_error"}})

    # ---- OpenAI Responses API endpoint (for codex + Responses-native agents) ----------
    @app.post("/v1/responses")
    async def responses_endpoint(request: Request):
        # Caucus speaks the OpenAI Responses shape too. Passthrough via litellm.responses, which
        # translates Responses<->chat for the provider (deepseek), so function_call items survive
        # and the streamed Responses event protocol is emitted intact (what codex consumes).
        import asyncio
        import json as _json

        import litellm
        rid = uuid.uuid4().hex[:12]
        started = time.monotonic()
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"error": {
                "message": "Request body must be valid JSON.", "type": "invalid_request_error"}})
        combo = resolve(body.get("model", ""), cfg.model, combos)
        if combo is None:
            return JSONResponse(status_code=400, content={"error": {
                "message": "No provider configured (set CAUCUS_MODEL / a combo).", "type": "invalid_request_error"}})
        stream = bool(body.get("stream", False))
        call_trace.set([])
        kwargs = {"model": combo.primary}
        for k in ("input", "instructions", "tool_choice", "max_output_tokens", "temperature",
                  "top_p", "parallel_tool_calls", "reasoning", "metadata", "include", "truncation", "text"):
            if body.get(k) is not None:
                kwargs[k] = body[k]
        # Sanitize tools: a plain chat backend (deepseek) only understands type:function. Agents
        # like codex also send `namespace` / MCP-group tools — drop those so the provider doesn't
        # reject the whole request (the function tools, e.g. shell/apply_patch, still pass through).
        tools = body.get("tools")
        if isinstance(tools, list):
            tools = [t for t in tools if isinstance(t, dict) and t.get("type") == "function"]
            if tools:
                kwargs["tools"] = tools

        def record(model: str, tokens: int) -> None:
            ms = round((time.monotonic() - started) * 1000)
            calls = list(call_trace.get() or [])
            ledger.record(TurnRecord(rid, combo.name, "responses", "passthrough-responses", model,
                                     tokens, turn_cost(model, tokens), ms, calls))
            event(log, "caucus.responses", rid=rid, combo=combo.name, model=model, stream=stream,
                  tools=bool(kwargs.get("tools")), tokens=tokens, ms=ms)

        try:
            if stream:
                async def sse():
                    # Fully async (litellm.aresponses) so the loop never blocks between chunks. The
                    # stream is wrapped so a mid-stream upstream error is LOGGED (not a silent socket
                    # close), and codex ALWAYS gets a terminal response.completed — its tool-call
                    # turns can otherwise end early upstream and the client just reconnects.
                    saw_completed = False
                    last_response = None
                    try:
                        response_stream = await litellm.aresponses(**{**kwargs, "stream": True})
                        async for ev in response_stream:
                            d = ev.model_dump() if hasattr(ev, "model_dump") else dict(ev)
                            t = getattr(d.get("type"), "value", d.get("type"))
                            d["type"] = t
                            if isinstance(d.get("response"), dict):
                                last_response = d["response"]
                            saw_completed = saw_completed or t == "response.completed"
                            yield f"event: {t}\ndata: {_json.dumps(d, default=str)}\n\n".encode()
                    except Exception as exc:
                        event(log, "caucus.responses.stream_error", rid=rid, combo=combo.name,
                              error=type(exc).__name__)
                    if not saw_completed:  # guarantee a terminal event so the client never hangs
                        done = {"type": "response.completed",
                                "response": {**(last_response or {"id": f"resp_{rid}", "object": "response"}),
                                             "status": "completed"}}
                        yield f"event: response.completed\ndata: {_json.dumps(done, default=str)}\n\n".encode()
                record(combo.primary, 0)
                return StreamingResponse(sse(), media_type="text/event-stream")
            resp = await litellm.aresponses(**kwargs)
            out = resp.model_dump() if hasattr(resp, "model_dump") else dict(resp)
            record(combo.primary, (out.get("usage") or {}).get("output_tokens", 0))
            return JSONResponse(out)
        except Exception as exc:
            event(log, "caucus.responses.error", rid=rid, combo=combo.name, error=type(exc).__name__)
            return JSONResponse(status_code=502, content={"error": {
                "message": f"Upstream provider error ({type(exc).__name__}).", "type": "api_error"}})

    @app.get("/v1/models")
    async def list_models() -> JSONResponse:
        # Agents query GET /v1/models on startup. Caucus substitutes its combo regardless, so we
        # advertise the combos + active model. `data` is the OpenAI-standard list; `models` is
        # codex's model-refresh shape (it errors without that key). Both, so everyone's happy.
        names = sorted(set(combos) | {cfg.model or "caucus"})
        data = [{"id": n, "object": "model", "created": 0, "owned_by": "caucus"} for n in names]
        # `data` is the OpenAI-standard list for normal clients. `models` is codex's model-refresh
        # shape — its model objects require an undocumented internal schema, so we return an empty
        # list: codex's parser validates nothing, the refresh succeeds with 0 listed models, and
        # codex uses the explicitly-configured model. (Caucus substitutes its combo regardless.)
        return JSONResponse({"object": "list", "data": data, "models": []})

    # ---- status + console API --------------------------------------------------------
    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({
            "status": "ok", "service": "caucus", "version": __version__,
            "bind": f"{cfg.bind}:{cfg.port}", "active": cfg.model or "(agent-supplied)",
            "fallback": cfg.fallback_model or None, "combos": list(combos),
            "engine": engine_status(), "ledger": ledger.summary(), "mock_available": True,
        })

    @app.get("/v1/combos")
    async def get_combos() -> JSONResponse:
        return JSONResponse({
            "active": cfg.model, "aliases": ALIASES,
            "combos": {n: c.to_toml_table() for n, c in combos.items()},
        })

    @app.put("/v1/combos/{name}")
    async def put_combo(name: str, request: Request) -> JSONResponse:
        data = await request.json()
        panel = [str(m) for m in (data.get("panel") or [])]
        judge = str(data.get("judge", ""))
        strategy = str(data.get("strategy", "passthrough"))
        # Validate every model slug so a crafted string can't be smuggled into LiteLLM routing (SSRF).
        bad = [m for m in panel + ([judge] if judge else []) if not _valid_model(m)]
        if bad or not _valid_model(name):
            return JSONResponse(status_code=400, content={"ok": False, "error":
                f"Invalid model/combo name: {bad[0] if bad else name!r}. "
                "Use plain provider/model slugs (e.g. openai/gpt-4o-mini) — no URLs."})
        if strategy not in ("passthrough", "sandbox-and-test"):
            return JSONResponse(status_code=400, content={"ok": False, "error":
                f"Invalid strategy {strategy!r} — use 'passthrough' or 'sandbox-and-test'."})
        combos[name] = Combo(name, panel, judge, strategy)
        save_config(cfg, combos)  # persist server-side (never keys)
        return JSONResponse({"ok": True, "combo": combos[name].to_toml_table()})

    @app.post("/v1/combos/active")
    async def set_active(request: Request) -> JSONResponse:
        data = await request.json()
        cfg.model = str(data.get("name", ""))
        save_config(cfg, combos)
        return JSONResponse({"ok": True, "active": cfg.model})

    @app.get("/v1/ledger")
    async def get_ledger() -> JSONResponse:
        return JSONResponse({"summary": ledger.summary(), "recent": ledger.recent()})

    @app.get("/v1/logs")
    async def get_logs(since: int = 0, limit: int = 600) -> JSONResponse:
        # The live LiteLLM + synth engine (+ caucus) log stream for the console "Logs" tab. Mirrors the
        # loggers' verbosity: metadata only in normal mode; full trace under `--debug`. Gated like
        # every surface when exposed, so an exposed daemon never leaks its logs.
        import os as _os
        return JSONResponse({"lines": ring_tail(since, limit),
                             "debug": bool(_os.environ.get("CAUCUS_DEBUG"))})

    @app.delete("/v1/logs")
    async def clear_logs() -> JSONResponse:
        ring_clear()
        return JSONResponse({"ok": True})

    @app.get("/v1/selections")
    async def get_selections() -> JSONResponse:
        return JSONResponse({"recent": list(selections)})

    @app.get("/v1/keys")
    async def get_keys() -> JSONResponse:
        store = get_keystore()
        return JSONResponse({"backend": store.backend,
                             "keys": {p: fingerprint(store, p) for p in store.providers()}})

    @app.post("/v1/keys/{provider}")
    async def set_key(provider: str, request: Request) -> JSONResponse:
        # The key travels browser→daemon over localhost and is stored server-side. It is NEVER
        # written to config.toml and NEVER returned to the browser — only a fingerprint.
        data = await request.json()
        key = str(data.get("key", ""))
        if not key:
            return JSONResponse(status_code=400, content={"ok": False, "error": "empty key"})
        store = get_keystore()
        store.set(provider, key)
        env = env_var_for(provider)
        if env:
            import os
            os.environ[env] = key  # make it live this session without a restart
        return JSONResponse({"ok": True, "provider": provider, "fingerprint": fingerprint(store, provider)})

    @app.delete("/v1/keys/{provider}")
    async def delete_key(provider: str) -> JSONResponse:
        store = get_keystore()
        return JSONResponse({"ok": store.delete(provider), "provider": provider})

    @app.post("/v1/keys/{provider}/test")
    async def test_key(provider: str, request: Request) -> JSONResponse:
        # Verify a stored key actually authenticates: a 1-token completion against a model for this
        # provider. Prefer a model the user already put in a combo (tests what they configured); else
        # fall back to a cheap default. Never raises — returns {ok, model, ms} or {ok:false, error}.
        import asyncio

        import litellm
        try:
            data = await request.json()
        except Exception:
            data = {}
        model = (data or {}).get("model")
        if not model:
            for c in combos.values():
                for m in [*(c.panel or []), c.judge]:
                    if m and m.split("/", 1)[0] == provider:
                        model = m
                        break
                if model:
                    break
        model = model or _TEST_MODELS.get(provider)
        if not model:
            return JSONResponse({"ok": False, "provider": provider,
                                 "error": f"No test model known for '{provider}' — add it to a combo first."})
        t0 = time.monotonic()
        try:
            await asyncio.to_thread(lambda: litellm.completion(
                model=model, messages=[{"role": "user", "content": "ping"}],
                max_tokens=1, temperature=0, timeout=20))
            return JSONResponse({"ok": True, "provider": provider, "model": model,
                                 "ms": round((time.monotonic() - t0) * 1000)})
        except Exception as exc:
            # NEVER echo the raw provider exception — it can contain the API key. Surface only the
            # exception class + a key-free category hint (full detail goes to the daemon logs).
            name = type(exc).__name__
            low = name.lower()
            hint = ("authentication failed — check the key" if "auth" in low or "permission" in low
                    else "rate-limited or out of quota" if "ratelimit" in low or "quota" in low
                    else "timed out" if "timeout" in low
                    else "could not reach the provider" if "connection" in low or "apiconnection" in low
                    else "the provider rejected the request")
            event(log, "caucus.keytest.fail", provider=provider, model=model, error=name)
            return JSONResponse({"ok": False, "provider": provider, "model": model,
                                 "error": f"{name} — {hint}"})

    @app.get("/v1/config")
    async def get_config() -> JSONResponse:
        # The safe, full daemon config for the Settings → Daemon panel. /health omits workspace,
        # test_command and debug; this exposes them — but NEVER the auth_token. expose +
        # auth_token are launch-flag-managed, surfaced read-only so the UI can explain, not edit them.
        import os as _os
        store = get_keystore()
        return JSONResponse({
            "bind": cfg.bind, "port": cfg.port, "is_local_bind": cfg.is_local_bind,
            "active": cfg.model or None, "fallback": cfg.fallback_model or None,
            "workspace": cfg.workspace or None, "test_command": cfg.test_command or None,
            "expose": cfg.expose, "source": getattr(cfg, "source", None),
            "keystore_backend": store.backend, "debug": bool(_os.environ.get("CAUCUS_DEBUG")),
            "engine": engine_status(),
        })

    # ---- console (served files; script-src 'self', same-origin) ----------------------
    @app.get("/", response_class=HTMLResponse)
    async def console_index() -> HTMLResponse:
        return HTMLResponse((_CONSOLE_DIR / "index.html").read_text(),
                            headers={"Content-Security-Policy": _CONSOLE_CSP})

    @app.get("/console.js")
    async def console_js() -> Response:
        return Response((_CONSOLE_DIR / "app.js").read_text(),
                        media_type="application/javascript",
                        headers={"Content-Security-Policy": _CONSOLE_CSP})

    @app.get("/console.css")
    async def console_css() -> Response:
        return Response((_CONSOLE_DIR / "app.css").read_text(), media_type="text/css")

    return app
