"""The benchmark consumer (§14): A/B table + cost accounting (the runner, not a benchmark)."""

from caucus.benchmarks.bench import SMOKE, Result, format_table


def test_smoke_set_is_question_answer_pairs():
    assert all(isinstance(q, str) and isinstance(a, str) for q, a in SMOKE)


def test_result_accuracy_and_cost_per_correct():
    r = Result("caucus", correct=3, total=4, output_tokens=500, cost_usd=0.012, seconds=2.0)
    assert r.accuracy == 0.75
    assert round(r.cost_per_correct, 4) == 0.004
    zero = Result("x", correct=0, total=4, output_tokens=10, cost_usd=0.0, seconds=0.1)
    assert zero.cost_per_correct == float("inf")


def test_format_table_shows_accuracy_and_cost():
    table = format_table([
        Result("baseline", 2, 4, 300, 0.001, 1.0),
        Result("caucus", 3, 4, 1500, 0.005, 3.0),
    ])
    assert "accuracy" in table and "cost($)" in table and "$/correct" in table
    assert "baseline" in table and "caucus" in table
