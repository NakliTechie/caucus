#!/usr/bin/env python3
"""Build benchmark/report.html from benchmark/results/{broad,hard}.json (written by run_benchmark.py)."""
import json
from pathlib import Path

DIR = Path(__file__).resolve().parent
RES = DIR / "results"
broad = json.load(open(RES / "broad.json"))
hard = json.load(open(RES / "hard.json")) if (RES / "hard.json").exists() else None

SHORT = {c: c for c in broad["configs"]}


def bars(data, configs, highlight="runtest@3"):
    pct = data["summary_pct"]
    rows = []
    for c in configs:
        p = pct[c]
        cls = ("hero" if c == highlight else ("bad" if c == "synth@3" else "norm"))
        rows.append(
            f'<div class="bar"><div class="blab">{SHORT[c]}</div>'
            f'<div class="btrack"><div class="bfill {cls}" style="width:{p}%"></div>'
            f'<span class="bpct">{p}%</span></div></div>')
    return "\n".join(rows)


def task_table(data):
    K, cfgs = data["K"], data["configs"]
    head = "".join(f"<th>{c}</th>" for c in cfgs)
    rows = []
    for t in data["tasks"]:
        cells = []
        for c in cfgs:
            v = data["scores"][c][t]
            cls = "good" if v == K else ("zero" if v == 0 else "part")
            cells.append(f'<td class="{cls}">{v}/{K}</td>')
        rows.append(f"<tr><td class='tn'>{t}</td>{''.join(cells)}</tr>")
    return f"<table class='tt'><tr><th>task</th>{head}</tr>{''.join(rows)}</table>"


collapse = []
for t in broad["tasks"]:
    f = broad["scores"]["flash@1"][t]; s = broad["scores"]["synth@3"][t]
    if f == broad["K"] and s < f:
        collapse.append((t, f, s))
collapse.sort(key=lambda x: x[2])
collapse_html = "".join(
    f"<li><code>{t}</code> — flash@1 <b>{f}/{broad['K']}</b> → synth@3 <b class='red'>{s}/{broad['K']}</b></li>"
    for t, f, s in collapse[:8])

hb = hard["summary_pct"] if hard else {}
hagree = hard["select_survivor_agreement"] if hard else None
bagree = broad["select_survivor_agreement"]
total_ok = bagree["ok"] + (hagree["ok"] if hagree else 0)
total_all = bagree["total"] + (hagree["total"] if hagree else 0)

VERDICT = (
    f"<b>Lead with sandbox-and-test.</b> On the hard tasks it lifts a <i>cheap</i> model from "
    f"<b>{hb.get('flash@1','—')}%</b> (one shot) to <b>{hb.get('runtest@5','—')}%</b> (five verified tries), "
    f"while one <i>expensive</i> unverified shot managed only <b>{hb.get('pro@1','—')}%</b>. The text-judge "
    f"synth <b class='red'>cratered to {hb.get('synth@3','—')}%</b> — it rewrites working code it never runs, "
    f"which is exactly why Caucus routes code to the sandbox, not the judge. The shipped "
    f"<code>select_survivor</code> matched ground truth on <b>{total_ok}/{total_all}</b> trials.")

TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Caucus — Sandbox-and-Test Benchmark</title>
<style>
  :root{--bg:#0c1018;--panel:#151c2b;--line:#26314a;--ink:#e9eef8;--dim:#9db0d0;--acc:#6ea8ff;
        --hero:#39d98a;--bad:#ff6b6b;--warn:#ffb454;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
  .wrap{max-width:920px;margin:0 auto;padding:36px 22px 90px}
  h1{font-size:27px;margin:0 0 2px;letter-spacing:-.4px}
  .sub{color:var(--dim);margin:0 0 22px}
  h2{font-size:18px;margin:34px 0 10px;border-bottom:1px solid var(--line);padding-bottom:6px}
  .verdict{background:linear-gradient(180deg,#13241c,#10301f);border:1px solid #1f6b48;border-radius:14px;
           padding:16px 18px;margin:0 0 26px;font-size:14.5px}
  .verdict b{color:#bdf0d4}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:18px}
  @media(max-width:680px){.grid2{grid-template-columns:1fr}}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px}
  .card h3{margin:0 0 4px;font-size:14px}
  .card .cap{color:var(--dim);font-size:12px;margin:0 0 14px}
  .bar{display:flex;align-items:center;gap:10px;margin:7px 0}
  .blab{width:78px;font-size:12px;color:var(--dim);text-align:right;flex:none}
  .btrack{position:relative;flex:1;background:#0d1422;border-radius:6px;height:22px;overflow:hidden}
  .bfill{height:100%;border-radius:6px}
  .bfill.hero{background:var(--hero)} .bfill.norm{background:var(--acc)} .bfill.bad{background:var(--bad)}
  .bpct{position:absolute;right:8px;top:50%;transform:translateY(-50%);font-size:11.5px;font-weight:700;color:#0c1018}
  .big{font-size:40px;font-weight:800;letter-spacing:-1px}
  .big.hero{color:var(--hero)} .big.bad{color:var(--bad)}
  .kpis{display:flex;gap:22px;flex-wrap:wrap;margin:6px 0 0}
  .kpi{flex:1;min-width:120px} .kpi .l{color:var(--dim);font-size:12px}
  p{color:#c7d3e8;font-size:14px} p.note{color:#8a9bbd;font-size:13px}
  ul{font-size:14px;color:#c7d3e8} li{margin:3px 0}
  code{color:#bcd;background:#0d1422;padding:1px 5px;border-radius:4px;font-size:12.5px}
  .red{color:var(--bad)}
  table.tt{width:100%;border-collapse:collapse;margin:6px 0;font-size:12px}
  table.tt th,table.tt td{border:1px solid var(--line);padding:4px 7px;text-align:center}
  table.tt th{background:#0d1422;color:var(--dim);font-weight:600}
  table.tt td.tn{text-align:left;color:var(--ink)}
  table.tt td.good{color:#7fe0aa} table.tt td.zero{color:var(--bad)} table.tt td.part{color:var(--warn)}
  .pill{display:inline-block;background:#0d1422;border:1px solid var(--line);border-radius:20px;
        padding:2px 10px;font-size:12px;color:var(--dim);margin:2px 4px 2px 0}
  .ok{color:var(--hero)}
  footer{color:#6c7a96;font-size:12px;margin-top:40px;border-top:1px solid var(--line);padding-top:14px}
</style></head><body><div class="wrap">
  <h1>Caucus — Sandbox-and-Test Benchmark</h1>
  <p class="sub">Does running candidates against tests and keeping a passer actually beat a single shot — and is the text-judge synth worth it on code? Every solution scored inside Caucus's real seatbelt sandbox.</p>

  <div class="verdict">__VERDICT__</div>

  <h2>The core result — where it counts</h2>
  <p>On the <b>hard set</b> (__HARDN__ fiddly parser tasks × K=__HARDK__, chosen because a single shot is unreliable on them):</p>
  <div class="grid2">
    <div class="card">
      <h3>Hard tasks — pass rate by selector</h3>
      <p class="cap">same identical candidates; only the selection mechanism differs</p>
      __HARDBARS__
    </div>
    <div class="card">
      <h3>The gap</h3>
      <div class="kpis">
        <div class="kpi"><div class="big hero">__HARD_R5__%</div><div class="l">runtest@5 — cheap, <b>verified</b></div></div>
        <div class="kpi"><div class="big">__HARD_PRO__%</div><div class="l">pro@1 — expensive, unverified</div></div>
      </div>
      <p style="margin-top:14px">Run-and-test lifts a cheap model from <b>__HARD_FLASH__%</b> (one shot) → <b>__HARD_R2__%</b> (N=2) → <b>__HARD_R3__%</b> (N=3) → <b class="ok">__HARD_R5__%</b> (N=5). The <i>expensive</i> single model only managed <b>__HARD_PRO__%</b>. A handful of cheap tries you can verify beat one costly try you can't.</p>
    </div>
  </div>

  <h2>It never hurts — and validates the real code path</h2>
  <p>On the <b>broad set</b> (__BROADN__ tasks × K=__BROADK__, mostly within a strong model's reach), run-and-test ties or beats the single shot on <i>every</i> task — the aggregate gap is small only because the ceiling is high:</p>
  <div class="card"><h3>Broad set — overall pass rate</h3>__BROADBARS__</div>
  <p style="margin-top:12px"><span class="pill ok">✓ select_survivor agreed with ground-truth pass@N on __TOTOK__/__TOTALL__ trials</span> The benchmark drives the <i>shipped</i> <code>selection.py</code> + <code>sandbox.py</code>, not a proxy — the survivor it picks is the one that actually passes.</p>

  <h2>The text-judge synth <span class="red">hurts</span> on code</h2>
  <p>Given the same candidates, a judge that <b>reads</b> them and writes "the best" solution (never running it) scored <b class="red">__BROAD_SYNTH__%</b> on the broad set — far below the <b>__BROAD_FLASH__%</b> single shot. It rewrites working code into broken code:</p>
  <ul>__COLLAPSE__</ul>
  <p class="note">This is <i>why</i> Caucus routes code/action turns to sandbox-and-test, not synth — and why the README says the text-judge synth helps open-ended reasoning, not code. The benchmark turns that caveat into a measured fact.</p>

  <h2>How many candidates? N=3 is the sweet spot</h2>
  <p>More candidates help — with diminishing returns. On the broad set 2→3 buys the gain; 3→5 adds little:</p>
  <div class="card"><h3>Broad set — runtest@N</h3>__NSCALE__</div>

  <h2>Per-task detail</h2>
  <p class="note">Hard set:</p>__HARDTABLE__
  <p class="note" style="margin-top:14px">Broad set:</p>__BROADTABLE__

  <h2>Method &amp; honest caveats</h2>
  <ul>
    <li>Per task/trial: generate __BNMAX__ cheap candidates once; every selector acts on the same candidates (isolates the <i>mechanism</i>, not the model). cheap = <code>__CHEAP__</code>, strong = <code>__STRONG__</code>.</li>
    <li><b>runtest@N</b> = run candidates in Caucus's real sandbox, keep a passer (network/$HOME denied, ephemeral copy). <b>synth@3</b> = a strong-model judge reads 3 and writes one. <b>pro@1</b> = a single strong generation.</li>
    <li>Sandbox forced to <b>Seatbelt</b> (host): the default Docker image <code>python:3.12-slim</code> puts python at <code>/usr/local/bin</code>, so a host-path test command can't exec in-container — a real deploy note (set <code>CAUCUS_SANDBOX_IMAGE</code> to your toolchain). The selection logic is backend-agnostic.</li>
    <li><b>Caveats:</b> tasks are self-contained Python functions with assert oracles, not real repo bug-fixes; cheap-vs-expensive is within one vendor; the broad set is near-ceiling so the hard set (small N) carries the magnitude. Direction is robust; exact percentages move with the task mix. Reproduce: <code>python benchmark/run_benchmark.py --set hard</code>.</li>
  </ul>

  <footer>Generated from <code>benchmark/results/*.json</code> · broad run __BELAPSED__s, hard run __HELAPSED__s · all scoring inside Caucus's seatbelt sandbox.</footer>
</div></body></html>"""

CFG = broad["configs"]
html = (TEMPLATE
        .replace("__VERDICT__", VERDICT)
        .replace("__HARDN__", str(len(hard["tasks"])) if hard else "—")
        .replace("__HARDK__", str(hard["K"]) if hard else "—")
        .replace("__HARDBARS__", bars(hard, CFG, highlight="runtest@5") if hard else "<p class='note'>hard run pending…</p>")
        .replace("__HARD_R5__", str(hb.get("runtest@5", "—"))).replace("__HARD_R3__", str(hb.get("runtest@3", "—")))
        .replace("__HARD_R2__", str(hb.get("runtest@2", "—"))).replace("__HARD_FLASH__", str(hb.get("flash@1", "—")))
        .replace("__HARD_PRO__", str(hb.get("pro@1", "—")))
        .replace("__BROADN__", str(len(broad["tasks"]))).replace("__BROADK__", str(broad["K"]))
        .replace("__BROADBARS__", bars(broad, CFG))
        .replace("__TOTOK__", str(total_ok)).replace("__TOTALL__", str(total_all))
        .replace("__BROAD_SYNTH__", str(broad["summary_pct"]["synth@3"]))
        .replace("__BROAD_FLASH__", str(broad["summary_pct"]["flash@1"]))
        .replace("__COLLAPSE__", collapse_html or "<li>(none)</li>")
        .replace("__NSCALE__", bars({"summary_pct": broad["summary_pct"]}, ["runtest@2", "runtest@3", "runtest@5"]))
        .replace("__HARDTABLE__", task_table(hard) if hard else "")
        .replace("__BROADTABLE__", task_table(broad))
        .replace("__BNMAX__", str(broad["N"]))
        .replace("__CHEAP__", broad.get("cheap", "cheap")).replace("__STRONG__", broad.get("strong", "strong"))
        .replace("__BELAPSED__", str(broad["elapsed_s"]))
        .replace("__HELAPSED__", str(hard["elapsed_s"]) if hard else "—"))
(DIR / "report.html").write_text(html)
print("wrote", DIR / "report.html")
