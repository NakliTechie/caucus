# Caucus console — a guided walkthrough

The console is the local web UI Caucus serves in-process at `http://127.0.0.1:8787/`. This is a
narrated tour of every surface: **what you see → what it means → what to do next.** It's served
same-origin under a strict CSP — not a hosted page reaching into your machine.

To follow along with nothing to lose, start keyless: `caucus start --mock`, then open the URL.

---

## First run — the welcome overlay

On your **first** visit the console greets you with a three-step overlay (remembered afterward, so
it shows once — re-open it any time with the **?** in the top bar):

1. **Welcome** — what Caucus is, in one breath: a local *council of models* — a **panel** answers,
   a **judge** synthesizes, all on your machine. *Set it up*, or *Skip*.
2. **Bring a key** — pick a provider, paste a key, **Add & test** (a live ✓). Keys you've already
   set (e.g. via `caucus config set-key`) are detected and shown. No key? *Try it with a keyless
   mock* drops you straight into a working chat.
3. **Choose your council** — the combos as selectable cards, the ones your keys make **ready**
   sorted to the top and one pre-selected. If your single key doesn't satisfy any of the
   multi-provider default combos, Caucus mints a simple single-model combo from it so there's always
   a ready pick. *Start chatting →* sets it active and dismisses the overlay.

Then you land on the **Chat** tab with your combo active — the beats below tour every surface.

---

## Beat 0 — The top bar

**What you see.** The Caucus brandmark + version on the left; four tabs — **Chat · Activity ·
Logs · Settings**; the **bind address** (`127.0.0.1:8787`) on the right; a light/dark toggle.

**What it means.** The bind chip is the sovereignty tell — it says `127.0.0.1`, so this is your
machine talking to itself. The tabs are the four jobs the console does: *talk to your synth*,
*watch it work*, *read the engine*, *configure it*.

**What to do next.** You'll spend setup time in **Settings** and runtime in **Chat** / **Activity**.

---

## Beat 1 — Chat: talk to your own synth

**What you see.** A combo selector ("Synth with …") across the top, a chat composer, and (when a
combo needs a key you haven't added) an amber banner: *"Add a `<provider>` key in Settings → to
chat with the `<combo>` combo."* Send a message and an assistant bubble streams a *"synthesizing…"*
state, then the judged answer.

**What it means.** This isn't a toy — your message goes through the **real `/v1/messages` proxy**,
the same path a coding agent uses. A panel of models answered and a judge synthesized them, on
your machine, with your keys. Nothing is persisted: chat history is in memory for this session only.

**What to do next.** Send a planning question ("How should I design the caching layer?"), then
switch to **Activity** to see what just happened under the hood.

---

## Beat 2 — Activity: every turn, every cent

**What you see.** A **Live turns** stream. Each turn is a row tagged **`plan`** (purple) or
**`action`** (blue), showing the mode (`synth` / `pass-through`), the combo · model, and
tokens · cost · milliseconds. A rollup across the top totals turns / tokens / est. cost.

**What it means.** This is the transparency the hosted routers hide. Click a turn to expand it and
see **every provider sub-call** — each panel member, the judge — with its own tokens and cost, under
the header *"N provider calls — every model, every cent (nothing hidden)."*

**What to do next.** Expand a `plan` turn and confirm you can see the panel calls and the judge
call separately. That itemized list *is* the proof that a panel deliberated.

---

## Beat 3 — Activity: action selection (v1.1)

**What you see.** A second card, **Action selection**. Until you run a `sandbox-and-test` combo
it shows an empty state: *"No selections yet — needs a `sandbox-and-test` combo + a workspace."*

**What it means.** On action turns, the v1.1 path fans out N candidate edits, applies each in an
ephemeral network-isolated copy of your repo, runs your test command, and ships the survivor
(tests-pass, judge-score tiebreak). This card shows each candidate's applied / passed / return-code
and stars the survivor ★.

**What to do next.** To light it up, start with `--workspace <repo> --test-command "<cmd>"` and a
combo whose strategy is `sandbox-and-test`.

---

## Beat 4 — Logs: the engine, live

**What you see.** A streaming log view with source filters — **all / litellm / synth / caucus** —
a search box (substring, or `/regex/`), and **pause** / **clear**. A hint may say *"Showing
metadata only — restart with `caucus start --debug` to stream the full LiteLLM + synth trace."*

**What it means.** This is the raw engine trace: LiteLLM's per-call stream and the synth engine's
panel→judge pipeline, color-coded by source. In normal mode you get metadata; `--debug` opens the
full firehose. Same auth gate as everything else — an exposed daemon never leaks its logs.

**What to do next.** Filter to `synth` while you send a chat message to watch the panel fan out and
the judge collapse it. Search `/error/` if a turn misbehaves.

---

## Beat 5 — Settings ① Providers

**What you see.** A numbered **Providers** section. Each stored key is a row: provider name ·
fingerprint · **test** · **forget**. Below, a **provider dropdown** + a key field + **Save key**.

**What it means.** BYOK, stored locally in `~/.config/caucus/.env` (or your OS keychain), sent only
to the provider you chose, only a fingerprint shown back. The dropdown means you pick a provider by
name — no pasting API URLs. **test** does a 1-token call to confirm the key actually authenticates
(✓ with latency, or ✗ with the reason) — you never have to go to Chat and hit an error to find out.

**What to do next.** Add a key, watch it test green, then look at how the combos light up below.

---

## Beat 6 — Settings ② Combos

**What you see.** A card per combo: name (+ alias), a **readiness** badge — **`ready`** (green) or
**`needs <provider>`** (amber, click-through to add the key) — and the panel pills, judge pill, and
strategy. Buttons: **use** / **active**, **edit**, **duplicate**, plus **+ New combo** at the bottom.

**What it means.** A combo = *panel* + *judge* + *strategy*, and the readiness badge cross-references
your stored keys so you know *before* you chat whether a combo can actually run. Combos are yours:
**edit** opens an inline editor (one model per line for the panel, a judge, and a `passthrough` /
`sandbox-and-test` strategy), **duplicate** clones one to tweak, **+ New combo** starts blank — all
saved to `config.toml` (never keys) via the daemon.

**What to do next.** Click **edit** on a combo to see its panel/judge/strategy, or **use** to make
one the active default. Pills are color-coded: green = local (`ollama/…`), blue = cloud.

---

## Beat 7 — Settings ③ Connect an agent

**What you see.** Variant tabs — **Claude Code · Codex · aider · OpenAI / generic** — a snippet box
with a **copy** button, and a one-line hint explaining each.

**What it means.** Caucus speaks three wire formats on one port, so most agents connect as-is. The
snippet is generated for your *live* bind: `ANTHROPIC_BASE_URL` for Claude Code, a `~/.codex/config.toml`
Responses block for Codex, an OpenAI base-URL for aider / generic clients. The agent's key can be
any non-empty string — Caucus authenticates upstream with your stored keys.

**What to do next.** Pick your agent's tab, copy, paste into your shell or its config. The agent
will believe it's talking to one model; Caucus convenes the panel behind it.

---

## Beat 8 — Settings ⚙ Daemon

**What you see.** Read-only status tiles — **bind, network, active combo, fallback, engine, debug
logs, keystore, workspace, test command** — and below them the **exact commands** to change each
(stream the full trace, switch the active combo, expose on the network, stop the daemon), with your
live port already filled in.

**What it means.** This is the operational truth of the running process in one place. Network
exposure and the auth token are deliberately **launch-flag-managed** — set with `--expose` /
`--auth-token`, never editable from the browser — so a page can never turn exposure on or rotate
the token. The tiles read; the commands tell you how to write.

**What to do next.** Copy the `--debug` command if you want the full Logs trace, or the `--expose`
command (with an auth token) only if you intend to reach this daemon from another machine.

---

## Beat 9 — The public demo (front door, optional)

Separate from this console, Caucus ships a single-file **browser demo** ("the prose half") that
runs a panel→judge synth *in the browser* — recorded run, a shared key, or your own OpenRouter key
(BYOK, kept only in IndexedDB). Its job is the rhetorical close: a web page is a *client*, not a
*proxy*, so it can't sit in front of your agent or sandbox-and-test edits against your repo. That
runs locally — which is what this console, and `pipx install caucus`, give you.

---

*New to Caucus? Start with [GETTING-STARTED.md](GETTING-STARTED.md). For the vision and
positioning, see [`CAUCUS-VISION-AND-ROADMAP.md`](CAUCUS-VISION-AND-ROADMAP.md).*
