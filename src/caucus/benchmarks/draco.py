"""DRACO benchmark — wrapped, not reimplemented.

DRACO (Zhong et al., 2026, Perplexity) — *Deep Research Accuracy, Completeness, and
Objectivity*: 100 complex deep-research tasks across 10 domains / 40 countries, each paired
with an expert, weighted rubric. We load the **official** dataset
(``hf.co/datasets/perplexity-ai/draco``) and grade reports with the **published protocol**
(paper §4.2) — an LLM-as-a-judge emits a binary MET/UNMET verdict per criterion, then:

    raw            = Σ_i  1[verdict_i = MET] · w_i           (weights may be negative)
    normalized (%) = clamp(0, 1,  raw / Σ_i max(0, w_i)) · 100
    pass_rate (%)  = mean_i [ 1(w_i>0)·1[MET] + 1(w_i<0)·1[UNMET] ] · 100

scored per axis (factual-accuracy, breadth-and-depth, presentation, citation) and overall.

**HONEST FRAMING.** DRACO measures *deep-research systems* — ones that retrieve from the live
web and cite primary sources. Caucus in front of a plain LLM has **no retrieval** and produces
**no real citations**, so absolute scores (especially citation + completeness) are low and are
*not* comparable to the paper's leaderboard. The signal that matters here is the **A/B delta**:
does Caucus synth improve the rubric score over the same model called once? Same judge grades
both sides, so the comparison is fair even though the judge is itself an LLM.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

AXES = ("factual-accuracy", "breadth-and-depth-of-analysis", "presentation-quality", "citation-quality")
AXIS_LABEL = {
    "factual-accuracy": "Factual accuracy",
    "breadth-and-depth-of-analysis": "Breadth & depth",
    "presentation-quality": "Presentation",
    "citation-quality": "Citation",
}

_DATA_DIR = Path(__file__).parent / "draco_data"
_DATA_PATH = _DATA_DIR / "test.jsonl"
_DATA_URL = "https://huggingface.co/datasets/perplexity-ai/draco/resolve/main/test.jsonl"


@dataclass
class Criterion:
    axis: str
    cid: str
    weight: float
    requirement: str


@dataclass
class Task:
    id: str
    problem: str
    domain: str
    criteria: list[Criterion]


# --------------------------------------------------------------------------------------------
# Dataset (fetched on demand, never vendored)
# --------------------------------------------------------------------------------------------

def ensure_dataset(path: Path = _DATA_PATH) -> Path:
    """Download the official DRACO test set if it isn't cached. Returns the path."""
    if path.is_file() and path.stat().st_size > 1000:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(_DATA_URL, timeout=90) as resp:  # public, ungated dataset
        path.write_bytes(resp.read())
    return path


def _parse_rubric(answer_json: str) -> list[Criterion]:
    rub = json.loads(answer_json)
    crits: list[Criterion] = []
    for section in rub.get("sections", []):
        axis = section.get("id", "")
        for c in section.get("criteria", []):
            crits.append(Criterion(axis=axis, cid=c.get("id", ""),
                                   weight=float(c.get("weight", 0)), requirement=c.get("requirement", "")))
    return crits


def load_tasks(n: int | None = None, *, domains: list[str] | None = None,
               path: Path = _DATA_PATH) -> list[Task]:
    """Load DRACO tasks. ``n`` (if given) samples a domain-balanced subset, deterministically."""
    ensure_dataset(path)
    tasks: list[Task] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        tasks.append(Task(id=row["id"], problem=row["problem"], domain=row.get("domain", ""),
                          criteria=_parse_rubric(row["answer"])))
    if domains:
        wanted = {d.lower() for d in domains}
        tasks = [t for t in tasks if t.domain.lower() in wanted]
    if n is not None and n < len(tasks):
        # Domain-balanced round-robin (deterministic — dataset order is fixed) so a small n
        # still spans domains instead of clustering in Finance.
        by_domain: dict[str, list[Task]] = {}
        for t in tasks:
            by_domain.setdefault(t.domain, []).append(t)
        order = sorted(by_domain)
        picked: list[Task] = []
        i = 0
        while len(picked) < n:
            d = order[i % len(order)]
            if by_domain[d]:
                picked.append(by_domain[d].pop(0))
            elif all(not v for v in by_domain.values()):
                break
            i += 1
        tasks = picked[:n]
    return tasks


# --------------------------------------------------------------------------------------------
# Scoring (paper §4.2) — pure, exact, unit-tested
# --------------------------------------------------------------------------------------------

@dataclass
class AxisScore:
    axis: str
    normalized: float       # 0..100
    pass_rate: float        # 0..100
    n: int
    met: int


@dataclass
class DracoScore:
    normalized: float
    pass_rate: float
    axes: dict[str, AxisScore] = field(default_factory=dict)


def _normalized(crits: list[Criterion], met: list[bool]) -> float:
    raw = sum(c.weight for c, m in zip(crits, met) if m)
    denom = sum(w for w in (max(0.0, c.weight) for c in crits))
    if denom <= 0:
        return 0.0
    return max(0.0, min(1.0, raw / denom)) * 100.0


def _pass_rate(crits: list[Criterion], met: list[bool]) -> float:
    if not crits:
        return 0.0
    hits = sum(1 for c, m in zip(crits, met)
               if (c.weight > 0 and m) or (c.weight < 0 and not m))
    return hits / len(crits) * 100.0


def score(criteria: list[Criterion], met: list[bool]) -> DracoScore:
    """Compute overall + per-axis normalized score and pass rate from MET verdicts."""
    overall = DracoScore(normalized=_normalized(criteria, met), pass_rate=_pass_rate(criteria, met))
    for axis in AXES:
        idx = [i for i, c in enumerate(criteria) if c.axis == axis]
        a_crits = [criteria[i] for i in idx]
        a_met = [met[i] for i in idx]
        overall.axes[axis] = AxisScore(axis=axis, normalized=_normalized(a_crits, a_met),
                                       pass_rate=_pass_rate(a_crits, a_met),
                                       n=len(a_crits), met=sum(a_met))
    return overall


# --------------------------------------------------------------------------------------------
# LLM-as-a-judge (the §4.2 protocol)
# --------------------------------------------------------------------------------------------

_JUDGE_SYSTEM = (
    "You are a meticulous, impartial evaluator. You grade a research report against a rubric. "
    "For EACH numbered criterion, decide strictly from the report's content whether it is MET "
    "or UNMET. A criterion is MET only if the report clearly and correctly satisfies it; if it "
    "is absent, vague, or wrong, it is UNMET. Do not reward fluent writing that lacks the "
    "required substance. Output ONLY a JSON array, one object per criterion: "
    '[{"n": 1, "verdict": "MET"}, {"n": 2, "verdict": "UNMET"}, ...]. No prose.'
)


def build_judge_prompt(problem: str, report: str, criteria: list[Criterion]) -> str:
    lines = [f"TASK:\n{problem}\n", "REPORT TO GRADE:\n" + (report or "(empty)") + "\n",
             "RUBRIC CRITERIA (grade each as MET or UNMET):"]
    for i, c in enumerate(criteria, 1):
        lines.append(f"{i}. {c.requirement}")
    lines.append(f"\nReturn a JSON array of exactly {len(criteria)} verdicts.")
    return "\n".join(lines)


def parse_verdicts(text: str, n: int) -> list[bool]:
    """Parse the judge's JSON into n booleans (MET=True). Missing/garbled → UNMET (False)."""
    met = [False] * n
    if not text:
        return met
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return met
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return met
    for obj in arr:
        if not isinstance(obj, dict):
            continue
        idx = obj.get("n")
        verdict = str(obj.get("verdict", "")).strip().upper()
        if isinstance(idx, int) and 1 <= idx <= n:
            met[idx - 1] = verdict.startswith("MET") or verdict in ("YES", "TRUE", "1")
    return met


def judge_report(problem: str, report: str, criteria: list[Criterion], *, complete) -> list[bool] | None:
    """Grade one report. ``complete(system, user) -> text`` is an injectable LLM call.

    Returns ``None`` — NOT a silent all-UNMET list — when the judge yields no usable verdicts
    (an empty or array-less response). That happens e.g. when the judge is a *reasoning* model
    whose token budget is consumed by hidden reasoning, leaving the JSON ``content`` empty. The
    caller surfaces it as a ``judge-no-verdict`` error and excludes the task, so a broken judge
    reads as a loud failure rather than a fake 0% across every system (the "silent failures" rule).
    """
    if not criteria:
        return []
    text = complete(_JUDGE_SYSTEM, build_judge_prompt(problem, report, criteria))
    if not text or re.search(r"\[.*\]", text, re.DOTALL) is None:
        return None
    return parse_verdicts(text, len(criteria))


# Generation prompt used to elicit a research report (the system under test).
RESEARCH_SYSTEM = (
    "You are an expert research analyst. Write a thorough, well-structured, objective research "
    "report that fully answers the user's request. Be specific: include concrete facts, figures, "
    "named entities, comparisons, and trade-offs. Cite sources where you can. Prefer accuracy "
    "over breadth; never invent facts or citations."
)


# --------------------------------------------------------------------------------------------
# Run orchestration — generate (baseline direct + Caucus via daemon), grade, aggregate
# --------------------------------------------------------------------------------------------

def _safe_cost(response) -> float:
    try:
        import litellm
        return float(litellm.completion_cost(completion_response=response) or 0.0)
    except Exception:
        return 0.0


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def run_draco(base_url: str, configs: list[dict], tasks: list[Task], *, judge_model: str,
              max_tokens: int = 3000, workers: int = 4, log=lambda *_: None) -> dict:
    """Run DRACO over ``tasks`` for each config and grade with ``judge_model``.

    Each config is ``{"label", "kind": "direct"|"daemon", "target": model-or-combo-alias}``.
    Generation cost for a ``direct`` config sums LiteLLM's per-call cost; for the ``daemon``
    config it is the daemon's ledger delta over the batch (captures the full 5-call synth).
    Judge cost is tracked separately as grading *overhead* (same judge for every config, so the
    A/B is fair). Returns a JSON-able dict consumed by ``report.draco_card_html``.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import httpx
    import litellm

    judge_cost = [0.0]
    judge_lock = threading.Lock()

    def judge_complete(system: str, user: str) -> str:
        # Generous max_tokens: a reasoning judge spends its budget on hidden reasoning FIRST, so a
        # tight cap leaves `content` (the JSON verdicts) empty. Prefer content; fall back to
        # reasoning_content so a reasoning model degrades to "parse what we can" — not a silent 0.
        resp = litellm.completion(model=judge_model, max_tokens=4096, temperature=0.0,
                                  messages=[{"role": "system", "content": system},
                                            {"role": "user", "content": user}])
        with judge_lock:
            judge_cost[0] += _safe_cost(resp)
        msg = resp.choices[0].message
        return (msg.content or "") or (getattr(msg, "reasoning_content", "") or "")

    def gen_direct(model: str, problem: str):
        resp = litellm.completion(model=model, max_tokens=max_tokens, temperature=0.7,
                                  messages=[{"role": "system", "content": RESEARCH_SYSTEM},
                                            {"role": "user", "content": problem}])
        text = resp.choices[0].message.content or ""
        return text, _safe_cost(resp), int(getattr(resp.usage, "completion_tokens", 0) or 0)

    def gen_daemon(alias: str, problem: str):
        r = httpx.post(f"{base_url}/v1/messages", timeout=600, json={
            "model": alias, "max_tokens": max_tokens, "system": RESEARCH_SYSTEM,
            "messages": [{"role": "user", "content": problem}]})
        r.raise_for_status()
        data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        return text, 0.0, int((data.get("usage") or {}).get("output_tokens", 0))

    def ledger(base):
        try:
            led = httpx.get(f"{base}/health", timeout=10).json().get("ledger", {})
            return led.get("total_cost_usd", 0.0), led.get("total_tokens", 0)
        except Exception:
            return 0.0, 0

    import time as _time
    out_configs = []
    per_task: list[dict] = [{"id": t.id, "domain": t.domain,
                             "problem": t.problem, "results": {}} for t in tasks]

    for cfg in configs:
        label, kind, target = cfg["label"], cfg["kind"], cfg["target"]
        log(f"DRACO · {label}: generating {len(tasks)} reports…")
        t0 = _time.monotonic()
        led_c0, led_t0 = ledger(base_url) if kind == "daemon" else (0.0, 0)

        def work(i_task):
            i, task = i_task
            try:
                if kind == "daemon":
                    report, cost, tok = gen_daemon(target, task.problem)
                else:
                    report, cost, tok = gen_direct(target, task.problem)
            except Exception as exc:
                return i, "", 0.0, 0, None, f"gen-error:{type(exc).__name__}"
            met = judge_report(task.problem, report, task.criteria, complete=judge_complete)
            if met is None:  # judge gave no usable verdict — surface, don't silently score 0
                return i, report, cost, tok, None, "judge-no-verdict"
            return i, report, cost, tok, score(task.criteria, met), None

        results = [None] * len(tasks)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for i, report, cost, tok, sc, err in ex.map(work, list(enumerate(tasks))):
                results[i] = (report, cost, tok, sc, err)

        gen_cost = sum(r[1] for r in results)
        gen_tok = sum(r[2] for r in results)
        if kind == "daemon":
            led_c1, led_t1 = ledger(base_url)
            gen_cost = max(0.0, led_c1 - led_c0)
            gen_tok = max(0, led_t1 - led_t0)

        norms, passes = [], []
        axis_norms = {a: [] for a in AXES}
        for i, (report, cost, tok, sc, err) in enumerate(results):
            if sc is not None:
                norms.append(sc.normalized)
                passes.append(sc.pass_rate)
                for a in AXES:
                    axis_norms[a].append(sc.axes[a].normalized)
            per_task[i]["results"][label] = {
                "normalized": sc.normalized if sc else 0.0,
                "pass_rate": sc.pass_rate if sc else 0.0,
                "report": (report or "")[:600],
                "error": err,
            }

        judge_fails = sum(1 for r in results if r[4] == "judge-no-verdict")
        gen_fails = sum(1 for r in results if r[4] and r[4].startswith("gen-error"))
        out_configs.append({
            "label": label, "kind": kind, "target": target,
            "normalized": _mean(norms), "pass_rate": _mean(passes),
            "axes": {a: _mean(axis_norms[a]) for a in AXES},
            "gen_cost_usd": round(gen_cost, 6), "gen_tokens": gen_tok,
            "judge_cost_usd": 0.0,  # filled below (shared overhead)
            "seconds": round(_time.monotonic() - t0, 1), "tasks": len(tasks),
            "graded": len(norms), "judge_fails": judge_fails, "gen_fails": gen_fails,
        })
        if judge_fails:
            log(f"DRACO · {label}: ⚠ {judge_fails}/{len(tasks)} reports got NO judge verdict — "
                f"judge '{judge_model}' may be a reasoning model returning empty content; those "
                f"tasks are EXCLUDED (don't read the score below as real until this is 0).")
        log(f"DRACO · {label}: normalized {_mean(norms):.1f}% · pass {_mean(passes):.1f}% "
            f"· graded {len(norms)}/{len(tasks)} · gen ${gen_cost:.4f}")

    total_judge = round(judge_cost[0], 6)
    return {
        "configs": out_configs, "tasks": per_task, "judge_model": judge_model,
        "judge_cost_usd": total_judge, "n_tasks": len(tasks),
        "domains": sorted({t.domain for t in tasks}),
    }
