"use strict";
// Caucus console — chat-first. Chat with your LOCAL synth (Chat), watch every turn
// (Activity), and configure combos + keys (Settings). Same-origin only (strict CSP); the
// chat streams through the real /v1/messages proxy, so it exercises the actual synth path.
// Nothing is persisted in the browser — chat history lives in memory for this session only.

const $ = (id) => document.getElementById(id);
const api = (p, o) => fetch(p, o).then((r) => r.json());
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

let BASE = "127.0.0.1:8787";
let combos = {}, aliases = {}, selected = "";
let storedKeys = {}, _ledgerTurns = 0;

// ---------- tabs ----------
function showView(v) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("on", t.dataset.view === v));
  document.querySelectorAll(".view").forEach((m) => m.classList.toggle("on", m.id === "view-" + v));
  if (v === "activity") { pollLedger(); pollSelections(); }
  if (v === "logs") { pollLogs(); }
  if (v === "settings") { loadSettings(); }
}
document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => showView(t.dataset.view)));

// ---------- settings sub-tabs (Providers · Combos · Connections) ----------
function showSub(name) {
  document.querySelectorAll("#subtabs .subtab").forEach((t) => t.classList.toggle("on", t.dataset.sub === name));
  document.querySelectorAll("#view-settings .subview").forEach((v) => v.classList.toggle("on", v.id === "sub-" + name));
}
document.querySelectorAll("#subtabs .subtab").forEach((t) =>
  t.addEventListener("click", () => showSub(t.dataset.sub)));

// ---------- health / combos ----------
async function loadHealth() {
  const h = await api("/health");
  $("ver").textContent = "v" + h.version;
  $("bind").textContent = h.bind;
  BASE = h.bind;
  $("baseurl").textContent = "http://" + h.bind;
  selected = h.active && h.active !== "(agent-supplied)" ? h.active : "";
}

function comboModels(c) { return [...(c.panel || []), c.judge].filter(Boolean); }
function providersOf(c) { return new Set(comboModels(c).map((m) => m.split("/")[0])); }
function pill(model) {
  const local = /^(ollama|caucus-mock)\//.test(model);
  return `<span class="pill ${local ? "local" : "cloud"}">${esc(model)}</span>`;
}

async function loadCombos() {
  const d = await api("/v1/combos");
  combos = d.combos; aliases = d.aliases || {};
  const aliasFor = {}; Object.entries(aliases).forEach(([a, n]) => (aliasFor[n] = a));
  if (!selected || !(selected in combos)) selected = Object.keys(combos)[0];

  // chat combo chips
  $("combochips").innerHTML = Object.keys(combos).map((n) =>
    `<span class="chip${n === selected ? " on" : ""}" data-combo="${esc(n)}">${esc(n)}</span>`).join("");
  $("combochips").querySelectorAll(".chip").forEach((ch) =>
    ch.addEventListener("click", () => setActive(ch.dataset.combo)));
  const c = combos[selected] || {};
  $("comboinfo").textContent = (c.panel ? c.panel.length + "× " + (c.panel[0] || "?") : "?") + " → " + (c.judge || "?");

  // settings combo cards — with key-readiness + inline editor
  $("combos").innerHTML = Object.entries(combos).map(([name, cc]) => {
    const active = name === selected;
    const need = missingFor(cc);
    const ready = need.length === 0
      ? `<span class="ready ok">ready</span>`
      : `<span class="ready warn" data-need="${esc(need[0])}" title="add a ${esc(need[0])} key">needs ${esc(need[0])}</span>`;
    return `<div class="combo${active ? " active" : ""}" data-combo="${esc(name)}">
      <div class="head"><div><span class="name">${esc(name)}</span>
        ${aliasFor[name] ? `<span class="alias"> · ${esc(aliasFor[name])}</span>` : ""}</div>
        <div class="btns">${ready}
          <button class="dupbtn" data-dup="${esc(name)}">duplicate</button>
          <button class="editbtn" data-edit="${esc(name)}">edit</button>
          <button class="use" data-name="${esc(name)}" ${active ? "disabled" : ""}>${active ? "active" : "use"}</button></div></div>
      <div class="role"><label>panel</label> ${(cc.panel || []).map(pill).join(" ")}</div>
      <div class="role"><label>judge</label> ${pill(cc.judge)}</div>
      <div class="role"><label>strategy</label> <span class="alias">${esc(cc.strategy)}</span></div>
      <div class="editslot"></div></div>`;
  }).join("");
  $("combos").querySelectorAll("button.use").forEach((b) =>
    b.addEventListener("click", () => setActive(b.dataset.name)));
  $("combos").querySelectorAll(".ready.warn").forEach((b) => b.addEventListener("click", () => jumpToKey(b.dataset.need)));
  $("combos").querySelectorAll("[data-edit]").forEach((b) =>
    b.addEventListener("click", () => openComboEditor(b.dataset.edit, combos[b.dataset.edit], false)));
  $("combos").querySelectorAll("[data-dup]").forEach((b) => b.addEventListener("click", () => {
    const s = combos[b.dataset.dup];
    openComboEditor(b.dataset.dup + "-copy", { panel: [...(s.panel || [])], judge: s.judge, strategy: s.strategy }, true);
  }));
  checkKeyForCombo();
}

function missingFor(c) {
  return [...providersOf(c)].filter((p) => p !== "ollama" && p !== "caucus-mock" && !(p in storedKeys));
}

function checkKeyForCombo() {
  const c = combos[selected]; if (!c) return;
  const need = [...providersOf(c)].filter((p) => p !== "ollama" && p !== "caucus-mock" && !(p in storedKeys));
  const el = $("needkey");
  if (need.length) {
    el.hidden = false;
    el.innerHTML = `Add a <b>${esc(need[0])}</b> key in <a href="#" id="gokeys" style="color:var(--warn)">Settings → Providers</a> to chat with the “${esc(selected)}” combo.`;
    const g = $("gokeys"); if (g) g.addEventListener("click", (e) => { e.preventDefault(); showView("settings"); showSub("providers"); });
  } else { el.hidden = true; }
}

// ---------- chat ----------
let chats = [], current = null;
function newChat() {
  current = { id: Date.now() + "" + Math.floor(Math.random() * 1e4), title: "New chat", messages: [] };
  chats.unshift(current); renderHistory(); renderTranscript();
}
function renderHistory() {
  $("history").innerHTML = chats.map((c) =>
    `<div class="h${c === current ? " on" : ""}" data-id="${c.id}">${esc(c.title)}</div>`).join("");
  $("history").querySelectorAll(".h").forEach((h) => h.addEventListener("click", () => {
    current = chats.find((c) => c.id === h.dataset.id); renderHistory(); renderTranscript();
  }));
}
function renderTranscript() {
  const t = $("transcript");
  if (!current || !current.messages.length) {
    t.innerHTML = `<div class="empty"><h2>Chat with your synth</h2>
      <p>A panel of models answers and a judge synthesizes them into one — on your machine, with your keys.
      Pick a combo above and ask anything.</p><div class="suggest"></div></div>`;
    const sug = ["Explain the trade-offs between optimistic and pessimistic locking.",
      "What's the best way to add caching to a REST API?", "Outline a migration from REST to gRPC."];
    t.querySelector(".suggest").innerHTML = sug.map((s) => `<button>${esc(s)}</button>`).join("");
    t.querySelectorAll(".suggest button").forEach((b) =>
      b.addEventListener("click", () => { $("input").value = b.textContent; send(); }));
    return;
  }
  t.innerHTML = current.messages.map((m) => msgHtml(m.role, m.content)).join("");
  t.scrollTop = t.scrollHeight;
}
function msgHtml(role, body, streaming) {
  const who = role === "user" ? "you" : "synth";
  const inner = streaming ? `<span class="synthing"><span class="spin"></span>synthesizing…</span>` : esc(body);
  return `<div class="msg ${role}"><div class="who">${who}</div>
    <div class="bubble"><div class="body">${inner}</div></div></div>`;
}

async function send() {
  const text = $("input").value.trim();
  if (!text || !current) return;
  $("input").value = ""; autosize();
  current.messages.push({ role: "user", content: text });
  if (current.title === "New chat") { current.title = text.slice(0, 40); renderHistory(); }
  renderTranscript();

  // append a streaming assistant bubble
  const t = $("transcript");
  t.insertAdjacentHTML("beforeend", msgHtml("assistant", "", true));
  const bodyEl = t.lastElementChild.querySelector(".body");
  t.scrollTop = t.scrollHeight;
  $("send").disabled = true;

  let acc = "", first = true;
  try {
    await streamChat(current.messages, selected, (delta) => {
      if (first) { bodyEl.innerHTML = ""; first = false; }
      acc += delta; bodyEl.innerHTML = esc(acc) + '<span class="cursor"></span>';
      t.scrollTop = t.scrollHeight;
    });
    bodyEl.innerHTML = esc(acc) || "(empty response)";
  } catch (err) {
    bodyEl.innerHTML = `<span style="color:var(--err)">Error: ${esc(err.message || err)}</span> — is a key set for this combo? (Settings → Providers)`;
    acc = "";
  }
  current.messages.push({ role: "assistant", content: acc });
  $("send").disabled = false;
  if (current.tab === undefined) pollLedger(); // refresh activity counters lazily
}

async function streamChat(messages, combo, onDelta) {
  const r = await fetch("/v1/messages", { method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ model: combo, max_tokens: 1024, stream: true,
      messages: messages.map((m) => ({ role: m.role, content: m.content })) }) });
  if (!r.ok) { let m = "HTTP " + r.status; try { m = (await r.json()).error.message || m; } catch (_) {} throw new Error(m); }
  const reader = r.body.getReader(), dec = new TextDecoder(); let buf = "";
  for (;;) {
    const { done, value } = await reader.read(); if (done) break;
    buf += dec.decode(value, { stream: true });
    let i; while ((i = buf.indexOf("\n\n")) >= 0) {
      const block = buf.slice(0, i); buf = buf.slice(i + 2);
      const line = block.split("\n").find((l) => l.startsWith("data:"));
      if (!line) continue;
      try {
        const ev = JSON.parse(line.slice(5).trim());
        if (ev.type === "content_block_delta" && ev.delta && ev.delta.type === "text_delta") onDelta(ev.delta.text);
      } catch (_) {}
    }
  }
}

function autosize() { const i = $("input"); i.style.height = "auto"; i.style.height = Math.min(200, i.scrollHeight) + "px"; }
$("input").addEventListener("input", autosize);
$("input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });
$("composer").addEventListener("submit", (e) => { e.preventDefault(); send(); });
$("newchat").addEventListener("click", newChat);

// ---------- settings §1: providers / keys ----------
const KEY_PROVIDERS = ["deepseek", "openai", "anthropic", "openrouter", "gemini", "groq",
  "mistral", "nvidia_nim", "together", "fireworks", "xai", "perplexity"];
function populateProviders() {
  const sel = $("kp"); if (!sel || sel.options.length) return;
  sel.innerHTML = KEY_PROVIDERS.map((p) => `<option value="${p}">${p}</option>`).join("");
}
async function loadKeys() {
  populateProviders();
  const d = await api("/v1/keys");
  storedKeys = d.keys || {};
  const entries = Object.entries(storedKeys);
  $("keys").innerHTML = entries.length
    ? entries.map(([p, fp]) => `<div class="keyrow"><span class="prov">${esc(p)}</span>` +
        `<span class="fp">${esc(fp)}</span><span class="kstat" data-stat="${esc(p)}"></span>` +
        `<span><button class="kbtn" data-test="${esc(p)}">test</button>` +
        `<button class="kbtn" data-forget="${esc(p)}">forget</button></span></div>`).join("")
    : `<p class="muted">No keys yet (${esc(d.backend)} store) — add one below. BYOK; keys never leave this machine.</p>`;
  $("keys").querySelectorAll("[data-forget]").forEach((b) => b.addEventListener("click", async () => {
    await fetch("/v1/keys/" + encodeURIComponent(b.dataset.forget), { method: "DELETE" });
    await loadKeys(); loadCombos();
  }));
  $("keys").querySelectorAll("[data-test]").forEach((b) =>
    b.addEventListener("click", () => testKey(b.dataset.test)));
  checkKeyForCombo(); renderChecklist();
}
async function testKey(p) {
  const st = $("keys").querySelector('.kstat[data-stat="' + p + '"]'); if (!st) return;
  st.className = "kstat pend"; st.textContent = "testing…"; st.title = "";
  try {
    const r = await api("/v1/keys/" + encodeURIComponent(p) + "/test",
      { method: "POST", headers: { "content-type": "application/json" }, body: "{}" });
    if (r.ok) { st.className = "kstat ok"; st.textContent = "✓ " + (r.ms != null ? r.ms + " ms" : "ok"); st.title = r.model || ""; }
    else { st.className = "kstat bad"; st.textContent = "✗ failed"; st.title = r.error || ""; }
  } catch (_) { st.className = "kstat bad"; st.textContent = "✗ error"; }
}
function keyStatus(msg, kind) {
  const el = $("keystatus"); if (!el) return;
  el.textContent = msg;
  el.style.color = kind === "ok" ? "var(--local)" : kind === "err" ? "var(--err)" : "var(--faint)";
}
function jumpToKey(provider) {
  populateProviders();
  showSub("providers");
  if (provider && KEY_PROVIDERS.includes(provider)) $("kp").value = provider;
  $("kv").focus();
  if (provider) keyStatus("Paste your " + provider + " key, then Save.", "");
}
$("keyform").addEventListener("submit", async (e) => {
  e.preventDefault();
  const p = $("kp").value, v = $("kv").value;
  if (!v) { keyStatus("Paste a key for " + p + ".", "err"); return; }
  keyStatus("Saving…", "");
  try {
    const r = await fetch("/v1/keys/" + encodeURIComponent(p), { method: "POST",
      headers: { "content-type": "application/json" }, body: JSON.stringify({ key: v }) });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const data = await r.json();
    if (!data.ok) throw new Error(data.error || "save rejected");
    $("kv").value = "";
    keyStatus("Saved " + p + " · " + (data.fingerprint || "stored locally") + " — testing…", "ok");
    await loadKeys(); loadCombos(); testKey(p);
  } catch (err) {
    keyStatus("Could not save: " + (err && err.message || err), "err");
  }
});

// ---------- settings §2: combo editor (uses the existing PUT /v1/combos/{name}) ----------
function openComboEditor(name, cc, isNew) {
  const target = isNew ? $("newcomboslot")
    : $("combos").querySelector('.combo[data-combo="' + name + '"] .editslot');
  if (!target) return;
  target.innerHTML = `<div class="comboedit">
    <div><label>name</label><input class="ce-name" value="${esc(name)}"${isNew ? "" : " readonly"}></div>
    <div><label>panel — one model per line, as provider/model</label>
      <textarea class="ce-panel" rows="3" spellcheck="false">${esc((cc.panel || []).join("\n"))}</textarea></div>
    <div class="row2">
      <div class="erow"><label>judge</label><input class="ce-judge" spellcheck="false" value="${esc(cc.judge || "")}"></div>
      <div class="erow"><label>strategy</label><select class="ce-strategy">
        <option value="passthrough"${cc.strategy === "passthrough" ? " selected" : ""}>passthrough</option>
        <option value="sandbox-and-test"${cc.strategy === "sandbox-and-test" ? " selected" : ""}>sandbox-and-test</option>
      </select></div></div>
    <div class="ehint">panel = models that answer in parallel · judge = the model that synthesizes ·
      <code>sandbox-and-test</code> needs a workspace + test command (set at launch).</div>
    <div class="editactions"><button class="savebtn" type="button">Save combo</button>
      <button class="cancelbtn" type="button">Cancel</button></div></div>`;
  const ed = target.querySelector(".comboedit");
  ed.querySelector(".cancelbtn").addEventListener("click", () => { target.innerHTML = ""; });
  ed.querySelector(".savebtn").addEventListener("click", async () => {
    const nm = (ed.querySelector(".ce-name").value || name).trim();
    if (!nm) return;
    const panel = ed.querySelector(".ce-panel").value.split("\n").map((s) => s.trim()).filter(Boolean);
    const judge = ed.querySelector(".ce-judge").value.trim();
    const strategy = ed.querySelector(".ce-strategy").value;
    await fetch("/v1/combos/" + encodeURIComponent(nm), { method: "PUT",
      headers: { "content-type": "application/json" }, body: JSON.stringify({ panel, judge, strategy }) });
    target.innerHTML = ""; await loadCombos();
  });
}
$("newcombo").addEventListener("click", () =>
  openComboEditor("my-combo", { panel: [], judge: "", strategy: "passthrough" }, true));

// ---------- settings §3: connect an agent ----------
let _connectVariant = "claude";
function connectSnippet(v, base) {
  const url = "http://" + base;
  if (v === "claude") return { snip: "export ANTHROPIC_BASE_URL=" + url,
    hint: "Claude Code, or any agent that speaks the Anthropic /v1/messages wire format. Caucus convenes the panel; the agent only ever sees one model." };
  if (v === "codex") return { snip:
    "# ~/.codex/config.toml\n[model_providers.caucus]\nbase_url = \"" + url + "/v1\"\nwire_api = \"responses\"\nenv_key = \"CAUCUS_KEY\"\n\n# then:  CAUCUS_KEY=x codex --config model_provider=caucus",
    hint: "codex speaks the OpenAI Responses API — Caucus serves /v1/responses with tool passthrough. Any non-empty key works; Caucus authenticates upstream with your stored provider keys." };
  if (v === "aider") return { snip:
    "export OPENAI_API_BASE=" + url + "/v1\nexport OPENAI_API_KEY=caucus   # any non-empty value\naider --model caucus-quality",
    hint: "aider via the OpenAI Chat Completions endpoint (/v1/chat/completions); tool calls pass through intact." };
  return { snip: "export OPENAI_BASE_URL=" + url + "/v1\nexport OPENAI_API_KEY=caucus   # any non-empty value",
    hint: "Any OpenAI-compatible client: point its base URL at " + url + "/v1 with any non-empty key. Caucus authenticates upstream with your stored provider keys, not this one." };
}
function renderConnect() {
  const tabs = [["claude", "Claude Code"], ["codex", "Codex"], ["aider", "aider"], ["openai", "OpenAI / generic"]];
  $("agenttabs").innerHTML = tabs.map(([k, l]) =>
    `<button class="vtab${k === _connectVariant ? " on" : ""}" data-v="${k}" type="button">${esc(l)}</button>`).join("");
  $("agenttabs").querySelectorAll(".vtab").forEach((b) =>
    b.addEventListener("click", () => { _connectVariant = b.dataset.v; renderConnect(); }));
  const { snip, hint } = connectSnippet(_connectVariant, BASE);
  $("agentsnippet").textContent = snip;
  $("agenthint").textContent = hint;
}
function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) return navigator.clipboard.writeText(text);
  return new Promise((res) => {
    const ta = document.createElement("textarea"); ta.value = text;
    ta.style.position = "fixed"; ta.style.opacity = "0"; document.body.appendChild(ta);
    ta.select(); try { document.execCommand("copy"); } catch (_) {} document.body.removeChild(ta); res();
  });
}
$("agentcopy").addEventListener("click", async () => {
  await copyText($("agentsnippet").textContent);
  const b = $("agentcopy"); b.textContent = "copied"; b.classList.add("done");
  setTimeout(() => { b.textContent = "copy"; b.classList.remove("done"); }, 1400);
});

// ---------- settings §4: daemon (read-only status + the exact commands to change it) ----------
async function loadConfig() {
  let c; try { c = await api("/v1/config"); } catch (_) { return; }
  const eng = c.engine || {};
  const tile = (l, v, cls) => `<div class="tile"><div class="tl">${esc(l)}</div>` +
    `<div class="tv${cls ? " " + cls : ""}">${esc(v)}</div></div>`;
  $("daemontiles").innerHTML =
    tile("bind", c.bind + ":" + c.port) +
    tile("network", c.is_local_bind ? "local only" : "exposed", c.is_local_bind ? "off" : "on") +
    tile("active combo", c.active || "(agent-supplied)") +
    tile("fallback", c.fallback || "none") +
    tile("engine", (eng.engine || "?") + (eng.reachable ? " · ok" : " · down"), eng.reachable ? "on" : "off") +
    tile("debug logs", c.debug ? "on" : "off", c.debug ? "on" : "off") +
    tile("keystore", c.keystore_backend) +
    tile("workspace", c.workspace || "—") +
    tile("test command", c.test_command || "—");
  const portArg = c.port !== 8787 ? " --port " + c.port : "";
  const cmd = (label, code) => `<div class="daemoncmd"><span class="dl">${esc(label)}</span><code>${esc(code)}</code></div>`;
  $("daemoncmds").innerHTML =
    cmd(c.debug ? "Full trace is ON — turn off:" : "Stream the full LiteLLM + synth trace:",
        c.debug ? ("caucus start" + portArg) : ("caucus start --debug" + portArg)) +
    cmd("Switch the active combo:", "caucus combo use <name>") +
    cmd("Expose on the network:", "caucus start --expose --auth-token <token> --bind 0.0.0.0") +
    cmd("Stop the daemon:", "caucus stop" + portArg);
}

// ---------- settings: setup checklist + orchestration ----------
function renderChecklist() {
  const setup = $("setup"); if (!setup) return;
  const hasKey = Object.keys(storedKeys).length > 0;
  const comboReady = hasKey && missingFor(combos[selected] || {}).length === 0;
  const items = [[hasKey, "Add a provider key"],
    [comboReady, "Choose a combo whose keys are all set"],
    [_ledgerTurns > 0, "Run your first turn — chat here, or connect an agent"]];
  if (items.every((i) => i[0])) { setup.hidden = true; return; }
  setup.hidden = false;
  $("checklist").innerHTML = items.map(([ok, label]) =>
    `<li class="${ok ? "done" : ""}"><span class="box">${ok ? "✓" : ""}</span><span>${esc(label)}</span></li>`).join("");
}
async function setActive(name) {
  try {
    await fetch("/v1/combos/active", { method: "POST",
      headers: { "content-type": "application/json" }, body: JSON.stringify({ name }) });
  } catch (_) {}
  selected = name; await loadCombos(); renderChecklist();
}
async function loadSettings() {
  await loadKeys();
  await loadCombos();
  loadConfig();
  renderConnect();
  renderChecklist();
}

// ---------- activity ----------
function usd(n) { return n ? "$" + n.toFixed(6) : "$0"; }
function renderCalls(tn) {
  const calls = tn.calls || [];
  const rows = calls.map((c) => {
    const cls = c.kind.indexOf("panel") === 0 ? "cloud" : "judge";
    return `<div class="callrow"><span class="ck ${cls}">${esc(c.kind)}</span>
      <span class="cm">${esc(c.model)}</span>
      <span class="cn">${c.prompt_tokens}&rarr;${c.completion_tokens} tok · ${usd(c.cost_usd)}</span></div>`;
  }).join("");
  const outTok = calls.reduce((a, c) => a + (c.completion_tokens || 0), 0);
  const cost = calls.reduce((a, c) => a + (c.cost_usd || 0), 0);
  return `<div class="callhdr">${calls.length} provider calls — every model, every cent (nothing hidden)</div>
    ${rows}<div class="calltot">Σ ${calls.length} calls · ${outTok} out tok · ${usd(cost)}</div>`;
}
let _openTurns = new Set();
async function pollLedger() {
  try {
    const d = await api("/v1/ledger");
    const s = d.summary;
    if (s.turns !== _ledgerTurns) { _ledgerTurns = s.turns; renderChecklist(); }
    $("rollup").innerHTML =
      `<div class="stat"><b>${s.turns}</b><span>turns</span></div>
       <div class="stat"><b>${s.total_tokens.toLocaleString()}</b><span>tokens</span></div>
       <div class="stat"><b>${usd(s.total_cost_usd)}</b><span>est. cost</span></div>`;
    if (!d.recent.length) return;
    $("stream").innerHTML = d.recent.map((tn) => {
      const n = (tn.calls || []).length;
      const open = _openTurns.has(tn.rid);
      return `<div class="turn ${tn.turn}">
        <div class="turnhead" data-rid="${tn.rid}">
          <span class="tag">${tn.turn}</span>
          <div class="tb"><div class="mode">${esc(tn.mode)}${n ? ` · <span class="callcount">${n} calls</span>` : ""}</div>
            <div class="meta">${esc(tn.combo)} · ${esc(tn.model)}</div></div>
          <div class="nums"><b>${tn.tokens}</b> tok · ${usd(tn.cost_usd)}<br>${tn.ms} ms</div>
          ${n ? `<span class="exp">${open ? "▴" : "▾"}</span>` : "<span></span>"}
        </div>
        <div class="turndetail"${open ? "" : " hidden"}>${n ? renderCalls(tn) : ""}</div>
      </div>`;
    }).join("");
    $("stream").querySelectorAll(".turnhead").forEach((h) => h.addEventListener("click", () => {
      const rid = h.dataset.rid, det = h.nextElementSibling;
      if (!det || !det.classList.contains("turndetail") || !det.innerHTML.trim()) return;
      det.hidden = !det.hidden;
      if (det.hidden) _openTurns.delete(rid); else _openTurns.add(rid);
      h.querySelector(".exp").textContent = det.hidden ? "▾" : "▴";
    }));
  } catch (_) {}
}
async function pollSelections() {
  try {
    const d = await api("/v1/selections");
    if (!d.recent || !d.recent.length) return;
    $("selections").innerHTML = d.recent.map((s) => {
      const cands = s.results.map((r) => {
        const cls = r.passed ? "local" : r.applied ? "cloud" : "";
        const mark = r.passed ? "✓ pass" : r.applied ? "✗ fail" : "— no edit";
        return `<span class="pill ${cls}">#${r.index} ${mark}${r.index === s.survivor ? " ★" : ""}</span>`;
      }).join(" ");
      return `<div class="combo"><div class="head"><span class="name">${esc(s.combo)}</span>
        <span class="alias">${esc(s.reason)}</span></div><div class="role" style="margin-top:6px">${cands}</div></div>`;
    }).join("");
  } catch (_) {}
}
setInterval(() => { if ($("view-activity").classList.contains("on")) { pollLedger(); pollSelections(); } }, 2500);

// ---------- logs (live LiteLLM + the synth (MOA) trace) ----------
let _logSince = 0, _logFilter = "all", _logPaused = false, _logFirst = true, _logSearch = "";
function logSrcClass(s) { return "lsrc-" + (s === "litellm" || s === "synth" ? s : "caucus"); }
function logTime(ts) { return new Date(ts * 1000).toTimeString().slice(0, 8); }
function logMatch(el) {
  if (_logFilter !== "all" && el.dataset.src !== _logFilter) return false;
  const q = _logSearch.trim();
  if (!q) return true;
  const text = el.dataset.text || "";
  if (q.length > 2 && q[0] === "/" && q[q.length - 1] === "/") {
    try { return new RegExp(q.slice(1, -1), "i").test(text); } catch (_) { return true; }
  }
  return text.includes(q.toLowerCase());
}
function applyLogVis() {
  let shown = 0, total = 0;
  document.querySelectorAll("#logview .logline").forEach((el) => {
    total++; const ok = logMatch(el); el.hidden = !ok; if (ok) shown++;
  });
  if ($("logstate")) $("logstate").textContent =
    (_logPaused ? "paused · " : "") + (_logSearch.trim() ? shown + " / " + total : total + " lines");
}
async function pollLogs() {
  try {
    const r = await fetch("/v1/logs?since=" + _logSince + "&limit=600");
    if (!r.ok) return;
    const data = await r.json();
    const view = $("logview"), hint = $("loghint");
    if (hint) {
      hint.hidden = !!data.debug;
      if (!data.debug) hint.innerHTML = "Showing <b>metadata only</b> — restart with " +
        "<code>caucus start --debug</code> to stream the full LiteLLM + synth (MOA) trace.";
    }
    const lines = data.lines || [];
    if (lines.length) {
      if (_logFirst) { view.innerHTML = ""; _logFirst = false; }
      _logSince = lines[lines.length - 1].seq;
      view.insertAdjacentHTML("beforeend", lines.map((l) =>
        `<div class="logline ${logSrcClass(l.source)}" data-src="${esc(l.source)}" ` +
        `data-text="${esc((l.source + " " + l.level + " " + l.msg).toLowerCase())}">` +
        `<span class="lts">${logTime(l.ts)}</span>` +
        `<span class="lsrc">${esc(l.source)}</span>` +
        `<span class="llvl lv-${esc(l.level)}">${esc(l.level)}</span>` +
        `<span class="lmsg">${esc(l.msg)}</span></div>`).join(""));
      while (view.children.length > 1500) view.removeChild(view.firstChild);
      applyLogVis();
      if (!_logPaused) view.scrollTop = view.scrollHeight;
    }
  } catch (_) {}
}
document.querySelectorAll(".logf").forEach((b) => b.addEventListener("click", () => {
  _logFilter = b.dataset.src;
  document.querySelectorAll(".logf").forEach((x) => x.classList.toggle("on", x === b));
  applyLogVis();
}));
$("logsearch").addEventListener("input", (e) => { _logSearch = e.target.value; applyLogVis(); });
$("logpause").addEventListener("click", () => {
  _logPaused = !_logPaused;
  $("logpause").textContent = _logPaused ? "▶ resume" : "⏸ pause";
  $("logpause").classList.toggle("on", _logPaused);
  applyLogVis();
  if (!_logPaused) { const v = $("logview"); v.scrollTop = v.scrollHeight; }
});
$("logclear").addEventListener("click", async () => {
  try { await fetch("/v1/logs", { method: "DELETE" }); } catch (_) {}
  $("logview").innerHTML = ""; _logSince = 0; _logFirst = true; applyLogVis();
});
setInterval(() => { if ($("view-logs").classList.contains("on")) pollLogs(); }, 1500);

// ---------- theme (light = cream default, dark = navy) ----------
function applyTheme(t) {
  document.documentElement.setAttribute("data-theme", t);
  try { localStorage.setItem("caucus-theme", t); } catch (_) {}
  $("themetoggle").textContent = t === "dark" ? "☀" : "☾";
}
(function initTheme() {
  let t; try { t = localStorage.getItem("caucus-theme"); } catch (_) {}
  if (!t) t = matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  applyTheme(t);
  $("themetoggle").addEventListener("click", () =>
    applyTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark"));
})();

// ---------- first-run onboarding (Welcome → Key → Council → Chat) ----------
const BRANDMARK = '<svg viewBox="0 0 64 64" width="42" height="42" fill="currentColor">' +
  '<g stroke="currentColor" stroke-width="3.4" stroke-linecap="round" opacity=".34" fill="none">' +
  '<line x1="32" y1="32" x2="32" y2="13"/><line x1="32" y1="32" x2="15" y2="49"/>' +
  '<line x1="32" y1="32" x2="49" y2="49"/></g><circle cx="32" cy="13" r="6"/>' +
  '<circle cx="15" cy="49" r="6"/><circle cx="49" cy="49" r="6"/><circle cx="32" cy="32" r="11"/></svg>';
// A sensible cheap default model per provider — used to mint a simple single-model combo from the
// key the user just added, so the Council step always has a "ready" option (the default combos each
// need several providers). Mirrors the server's test models.
const SOLO_MODELS = { deepseek: "deepseek/deepseek-chat", openai: "openai/gpt-4o-mini",
  anthropic: "anthropic/claude-3-5-haiku-latest", gemini: "gemini/gemini-2.5-flash",
  openrouter: "openrouter/openai/gpt-4o-mini", groq: "groq/llama-3.1-8b-instant",
  mistral: "mistral/mistral-small-latest", nvidia_nim: "nvidia_nim/meta/llama-3.1-8b-instruct",
  together: "together_ai/meta-llama/Llama-3-8b-chat-hf", xai: "xai/grok-2-latest",
  fireworks: "fireworks_ai/accounts/fireworks/models/llama-v3p1-8b-instruct",
  perplexity: "perplexity/llama-3.1-sonar-small-128k-online" };
let _obStep = 0, _obMock = false;

function obClose(done) {
  $("onboard").hidden = true;
  if (done) { try { localStorage.setItem("caucus-onboarded", "1"); } catch (_) {} }
}
async function obShow() {
  _obStep = 0; _obMock = false;
  await loadKeys().catch(() => {});   // reflect any already-set keys (e.g. added via CLI)
  $("onboard").hidden = false;
  renderOnboard();
}
function obHasKey() { return Object.keys(storedKeys).length > 0 || _obMock; }

async function renderOnboard() {
  const body = $("onboardbody");
  if (_obStep === 0) {
    body.innerHTML = '<div class="ob-hero"><span class="ob-mark">' + BRANDMARK + '</span>' +
      '<h2 class="ob-h">Welcome to Caucus</h2>' +
      '<p class="ob-p">Your local <b>council of models</b>. Ask a hard question and a <b>panel</b> of ' +
      'models answers in parallel; a <b>judge</b> synthesizes them into one. It all runs on your ' +
      'machine, with your keys — nothing leaves.</p>' +
      '<div class="ob-flow"><span class="node">panel</span><span class="arrow">→</span>' +
      '<span class="node">judge</span><span class="arrow">→</span><span class="node">one answer</span></div></div>';
    renderObNav();
  } else if (_obStep === 1) {
    const keys = Object.keys(storedKeys);
    body.innerHTML = '<div><h2 class="ob-section-h">Bring a key</h2>' +
      '<p class="ob-p">Caucus uses <b>your own</b> provider keys — stored locally in ' +
      '<code>~/.config/caucus/.env</code>, sent only to the provider, never anywhere else.</p>' +
      (keys.length ? '<p class="ob-have">✓ already set: ' + keys.map(esc).join(", ") + '</p>' : "") +
      '<form class="keyform" id="obkeyform" autocomplete="off">' +
      '<select class="provselect" id="obkp" aria-label="Provider"></select>' +
      '<input id="obkv" type="password" placeholder="paste API key" autocomplete="off">' +
      '<button type="submit">Add &amp; test</button></form>' +
      '<div class="note" id="obkeystatus" role="status" aria-live="polite"></div>' +
      '<button class="ob-skip" id="obmock" type="button">No key yet? Just try it with a keyless mock →</button></div>';
    $("obkp").innerHTML = KEY_PROVIDERS.map((p) => '<option>' + p + '</option>').join("");
    $("obkeyform").addEventListener("submit", obSaveKey);
    $("obmock").addEventListener("click", async () => {
      _obMock = true; await obSetActive("caucus-mock/echo"); obClose(true); loadCombos();
    });
    renderObNav();
  } else {
    body.innerHTML = '<div><h2 class="ob-section-h">Choose your council</h2>' +
      '<p class="ob-p">A <b>combo</b> is a panel + a judge. Pick one to start — you can edit panels ' +
      'and judges any time in Settings → Combos.</p>' +
      '<div class="ob-combos" id="obcombos"><p class="muted">setting up…</p></div></div>';
    renderObNav();
    await obEnsureReady();   // mint a solo combo from the new key if nothing else is ready
    // Default to a ready combo BACKED BY A KEY the user actually has (prefer that over a keyless
    // ollama combo, which reads "ready" but needs Ollama running).
    const isReady = (n) => combos[n] && missingFor(combos[n]).length === 0;
    const usesKey = (n) => [...providersOf(combos[n])].some((p) => p in storedKeys);
    if (!isReady(selected)) {
      const names = Object.keys(combos);
      selected = names.find((n) => isReady(n) && usesKey(n)) || names.find(isReady) || names[0] || selected;
    }
    renderObCombos();
  }
}

async function obEnsureReady() {
  if (Object.keys(combos).some((n) => missingFor(combos[n]).length === 0)) return;
  const p = Object.keys(storedKeys).find((x) => SOLO_MODELS[x]);
  if (!p) return;
  await fetch("/v1/combos/" + encodeURIComponent(p), { method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ panel: [SOLO_MODELS[p]], judge: SOLO_MODELS[p], strategy: "passthrough" }) }).catch(() => {});
  await loadCombos();
}

function renderObNav() {
  const dots = '<div class="ob-dots">' + [0, 1, 2].map((i) =>
    '<span class="ob-dot' + (i === _obStep ? " on" : "") + '"></span>').join("") + '</div>';
  let btns;
  if (_obStep === 0) btns = '<button class="ob-skip" id="obskip" type="button">Skip</button>' +
    '<button class="ob-next" id="obnext" type="button">Set it up →</button>';
  else if (_obStep === 1) btns = '<button class="ob-back" id="obback" type="button">Back</button>' +
    '<button class="ob-next" id="obnext" type="button"' + (obHasKey() ? "" : " disabled") + '>Next →</button>';
  else btns = '<button class="ob-back" id="obback" type="button">Back</button>' +
    '<button class="ob-next" id="obnext" type="button">Start chatting →</button>';
  $("onboardnav").innerHTML = dots + '<div class="ob-btns">' + btns + '</div>';
  const n = $("obnext"); if (n) n.addEventListener("click", obNext);
  const bk = $("obback"); if (bk) bk.addEventListener("click", () => { _obStep--; renderOnboard(); });
  const sk = $("obskip"); if (sk) sk.addEventListener("click", () => obClose(true));
}

function obNext() {
  if (_obStep < 2) { _obStep++; renderOnboard(); }
  else { obClose(true); loadCombos(); }   // finish → drop into chat with the chosen combo
}

async function obSaveKey(e) {
  e.preventDefault();
  const p = $("obkp").value, v = $("obkv").value, st = $("obkeystatus");
  const set = (m, c) => { st.textContent = m; st.style.color = c; };
  if (!v) { set("Paste a key for " + p + ".", "var(--err)"); return; }
  set("Saving + testing…", "var(--faint)");
  try {
    const r = await fetch("/v1/keys/" + encodeURIComponent(p), { method: "POST",
      headers: { "content-type": "application/json" }, body: JSON.stringify({ key: v }) });
    const d = await r.json(); if (!d.ok) throw new Error(d.error || "rejected");
    await loadKeys();
    const t = await api("/v1/keys/" + encodeURIComponent(p) + "/test",
      { method: "POST", headers: { "content-type": "application/json" }, body: "{}" });
    if (t.ok) set("✓ " + p + " works (" + (t.ms != null ? t.ms + " ms" : "ok") + ")", "var(--local)");
    else set("Saved — but the live test failed: " + (t.error || ""), "var(--warn)");
    $("obkv").value = "";
    renderObNav();   // obHasKey() is now true → Next enabled
  } catch (err) { set("Could not save: " + (err && err.message || err), "var(--err)"); }
}

function renderObCombos() {
  // ready + key-backed combos first, then ready, then the rest — so the usable picks lead.
  const keyBacked = (cc) => [...providersOf(cc)].some((p) => p in storedKeys);
  const entries = Object.entries(combos).sort((a, b) => {
    const ra = missingFor(a[1]).length === 0, rb = missingFor(b[1]).length === 0;
    if (ra !== rb) return ra ? -1 : 1;
    const ka = keyBacked(a[1]), kb = keyBacked(b[1]);
    if (ka !== kb) return ka ? -1 : 1;
    return 0;
  });
  $("obcombos").innerHTML = entries.map(([name, cc]) => {
    const miss = missingFor(cc), ready = miss.length === 0, on = name === selected;
    const badge = ready ? '<span class="ob-rdy">ready</span>' : '<span class="ob-need">needs ' + esc(miss[0]) + '</span>';
    return '<button class="ob-combo' + (on ? " on" : "") + (ready ? "" : " off") + '" data-name="' +
      esc(name) + '" type="button"' + (ready ? "" : " disabled") + '>' +
      '<div class="ob-combo-h"><b>' + esc(name) + '</b>' + badge + '</div>' +
      '<div class="ob-combo-m">' + (cc.panel || []).length + '× panel → ' + esc(cc.judge || "?") + '</div></button>';
  }).join("");
  $("obcombos").querySelectorAll(".ob-combo:not([disabled])").forEach((b) =>
    b.addEventListener("click", async () => { await obSetActive(b.dataset.name); renderObCombos(); }));
}
async function obSetActive(name) {
  try { await fetch("/v1/combos/active", { method: "POST",
    headers: { "content-type": "application/json" }, body: JSON.stringify({ name }) }); } catch (_) {}
  selected = name;
}

$("onboardx").addEventListener("click", () => obClose(true));
$("tourbtn").addEventListener("click", obShow);
function obMaybeShow() {
  let seen; try { seen = localStorage.getItem("caucus-onboarded"); } catch (_) {}
  if (!seen) obShow();
}

// ---------- boot ----------
(async () => { await loadHealth(); await loadCombos(); await loadKeys(); newChat(); obMaybeShow(); })();
