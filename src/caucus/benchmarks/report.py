"""``caucus bench`` HTML report — render an A/B result set into one self-contained file.

This is a pure *presentation* sibling to :mod:`caucus.benchmarks.bench`: it takes already-computed
A/B numbers (baseline vs Caucus synth) and writes a single, dependency-free HTML file that opens
straight from disk. **No network, no external assets, no JavaScript** — every byte (styles + the
bar charts, hand-rolled as inline SVG) lives in the file. It honours the rule that a score never
appears without its cost: the summary always carries cost and cost-per-correct alongside accuracy.

Pure stdlib only (``html.escape``). The caller passes a pre-formatted ``generated_at`` string; this
module never touches ``datetime``/``time`` so it stays deterministic and trivially testable.
"""

from __future__ import annotations

from html import escape

# The report is theme-reactive via CSS variables + a prefers-color-scheme media query (see the
# :root block below). These constants now emit `var(--x)` so every color — including the inline
# SVG charts and table swatches — follows the active light/dark theme automatically.
_BG = "var(--bg)"
_PANEL = "var(--panel)"
_PANEL2 = "var(--panel-2)"
_BORDER = "var(--border)"
_TEXT = "var(--text)"
_MUTED = "var(--muted)"
_FAINT = "var(--faint)"
_GREEN = "var(--green)"   # local / correct
_BLUE = "var(--blue)"     # cloud / baseline
_PURPLE = "var(--purple)"  # judge / caucus synth accent
_WARN = "var(--warn)"


def _e(value) -> str:
    """HTML-escape any value (quotes included — used in text and attributes)."""
    return escape(str(value), quote=True)


def _truncate(text: str, limit: int) -> str:
    text = "" if text is None else str(text)
    text = " ".join(text.split())  # collapse whitespace so the table stays tidy
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _fmt_cost_per_correct(value: float) -> str:
    try:
        if value == float("inf"):
            return "—"
    except (TypeError, ValueError):
        return "—"
    return f"${value:.6f}"


def _color_for(index: int) -> str:
    """Baseline → blue, Caucus → purple; cycle for any extra configs."""
    return (_BLUE, _PURPLE, _GREEN, _WARN)[index % 4]


def _bar_chart(configs, *, metric, fmt, title, height=170, color_by_index=True):
    """Hand-rolled inline-SVG vertical bar chart (no JS, no libraries).

    ``metric`` pulls a number off each config dict; ``fmt`` turns that number into a label.
    Bars are scaled to the largest value in the set; a faint baseline axis grounds them.
    """
    values = [float(c.get(metric, 0.0) or 0.0) for c in configs]
    peak = max(values) if values else 0.0
    peak = peak if peak > 0 else 1.0

    n = len(configs) or 1
    pad_l, pad_r, pad_t, pad_b = 16, 16, 30, 34
    plot_w = max(120, n * 130)
    width = pad_l + plot_w + pad_r
    plot_h = height - pad_t - pad_b
    slot = plot_w / n
    bar_w = min(86.0, slot * 0.56)
    base_y = pad_t + plot_h

    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" '
        f'preserveAspectRatio="xMidYMid meet" role="img" '
        f'aria-label="{_e(title)}" xmlns="http://www.w3.org/2000/svg">',
        f'<text x="{pad_l}" y="18" fill="{_TEXT}" font-family="system-ui, sans-serif" '
        f'font-size="12" font-weight="600">{_e(title)}</text>',
        # baseline axis
        f'<line x1="{pad_l}" y1="{base_y:.1f}" x2="{pad_l + plot_w}" y2="{base_y:.1f}" '
        f'stroke="{_BORDER}" stroke-width="1"/>',
    ]

    for i, cfg in enumerate(configs):
        val = values[i]
        bar_h = (val / peak) * plot_h
        cx = pad_l + slot * i + slot / 2
        x = cx - bar_w / 2
        y = base_y - bar_h
        color = _color_for(i) if color_by_index else _GREEN
        label = _e(cfg.get("label", f"config {i + 1}"))
        value_label = _e(fmt(val))
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{max(bar_h, 0.0):.1f}" '
            f'rx="3" fill="{color}" fill-opacity="0.85" stroke="{color}" stroke-width="1">'
            f'<title>{label}: {value_label}</title></rect>'
        )
        # value on top of the bar
        parts.append(
            f'<text x="{cx:.1f}" y="{y - 6:.1f}" fill="{_TEXT}" text-anchor="middle" '
            f'font-family="ui-monospace, monospace" font-size="12" '
            f'font-weight="600">{value_label}</text>'
        )
        # config label under the axis
        parts.append(
            f'<text x="{cx:.1f}" y="{base_y + 18:.1f}" fill="{_MUTED}" text-anchor="middle" '
            f'font-family="system-ui, sans-serif" font-size="11">{label}</text>'
        )

    parts.append("</svg>")
    return "".join(parts)


def _summary_rows(configs, best_idx):
    rows = []
    for i, c in enumerate(configs):
        acc = float(c.get("accuracy", 0.0) or 0.0)
        win = " win" if i == best_idx else ""
        dot = _color_for(i)
        rows.append(
            "<tr>"
            f'<td class="lbl"><span class="swatch" style="background:{dot}"></span>'
            f"{_e(c.get('label', f'config {i + 1}'))}</td>"
            f'<td class="num acc{win}">{acc * 100:.1f}%</td>'
            f'<td class="num">{_e(c.get("correct", 0))}/{_e(c.get("total", 0))}</td>'
            f'<td class="num">{_e(c.get("output_tokens", 0))}</td>'
            f'<td class="num">${float(c.get("cost_usd", 0.0) or 0.0):.6f}</td>'
            f'<td class="num">{_fmt_cost_per_correct(c.get("cost_per_correct", float("inf")))}</td>'
            f'<td class="num">{float(c.get("seconds", 0.0) or 0.0):.1f}s</td>'
            "</tr>"
        )
    return "".join(rows)


def _item_rows(configs, items):
    labels = [c.get("label", f"config {i + 1}") for i, c in enumerate(configs)]
    rows = []
    for item in items:
        results = item.get("results", {}) or {}
        cells = [
            f'<td class="q"><div class="qtext">{_e(_truncate(item.get("question", ""), 160))}</div></td>',
            f'<td class="cat">{_e(_truncate(item.get("category", ""), 24))}</td>',
            f'<td class="exp"><code>{_e(_truncate(item.get("expected", ""), 40))}</code></td>',
        ]
        for label in labels:
            res = results.get(label) or {}
            correct = bool(res.get("correct"))
            answer = _truncate(res.get("answer", ""), 90)
            mark = "✓" if correct else "✗"
            cls = "ok" if correct else "bad"
            cells.append(
                f'<td class="ans {cls}"><span class="mark">{mark}</span>'
                f'<span class="atext">{_e(answer)}</span></td>'
            )
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return "".join(rows)


_DRACO_AXES = [
    ("factual-accuracy", "Factual"),
    ("breadth-and-depth-of-analysis", "Breadth"),
    ("presentation-quality", "Presentation"),
    ("citation-quality", "Citation"),
]


def draco_card_html(draco) -> str:
    """Render a DRACO result dict (from ``caucus.benchmarks.draco.run_draco``) as one HTML card."""
    configs = list(draco.get("configs", []))
    tasks = list(draco.get("tasks", []))
    if not configs:
        return ""

    best = max(range(len(configs)), key=lambda i: configs[i].get("normalized", 0.0))

    # summary rows
    srows = []
    for i, c in enumerate(configs):
        win = " win" if i == best else ""
        srows.append(
            "<tr>"
            f'<td class="lbl"><span class="swatch" style="background:{_color_for(i)}"></span>'
            f'{_e(c.get("label",""))} <span style="color:{_FAINT};font-weight:400">'
            f'{_e(c.get("target",""))}</span></td>'
            f'<td class="num acc{win}">{float(c.get("normalized",0)):.1f}%</td>'
            f'<td class="num">{float(c.get("pass_rate",0)):.1f}%</td>'
            f'<td class="num">{_e(c.get("gen_tokens",0))}</td>'
            f'<td class="num">${float(c.get("gen_cost_usd",0)):.6f}</td>'
            f'<td class="num">{float(c.get("seconds",0)):.0f}s</td>'
            "</tr>")

    # axis breakdown table (config × 4 axes normalized %)
    axis_head = "".join(f'<th class="num">{lbl}</th>' for _, lbl in _DRACO_AXES)
    arows = []
    for i, c in enumerate(configs):
        cells = "".join(f'<td class="num">{float(c.get("axes",{}).get(a,0)):.1f}%</td>'
                        for a, _ in _DRACO_AXES)
        arows.append(f'<tr><td class="lbl"><span class="swatch" style="background:'
                     f'{_color_for(i)}"></span>{_e(c.get("label",""))}</td>{cells}</tr>')

    # per-task table (domain × config normalized %)
    labels = [c.get("label", "") for c in configs]
    task_head = "".join(f'<th class="num">{_e(l)}</th>' for l in labels)
    trows = []
    for t in tasks:
        cells = []
        for lab in labels:
            r = (t.get("results", {}) or {}).get(lab, {})
            cells.append(f'<td class="num">{float(r.get("normalized",0)):.1f}%</td>')
        trows.append(f'<tr><td class="q"><div class="qtext">{_e(_truncate(t.get("problem",""),120))}</div></td>'
                     f'<td class="cat">{_e(t.get("domain",""))}</td>{"".join(cells)}</tr>')

    norm_chart = _bar_chart(
        [{"label": c["label"], "normalized": c.get("normalized", 0)} for c in configs],
        metric="normalized", fmt=lambda v: f"{v:.1f}%", title="DRACO normalized score (%)", height=190)
    cost_chart = _bar_chart(
        [{"label": c["label"], "gen_cost_usd": c.get("gen_cost_usd", 0)} for c in configs],
        metric="gen_cost_usd", fmt=lambda v: f"${v:.4f}", title="Generation cost (USD)", height=190)

    return f"""
  <section class="card">
    <h2>DRACO <span class="sub">Perplexity deep-research benchmark &mdash; {_e(draco.get('n_tasks',0))} tasks,
      LLM-judge rubric scoring (§4.2)</span></h2>
    <div class="draco-note">
      DRACO grades <b>deep-research systems</b> (live web retrieval + primary-source citations).
      Caucus in front of a plain LLM has <b>no retrieval and no real citations</b>, so absolute
      scores &mdash; especially citation and completeness &mdash; are low and are <b>not</b>
      comparable to the paper's leaderboard. The signal here is the <b>synth-vs-baseline
      delta</b>. Judge model: <code>{_e(draco.get('judge_model',''))}</code>
      (grading overhead ${float(draco.get('judge_cost_usd',0)):.4f}, the same judge for every
      config &mdash; so the A/B is fair).
    </div>
    <table class="summary" style="margin-bottom:18px">
      <thead><tr><th>config</th><th class="num">normalized</th><th class="num">pass rate</th>
        <th class="num">gen tokens</th><th class="num">gen cost ($)</th><th class="num">time</th></tr></thead>
      <tbody>{''.join(srows)}</tbody>
    </table>
    <div class="charts">
      <div class="chartbox">{norm_chart}</div>
      <div class="chartbox">{cost_chart}</div>
    </div>
    <h3 style="font-size:13px;margin:20px 0 8px;color:{_MUTED}">Per-axis normalized score</h3>
    <table class="summary"><thead><tr><th>config</th>{axis_head}</tr></thead>
      <tbody>{''.join(arows)}</tbody></table>
    <h3 style="font-size:13px;margin:22px 0 8px;color:{_MUTED}">Per-task (normalized %)</h3>
    <table class="items"><thead><tr><th>task</th><th>domain</th>{task_head}</tr></thead>
      <tbody>{''.join(trows)}</tbody></table>
  </section>"""


def write_html_report(path, *, title, generated_at, model, configs=None, items=None,
                      extra_sections=None) -> None:
    """Write a single self-contained HTML A/B report (baseline vs Caucus synth) to ``path``.

    ``configs``/``items`` carry the curated-probe A/B (may be empty for a DRACO-only report);
    ``extra_sections`` is a list of pre-rendered HTML card strings (e.g. ``draco_card_html``).
    Idempotent: the file is fully overwritten on each call. No network, no external assets, no JS.
    """
    configs = list(configs or [])
    items = list(items or [])
    extra_sections = list(extra_sections or [])

    # Winning accuracy → highlighted in the summary table and reflected in the chart legend.
    best_idx = -1
    best_acc = -1.0
    for i, c in enumerate(configs):
        acc = float(c.get("accuracy", 0.0) or 0.0)
        if acc > best_acc:
            best_acc = acc
            best_idx = i

    acc_chart = _bar_chart(
        configs,
        metric="accuracy",
        fmt=lambda v: f"{v * 100:.1f}%",
        title="Accuracy (%)",
        height=190,
    )
    cost_chart = _bar_chart(
        configs,
        metric="cost_usd",
        fmt=lambda v: f"${v:.6f}",
        title="Cost (USD)",
        height=190,
    )

    config_label_th = "".join(
        f'<th class="num">{_e(c.get("label", f"config {i + 1}"))}</th>'
        for i, c in enumerate(configs)
    )

    probe_cards = f"""
  <section class="card">
    <h2>Curated probe <span class="sub">A/B &mdash; accuracy and cost, side by side</span></h2>
    <table class="summary"><thead><tr><th>config</th><th class="num">accuracy</th>
      <th class="num">correct</th><th class="num">out tokens</th><th class="num">cost ($)</th>
      <th class="num">$ / correct</th><th class="num">time</th></tr></thead>
      <tbody>{_summary_rows(configs, best_idx)}</tbody></table>
    <div class="charts" style="margin-top:16px"><div class="chartbox">{acc_chart}</div>
      <div class="chartbox">{cost_chart}</div></div>
    <div class="legend"><span><b style="color:{_BLUE}">&#9632;</b> baseline</span>
      <span><b style="color:{_PURPLE}">&#9632;</b> caucus</span>
      <span>higher accuracy is better; lower cost is better</span></div>
  </section>
  <section class="card">
    <h2>Probe items <span class="sub">{len(items)} items (not a benchmark &mdash; a quick probe)</span></h2>
    <table class="items"><thead><tr><th>question</th><th>category</th><th>expected</th>
      {config_label_th}</tr></thead><tbody>{_item_rows(configs, items)}</tbody></table>
  </section>""" if (configs and items) else ""
    extra_html = "".join(extra_sections)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
<style>
  :root {{ /* LIGHT — cream background + blue (matches the console/demo light mode) */
    --bg:#f4edda; --panel:#fbf7ec; --panel-2:#ece2cb; --border:#ddd0b2;
    --text:#222d3b; --muted:#586472; --faint:#8892a0;
    --green:#1f9d6b; --blue:#2f6fd6; --purple:#4257c4; --warn:#b07d12;
    --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
    --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, system-ui, sans-serif;
    --radius: 10px;
  }}
  @media (prefers-color-scheme: dark) {{ :root {{ /* DARK — navy + cream + blue */
    --bg:#0e1622; --panel:#161f2e; --panel-2:#1d2838; --border:#2a384b;
    --text:#f2ebd9; --muted:#9db0c6; --faint:#69788c;
    --green:#63d0a6; --blue:#5ea2ff; --purple:#ead9b3; --warn:#e3b54f;
  }} }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--text); font-family:var(--sans);
    font-size:14px; line-height:1.55; }}
  .wrap {{ max-width:1040px; margin:0 auto; padding:28px 22px 64px; }}
  header.hdr {{ border-bottom:1px solid var(--border); padding-bottom:18px; margin-bottom:24px; }}
  header.hdr h1 {{ margin:0 0 8px; font-size:22px; font-weight:650; letter-spacing:.2px; }}
  header.hdr .meta {{ display:flex; flex-wrap:wrap; gap:8px 18px; color:var(--muted);
    font:12px/1.4 var(--mono); }}
  header.hdr .meta b {{ color:var(--text); font-weight:600; }}
  header.hdr .note {{ margin-top:12px; color:var(--faint); font-size:12.5px; }}
  header.hdr .note .b {{ color:var(--blue); }}
  header.hdr .note .c {{ color:var(--purple); }}

  .card {{ background:var(--panel); border:1px solid var(--border); border-radius:var(--radius);
    padding:18px 20px; margin-bottom:22px; }}
  .card h2 {{ margin:0 0 14px; font-size:15px; font-weight:600; }}
  .card h2 .sub {{ color:var(--faint); font-weight:400; font-size:12px; margin-left:8px; }}
  .draco-note {{ background:var(--panel-2); border:1px solid var(--border); border-left:3px solid var(--warn);
    border-radius:8px; padding:11px 14px; margin-bottom:16px; color:var(--muted); font-size:12.5px;
    line-height:1.5; }}
  .draco-note b {{ color:var(--text); }} .draco-note code {{ color:var(--purple);
    font-family:var(--mono); }}

  table {{ width:100%; border-collapse:collapse; }}
  th, td {{ text-align:left; padding:9px 12px; border-bottom:1px solid var(--border);
    vertical-align:top; }}
  thead th {{ color:var(--muted); font-size:11px; font-weight:600; text-transform:uppercase;
    letter-spacing:.6px; border-bottom:1px solid var(--border); }}
  tbody tr:last-child td {{ border-bottom:none; }}
  .num {{ font-family:var(--mono); text-align:right; white-space:nowrap; }}
  thead th.num {{ text-align:right; }}

  .summary td.lbl {{ font-weight:600; white-space:nowrap; }}
  .summary .swatch {{ display:inline-block; width:9px; height:9px; border-radius:2px;
    margin-right:8px; vertical-align:middle; }}
  .summary td.acc {{ color:var(--text); }}
  .summary td.acc.win {{ color:var(--green); font-weight:700; }}
  .summary td.acc.win::after {{ content:" \\2605"; color:var(--green); font-size:11px; }}

  .charts {{ display:grid; gap:18px; grid-template-columns:1fr; }}
  @media (min-width:720px) {{ .charts {{ grid-template-columns:1fr 1fr; }} }}
  .chartbox {{ background:var(--panel-2); border:1px solid var(--border); border-radius:8px;
    padding:10px 12px 6px; }}

  .items th, .items td {{ font-size:12.5px; }}
  .items td.q {{ max-width:280px; }}
  .items .qtext {{ color:var(--text); }}
  .items td.cat {{ color:var(--muted); white-space:nowrap; }}
  .items td.exp code {{ font-family:var(--mono); color:var(--warn); font-size:12px;
    background:var(--panel-2); padding:1px 6px; border-radius:4px; }}
  .items td.ans {{ max-width:220px; }}
  .items td.ans .mark {{ font-family:var(--mono); font-weight:700; margin-right:7px; }}
  .items td.ans .atext {{ color:var(--muted); }}
  .items td.ans.ok .mark {{ color:var(--green); }}
  .items td.ans.bad .mark {{ color:#f0857f; }}
  .items td.ans.ok {{ background:rgba(99,208,166,.07); }}
  .items td.ans.bad {{ background:rgba(240,133,127,.07); }}

  .legend {{ margin-top:12px; color:var(--faint); font-size:11.5px; display:flex;
    gap:16px; flex-wrap:wrap; }}
  .legend span b {{ font-family:var(--mono); font-weight:600; }}
  footer {{ color:var(--faint); font-size:11.5px; margin-top:8px; text-align:center; }}
</style>
</head>
<body>
<div class="wrap">
  <header class="hdr">
    <h1>{_e(title)}</h1>
    <div class="meta">
      <span>model <b>{_e(model)}</b></span>
      <span>generated <b>{_e(generated_at)}</b></span>
    </div>
    <div class="note">
      Compares a <span class="b">single-model baseline</span> against
      <span class="c">Caucus synth</span> &mdash; accuracy is always shown with its cost.
    </div>
  </header>
{probe_cards}{extra_html}
  <footer>Generated by caucus bench &mdash; self-contained, offline-viewable. No scripts, no network.</footer>
</div>
</body>
</html>
"""

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
