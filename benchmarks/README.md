# Caucus benchmarks (`caucus bench`) — a consumer of the daemon (§14)

The benchmark routine proves Caucus's empirical claim — *fusion produces better output* — by
running popular benchmarks **through** Caucus and comparing against a single-model baseline,
reporting **accuracy AND cost** side by side. It is a **consumer**, not part of the serving
core: it talks to the running daemon over its API like any other client.

The runner + cost accounting live in [`caucus.benchmarks.bench`](../src/caucus/benchmarks/bench.py);
`caucus bench --smoke` exercises the plumbing against a running daemon.

## Reuse existing harnesses — never reimplement a benchmark

Because Caucus is an OpenAI/Anthropic-compatible endpoint, existing harnesses run against it
with a **base-URL swap**. Point the harness at the daemon and configure two runs.

### MB1 — reasoning / prose (post-v1.0)

```sh
caucus start --model caucus-quality          # daemon on 127.0.0.1:8787

# lm-evaluation-harness — MMLU-Pro, GPQA (single-turn, point straight at Caucus):
lm_eval --model openai-chat-completions \
  --model_args model=caucus-quality,base_url=http://127.0.0.1:8787/v1,num_concurrent=1 \
  --tasks mmlu_pro,gpqa_main_zeroshot --output_path results/caucus

# Baseline — the combo's primary model alone (no panel), same harness:
caucus start --model openai/gpt-4o          # single model
lm_eval ... --output_path results/baseline

# evalplus — HumanEval+, MBPP+:
evalplus.evaluate --model caucus-quality --backend openai \
  --base-url http://127.0.0.1:8787/v1 --dataset humaneval
```

### MB2 — coding / agentic (post-v1.1) — the headline

```sh
# SWE-bench Verified via an existing agent scaffold pointed at Caucus, then the OFFICIAL scorer:
#   point SWE-agent / mini-swe-agent / Aider's base_url at http://127.0.0.1:8787
#   then score the produced patches with the official `swebench` harness (its own Docker).
python -m swebench.harness.run_evaluation --predictions_path preds_caucus.jsonl ...
# Aider polyglot likewise (base-URL swap), baseline vs caucus-quality.
```

> Sandbox boundary (don't conflate, §14): SWE-bench's official harness scores the *final patch*
> against the task's tests in its own Docker. Caucus's v1.1 sandbox runs *candidate edits during
> selection*. They coexist — the scaffold+Caucus produce the patch; the official harness scores
> it. Do **not** merge the two test runners.

## The comparison is the point

For each bench, report **both** configs: Baseline (primary model alone) vs Caucus (full combo).
Fusion is ~4–5× the spend — **never report a score without its cost** (track cost-per-correct).
Prefer contamination-resistant benches (SWE-bench Verified, LiveCodeBench) for credible numbers;
treat HumanEval/MBPP as contaminated baselines, not proof. Keys for runs follow §6 (BYOK, local
only); result reports carry scores + cost only — never request/response bodies.

## Status

The runner + A/B table + cost accounting are built and smoke-verified against the daemon.
**Producing real numbers is blocked on a live provider + API budget + the heavy harness/dataset
installs** — see `plan/pending.md`.
