# Caucus benchmark — sandbox-and-test vs. one shot vs. text-judge synth

An experiment, run honestly: on code (action) turns, does Caucus's **sandbox-and-test** selection
(run N candidates against the tests, keep one that passes) actually beat a single model call — and
is the **text-judge synth** (read the candidates, write "the best") worth it on code?

**Open [`report.html`](report.html) for the full write-up.** Headline, on the hard set:

| selector | hard tasks (6 × K=5) | what it is |
|---|---:|---|
| `flash@1` | 60% | one cheap shot |
| `pro@1` | 53% | one **expensive** shot |
| `synth@3` | **27%** | text-judge — reads candidates, writes one (never runs them) |
| `runtest@2` | 73% | sandbox-and-test, N=2 |
| `runtest@3` | 87% | sandbox-and-test, N=3 |
| **`runtest@5`** | **97%** | sandbox-and-test, N=5 |

Three findings:

1. **Sandbox-and-test works and scales.** A *cheap* model goes 60 → 73 → 87 → 97% as N grows 1→5.
   One *expensive* unverified shot got 53%. A few cheap tries you can verify beat one costly try you can't.
2. **The text-judge synth *hurts* on code (27%).** It rewrites working solutions into broken ones —
   e.g. on the broad set it turned `regex_match`, `multiply_strings`, `three_sum_count` from 3/3 → 0/3.
   This is *why* Caucus routes code/action turns to the sandbox, not the judge.
3. **The shipped code path is real.** The benchmark drives the actual `selection.py` + `sandbox.py`;
   `select_survivor` agreed with measured pass@N on **84/84** trials (54 broad + 30 hard).

## Reproduce

```sh
uv pip install -e .                          # from the repo root
caucus config set-key deepseek               # or set BENCH_CHEAP/BENCH_STRONG to your own models

python benchmark/run_benchmark.py --set broad # 18 tasks × K=3  (writes results/broad.json)
python benchmark/run_benchmark.py --set hard  # 6 tasks  × K=5  (writes results/hard.json)
python benchmark/build_report.py              # regenerates report.html
```

Scoring runs inside Caucus's real sandbox. Backend is forced to **Seatbelt** (macOS host): the default
Docker image `python:3.12-slim` keeps python at `/usr/local/bin`, so a host-path test command can't
exec in-container — a real deploy note (set `CAUCUS_SANDBOX_IMAGE` to an image with your toolchain).
The selection logic is backend-agnostic.

## Honest caveats

- Tasks are self-contained Python functions with `assert` oracles — **not** real repo bug-fixes.
- "Cheap vs. expensive" is within one vendor (DeepSeek `v4-flash` vs `v4-pro`).
- The broad set is near a strong model's ceiling, so the small hard set carries the magnitude.
- Direction is robust; exact percentages will move with the task mix. It's an experiment, not a leaderboard.
