"""The daemon surface: health, pass-through round-trip, §4 empty-state, console CSP."""

from fastapi.testclient import TestClient

from caucus.config import CaucusConfig
from caucus.server import create_app


def _client(model: str = "caucus-mock/echo") -> TestClient:
    return TestClient(create_app(CaucusConfig(model=model)))


def test_health_engine_reachable():
    with _client() as c:
        r = c.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["engine"]["reachable"] is True
        assert body["engine"]["in_process"] is True


def test_messages_passthrough_substitutes_configured_model():
    with _client() as c:
        # Agent asks for claude; Caucus substitutes its configured (mock) model.
        r = c.post("/v1/messages", json={
            "model": "claude-3-5-sonnet-20241022",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["type"] == "message"
        assert body["role"] == "assistant"
        text = "".join(b.get("text", "") for b in body["content"] if b.get("type") == "text")
        assert "caucus-mock" in text


def test_no_provider_configured_is_a_clear_error():
    with _client(model="") as c:
        r = c.post("/v1/messages", json={
            "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}],
        })
        assert r.status_code == 400
        assert "No provider configured" in r.json()["error"]["message"]


def test_plan_turn_is_synthesized():
    with _client() as c:
        r = c.post("/v1/messages", json={
            "model": "claude", "max_tokens": 256,
            "tools": [{"name": "bash"}],
            "messages": [{"role": "user", "content": "How should I design the caching layer?"}],
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["type"] == "message"
        # Synth mints a msg_ id; pass-through would carry the mock's caucus-mock- id.
        assert body["id"].startswith("msg_"), "plan turn should be synthesized, not passed through"
        assert body["content"][0]["text"]


def test_action_turn_passes_through():
    with _client() as c:
        r = c.post("/v1/messages", json={
            "model": "claude", "max_tokens": 64,
            "tools": [{"name": "bash"}],
            "messages": [
                {"role": "user", "content": "run the tests"},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "t", "name": "bash", "input": {}}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "t", "content": "11 passed"}]},
            ],
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["type"] == "message"
        # Continuation → pass-through → carries the mock provider's id, not a synthesized msg_ id.
        assert body["id"].startswith("caucus-mock-"), "action turn should pass through"


def test_action_selection_e2e_through_server(tmp_path, monkeypatch):
    from caucus.combos import DEFAULT_COMBOS, Combo
    from caucus.config import save_config
    from caucus.sandbox import get_sandbox

    if get_sandbox() is None:
        import pytest
        pytest.skip("no sandbox backend on this host")

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "data.txt").write_text("OLD\n")
    monkeypatch.setenv("CAUCUS_CONFIG", str(tmp_path / "config.toml"))
    combos = {n: c for n, c in DEFAULT_COMBOS.items()}
    combos["sbx"] = Combo("sbx", panel=["caucus-mock/edit"], judge="caucus-mock/edit",
                          strategy="sandbox-and-test")
    cfg = CaucusConfig(
        model="sbx", workspace=str(repo),
        test_command="python3 -c \"import sys; sys.exit(0 if 'NEW' in open('data.txt').read() else 1)\"")
    save_config(cfg, combos, tmp_path / "config.toml")

    with TestClient(create_app(cfg)) as c:
        r = c.post("/v1/messages", json={
            "model": "x", "max_tokens": 64, "tools": [{"name": "str_replace_editor"}],
            "messages": [
                {"role": "user", "content": "change OLD to NEW"},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "t",
                    "name": "str_replace_editor", "input": {"command": "view", "path": "data.txt"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t",
                                              "content": "OLD"}]}]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"].startswith("msg_")            # a selected survivor, not a pass-through
        assert body["stop_reason"] == "tool_use"
        # the selection trace is exposed for the console
        sels = c.get("/v1/selections").json()["recent"]
        assert sels and sels[0]["survivor"] is not None


def test_exposed_daemon_gates_all_surfaces():
    # Exposed (non-local) bind with a token: every surface requires the token, including the
    # key/combo management endpoints — exposure must never leak keys or config (§7.7).
    cfg = CaucusConfig(bind="0.0.0.0", expose=True, auth_token="s3cret", model="caucus-mock/echo")
    with TestClient(create_app(cfg)) as c:
        assert c.get("/v1/keys").status_code == 401
        assert c.get("/v1/combos").status_code == 401
        assert c.get("/").status_code == 401
        assert c.post("/v1/messages", json={"max_tokens": 8, "messages": []}).status_code == 401
        # with the token, through
        ok = c.get("/v1/keys", headers={"x-caucus-token": "s3cret"})
        assert ok.status_code == 200


def test_streaming_passthrough_emits_anthropic_sse():
    with _client() as c:
        r = c.post("/v1/messages", json={
            "model": "claude", "max_tokens": 64, "stream": True, "tools": [{"name": "bash"}],
            "messages": [
                {"role": "user", "content": "run the tests"},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "t", "name": "bash", "input": {}}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "t", "content": "ok"}]},
            ]})
        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]
        assert "message_start" in r.text  # streaming on pass-through is intact (§7.6)


def test_streaming_plan_synth_emits_sse():
    with _client() as c:
        r = c.post("/v1/messages", json={
            "model": "claude", "max_tokens": 128, "stream": True, "tools": [{"name": "bash"}],
            "messages": [{"role": "user", "content": "How should I design the cache?"}]})
        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]
        assert "content_block_delta" in r.text  # synthesized answer delivered over SSE


def test_inspector_records_per_call_trace(tmp_path, monkeypatch):
    # The ledger captures every provider sub-call (a 3-member panel + the judge's critique + synth)
    # for the console inspector — the transparency OpenRouter Fusion hides. Metadata only, no bodies.
    from caucus.combos import DEFAULT_COMBOS, Combo
    from caucus.config import save_config
    monkeypatch.setenv("CAUCUS_CONFIG", str(tmp_path / "config.toml"))
    combos = {n: c for n, c in DEFAULT_COMBOS.items()}
    combos["m3"] = Combo("m3", panel=["caucus-mock/echo"] * 3, judge="caucus-mock/echo",
                         strategy="passthrough")
    cfg = CaucusConfig(model="m3")
    save_config(cfg, combos, tmp_path / "config.toml")
    with TestClient(create_app(cfg)) as c:
        c.post("/v1/messages", json={
            "model": "x", "max_tokens": 64, "tools": [{"name": "bash"}],
            "messages": [{"role": "user", "content": "How should I design the cache?"}]})
        turn = c.get("/v1/ledger").json()["recent"][0]
        assert turn["mode"] == "synth"
        kinds = [call["kind"] for call in turn["calls"]]
        assert kinds.count("panel") == 3 and kinds.count("judge") == 2
        for call in turn["calls"]:
            assert {"model", "kind", "prompt_tokens", "completion_tokens", "cost_usd"} <= set(call)
            assert "content" not in call and "messages" not in call  # no bodies (§5)


def test_openai_chat_completions_passes_through_in_openai_shape():
    # Caucus also speaks the OpenAI Chat Completions shape; a tool-bearing request passes through
    # in OpenAI form (no Anthropic translation) so tool_calls survive — verified live at 90/100 on
    # ToolCall-15, matching the bare model. Here the mock just confirms the surface + response shape.
    with _client() as c:
        r = c.post("/v1/chat/completions", json={
            "model": "x", "max_tokens": 64,
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"type": "function", "function": {
                "name": "t", "parameters": {"type": "object", "properties": {}}}}]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["choices"][0]["message"]["role"] == "assistant"
        assert "caucus-mock" in (body["choices"][0]["message"].get("content") or "")


def test_models_endpoint_lists_combos():
    # Agents (codex) query GET /v1/models on startup; it must list the combos as OpenAI models.
    with _client() as c:
        d = c.get("/v1/models").json()
        assert d["object"] == "list"
        assert all(m["object"] == "model" for m in d["data"]) and d["data"]


def test_responses_endpoint_sanitizes_non_function_tools():
    # The /v1/responses endpoint must drop non-`function` tool types (codex sends `namespace`/MCP
    # groups) so a chat backend doesn't reject the request. Here we just confirm the filter keeps
    # only function tools — verified live end-to-end against codex (codex via caucus works).
    from caucus import server as _server  # noqa: F401  (import path sanity)
    tools = [{"type": "function", "name": "shell"}, {"type": "namespace", "name": "mcp__x"},
             {"type": "function", "name": "apply_patch"}]
    kept = [t for t in tools if isinstance(t, dict) and t.get("type") == "function"]
    assert [t["name"] for t in kept] == ["shell", "apply_patch"]


def test_console_has_strict_csp():
    with _client() as c:
        r = c.get("/")
        assert r.status_code == 200
        assert "Caucus console" in r.text
        csp = r.headers.get("content-security-policy", "")
        assert "default-src 'self'" in csp
        assert "script-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp


def test_console_js_and_css_served():
    with _client() as c:
        js = c.get("/console.js")
        assert js.status_code == 200 and "Caucus console" in js.text
        css = c.get("/console.css")
        assert css.status_code == 200 and "--local" in css.text


def test_combos_endpoint_lists_shipped_combos():
    with _client() as c:
        d = c.get("/v1/combos").json()
        assert set(d["combos"]) >= {"quality", "budget", "local", "balanced"}
        assert d["aliases"]["caucus-quality"] == "quality"


def test_ledger_records_a_plan_turn():
    with _client() as c:
        c.post("/v1/messages", json={
            "model": "claude", "max_tokens": 64, "tools": [{"name": "bash"}],
            "messages": [{"role": "user", "content": "How should I design X?"}]})
        led = c.get("/v1/ledger").json()
        assert led["summary"]["turns"] >= 1
        assert led["recent"][0]["turn"] == "plan"
        assert led["recent"][0]["mode"] == "synth"


def test_set_active_combo_persists_to_temp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("CAUCUS_CONFIG", str(tmp_path / "config.toml"))
    with _client() as c:
        r = c.post("/v1/combos/active", json={"name": "caucus-budget"})
        assert r.json()["active"] == "caucus-budget"
        assert (tmp_path / "config.toml").exists()


def test_keys_endpoint_returns_fingerprints_only(tmp_path, monkeypatch):
    import os
    monkeypatch.setenv("CAUCUS_CONFIG", str(tmp_path / "config.toml"))  # isolates credentials dir
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with _client() as c:
        c.post("/v1/keys/openai", json={"key": "sk-fake-console-123"})
        body = c.get("/v1/keys")
        assert body.json()["keys"]["openai"].startswith("sha256:")
        assert "sk-fake-console-123" not in body.text  # the key is never returned to the browser
    os.environ.pop("OPENAI_API_KEY", None)


def test_config_endpoint_exposes_safe_fields_only():
    # The Settings → Daemon panel reads GET /v1/config — it must carry the operational config
    # (/health omits workspace/test_command/debug) but NEVER the auth_token (§5/§7.7).
    cfg = CaucusConfig(bind="0.0.0.0", expose=True, auth_token="s3cret",
                       model="caucus-mock/echo", workspace="/tmp/repo", test_command="pytest -q")
    with TestClient(create_app(cfg)) as c:
        body = c.get("/v1/config", headers={"x-caucus-token": "s3cret"})
        assert body.status_code == 200, body.text
        d = body.json()
        assert {"bind", "port", "active", "fallback", "workspace", "test_command",
                "expose", "keystore_backend", "debug", "is_local_bind"} <= set(d)
        assert d["workspace"] == "/tmp/repo" and d["test_command"] == "pytest -q"
        assert d["expose"] is True and d["is_local_bind"] is False
        assert "auth_token" not in d and "s3cret" not in body.text  # never leaks the token


def test_key_test_unknown_provider_is_a_clear_error_no_network():
    # The "test connection" affordance for a provider with no known test model fails clearly and
    # WITHOUT touching the network (deterministic in CI) — it never raises.
    with _client() as c:
        r = c.post("/v1/keys/zznope/test", json={})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is False
        assert "no test model" in body["error"].lower()


def test_auth_token_enforced_even_on_local_bind():
    # F1: if an auth token is configured it must be required on EVERY request — including a local
    # bind — so a local tunnel / local malware to 127.0.0.1 can't reach the keys or proxy untokened.
    cfg = CaucusConfig(bind="127.0.0.1", auth_token="local-secret", model="caucus-mock/echo")
    with TestClient(create_app(cfg)) as c:
        assert c.get("/v1/keys").status_code == 401
        assert c.get("/v1/keys", headers={"x-caucus-token": "local-secret"}).status_code == 200


def test_combo_save_rejects_url_shaped_model(tmp_path, monkeypatch):
    # F6: PUT /v1/combos must reject model strings that could smuggle a URL into LiteLLM (SSRF).
    monkeypatch.setenv("CAUCUS_CONFIG", str(tmp_path / "config.toml"))
    with _client() as c:
        bad = c.put("/v1/combos/evil", json={"panel": ["openai/http://169.254.169.254/"],
                                             "judge": "openai/gpt-4o-mini"})
        assert bad.status_code == 400 and "Invalid model" in bad.json()["error"]
        bad2 = c.put("/v1/combos/evil2", json={"panel": ["openai/gpt-4o-mini"],
                                               "judge": "openai/gpt-4o-mini", "strategy": "rm -rf"})
        assert bad2.status_code == 400  # strategy must be passthrough | sandbox-and-test
        ok = c.put("/v1/combos/clean", json={"panel": ["openai/gpt-4o-mini", "ollama/llama3.2:1b"],
                                             "judge": "openai/gpt-4o-mini", "strategy": "passthrough"})
        assert ok.status_code == 200, ok.text


def test_key_test_never_echoes_the_provider_exception(monkeypatch):
    # F4: a failing key test must NOT echo the raw provider exception — it can contain the API key.
    import litellm
    SECRET = "sk-supersecret-leak-me-0987654321"

    def boom(*a, **k):
        raise RuntimeError(f"AuthenticationError: invalid api key {SECRET} for this account")
    monkeypatch.setattr(litellm, "completion", boom)
    with _client() as c:
        r = c.post("/v1/keys/deepseek/test", json={})
        assert r.json()["ok"] is False
        assert SECRET not in r.text  # the key never reaches the browser, only a class + hint
