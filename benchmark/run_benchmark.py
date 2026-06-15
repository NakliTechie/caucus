#!/usr/bin/env python3
"""Caucus sandbox-and-test benchmark.

Question: on code (action) turns, does Caucus's sandbox-and-test selection beat (a) a single cheap
shot, (b) a single EXPENSIVE shot, and (c) a text-judge that reads candidates without running them?

Per task, per trial: generate N cheap candidates ONCE, then apply each selector to data derived from
the SAME candidates (isolates the mechanism, not the model):

  flash@1     one cheap candidate alone                        -> one cheap shot
  pro@1       one strong-model generation                      -> the expensive single model
  synth@3     a judge READS 3 candidates and writes one        -> Caucus plan-turn synth (never runs)
  runtest@N   RUN the candidates in Caucus's real sandbox,     -> Caucus action-turn (sandbox-and-test)
              keep one that passes (N = 2,3,5)

Every produced solution is scored by running it against the task's assertions INSIDE Caucus's real
seatbelt sandbox (ephemeral copy, network denied, $HOME denied) via the shipped selection.py +
sandbox.py — not a proxy. A validation pass confirms the shipped select_survivor() agrees with the
measured pass@N on every trial.

Usage:
  python benchmark/run_benchmark.py --set broad   # 18 tasks, K=3 (mostly within a strong model's reach)
  python benchmark/run_benchmark.py --set hard     # 6 fiddly parser tasks, K=5 (single shot unreliable)

Keys are read from the Caucus keystore (~/.config/caucus/.env). Override models with BENCH_CHEAP /
BENCH_STRONG env vars.
"""
import os, re, sys, json, time, tempfile, argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[1]
try:
    from caucus import sandbox as sbx
    from caucus.selection import select_survivor, CandidateEdit
except ImportError:
    sys.path.insert(0, str(ROOT / "src"))
    from caucus import sandbox as sbx
    from caucus.selection import select_survivor, CandidateEdit
import litellm
litellm.suppress_debug_info = True
litellm.num_retries = 2
litellm.request_timeout = 150

_env = Path.home() / ".config/caucus/.env"
if _env.exists():
    for line in _env.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

CHEAP = os.environ.get("BENCH_CHEAP", "deepseek/deepseek-v4-flash")
STRONG = os.environ.get("BENCH_STRONG", "deepseek/deepseek-v4-pro")
# the seatbelt sandbox denies $HOME reads, so the test interpreter must live OUTSIDE $HOME
HOME = str(Path.home())
TEST_INTERP = next((p for p in ("/usr/bin/python3", "/usr/local/bin/python3",
                                "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3")
                    if os.path.exists(p) and not p.startswith(HOME)), "/usr/bin/python3")
TEST_CMD = [TEST_INTERP, "test_solution.py"]
NMAX = 5
SANDBOX = sbx.SeatbeltSandbox() if sbx.SeatbeltSandbox.available() else sbx.get_sandbox()
CONFIGS = ["flash@1", "pro@1", "synth@3", "runtest@2", "runtest@3", "runtest@5"]


def P(name, spec):
    return (f"Write a Python function {name} that {spec} "
            f"Return ONLY the function definition in a single ```python code block, no prose.")


BROAD = [
 ("regex_match", P("regex_match(s, p)", "implements regular-expression matching where '.' matches any single char and '*' matches zero or more of the PRECEDING element; the match must cover the entire string."),
  "assert regex_match('aa','a')==False\nassert regex_match('aa','a*')==True\nassert regex_match('ab','.*')==True\nassert regex_match('aab','c*a*b')==True\nassert regex_match('mississippi','mis*is*p*.')==False"),
 ("valid_number", P("valid_number(s)", "returns True iff s is a valid number: an optional sign, then an integer or decimal, then an optional exponent (e/E followed by an optional-signed integer). No surrounding spaces."),
  "assert valid_number('0')==True\nassert valid_number('3.14')==True\nassert valid_number('-1E-16')==True\nassert valid_number('+.8')==True\nassert valid_number('e')==False\nassert valid_number('.')==False\nassert valid_number('1e')==False\nassert valid_number('99e2.5')==False\nassert valid_number('--6')==False"),
 ("calculate", P("calculate(s)", "evaluates a string arithmetic expression with +, -, *, / and parentheses, honoring operator precedence; integer division truncates toward zero."),
  "assert calculate('3+2*2')==7\nassert calculate('(1+(4+5+2)-3)+(6+8)')==23\nassert calculate(' 2-1 + 2 ')==3\nassert calculate('2*(5+5*2)/3+(6/2+8)')==21"),
 ("multiply_strings", P("multiply_strings(a, b)", "multiplies two non-negative integers given as strings and returns the product as a string, WITHOUT converting the inputs to int."),
  "assert multiply_strings('2','3')=='6'\nassert multiply_strings('123','456')=='56088'\nassert multiply_strings('0','52')=='0'\nassert multiply_strings('999','999')=='998001'"),
 ("decode_ways", P("decode_ways(s)", "counts the ways to decode a digit string where A=1..Z=26 (a leading 0 or an invalid pair contributes 0 ways)."),
  "assert decode_ways('12')==2\nassert decode_ways('226')==3\nassert decode_ways('06')==0\nassert decode_ways('0')==0\nassert decode_ways('100')==0\nassert decode_ways('2101')==1"),
 ("trap", P("trap(height)", "returns how much rain water can be trapped given an elevation map (list of non-negative ints)."),
  "assert trap([0,1,0,2,1,0,1,3,2,1,2,1])==6\nassert trap([4,2,0,3,2,5])==9\nassert trap([])==0"),
 ("largest_rectangle", P("largest_rectangle(heights)", "returns the area of the largest rectangle in a histogram given by the list of bar heights."),
  "assert largest_rectangle([2,1,5,6,2,3])==10\nassert largest_rectangle([2,4])==4\nassert largest_rectangle([1])==1\nassert largest_rectangle([2,1,2])==3"),
 ("min_window", P("min_window(s, t)", "returns the minimum-length substring of s that contains every character of t (with multiplicity), or '' if none exists."),
  "assert min_window('ADOBECODEBANC','ABC')=='BANC'\nassert min_window('a','a')=='a'\nassert min_window('a','aa')==''"),
 ("next_permutation", P("next_permutation(nums)", "returns the next lexicographically greater permutation of the list of ints; if none exists, returns the smallest (ascending) permutation."),
  "assert next_permutation([1,2,3])==[1,3,2]\nassert next_permutation([3,2,1])==[1,2,3]\nassert next_permutation([1,1,5])==[1,5,1]\nassert next_permutation([1,3,2])==[2,1,3]"),
 ("jump_game_min", P("jump_game_min(nums)", "returns the minimum number of jumps to reach the last index, where nums[i] is the max jump length from index i (assume the end is always reachable)."),
  "assert jump_game_min([2,3,1,1,4])==2\nassert jump_game_min([2,3,0,1,4])==2\nassert jump_game_min([0])==0\nassert jump_game_min([1,2,3])==2"),
 ("atoi", P("atoi(s)", "converts a string to a 32-bit signed integer (like C atoi): skip leading spaces, then an optional +/- sign, then digits until a non-digit; ignore the rest. If no digits, return 0. Clamp to [-2**31, 2**31-1]."),
  "assert atoi('42')==42\nassert atoi('   -42')==-42\nassert atoi('4193 with words')==4193\nassert atoi('words and 987')==0\nassert atoi('-91283472332')==-2147483648\nassert atoi('21474836460')==2147483647\nassert atoi('+1')==1\nassert atoi('  +0 123')==0"),
 ("wildcard_match", P("wildcard_match(s, p)", "implements wildcard pattern matching where '?' matches any single char and '*' matches any sequence (including empty); the match must cover the entire string."),
  "assert wildcard_match('aa','a')==False\nassert wildcard_match('aa','*')==True\nassert wildcard_match('cb','?a')==False\nassert wildcard_match('adceb','*a*b')==True\nassert wildcard_match('acdcb','a*c?b')==False"),
 ("edit_distance", P("edit_distance(a, b)", "returns the minimum number of single-character insertions, deletions, or substitutions to turn string a into string b (Levenshtein distance)."),
  "assert edit_distance('horse','ros')==3\nassert edit_distance('intention','execution')==5\nassert edit_distance('','abc')==3\nassert edit_distance('abc','')==3\nassert edit_distance('abc','abc')==0"),
 ("longest_valid_parens", P("longest_valid_parens(s)", "returns the length of the longest substring of well-formed (valid) parentheses in the string s of '(' and ')'."),
  "assert longest_valid_parens('(()')==2\nassert longest_valid_parens(')()())')==4\nassert longest_valid_parens('')==0\nassert longest_valid_parens('()(())')==6\nassert longest_valid_parens('((')==0"),
 ("three_sum_count", P("three_sum_count(nums)", "returns the number of UNIQUE triplets (no duplicate triplets by value) in the list that sum to zero."),
  "assert three_sum_count([-1,0,1,2,-1,-4])==2\nassert three_sum_count([0,0,0])==1\nassert three_sum_count([0,0,0,0])==1\nassert three_sum_count([1,2,-2,-1])==0\nassert three_sum_count([-2,0,1,1,2])==2"),
 ("word_break", P("word_break(s, words)", "returns True iff s can be segmented into a space-separated sequence of one or more words from the list `words` (each word reusable)."),
  "assert word_break('leetcode',['leet','code'])==True\nassert word_break('applepenapple',['apple','pen'])==True\nassert word_break('catsandog',['cats','dog','sand','and','cat'])==False\nassert word_break('',[])==True\nassert word_break('a',['a'])==True"),
 ("coin_change", P("coin_change(coins, amount)", "returns the fewest number of coins (denominations may repeat) that sum to `amount`, or -1 if it cannot be made."),
  "assert coin_change([1,2,5],11)==3\nassert coin_change([2],3)==-1\nassert coin_change([1],0)==0\nassert coin_change([1,2,5],100)==20\nassert coin_change([186,419,83,408],6249)==20"),
 ("add_binary", P("add_binary(a, b)", "adds two binary strings and returns their sum as a binary string, WITHOUT using int(x, 2) or bin()."),
  "assert add_binary('11','1')=='100'\nassert add_binary('1010','1011')=='10101'\nassert add_binary('0','0')=='0'\nassert add_binary('1','111')=='1000'"),
]

HARD = [
 ("calculate", P("calculate(s)", "evaluates a string arithmetic expression with +, -, *, / and parentheses, honoring operator precedence; integer division truncates toward zero."),
  "assert calculate('3+2*2')==7\nassert calculate('(1+(4+5+2)-3)+(6+8)')==23\nassert calculate(' 2-1 + 2 ')==3\nassert calculate('2*(5+5*2)/3+(6/2+8)')==21\nassert calculate('(2+6*3+5-(3*14/7+2)*5)+3')==-12"),
 ("valid_number", P("valid_number(s)", "returns True iff s is a valid number: an optional sign, then an integer or decimal, then an optional exponent (e/E followed by an optional-signed integer). No surrounding spaces."),
  "assert valid_number('0')==True\nassert valid_number('3.14')==True\nassert valid_number('-1E-16')==True\nassert valid_number('+.8')==True\nassert valid_number('e')==False\nassert valid_number('.')==False\nassert valid_number('1e')==False\nassert valid_number('99e2.5')==False\nassert valid_number('--6')==False\nassert valid_number('.e1')==False\nassert valid_number('4e+')==False"),
 ("word_break", P("word_break(s, words)", "returns True iff s can be segmented into a space-separated sequence of one or more words from the list `words` (each word reusable)."),
  "assert word_break('leetcode',['leet','code'])==True\nassert word_break('applepenapple',['apple','pen'])==True\nassert word_break('catsandog',['cats','dog','sand','and','cat'])==False\nassert word_break('',[])==True\nassert word_break('aaaaaaa',['aaaa','aaa'])==True\nassert word_break('aaaaaaab',['aaaa','aaa'])==False"),
 ("fraction_to_decimal", P("fraction_to_decimal(num, den)", "returns the fraction num/den as a string; if the fractional part repeats, enclose the repeating part in parentheses. Handle negatives."),
  "assert fraction_to_decimal(1,2)=='0.5'\nassert fraction_to_decimal(2,1)=='2'\nassert fraction_to_decimal(2,3)=='0.(6)'\nassert fraction_to_decimal(4,333)=='0.(012)'\nassert fraction_to_decimal(1,6)=='0.1(6)'\nassert fraction_to_decimal(-50,8)=='-6.25'"),
 ("decode_string", P("decode_string(s)", "decodes a string encoded as k[encoded] meaning the encoded part repeats k times; brackets may be nested."),
  "assert decode_string('3[a]2[bc]')=='aaabcbc'\nassert decode_string('3[a2[c]]')=='accaccacc'\nassert decode_string('2[abc]3[cd]ef')=='abcabccdcdcdef'\nassert decode_string('abc3[cd]xyz')=='abccdcdcdxyz'"),
 ("basic_calculator_iii", P("basic_calculator_iii(s)", "evaluates a string expression with +, -, *, / and parentheses, honoring precedence; integer division truncates toward zero."),
  "assert basic_calculator_iii('1+1')==2\nassert basic_calculator_iii('6-4/2')==4\nassert basic_calculator_iii('2*(5+5*2)/3+(6/2+8)')==21\nassert basic_calculator_iii('(2+6*3+5-(3*14/7+2)*5)+3')==-12"),
]


def gen(model, prompt, temp, mt=2000):
    try:
        r = litellm.completion(model=model, messages=[{"role": "user", "content": prompt}],
                               max_tokens=mt, temperature=temp)
        return r.choices[0].message.content or ""
    except Exception as e:
        return "__ERR__ " + type(e).__name__


def extract(a):
    b = re.findall(r"```(?:python)?\s*\n(.*?)```", a, re.DOTALL)
    return max(b, key=len) if b else a


def synth_judge(prompt, cands):
    body = "Several models attempted this task:\n\n" + prompt + "\n\nTheir solutions:\n"
    for i, c in enumerate(cands):
        body += f"\n--- candidate {i + 1} ---\n{c}\n"
    body += ("\nReview them and produce the single best, correct solution. Return ONLY the function "
             "in one ```python code block, no prose.")
    return gen(STRONG, body, 0.0)


def make_ws(test_src):
    d = Path(tempfile.mkdtemp(prefix="caucus-bench-")) / "ws"
    d.mkdir()
    (d / "solution.py").write_text("# placeholder\n")
    (d / "test_solution.py").write_text(f"from solution import *\n{test_src}\nprint('PASS')\n")
    return d


def real_pass(code, ws):
    copy = sbx.ephemeral_copy(ws)
    try:
        (copy / "solution.py").write_text(code)
        return sbx.run_tests(SANDBOX, copy, TEST_CMD, timeout=20).passed
    except Exception:
        return False
    finally:
        sbx.discard(copy)


class _Cand:
    def __init__(self, index, code):
        self.index, self.text = index, code
        self.edit = CandidateEdit(path="solution.py", op="create", new=code)


def one(job):
    (name, prompt, test), _trial = job
    ws = make_ws(test)
    try:
        codes = [extract(gen(CHEAP, prompt, 0.6)) for _ in range(NMAX)]
        passes = [real_pass(c, ws) for c in codes]
        sel = select_survivor(ws, [_Cand(i, c) for i, c in enumerate(codes)], TEST_CMD,
                              sandbox=SANDBOX, timeout=20)
        sel_pass = sel.reason.startswith("tests-pass") or sel.reason.startswith("judge")
        res = {
            "flash@1": passes[0],
            "pro@1": real_pass(extract(gen(STRONG, prompt, 0.4)), ws),
            "synth@3": real_pass(extract(synth_judge(prompt, codes[:3])), ws),
            "runtest@2": any(passes[:2]),
            "runtest@3": any(passes[:3]),
            "runtest@5": any(passes[:5]),
        }
        return name, res, sel_pass == any(passes)
    finally:
        sbx.discard(ws)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", choices=["broad", "hard"], default="broad")
    ap.add_argument("-k", type=int, default=None, help="trials per task (default 3 broad / 5 hard)")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()
    tasks = BROAD if args.set == "broad" else HARD
    K = args.k or (3 if args.set == "broad" else 5)
    if SANDBOX is None:
        sys.exit("No sandbox backend available (need Docker or macOS sandbox-exec) — cannot run.")

    jobs = [(t, k) for t in tasks for k in range(K)]
    scores = {c: {t[0]: 0 for t in tasks} for c in CONFIGS}
    agree = [0, 0]
    print(f"[{args.set}] {len(jobs)} trials ({len(tasks)} tasks x K={K}) · sandbox="
          f"{type(SANDBOX).__name__} · interp={TEST_INTERP} · cheap={CHEAP} strong={STRONG}", flush=True)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for name, res, ok in ex.map(one, jobs):
            for c in CONFIGS:
                scores[c][name] += int(res[c])
            agree[0] += int(ok); agree[1] += 1

    T = len(tasks) * K
    summary = {c: sum(scores[c].values()) for c in CONFIGS}
    out = {"set": args.set, "tasks": [t[0] for t in tasks], "K": K, "N": NMAX, "trials": T,
           "configs": CONFIGS, "scores": scores, "summary": summary,
           "summary_pct": {c: round(100 * summary[c] / T) for c in CONFIGS},
           "select_survivor_agreement": {"ok": agree[0], "total": agree[1]},
           "sandbox": type(SANDBOX).__name__, "cheap": CHEAP, "strong": STRONG,
           "elapsed_s": round(time.time() - t0, 1)}
    res_dir = ROOT / "benchmark" / "results"
    res_dir.mkdir(parents=True, exist_ok=True)
    (res_dir / f"{args.set}.json").write_text(json.dumps(out, indent=2))

    print(f"\n=== [{args.set}] pass rate over {T} trials ===")
    for c in CONFIGS:
        print(f"  {c:12} {summary[c]:3}/{T}  ({out['summary_pct'][c]}%)")
    print(f"select_survivor agreed {agree[0]}/{agree[1]} · {out['elapsed_s']}s · wrote results/{args.set}.json")


if __name__ == "__main__":
    main()
