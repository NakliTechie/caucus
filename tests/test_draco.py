"""DRACO wrapper: the §4.2 scoring math, judge parsing, and dataset loading."""

import pytest

from caucus.benchmarks.draco import (
    AXES,
    Criterion,
    build_judge_prompt,
    judge_report,
    load_tasks,
    parse_verdicts,
    score,
)


def _c(axis, w):
    return Criterion(axis=axis, cid="x", weight=w, requirement="r")


def test_normalized_score_clamps_and_weights():
    fa = "factual-accuracy"
    crits = [_c(fa, 10), _c(fa, 20), _c(fa, -30)]
    # MET a(10), UNMET b, MET c(-30) → raw = 10-30 = -20 ; denom = 10+20 = 30 ; clamp → 0
    assert score(crits, [True, False, True]).normalized == 0.0
    # MET a, MET b, UNMET c → raw 30 / denom 30 → 100
    assert score(crits, [True, True, False]).normalized == 100.0
    # MET a only → 10/30 → 33.3%
    assert round(score(crits, [True, False, False]).normalized, 1) == 33.3


def test_pass_rate_positive_met_negative_unmet():
    fa = "factual-accuracy"
    crits = [_c(fa, 10), _c(fa, 20), _c(fa, -30)]
    # a(+,MET)=hit, b(+,UNMET)=miss, c(-,MET)=miss → 1/3
    assert round(score(crits, [True, False, True]).pass_rate, 1) == 33.3
    # a(+,MET)=hit, b(+,MET)=hit, c(-,UNMET)=hit → 3/3
    assert score(crits, [True, True, False]).pass_rate == 100.0


def test_axis_breakdown():
    crits = [_c("factual-accuracy", 10), _c("citation-quality", 10)]
    s = score(crits, [True, False])
    assert set(s.axes) == set(AXES)
    assert s.axes["factual-accuracy"].normalized == 100.0
    assert s.axes["citation-quality"].normalized == 0.0
    assert s.axes["factual-accuracy"].n == 1 and s.axes["presentation-quality"].n == 0


def test_parse_verdicts_robust():
    n = 3
    assert parse_verdicts('[{"n":1,"verdict":"MET"},{"n":2,"verdict":"UNMET"},{"n":3,"verdict":"MET"}]', n) == [True, False, True]
    # markdown-fenced + extra prose
    assert parse_verdicts('```json\n[{"n":1,"verdict":"met"}]\n```', n) == [True, False, False]
    # garbled → all UNMET
    assert parse_verdicts("not json", n) == [False, False, False]
    # out-of-range index ignored
    assert parse_verdicts('[{"n":9,"verdict":"MET"}]', n) == [False, False, False]


def test_judge_no_verdict_is_flagged_not_silent_zero():
    # The bug that made every system score a fake 0%: a reasoning judge spends its budget on
    # hidden reasoning and returns empty `content`. judge_report MUST return None (a loud
    # "judge-no-verdict" the runner excludes) — never a silent all-UNMET list that reads as 0%.
    crits = [_c("factual-accuracy", 10), _c("citation-quality", 5)]
    assert judge_report("p", "a real report", crits, complete=lambda s, u: "") is None      # empty judge
    assert judge_report("p", "a real report", crits, complete=lambda s, u: "I think so") is None  # prose, no JSON array
    # a real verdict array still grades normally (and an honest all-UNMET is NOT treated as failure)
    ok = judge_report("p", "r", crits, complete=lambda s, u: '[{"n":1,"verdict":"MET"},{"n":2,"verdict":"UNMET"}]')
    assert ok == [True, False]
    allunmet = judge_report("p", "r", crits, complete=lambda s, u: '[{"n":1,"verdict":"UNMET"},{"n":2,"verdict":"UNMET"}]')
    assert allunmet == [False, False]


def test_build_judge_prompt_numbers_criteria():
    crits = [_c("factual-accuracy", 10), _c("citation-quality", -5)]
    p = build_judge_prompt("solve X", "my report", crits)
    assert "1. r" in p and "2. r" in p and "my report" in p and "solve X" in p


@pytest.mark.skipif(not (load_tasks.__module__),  reason="always run if importable")
def test_load_tasks_domain_balanced():
    # Uses the cached dataset (downloads on demand if absent).
    try:
        tasks = load_tasks(n=5)
    except Exception as exc:
        pytest.skip(f"dataset unavailable offline: {exc}")
    assert len(tasks) == 5
    assert len({t.domain for t in tasks}) >= 4  # balanced across domains, not all Finance
    assert all(t.criteria and all(c.requirement for c in t.criteria) for t in tasks)
