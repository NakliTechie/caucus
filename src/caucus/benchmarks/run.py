"""Benchmark orchestration for ``caucus bench`` — run the probe and/or DRACO and write the report.

A *consumer* of the running daemon. Baselines are a single direct LiteLLM completion
(the model alone); Caucus is the full combo through the daemon. The DeepSeek (or other) key is
loaded from the local keystore into this process so the direct baseline/judge calls work.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from ..keystore import get_keystore, inject_env
from . import draco as draco_mod
from . import reasoning
from .report import draco_card_html, write_html_report


def _ensure_keys() -> list[str]:
    """Load BYOK keys from the keystore into this process (for direct baseline/judge calls)."""
    return inject_env(get_keystore())


def _gen_direct(model: str, question: str, *, system: str, max_tokens: int):
    import litellm
    resp = litellm.completion(model=model, max_tokens=max_tokens, temperature=0.0,
                              messages=[{"role": "system", "content": system},
                                        {"role": "user", "content": question}])
    text = resp.choices[0].message.content or ""
    try:
        cost = float(litellm.completion_cost(completion_response=resp) or 0.0)
    except Exception:
        cost = 0.0
    return text, cost, int(getattr(resp.usage, "completion_tokens", 0) or 0)


def _gen_daemon(base_url: str, alias: str, question: str, *, system: str, max_tokens: int):
    r = httpx.post(f"{base_url}/v1/messages", timeout=300, json={
        "model": alias, "max_tokens": max_tokens, "system": system,
        "messages": [{"role": "user", "content": question}]})
    r.raise_for_status()
    data = r.json()
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return text, 0.0, int((data.get("usage") or {}).get("output_tokens", 0))


def _ledger(base_url: str):
    try:
        led = httpx.get(f"{base_url}/health", timeout=10).json().get("ledger", {})
        return led.get("total_cost_usd", 0.0), led.get("total_tokens", 0)
    except Exception:
        return 0.0, 0


_PROBE_SYSTEM = "Answer the question correctly and as briefly as it asks. Give only the answer."


def run_probe(base_url: str, *, baseline_model: str, caucus_alias: str, workers: int = 5,
              log=lambda *_: None) -> tuple[list[dict], list[dict]]:
    """Curated-probe A/B: baseline (direct) vs Caucus (daemon). Returns (configs, items)."""
    probe = reasoning.PROBE
    items = [{"question": q, "expected": a, "category": c, "results": {}} for q, a, c in probe]
    configs = []

    for label, kind, target in (("baseline", "direct", baseline_model),
                                ("caucus", "daemon", caucus_alias)):
        log(f"probe · {label}: {len(probe)} items…")
        t0 = time.monotonic()
        led0 = _ledger(base_url) if kind == "daemon" else (0.0, 0)

        def work(idx_item):
            i, (q, a, c) = idx_item
            try:
                if kind == "daemon":
                    text, cost, tok = _gen_daemon(base_url, target, q, system=_PROBE_SYSTEM, max_tokens=200)
                else:
                    text, cost, tok = _gen_direct(target, q, system=_PROBE_SYSTEM, max_tokens=200)
            except Exception as exc:
                return i, "", 0.0, 0, False, f"err:{type(exc).__name__}"
            return i, text, cost, tok, reasoning.matches(text, a), None

        results = [None] * len(probe)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for i, text, cost, tok, ok, err in ex.map(work, list(enumerate(probe))):
                results[i] = (text, cost, tok, ok, err)
                items[i]["results"][label] = {"answer": text, "correct": ok}

        correct = sum(1 for r in results if r[3])
        tokens = sum(r[2] for r in results)
        cost = sum(r[1] for r in results)
        if kind == "daemon":
            (c1, t1) = _ledger(base_url)
            cost = max(0.0, c1 - led0[0])
            tokens = max(0, t1 - led0[1])
        configs.append({
            "label": label, "accuracy": correct / len(probe), "correct": correct,
            "total": len(probe), "output_tokens": tokens, "cost_usd": round(cost, 6),
            "cost_per_correct": (cost / correct) if correct else float("inf"),
            "seconds": round(time.monotonic() - t0, 1),
        })
        log(f"probe · {label}: {correct}/{len(probe)} correct · ${cost:.4f}")
    return configs, items


def run(base_url: str, *, do_probe: bool, do_draco: bool, draco_n: int,
        baselines: list[str], caucus_alias: str, judge_model: str, domains=None,
        html_path: str, title: str, generated_at: str, log=lambda *_: None) -> dict:
    """Run the requested benchmarks and write the combined HTML report. Returns a summary dict."""
    injected = _ensure_keys()
    log(f"keys loaded: {', '.join(injected) or '(none — relying on env)'}")

    probe_configs, probe_items = ([], [])
    if do_probe:
        probe_configs, probe_items = run_probe(
            base_url, baseline_model=baselines[0], caucus_alias=caucus_alias, log=log)

    draco_result = None
    if do_draco:
        tasks = draco_mod.load_tasks(n=draco_n, domains=domains)
        configs = [{"label": f"baseline-{m.split('/')[-1].split('-')[-1]}", "kind": "direct", "target": m}
                   for m in baselines]
        configs.append({"label": "caucus", "kind": "daemon", "target": caucus_alias})
        draco_result = draco_mod.run_draco(base_url, configs, tasks, judge_model=judge_model, log=log)

    extra = [draco_card_html(draco_result)] if draco_result else []
    write_html_report(html_path, title=title, generated_at=generated_at,
                      model=f"{caucus_alias} combo (panel + judge)",
                      configs=probe_configs, items=probe_items, extra_sections=extra)
    return {"html": html_path, "probe": bool(probe_configs), "draco": draco_result}
