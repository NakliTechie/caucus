# Caucus

> A local, sovereign, turn-aware proxy between a coding agent and the models.

Caucus sits between your coding agent and the models. On **action turns** (the code) it can
generate several candidates and run them against your repo's tests in an isolated sandbox, returning
the one that passes — in a [small benchmark](benchmark/) this lifts a cheap model from **60% → 97%**
on hard tasks. On **reasoning turns** it convenes a panel of models and a judge for one synthesized
answer. The agent points its base URL at `127.0.0.1` and believes it's talking to one model — your
keys, code, and telemetry never leave the machine.

**New here?** [GETTING-STARTED.md](GETTING-STARTED.md) takes you from zero to a connected agent
in five minutes; [WALKTHROUGH.md](WALKTHROUGH.md) is a guided tour of every console surface.

## Quickstart

```sh
# 0.1.0.dev0 — install from this checkout (not yet on PyPI):
uv pip install -e .            # from the repo root · or: pipx install --editable .
# (once published:  pipx install caucus)

# BYOK — keys are stored locally (a chmod-600 ~/.config/caucus/.env, or your OS keychain via
# CAUCUS_KEYSTORE=keyring), never written to config.toml. Set once:
caucus config set-key openai           # hidden prompt; or add it in the console: Settings → Providers
caucus config set-key anthropic

caucus start --model caucus-quality    # or a single model: --model ollama/llama3.2:1b
#   no key yet? `caucus start --mock` runs a keyless mock provider for the handshake.

# Point your coding agent at Caucus (it speaks Anthropic + OpenAI + Responses on one port):
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787   # Claude Code / any Anthropic-API agent
#   Codex (Responses) and aider (OpenAI) connect too — see GETTING-STARTED.md § Connect.

# Watch every turn deliberate, on your own machine:
open http://127.0.0.1:8787/            # the local console (Linux: xdg-open · Windows: start)
```

No key and just want to see the handshake? `caucus start --mock` runs a keyless, clearly
labelled mock provider.

## The turn-aware model

| Turn | What Caucus does |
|------|------------------|
| **Plan / reasoning** | panel of models → judge → one synthesized answer |
| **Action** (tool calls, edits) | with a sandbox + your repo's tests: generate N candidates, run each, ship the survivor — otherwise pass through to one primary model (streams) |

Synth only spends at the decision points; the bulk of an agent loop (continuations) passes
through and streams normally.

## Does it help? (a benchmark)

A small experiment, run honestly ([full report](benchmark/report.html) · [reproduce](benchmark/)):

- **Sandbox-and-test wins where it counts.** On hard coding tasks, running 5 cheap candidates and
  keeping a passer hit **97%** — vs **60%** for one cheap shot and **53%** for one *expensive* shot.
  The win scales with N (60 → 73 → 87 → 97%), and the shipped `select_survivor` matched ground truth
  on **84/84** trials (it drives the real `selection.py` + `sandbox.py`, not a proxy).
- **The text-judge synth *hurts* on code — 27%.** Asked to read candidates and write "the best," it
  rewrites working solutions into broken ones. That's *why* code/action turns route to the sandbox,
  not the judge; synth is for open-ended reasoning, where models genuinely differ.

It's an experiment, not a leaderboard — see the caveats in [benchmark/](benchmark/).

## Where this helps — and where it doesn't yet

Being honest about where it is today:

- **The plan/action split is a heuristic.** `classify()` is a regex cascade over the latest turn —
  enough to route the obvious cases, but it can't read intent (a "let me rethink why the test
  failed" continuation looks like an action turn). A learned classifier is the roadmap; treat today's
  routing as a useful approximation, not a guarantee.
- **The text-judge synth helps reasoning more than code — and measurably *hurts* code.** A panel +
  judge pays off on open-ended turns (design, trade-offs) where models genuinely differ. For *code*,
  the judge reads candidates without running them, so it can't tell which is correct — in our
  [benchmark](benchmark/) it scored **27%** on hard tasks vs **60%** for a single shot, rewriting
  working solutions into broken ones. And when a turn is a tool call, synth is bypassed entirely (you
  can't synthesize a tool call). For code, the **sandbox-and-test path below is the real mechanism**:
  the test suite is the judge.
- **Best-of-N samples one model N times** (temperature varies the output, not the approach), and a
  combo's judge may also be one of its panel members (it critiques its own answer). Both are
  deliberate — just know that the "diversity" here is sampling diversity, not different models.
- **Only the Anthropic `/v1/messages` path synthesizes.** The OpenAI-compatible
  `/v1/chat/completions` and `/v1/responses` endpoints (for Codex / aider / OpenAI clients) are
  **pure pass-through** today — turn classification and synth run on the Anthropic surface; on the
  OpenAI surfaces Caucus is a transparent multi-provider proxy (still useful for BYOK, fallback,
  and the cost ledger). Synth on the OpenAI surfaces is roadmap.

## Combos — yours to configure

A **combo** is a panel + a judge + a strategy. Four ship, all editable (in the console or
`~/.config/caucus/config.toml` — definitions only, never keys):

- **Quality** — strong cloud panel + strong judge
- **Budget** — cheaper / faster
- **Local-only** — all-Ollama panel + local judge (fully sovereign, zero cloud)
- **Balanced** — local panel + cloud judge

Switch the active combo in the console, with `caucus combo use <name>`, or per-session by the
model your agent targets: `caucus-quality`, `caucus-budget`, `caucus-local`, `caucus-balanced`.

## CLI

`caucus start | stop | status | config | combo | logs | bench`. `status` shows the bind address,
the configured model, key fingerprints, the keystore backend, and engine + daemon health.

## Sovereign guarantees

- **Runs locally.** Binds `127.0.0.1` only; network exposure is opt-in behind an explicit
  `--expose` flag and requires an auth token.
- **Keys stored locally only.** BYOK, kept in a `chmod 600` file at `~/.config/caucus/.env` by
  default (or your OS keychain via `CAUCUS_KEYSTORE=keyring`), **never** transmitted off-machine
  except to the provider you chose, never logged, never written to `config.toml`. Only a
  fingerprint is ever shown; `caucus config clear-key` forgets it.
- **Zero telemetry.** No phone-home — even the model price map is the bundled copy.
- **The console is served in-process, same-origin**, under a strict CSP. It is not a hosted
  page reaching into localhost.

## Sandbox-and-test

Action turns fan out to N candidates from the combo's primary model (sampled N times), each
applied in an **ephemeral, network-isolated copy** of your workspace with resource caps; your
repo's test command runs inside the sandbox and the survivor (tests-pass primary, judge-score
tiebreak) is returned. Model-proposed code never runs unsandboxed (non-root, all capabilities
dropped, no network, host home denied); if isolation can't be guaranteed — no sandbox backend, no
workspace/test command — action turns degrade to pass-through and the response carries an
`X-Caucus-Degraded` header so the agent can tell.

> **Deploy note.** The preferred backend is Docker, whose default image is `python:3.12-slim`. If
> your repo's tests need a toolchain (pytest, project deps, another language), set
> `CAUCUS_SANDBOX_IMAGE` to an image that has it — otherwise the test command can't run in-container
> and action turns degrade to pass-through. On macOS without Docker, Caucus falls back to a
> `sandbox-exec` (seatbelt) backend that runs on the host. See [`benchmark/`](benchmark/) for the
> numbers behind this path.

## Under the hood

Caucus is one self-contained Python process. The mixture-of-agents / best-of-N synthesis is
**Caucus's own engine** (`src/caucus/synth_engine.py`, identity `caucus-synth`) — a native,
tool-aware panel-and-judge that fans the panel out in parallel and never flattens tool calls to
text. [LiteLLM](https://github.com/BerriAI/litellm) handles multi-provider routing and the
in-process **Anthropic + OpenAI + Responses** front doors. The result is a lean install (no ML
stack — no torch/transformers) with no second runtime and no sidecar — the value is the repo-aware
sandbox-and-test selection and turn-aware routing, not a borrowed engine.

See [GETTING-STARTED.md](GETTING-STARTED.md) for setup and [WALKTHROUGH.md](WALKTHROUGH.md) for the
console tour.
