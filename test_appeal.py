import json
import os
import re
import subprocess
import sys
from datetime import date
from functools import cache
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

import appeal
from appeal import _heading_above, run, verify

ROOT = Path(__file__).parent
CASES = ROOT / "cases"
POLICY = ROOT / "data" / "policy.pdf"
# Opt-in, not key presence: appeal.py loads .env at import, so a key is almost always set.
needs_llm = pytest.mark.skipif(
    not (os.environ.get("CHARTPROOF_LLM") == "1"
         and (os.environ.get("GEMINI_API_KEY") or os.environ.get("OPENROUTER_API_KEY")) and POLICY.exists()),
    reason="calls the LLM: needs CHARTPROOF_LLM=1, GEMINI_API_KEY (or OPENROUTER_API_KEY) and data/policy.pdf",
)
# Ground truth lives in cases/<case>/expected.json and is set by a human reviewer, never from output.
EXPECTED = {p.parent.name: json.loads(p.read_text(encoding="utf-8")) for p in sorted(CASES.glob("*/expected.json"))}


@cache  # one pipeline run (4 LLM calls) per case per test session
def run_case(name: str) -> tuple[str, dict]:
    out, decision = run(CASES / name / "denial.txt", POLICY, CASES / name / "chart.txt")
    print(out)  # shown by pytest on failure, or always with -rP
    return out, decision


def test_verify_drops_quote_not_in_chart_and_keeps_one_that_is():
    chart = (CASES / "supported" / "chart.txt").read_text(encoding="utf-8")
    policy = "Failure of conservative treatment* for at least six (6)\nweeks within the last six (6) months16"
    evidence = [
        {"claim": "real", "quote": "no improvement.  pain 7/10 AT REST", "note_date": "04/20/2026"},
        {"claim": "invented", "quote": "Completed 12 weeks of physical therapy", "note_date": "04/20/2026"},
        {"claim": "wrong note", "quote": "No improvement. Pain 7/10 at rest", "note_date": "03/02/2026"},
    ]
    criterion = {"section": "Back pain", "criterion_text": "failure of conservative treatment* for at least six (6) weeks"}

    kept, checked = verify(evidence, chart, policy, criterion)

    assert [e["claim"] for e in kept] == ["real"]
    assert kept[0]["quote"] == "No improvement. Pain 7/10 at rest"  # exact chart text, not the LLM's casing
    assert checked["criterion_text"] == "Failure of conservative treatment* for at least six (6) weeks"
    assert verify([], chart, policy, {**criterion, "criterion_text": "at least eight weeks"})[1] is None


def test_section_is_nearest_heading_above_criterion():
    policy = (  # shape of the pypdf text around the real criterion
        "INDICATIONS FOR LUMBAR SPINE MRI\n"
        "For evaluation of back pain with any of the following7-16 \n"
        "* National Imaging Associates, Inc. (NIA) is a subsidiary\n"
        "2— Lumbar Spine MRI\n© 2019-2022 National Imaging Associates\n"
        "(AAFP, 2012; ACEP, 2014;\nNASS, 2013; Schneider, 2019)\n"
        " With new or worsening objective neurologic deficits\n"
        " Failure of conservative treatment* for at least six (6) weeks\n"
    )
    assert _heading_above(policy, "Failure of conservative treatment*") == \
        "For evaluation of back pain with any of the following"


@pytest.mark.parametrize("body", [{"choices": []}, {"choices": [{"message": {"content": ""}}]}],
                         ids=["empty_choices", "empty_content"])
def test_empty_openrouter_reply_is_retried_then_fails_loudly(monkeypatch, body):
    posts = []

    def fake_post(*args, **kwargs):  # HTTP 200 with no usable reply; no network
        posts.append(kwargs["json"]["model"])
        r = requests.Response()
        r.status_code, r._content = 200, json.dumps(body).encode()
        return r

    def must_not_run(*args, **kwargs):
        raise AssertionError("pipeline continued past a failed LLM call")

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(appeal.requests, "post", fake_post)
    monkeypatch.setattr(appeal.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(appeal, "PdfReader", lambda path: SimpleNamespace(pages=[]))
    monkeypatch.setattr(appeal, "verify", must_not_run)
    monkeypatch.setattr(appeal, "draft_letter", must_not_run)

    with pytest.raises(RuntimeError, match="no reply text after 3 attempts"):
        appeal.run(CASES / "supported" / "denial.txt", POLICY, CASES / "supported" / "chart.txt")
    assert len(posts) == 3


@needs_llm
def test_insufficient_case_never_produces_a_letter():
    out, _ = run_case("insufficient")
    assert out.startswith("INSUFFICIENT DOCUMENTATION")
    assert "APPEAL" not in out


@needs_llm
@pytest.mark.parametrize("name", EXPECTED)
def test_case_matches_expected(name):
    exp = EXPECTED[name]
    out, decision = run_case(name)
    wants_letter = exp["verdict"] == "letter"
    assert decision["supported"] is wants_letter
    assert out.startswith("APPEAL OF PRIOR AUTHORIZATION DENIAL" if wants_letter else "INSUFFICIENT DOCUMENTATION")

    cited = {d[:5] for d in re.findall(r'\[Chart: (\d\d/\d\d/\d{4}) "', out)}  # MM/DD
    required, optional, forbidden = (set(exp[k]) for k in ("required_citations", "optional_citations", "forbidden_citations"))
    assert required <= cited, f"required dates not cited: {sorted(required - cited)}"
    assert cited <= required | optional, f"cited dates outside required + optional: {sorted(cited - required - optional)}"
    assert not cited & forbidden, f"forbidden dates cited: {sorted(cited & forbidden)}"

    weeks = decision.get("weeks")
    assert (None if weeks is None else round(weeks, 1)) == exp["weeks"]
    if name == "stale":  # the verdict must come from the six-month window, not a missing session
        assert "6-month window before the denial date" in decision["reason"]


def test_six_month_window_is_measured_from_the_denial_date(monkeypatch):
    sessions = [  # a 7-week PT course, 05/22/2025 to 07/10/2025, with documented failure
        {"claim": "PT start", "quote": "q1", "note_date": "05/22/2025", "type": "treatment_session"},
        {"claim": "PT end, no improvement", "quote": "q2", "note_date": "07/10/2025", "type": "treatment_session"},
        {"claim": "follow-up", "quote": "q3", "note_date": "03/02/2026", "type": "follow_up"},
    ]
    judged = appeal.Judgment(treatment_items=[0, 1], failure_items=[1], contrary_items=[], missing="")
    monkeypatch.setattr(appeal, "ask", lambda prompt, schema: judged)
    criterion = "Failure of conservative treatment* for at least six (6) weeks within the last six (6) months"

    stale = appeal.decide(sessions, criterion, appeal._parse_date("03/10/2026"))  # course ended 8 months earlier
    assert stale["supported"] is False and stale["weeks"] == 7.0
    assert "window starts 09/10/2025" in stale["reason"]

    fresh = appeal.decide(sessions, criterion, appeal._parse_date("08/01/2025"))  # course ended 3 weeks earlier
    assert fresh["supported"] is True and fresh["weeks"] == 7.0
    assert [e["note_date"] for e in fresh["supporting"]] == ["05/22/2025", "07/10/2025"]  # follow-up not cited, not counted


@needs_llm
@pytest.mark.parametrize("name", [n for n, exp in EXPECTED.items() if exp["verdict"] == "letter"])
def test_letter_citations_appear_in_chart_verbatim(name):
    out, _ = run_case(name)
    chart = " ".join((CASES / name / "chart.txt").read_text(encoding="utf-8").split())
    quotes = re.findall(r'\[Chart: \d\d/\d\d/\d{4} "(.*?)"\]', out)
    assert quotes
    for quote in quotes:
        assert quote in chart, quote


@pytest.mark.parametrize("chart, denial, error", [
    ("Patient seen for back pain; no dated notes.\n", "Date: 09/23/2026\n", "no dated note headers found in chart"),
    ("2026-07-06\nNaproxen started.\n", "Service requested: lumbar procedure\nDate of birth: unknown\n",
     "no 'Date:' or 'Review date:' line"),
], ids=["no_dated_notes", "no_denial_date"])
def test_parse_failure_is_an_input_error_never_a_verdict(tmp_path, chart, denial, error):
    (tmp_path / "chart.txt").write_text(chart, encoding="utf-8")
    (tmp_path / "denial.txt").write_text(denial, encoding="utf-8")
    env = {**os.environ, "OPENROUTER_API_KEY": "", "GEMINI_API_KEY": ""}  # blank keys: no LLM call is possible
    r = subprocess.run([sys.executable, str(ROOT / "appeal.py"), "--denial", str(tmp_path / "denial.txt"),
                        "--policy", str(tmp_path / "missing.pdf"), "--chart", str(tmp_path / "chart.txt")],
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode != 0
    assert r.stdout == ""  # no letter, no INSUFFICIENT DOCUMENTATION
    assert f"INPUT ERROR: {error}" in r.stderr


@pytest.mark.parametrize("header", ["=== 03/02/2026 | PT Initial Evaluation | Jordan Kim, PT ===",
                                    "=== 2026-03-02 PT Initial Evaluation ===", "03/02/2026", "2026-03-02"])
def test_note_header_formats_are_parsed_and_note_dates_become_iso(header):
    chart = f"SYNTHETIC\n\n{header}\nPT evaluation completed.\n\n2026-03-23\nNo improvement.\n"
    evidence = [{"claim": "a", "quote": "PT evaluation completed.", "note_date": "03/02/2026"},
                {"claim": "b", "quote": "No improvement.", "note_date": "2026-03-23"}]
    kept, _ = verify(evidence, chart, "x", {"section": "s", "criterion_text": "x"})
    assert [e["note_date"] for e in kept] == ["2026-03-02", "2026-03-23"]


@pytest.mark.parametrize("line", ["Date: 09/23/2026", "Date: 2026-09-23", "Review date: 09/23/2026", "Review date: 2026-09-23"])
def test_denial_date_formats(line):
    denial = f"SYNTHETIC\nService requested: lumbar procedure\n{line}\nClinical criterion: six weeks.\n"
    assert appeal._denial_date(denial) == date(2026, 9, 23)


def test_quote_mark_styles_match_but_changed_words_are_dropped():
    chart = "2026-09-10\nLater summary: \"PT x6wk, failed.\" Patient calls it ‘useless’.\n"
    evidence = [
        {"claim": "latex", "quote": "Later summary: ``PT x6wk, failed.''", "note_date": "09/10/2026"},
        {"claim": "curly", "quote": "Later summary: “PT x6wk, failed.”", "note_date": "09/10/2026"},
        {"claim": "straight for curly", "quote": "calls it 'useless'", "note_date": "09/10/2026"},
        {"claim": "changed words", "quote": "Later summary: ``PT x6wk, improved.''", "note_date": "09/10/2026"},
    ]
    kept, _ = verify(evidence, chart, "x", {"section": "s", "criterion_text": "x"})
    assert [e["claim"] for e in kept] == ["latex", "curly", "straight for curly"]
    assert kept[0]["quote"] == 'Later summary: "PT x6wk, failed."'  # the chart's own text


def test_letters_show_clean_policy_text_but_verification_uses_the_raw_text():
    raw = chr(0xF0B7) + " Failure of conservative treatment* for at least six (6) weeks within the last six (6) months16"
    policy = "For evaluation of back pain with any of the following7-16\n" + raw + "\n"
    _, checked = verify([], "2026-01-01\nx", policy, {"section": "Back pain", "criterion_text": raw})
    assert checked["criterion_text"] == raw  # verified against, and kept as, the raw policy text
    clean = '"Failure of conservative treatment for at least six (6) weeks within the last six (6) months"'
    assert clean in appeal.insufficient_report(checked, {"reason": "r", "missing": []})
    assert clean in appeal.draft_letter("MRI", checked, {"supporting": [], "weeks_needed": None})
