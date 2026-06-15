"""A CURATED reasoning probe — a quick, real baseline-vs-synth A/B against a live model.

**This is NOT a benchmark.** It is a small, hand-written probe set used to drive a fast,
real A/B (single-model baseline vs the full synthesized combo) when a live provider is available —
just enough signal to see synth move the needle before paying for the real thing. It makes
**no** claim of statistical power, coverage, or comparability with published results.

For the *real* wrapped harnesses, see ``benchmarks/README.md`` (and ``caucus.benchmarks.bench``):
reasoning/single-turn go through ``lm-evaluation-harness`` (MMLU-Pro, GPQA) and ``evalplus``
(HumanEval+, MBPP+) by base-URL swap; coding/agentic go through SWE-bench Verified +
the official ``swebench`` scorer. **Never reimplement a benchmark** — wrap the
existing harness. This probe deliberately does *not* try to be one.

Two things live here:

* ``PROBE`` — exactly 24 ``(question, expected_answer, category)`` tuples. Every question is
  **original** (hand-written, to avoid contamination + licensing — we do NOT copy MMLU / GPQA /
  HumanEval items) with a short, unambiguous answer that a normalized substring / numeric match
  can grade. Difficulty is calibrated so a single cheap model gets *some* wrong, while a careful
  answer (or synth) can do better: multi-step arithmetic, small logic puzzles, precise factual
  recall, short code-output prediction. Each question ends with a terseness instruction.
* ``matches(answer_text, expected)`` — a robust grader: lowercase + strip markdown/punctuation,
  collapse whitespace; for a numeric ``expected`` it compares the **last** number in the answer
  numerically (tolerating ``42``/``42.0``/``$42``/``1,000``); otherwise it accepts ``expected`` as
  a normalized whole-token or token-sequence substring. It is lenient about trailing punctuation
  and articles but avoids false positives (``"14"`` must not satisfy expected ``"4"``).

Feed ``PROBE`` to ``caucus.benchmarks.bench.run_config`` (passing ``matches`` as the grader) to
get the accuracy-AND-cost A/B table — never a score without its cost.
"""

from __future__ import annotations

import re

# Allowed categories (the probe spreads across all five).
CATEGORIES = ("math", "logic", "science", "knowledge", "code")

# EXACTLY 24 original, hand-written items: (question, expected_answer, category).
# Each has a short, unambiguous answer and ends with a terseness instruction.
PROBE: list[tuple[str, str, str]] = [
    # ---- math (5) — multi-step arithmetic where a cheap model can slip ----
    (
        "A train leaves at 9:45 and arrives at 14:20 the same day. "
        "How many minutes was the journey? Answer with only the number.",
        "275",
        "math",
    ),
    (
        "A shirt costs $80. It is discounted 25%, then 8% sales tax is added to the "
        "discounted price. What is the final price in dollars? Answer with only the number.",
        "64.80",
        "math",
    ),
    (
        "What is the smallest positive integer that is divisible by both 6 and 8? "
        "Answer with only the number.",
        "24",
        "math",
    ),
    (
        "I think of a number, multiply it by 3, then subtract 7, and get 20. "
        "What was the original number? Answer with only the number.",
        "9",
        "math",
    ),
    (
        "A rectangle is 7 cm wide and 12 cm long. What is its area in square centimetres? "
        "Answer with only the number.",
        "84",
        "math",
    ),

    # ---- logic (5) — small puzzles / careful reading ----
    (
        "All bloops are razzies, and all razzies are lazzies. Are all bloops definitely "
        "lazzies? Answer yes or no.",
        "yes",
        "logic",
    ),
    (
        "Tom is older than Sara. Sara is older than Mike. Who is the youngest? "
        "Answer with one name.",
        "Mike",
        "logic",
    ),
    (
        "If today is Wednesday, what day of the week will it be 10 days from now? "
        "Answer with one word.",
        "Saturday",
        "logic",
    ),
    (
        "A bat and a ball cost $1.10 in total. The bat costs $1.00 more than the ball. "
        "How much does the ball cost in cents? Answer with only the number.",
        "5",
        "logic",
    ),
    (
        "Some cats are black. No black things glow. Can a glowing thing be one of those "
        "black cats? Answer yes or no.",
        "no",
        "logic",
    ),

    # ---- science (5) — precise facts a careful model nails ----
    (
        "What is the chemical symbol for the element gold? Answer with the symbol only.",
        "Au",
        "science",
    ),
    (
        "How many bones are there in the adult human body? Answer with only the number.",
        "206",
        "science",
    ),
    (
        "Which planet in our solar system is the largest by volume? Answer with one word.",
        "Jupiter",
        "science",
    ),
    (
        "What gas do plants primarily absorb from the air during photosynthesis? "
        "Answer with two words.",
        "carbon dioxide",
        "science",
    ),
    (
        "At what temperature in degrees Celsius does water boil at sea level? "
        "Answer with only the number.",
        "100",
        "science",
    ),

    # ---- knowledge (5) — precise factual recall ----
    (
        "What is the capital city of Australia? Answer with one word.",
        "Canberra",
        "knowledge",
    ),
    (
        "How many sides does a hexagon have? Answer with only the number.",
        "6",
        "knowledge",
    ),
    (
        "In which year did the Second World War end? Answer with only the year.",
        "1945",
        "knowledge",
    ),
    (
        "What is the longest river in the world (by most common measure)? "
        "Answer with one word.",
        "Nile",
        "knowledge",
    ),
    (
        "How many continents are there on Earth? Answer with only the number.",
        "7",
        "knowledge",
    ),

    # ---- code (4) — short output-prediction, no execution needed ----
    (
        "In Python, what does len('banana') return? Answer with only the number.",
        "6",
        "code",
    ),
    (
        "In Python, what is the value of 7 // 2? Answer with only the number.",
        "3",
        "code",
    ),
    (
        "In Python, what does 'ab' * 3 evaluate to? Answer with the string value only, "
        "without quotes.",
        "ababab",
        "code",
    ),
    (
        "In Python, what does the expression 2 ** 5 evaluate to? Answer with only the number.",
        "32",
        "code",
    ),
]


# --------------------------------------------------------------------------------------------
# Grading
# --------------------------------------------------------------------------------------------

# A number like 42, 42.0, -3, 1,000, 64.80 — commas as thousands separators are tolerated.
_NUMBER_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")
# Leading articles to ignore so "the Nile" matches "Nile".
_ARTICLES = ("the ", "a ", "an ")


def _normalize(text: str) -> str:
    """Lowercase, strip markdown emphasis, collapse whitespace, drop surrounding punctuation."""
    s = text.lower()
    # Drop markdown emphasis / code fences that wrap a short answer.
    s = s.replace("**", " ").replace("*", " ").replace("`", " ").replace("#", " ")
    s = s.replace("_", " ")
    # Normalise smart quotes and assorted quote/bracket chars to spaces.
    s = re.sub(r"[\"'“”‘’()\[\]{}<>]", " ", s)
    # Collapse all whitespace runs to single spaces.
    s = re.sub(r"\s+", " ", s).strip()
    # Strip surrounding sentence punctuation.
    s = s.strip(" .,!?:;")
    return s


def _strip_article(token_seq: str) -> str:
    for art in _ARTICLES:
        if token_seq.startswith(art):
            return token_seq[len(art):]
    return token_seq


def _to_float(num_str: str) -> float:
    return float(num_str.replace(",", ""))


def _last_number(text: str) -> float | None:
    matches = _NUMBER_RE.findall(text)
    if not matches:
        return None
    try:
        return _to_float(matches[-1])
    except ValueError:
        return None


def _looks_numeric(expected_norm: str) -> bool:
    return bool(_NUMBER_RE.fullmatch(expected_norm))


def matches(answer_text: str, expected: str) -> bool:
    """Return True if ``answer_text`` correctly answers an item with answer ``expected``.

    Robust to formatting: lowercases, strips markdown/punctuation/quotes, collapses spaces,
    ignores leading articles. For a numeric ``expected`` it compares the **last** number in the
    answer numerically (so ``"The answer is 42."`` matches ``"42"``, and ``"$64.80"`` matches
    ``"64.80"``). For a textual ``expected`` it requires a whole-token / token-sequence match
    (so ``"14"`` does NOT match ``"4"`` and ``"sparrow"`` does NOT match ``"arrow"``).
    """
    if answer_text is None or expected is None:
        return False

    exp_norm = _normalize(expected)
    if not exp_norm:
        return False

    # ---- numeric expected: compare numerically against the last number in the answer ----
    if _looks_numeric(exp_norm):
        exp_val = _to_float(exp_norm)
        got = _last_number(answer_text)
        if got is None:
            return False
        # Exact for integers; small tolerance for floats (e.g. 64.8 vs 64.80).
        return abs(got - exp_val) < 1e-9

    # ---- textual expected: whole-token sequence match (no partial-word false positives) ----
    ans_norm = _normalize(answer_text)
    if not ans_norm:
        return False

    exp_tokens = _strip_article(exp_norm).split()
    ans_tokens = [_strip_article(t) for t in ans_norm.split()]
    # Also keep the un-stripped answer tokens so "the nile" still contains "nile".
    raw_ans_tokens = ans_norm.split()

    if not exp_tokens:
        return False

    n = len(exp_tokens)
    for window in (ans_tokens, raw_ans_tokens):
        for i in range(len(window) - n + 1):
            if window[i:i + n] == exp_tokens:
                return True
    return False
