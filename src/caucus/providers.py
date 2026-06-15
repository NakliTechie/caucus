"""Provider routing seam.

All model calls go through LiteLLM — LiteLLM owns multi-provider
routing, inside the synth engine. Two things live here:

1. ``LiteLLMClient`` — an OpenAI-shaped client (``client.chat.completions.create``)
   that the engine's in-process MOA / best-of-N consume. It forwards to
   ``litellm.completion`` so a single line of routing config reaches every provider
   LiteLLM supports (BYOK cloud + local Ollama).

2. ``CaucusMockLLM`` — a deterministic, keyless provider registered through
   LiteLLM's *real* ``CustomLLM`` extension point. It is explicit and clearly
   labelled (never silently fall back to nothing). It exists so the full
   pipeline — front door → translate → engine → translate back → stream — is
   verifiable offline, on a machine with no key and no working local model.
"""

from __future__ import annotations

import json
import logging as _logging
import time
from typing import Any, AsyncIterator, Iterator

import litellm
from litellm import CustomLLM
from litellm.types.utils import (
    ChatCompletionMessageToolCall,
    Choices,
    Function,
    GenericStreamingChunk,
    Message,
    ModelResponse,
    Usage,
)

from .config import MOCK_PROVIDER

# A sovereign, offline-capable daemon must not phone home. The cost-map fetch is
# pinned to the bundled file in caucus/__init__.py (set before litellm imports); here we
# silence telemetry, debug probes, and LiteLLM's chatty INFO loggers.
litellm.suppress_debug_info = True
litellm.telemetry = False
litellm.set_verbose = False
for _name in ("LiteLLM", "LiteLLM Proxy", "LiteLLM Router"):
    _logging.getLogger(_name).setLevel(_logging.WARNING)


def set_debug(on: bool = True) -> None:
    """Opt-in LOCAL exploration mode (``caucus start --debug``).

    Surfaces what the daemon normally keeps quiet: LiteLLM's per-call request/response logs and
    the native synth (MOA / best-of-N) pipeline trace (emitted from ``_record``). NOT for a shared
    or exposed daemon — under this explicit opt-in the body-redaction is relaxed so the trace can
    show short content previews of the panel candidates.
    """
    import os

    os.environ["CAUCUS_DEBUG"] = "1" if on else ""
    litellm.suppress_debug_info = not on
    if on:
        os.environ["LITELLM_LOG"] = "DEBUG"
        try:
            litellm._turn_on_debug()
        except Exception:  # older litellm
            litellm.set_verbose = True
    ll_level = _logging.DEBUG if on else _logging.WARNING
    for _name in ("LiteLLM", "LiteLLM Proxy", "LiteLLM Router"):
        _logging.getLogger(_name).setLevel(ll_level)
    cx_level = _logging.DEBUG if on else _logging.INFO
    for _name in ("caucus", "caucus.synth_engine", "caucus.synth_engine.trace"):
        _logging.getLogger(_name).setLevel(cx_level)

from contextvars import ContextVar

# Per-turn call trace — when a turn is in flight the server sets this to a fresh list, and every
# provider call appends its metadata (model, tokens, cost, kind) so the console inspector can show
# EXACTLY which models ran and what each cost — the transparency OpenRouter Fusion hides. It is
# metadata only: never message bodies. Copied into the synth worker thread by asyncio.to_thread.
call_trace: ContextVar = ContextVar("caucus_call_trace", default=None)
_synth_trace = _logging.getLogger("caucus.synth_engine.trace")


def _record(model: str, resp, kind: str) -> None:
    try:
        pt = int(getattr(resp.usage, "prompt_tokens", 0) or 0)
        ct = int(getattr(resp.usage, "completion_tokens", 0) or 0)
    except Exception:
        pt = ct = 0
    import os as _os
    if _os.environ.get("CAUCUS_DEBUG"):  # local exploration: emit the synth pipeline trace
        try:
            _synth_trace.info("synth step · model=%s kind=%s completion_tokens=%s", model, kind, ct)
            content = resp.choices[0].message.content or ""
            if content:
                _synth_trace.debug("    %s", content.replace("\n", " ")[:140])
        except Exception:
            pass
    trace = call_trace.get()
    if trace is None:
        return
    try:
        cost = float(litellm.completion_cost(completion_response=resp) or 0.0)
    except Exception:
        cost = 0.0
    trace.append({"model": model, "kind": kind, "prompt_tokens": pt,
                  "completion_tokens": ct, "cost_usd": round(cost, 6)})


def call(model: str, messages: list[dict], *, kind: str = "panel", max_tokens: int = 4096,
         temperature: float = 1.0, tools: list | None = None, n: int = 1,
         fallback: str | None = None) -> "ModelResponse":
    """One provider call through LiteLLM, with the inspector trace + fallback escalation.

    The native synth engine (caucus.synth_engine) routes every panel/judge call through here so it
    inherits the per-call trace (``_record`` with an explicit ``kind`` — panel vs judge) and the
    availability fallback, exactly like the engine seam did, but tool-aware (``tools`` flows
    through, so a panel member can return a real tool_call).
    """
    kwargs: dict = {"model": model, "messages": messages, "max_tokens": max_tokens,
                    "temperature": temperature, "n": n}
    if tools:
        kwargs["tools"] = tools
    try:
        resp = litellm.completion(**kwargs)
        _record(model, resp, kind)
        return resp
    except Exception:
        if fallback and model != fallback:
            _logging.getLogger("caucus.routing").info("provider call failed; escalating to fallback")
            resp = litellm.completion(**{**kwargs, "model": fallback})
            _record(fallback, resp, kind + "→fallback")
            return resp
        raise


def _last_user_text(messages: list[dict]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, list):  # multimodal block list
                return " ".join(
                    b.get("text", "") for b in content if isinstance(b, dict)
                ).strip()
            return str(content or "").strip()
    return ""


def _mock_contents(model: str, messages: list[dict], n: int) -> list[str]:
    """Deterministic, clearly-labelled stand-in responses (one per requested sample)."""
    asked = _last_user_text(messages)
    snippet = asked if len(asked) <= 160 else asked[:157] + "…"
    base = (
        f"[caucus-mock · {model}] You said: {snippet!r}. "
        "This is the Caucus mock provider — deterministic output, no real inference. "
        "Configure a real provider (Ollama or a BYOK key) for model answers."
    )
    if n <= 1:
        return [base]
    # Vary per sample so a panel/judge has distinct candidates to work with.
    return [f"{base} [variant {i + 1}/{n}]" for i in range(n)]


def _usage(prompt_tokens: int, completion_tokens: int) -> Usage:
    return Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )


class MockProviderError(Exception):
    """Raised by the mock's ``…/fail`` model to exercise availability escalation."""


class CaucusMockLLM(CustomLLM):
    """A deterministic provider behind LiteLLM's CustomLLM contract.

    Model suffix selects behaviour, so the routing ladder is testable offline:
    ``echo`` → normal · ``fail`` → raises (provider-down) · ``weak`` → sub-floor reply.
    """

    def _response(self, model: str, messages: list[dict], optional_params: dict) -> ModelResponse:
        suffix = model.rsplit("/", 1)[-1]
        if suffix == "fail":
            raise MockProviderError("caucus-mock/fail: simulated provider outage")
        n = int(optional_params.get("n", 1) or 1)
        if suffix == "edit":
            # Emits a str_replace tool_call (OLD→NEW in data.txt) to exercise v1.1 selection.
            tc = ChatCompletionMessageToolCall(
                id="toolu_mock", type="function",
                function=Function(name="str_replace_editor", arguments=json.dumps(
                    {"command": "str_replace", "path": "data.txt", "old_str": "OLD", "new_str": "NEW"})))
            choices = [Choices(index=i, message=Message(role="assistant", content="",
                                                        tool_calls=[tc]), finish_reason="tool_calls")
                       for i in range(n)]
            return ModelResponse(id=f"caucus-mock-{int(time.time() * 1000)}",
                                 created=int(time.time()), model=model, object="chat.completion",
                                 choices=choices, usage=_usage(1, 1))
        if suffix == "weak":
            contents = ["ok"] * n  # below the planning-quality floor → triggers honesty escalation
        else:
            contents = _mock_contents(model, messages, n)
        prompt_tokens = sum(len(str(m.get("content", ""))) for m in messages) // 4 or 1
        completion_tokens = sum(len(c) for c in contents) // 4 or 1
        choices = [
            Choices(index=i, message=Message(role="assistant", content=c), finish_reason="stop")
            for i, c in enumerate(contents)
        ]
        return ModelResponse(
            id=f"caucus-mock-{int(time.time() * 1000)}",
            created=int(time.time()),
            model=model,
            object="chat.completion",
            choices=choices,
            usage=_usage(prompt_tokens, completion_tokens),
        )

    def completion(self, *args: Any, **kwargs: Any) -> ModelResponse:  # noqa: D102
        return self._response(
            kwargs.get("model", "caucus-mock/echo"),
            kwargs.get("messages", []),
            kwargs.get("optional_params", {}) or {},
        )

    async def acompletion(self, *args: Any, **kwargs: Any) -> ModelResponse:  # noqa: D102
        return self._response(
            kwargs.get("model", "caucus-mock/echo"),
            kwargs.get("messages", []),
            kwargs.get("optional_params", {}) or {},
        )

    def _chunks(self, model: str, messages: list[dict]) -> list[str]:
        text = _mock_contents(model, messages, 1)[0]
        # Stream word-by-word so SSE pass-through has something realistic to chunk.
        words = text.split(" ")
        return [(" " if i else "") + w for i, w in enumerate(words)]

    def streaming(self, *args: Any, **kwargs: Any) -> Iterator[GenericStreamingChunk]:  # noqa: D102
        model = kwargs.get("model", "caucus-mock/echo")
        pieces = self._chunks(model, kwargs.get("messages", []))
        n = len(pieces)
        for i, piece in enumerate(pieces):
            last = i == n - 1
            yield GenericStreamingChunk(
                text=piece,
                tool_use=None,
                is_finished=last,
                finish_reason="stop" if last else "",
                # GenericStreamingChunk.usage is a TypedDict (mapping), not a Usage object —
                # LiteLLM's stream handler does Usage(**usage), so a dict is required.
                usage={"prompt_tokens": 1, "completion_tokens": n, "total_tokens": n + 1} if last else None,
                index=0,
            )

    async def astreaming(self, *args: Any, **kwargs: Any) -> AsyncIterator[GenericStreamingChunk]:  # noqa: D102
        for chunk in self.streaming(*args, **kwargs):
            yield chunk


_mock_handler = CaucusMockLLM()


def register_mock() -> None:
    """Idempotently register the mock provider with LiteLLM."""
    existing = litellm.custom_provider_map or []
    if any(entry.get("provider") == MOCK_PROVIDER for entry in existing):
        return
    litellm.custom_provider_map = existing + [
        {"provider": MOCK_PROVIDER, "custom_handler": _mock_handler}
    ]


# Register on import so any code path (server, CLI, tests, the in-process engine) can
# reach the mock without ceremony.
register_mock()


class _Completions:
    def __init__(self, fallback: str | None = None) -> None:
        self._fallback = fallback

    def create(self, **kwargs: Any) -> ModelResponse:
        kind = "panel" if int(kwargs.get("n", 1) or 1) > 1 else "judge"
        try:
            resp = litellm.completion(**kwargs)
            _record(kwargs.get("model", "?"), resp, kind)
            return resp
        except Exception:
            # Availability fallback: provider down → cloud fallback.
            fb = self._fallback
            if fb and kwargs.get("model") != fb:
                _logging.getLogger("caucus.routing").info(
                    "provider call failed; escalating to fallback model"
                )
                resp = litellm.completion(**{**kwargs, "model": fb})
                _record(fb, resp, kind + "→fallback")
                return resp
            raise


class _Chat:
    def __init__(self, fallback: str | None = None) -> None:
        self.completions = _Completions(fallback)


class LiteLLMClient:
    """OpenAI-shaped client the engine's in-process MOA / best-of-N call (``.chat.completions.create``).

    Carries the cloud fallback so every panel/critique/synthesis call escalates on a provider
    outage without the engine needing to know about the ladder.
    """

    def __init__(self, fallback: str | None = None) -> None:
        self.chat = _Chat(fallback)


def make_client(fallback: str | None = None) -> LiteLLMClient:
    return LiteLLMClient(fallback)


class _PanelCompletions:
    """Routes the engine's MOA calls onto a heterogeneous combo.

    The engine samples the panel with one ``n>1`` call, then critiques and synthesises with
    ``n==1`` calls. We send the sampling call across the combo's *panel members* (one provider
    call each, aggregated) and the judge calls to the *judge* model. The engine's MOA algorithm
    is untouched — only where the candidates come from changes (routing, the moat).
    """

    def __init__(self, panel: list[str], judge: str, fallback: str | None = None) -> None:
        self._panel = panel or [judge]
        self._judge = judge
        self._fallback = fallback

    def _one(self, model: str, kwargs: dict) -> ModelResponse:
        try:
            return litellm.completion(**{**kwargs, "model": model})
        except Exception:
            if self._fallback and model != self._fallback:
                return litellm.completion(**{**kwargs, "model": self._fallback})
            raise

    def create(self, **kwargs: Any) -> ModelResponse:
        n = int(kwargs.get("n", 1) or 1)
        if n <= 1:  # judge call (critique / synthesis)
            resp = self._one(self._judge, {**kwargs, "n": 1})
            _record(self._judge, resp, "judge")
            return resp

        # panel sampling: cycle members to fill n, one call each, aggregate the choices.
        members = [self._panel[i % len(self._panel)] for i in range(n)]
        choices: list[Choices] = []
        completion_tokens = 0
        for member in members:
            try:
                resp = self._one(member, {**kwargs, "n": 1})
                _record(member, resp, "panel")
            except Exception:
                continue  # a dead panel member is skipped; proceed with those that returned
            for ch in resp.choices:
                choices.append(Choices(index=len(choices), message=ch.message,
                                       finish_reason=ch.finish_reason or "stop"))
            completion_tokens += getattr(resp.usage, "completion_tokens", 0) or 0
        if not choices:
            raise RuntimeError("all panel members failed")
        return ModelResponse(
            id=f"caucus-panel-{int(time.time() * 1000)}", created=int(time.time()),
            model="caucus-panel", object="chat.completion", choices=choices,
            usage=_usage(0, completion_tokens),
        )


class _PanelChat:
    def __init__(self, panel: list[str], judge: str, fallback: str | None) -> None:
        self.completions = _PanelCompletions(panel, judge, fallback)


class PanelClient:
    """OpenAI-shaped client that turns the engine's MOA into a heterogeneous panel + judge."""

    def __init__(self, panel: list[str], judge: str, fallback: str | None = None) -> None:
        self.chat = _PanelChat(panel, judge, fallback)


def make_panel_client(panel: list[str], judge: str, fallback: str | None = None) -> PanelClient:
    return PanelClient(panel, judge, fallback)
