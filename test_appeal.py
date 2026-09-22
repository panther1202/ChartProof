import json
import os
import re
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
needs_llm = pytest.mark.skipif(
    not ((os.environ.get("GEMINI_API_KEY") or os.environ.get("OPENROUTER_API_KEY")) and POLICY.exists()),
    reason="needs GEMINI_API_KEY (or OPENROUTER_API_KEY) and data/policy.pdf (run fetch_policy.py)",
)
# case: (expects an appeal letter, treatment weeks decide() must compute; None = not checked)
EXPECTED = {
    "supported": (True, 7.0),
    "insufficient": (False, 3.0),
    "borderline_under": (False, 5.7),
    "borderline_exact": (True, 6.0),
    "messy": (True, None),
}


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
def test_verdict_and_computed_weeks(name):
    wants_letter, weeks = EXPECTED[name]
    out, decision = run_case(name)
    assert decision["supported"] is wants_letter
    assert out.startswith("APPEAL OF PRIOR AUTHORIZATION DENIAL" if wants_letter else "INSUFFICIENT DOCUMENTATION")
    if weeks is not None:
        assert decision.get("weeks") == weeks


@needs_llm
@pytest.mark.parametrize("name", [n for n, (letter, _) in EXPECTED.items() if letter])
def test_letter_citations_appear_in_chart_verbatim(name):
    out, _ = run_case(name)
    chart = " ".join((CASES / name / "chart.txt").read_text(encoding="utf-8").split())
    quotes = re.findall(r'\[Chart: \d\d/\d\d/\d{4} "(.*?)"\]', out)
    assert quotes
    for quote in quotes:
        assert quote in chart, quote
