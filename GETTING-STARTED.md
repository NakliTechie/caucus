# Getting started with Caucus

Caucus is a **local, sovereign, turn-aware synth proxy** that sits between your coding agent and
the models. This guide takes you from nothing to your first synthesized turn, then to a real
coding agent running through Caucus — in about five minutes.

If you just want the tour of the console, see [WALKTHROUGH.md](WALKTHROUGH.md).

---

## 1. What is Caucus, really?

Your coding agent (Claude Code, Codex, aider, …) normally talks straight to one model. Caucus
inserts itself in the middle and treats two kinds of turn differently:

| Turn | What the agent is doing | What Caucus does |
|------|-------------------------|------------------|
| **Plan turn** | reasoning, designing, "how should I…" | convenes a **panel** of models, a **judge** synthesizes their answers into one, returns that |
| **Action turn** | tool calls, file edits, continuations | **passes through** to a single model so streaming stays fast (v1.1: fan out candidates, sandbox-and-test, ship the survivor) |

The agent points its base URL at `127.0.0.1` and believes it's talking to one model. It never
learns a panel exists. Your keys, code, and telemetry never leave the machine.

> **Why split turns?** Thinking with a panel is worth it at the decision points, where being
> wrong is expensive. It's *not* worth it for the hundreds of mechanical continuations in an
> agent loop — those should stay fast and cheap. Caucus spends deliberation only where it pays.

A **combo** is the unit you configure: a *panel* (the models that answer in parallel) + a *judge*
(the model that synthesizes) + a *strategy* (`passthrough`, or `sandbox-and-test` for v1.1).

---

## 2. Prerequisites

- **Python 3.10–3.13** (`requires-python = ">=3.10,<3.14"`).
- **pipx** or **uv** to install the CLI.
- **At least one provider API key** (OpenAI, Anthropic, DeepSeek, OpenRouter, Gemini, …) — *or*
  nothing at all if you only want the keyless `--mock` handshake, or a local **Ollama** for the
  fully-sovereign local combos.
- *(Optional)* **[Ollama](https://ollama.com)** running locally for the **Local-only** /
  **Balanced** combos. Pull the models a combo references, e.g. `ollama pull llama3.2:1b`.

---

## 3. Install

**From a source checkout** (current `0.1.0.dev0` — this is the path today):

```sh
git clone <repo> caucus && cd caucus
uv pip install -e .            # or:  pipx install --editable .
```

**Once published to PyPI:**

```sh
pipx install caucus            # or:  uvx caucus …
```

Either way you get a single `caucus` command and a clean **~186 MB** install — no ML/GPU stack,
no second runtime, no sidecar (see [§Under the hood](#under-the-hood)).

---

## 4. Your first turn — keyless, in 60 seconds

Start with the mock provider so step 1 always works, with **no key required**:

```sh
caucus start --mock
```

Caucus binds `127.0.0.1:8787` and prints where to point an agent and the console URL. In another
terminal, confirm it's healthy:

```sh
caucus status                  # daemon running? active model? keystore + key fingerprints?
```

Now open the console and take the smoke test yourself:

```sh
open http://127.0.0.1:8787/    # macOS · Linux: xdg-open · Windows: start
```

In the **Chat** tab, send any question. You're talking to your local synth through the *real*
`/v1/messages` proxy — the same path an agent uses. Then open the **Activity** tab and watch the
turn appear, tagged `plan → synth`, with the per-model calls, tokens, and cost. That's the whole
product in one screen: **you can see every model and every cent.**

> `--mock` answers with an obvious labelled echo, not a real model — it exists to prove the
> handshake. Add a real key next.

---

## 5. Add a provider key

Caucus is **BYOK** (bring your own key). Keys are stored **locally only** and **never** written
to `config.toml` — only a fingerprint is ever shown back.

**In the console** (easiest): **Settings → ① Providers** → pick a provider from the dropdown →
paste the key → **Save key**. It's tested and live immediately; hit **test** any time to verify a
key still authenticates.

**Or from the CLI:**

```sh
caucus config set-key openai           # prompts for the key (hidden input)
caucus config keys                     # list stored providers + fingerprints
caucus config clear-key openai         # forget one
```

**Where keys live.** The default keystore is a **`chmod 600` dotenv at
`~/.config/caucus/.env`** (inside a `chmod 700` directory). To use your **OS keychain** instead
(macOS Keychain / Windows Credential Manager / Secret Service), opt in with:

```sh
export CAUCUS_KEYSTORE=keyring         # requires the `keyring` package; falls back to the .env file
```

Keyless providers (`ollama`, `caucus-mock`) store nothing.

---

## 6. Pick a combo

Four combos ship, all editable (console **Settings → ② Combos**, or
`~/.config/caucus/config.toml` — definitions only, never keys):

| Combo | When to use | Needs a cloud key? | Needs Ollama? |
|-------|-------------|:---:|:---:|
| **Quality** | hardest reasoning; strong cloud panel + strong judge | ✅ | — |
| **Budget** | cheaper / faster cloud | ✅ | — |
| **Local-only** | fully sovereign — all-Ollama panel + local judge, zero cloud | — | ✅ |
| **Balanced** | local panel + a cloud judge | ✅ (judge) | ✅ |

The console shows each combo's **readiness** — "ready" when every key it needs is present, or
"needs `<provider>`" with a click-through to add it. Three ways to switch the active combo:

```sh
caucus combo use quality               # set the daemon-wide default
# — or click "use" on a combo in the console Settings → Combos
# — or have the agent target the alias per-session: caucus-quality / -budget / -local / -balanced
```

Start the daemon on a combo directly:

```sh
caucus start --model caucus-quality    # or a bare model: --model ollama/llama3.2:1b
```

> A combo whose name you pass as `--model` (or that the agent targets) **wins** over the persisted
> default for that request. The agent never has to know which combo it got.

---

## 7. Connect your coding agent

Caucus speaks **three wire formats** on the same port, so most agents work as-is. Settings → ③
**Connect an agent** prints the exact snippet for your live bind, with a copy button.

**Claude Code** (and any Anthropic `/v1/messages` agent):

```sh
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
```

**Codex** (OpenAI **Responses** API — `/v1/responses`, with tool passthrough):

```toml
# ~/.codex/config.toml
[model_providers.caucus]
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"
env_key  = "CAUCUS_KEY"
# then:  CAUCUS_KEY=x codex --config model_provider=caucus
```

**aider / any OpenAI-compatible client** (OpenAI **Chat Completions** — `/v1/chat/completions`):

```sh
export OPENAI_API_BASE=http://127.0.0.1:8787/v1
export OPENAI_API_KEY=caucus            # any non-empty value
aider --model caucus-quality
```

The key you give the agent can be **any non-empty string** — Caucus authenticates upstream with
*your* stored provider keys, not the agent's. Verified working: Claude Code, Codex, aider, goose.
(opencode connects; some headless render quirks. Hosted agents like Amp can't point at localhost.)

---

## 8. Observe what happened

- **Activity tab** — every turn, tagged `plan → synth` or `action → pass-through`, with combo,
  model, tokens, cost, and latency. Expand a turn to see *every provider sub-call* and a rollup.
- **Logs tab** — the live LiteLLM + synth engine trace. Metadata only by default; restart with
  `caucus start --debug` to stream the **full** trace. Filter by source (all / litellm / synth /
  caucus) and search (substring or `/regex/`).
- **Settings → ⚙ Daemon** — read-only status (bind, network, active combo, fallback, engine,
  debug, keystore, workspace, test command) and the exact commands to change each.

---

## 9. Sovereignty, in one screen

- **Runs locally.** Binds `127.0.0.1` only. Network exposure is opt-in behind `--expose` and
  **requires** an `--auth-token`; when exposed, that token gates *every* surface (proxy, console,
  key endpoints).
- **Keys stay local.** BYOK, stored in `~/.config/caucus/.env` (`chmod 600`) or your OS keychain.
  Never transmitted off-machine except to the provider you chose, never logged, never in
  `config.toml`. Only a fingerprint is shown.
- **Zero telemetry.** No phone-home — even the model price map is the bundled copy.
- **The console is served in-process, same-origin**, under a strict CSP. It is not a hosted page
  reaching into your localhost.

---

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Chat / agent errors with "No provider configured" | active combo set, but no key for it | Settings → Providers, add the key the combo's readiness badge names; or `caucus config set-key <provider>` |
| Daemon starts but the first real turn fails | started a cloud combo with no key | add a key (above), or start with `--mock` to verify the handshake first |
| `caucus logs` prints a hint, no logs | the daemon wasn't started with a log file | start with `--log-file <path>` (or watch the live **Logs** tab in the console instead) |
| Logs tab shows "metadata only" | not in debug mode | restart with `caucus start --debug` |
| Local-only / Balanced combo fails | Ollama isn't running, or its models aren't pulled | start Ollama and `ollama pull <model>` for each model the combo lists |
| `address already in use` on start | port 8787 taken | `caucus start --port <other>` (or `caucus stop` the previous one) |
| Action selection never shows survivors | combo isn't `sandbox-and-test`, or no workspace/test command | set the combo strategy and start with `--workspace <repo> --test-command "<cmd>"` |

---

## Where things live on disk

| Path | Holds |
|------|-------|
| `~/.config/caucus/config.toml` | combo definitions + bind/port/model/fallback/workspace/test-command — **never keys** |
| `~/.config/caucus/.env` | provider keys (`chmod 600`), default keystore — or your OS keychain if `CAUCUS_KEYSTORE=keyring` |
| `~/.config/caucus/caucus-<port>.pid` | the running daemon's pid (used by `caucus stop`) |

Override the config location with `CAUCUS_CONFIG=/path/to/config.toml`.

---

## Under the hood

Caucus is **one self-contained Python process**. The mixture-of-agents / best-of-N synthesis is
**Caucus's own engine** (`src/caucus/synth_engine.py`, identity `caucus-synth`) — a native,
tool-aware panel-and-judge that runs the panel in parallel and never flattens tool calls to text.
[LiteLLM](https://github.com/BerriAI/litellm) handles multi-provider routing and the in-process
Anthropic / OpenAI / Responses front doors. The result is a clean install (~186 MB, no ML stack).
The moat is the turn-aware assembly and the repo-aware action selection — not a borrowed engine.

See [`CAUCUS-VISION-AND-ROADMAP.md`](CAUCUS-VISION-AND-ROADMAP.md) for the vision and positioning,
and [WALKTHROUGH.md](WALKTHROUGH.md) for a guided tour of every console surface.
