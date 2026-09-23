"""Prior-authorization denial appeal drafter (v0).

python appeal.py --denial cases/<case>/denial.txt --policy data/policy.pdf --chart cases/<case>/chart.txt

Prints either an appeal letter in which every claim cites a verbatim chart quote or the
policy section, or "INSUFFICIENT DOCUMENTATION" with the unmet criterion and missing evidence.
Diagnostics (including every dropped quote) go to stderr.
"""
import argparse
import calendar
import os
import re
import sys
import time
from datetime import date, datetime
from functools import cache
from pathlib import Path
from typing import Literal

import requests
from google import genai
from google.genai import types
from pydantic import BaseModel, ValidationError
from pypdf import PdfReader

MODEL = "gemini-3.8-flash"  # latest stable Flash per https://ai.google.dev/gemini-api/docs/models
# ponytail: assumes each chart note starts with a line like "=== 03/02/2026 | ..."; real EHR exports need their own header pattern.
NOTE_HEADER = re.compile(r"^=+\s*(\d{1,2}/\d{1,2}/\d{4})", re.M)
NUMBERS = {w: i for i, w in enumerate("one two three four five six seven eight nine ten eleven twelve".split(), 1)}

# Load KEY=value lines from .env into the environment; variables already set win.
_env = Path(__file__).with_name(".env")
for _line in _env.read_text(encoding="utf-8").splitlines() if _env.exists() else []:
    _key, _sep, _value = _line.partition("=")
    if _sep and _value.strip() and not _key.lstrip().startswith("#"):
        os.environ.setdefault(_key.strip(), _value.strip().strip("\"'"))


class DenialReason(BaseModel):
    service: str
    denied_criterion: str


class PolicyCriterion(BaseModel):
    section: str
    criterion_text: str


class EvidenceItem(BaseModel):
    claim: str
    quote: str
    note_date: str
    type: Literal["treatment_session", "follow_up", "other"]


class Evidence(BaseModel):
    items: list[EvidenceItem]


class Judgment(BaseModel):
    treatment_items: list[int]
    failure_items: list[int]
    contrary_items: list[int]
    missing: str


@cache
def client() -> genai.Client:
    if not os.environ.get("GEMINI_API_KEY"):
        raise SystemExit("Set the GEMINI_API_KEY environment variable.")
    # SDK default is no retries; back off on 429/5xx (e.g. 503 "high demand"). Few attempts: each one counts against quota.
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"],
                        http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=3, initial_delay=5)))


_last_call = 0.0


def _openrouter(prompt: str, schema: type[BaseModel], model: str) -> str:
    """OpenRouter's OpenAI-compatible API (used when OPENROUTER_API_KEY is set). Defaults to the same
    Gemini model; OPENROUTER_MODEL takes any full OpenRouter model ID instead."""
    global _last_call
    model = os.environ.get("OPENROUTER_MODEL", f"google/{model}")
    for attempt in (1, 2, 3):
        # ponytail: fixed 3.5 s spacing keeps one process under OpenRouter's 20 requests/min free-model limit.
        time.sleep(max(0.0, _last_call + 3.5 - time.monotonic()))
        _last_call = time.monotonic()
        print(f"LLM request: {schema.__name__} via OpenRouter {model}", file=sys.stderr)
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
            # max_tokens: replies are small JSON; the 65k default reservation is refused (402) on low-credit accounts.
            json={"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 8192, "temperature": 0,
                  "response_format": {"type": "json_schema",
                                      "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema()}}},
            timeout=300,
        )
        text = ""
        if r.ok:
            choices = r.json().get("choices") or [{}]
            text = (choices[0].get("message") or {}).get("content") or ""
        # An upstream provider failure can come back as HTTP 200 with no choices or empty content.
        transient = r.status_code in (429, 502, 503) or (r.ok and not text.strip())
        if not transient or attempt == 3:
            break
        print(f"OpenRouter {r.status_code} {r.text[:200]}; retrying in 20 s", file=sys.stderr)
        time.sleep(20)
    r.raise_for_status()  # 402 (out of credit) fails here immediately, no retry
    if not text.strip():
        raise RuntimeError(f"OpenRouter returned no reply text after 3 attempts: {r.text[:300]}")
    return text


def ask(prompt: str, schema: type[BaseModel]) -> BaseModel:
    """One Gemini call with JSON output validated by pydantic; retry once, then fail loudly."""
    config = types.GenerateContentConfig(
        temperature=0,
        response_mime_type="application/json",
        response_schema=schema,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    model = os.environ.get("GEMINI_MODEL", MODEL)  # override when the default is overloaded (503)
    for attempt in (1, 2):
        if os.environ.get("OPENROUTER_API_KEY"):
            text = _openrouter(prompt, schema, model)
        else:
            print(f"LLM request: {schema.__name__} via Gemini {model}", file=sys.stderr)
            text = client().models.generate_content(model=model, contents=prompt, config=config).text
        try:
            return schema.model_validate_json(text or "")
        except ValidationError as e:
            print(f"Invalid JSON from LLM for {schema.__name__} (attempt {attempt}/2): {e}", file=sys.stderr)
    raise SystemExit(f"LLM returned invalid JSON twice for {schema.__name__}; aborting.")


# 1
def extract_denial_reason(denial_text: str) -> dict:
    return ask(
        "From this prior-authorization denial letter return JSON with `service` (the denied service, "
        "with CPT code if given) and `denied_criterion` (the medical-necessity criterion the plan says "
        "was not met, worded as in the letter).\n\nDENIAL LETTER:\n" + denial_text,
        DenialReason,
    ).model_dump()


# 2
def find_policy_criterion(policy_text: str, denied_criterion: str) -> dict:
    return ask(
        f'Below is the full text of a clinical policy. Find the policy criterion matching: "{denied_criterion}"\n'
        "Return JSON with `section` (the policy heading and subheading the criterion sits under, without "
        "footnote or reference numbers) and "
        "`criterion_text` (the criterion copied VERBATIM from the policy text: one contiguous span, "
        "character for character, keeping asterisks, footnote numbers and odd spacing; never paraphrase)."
        "\n\nPOLICY:\n" + policy_text,
        PolicyCriterion,
    ).model_dump()


# 3
def find_evidence(chart_text: str, criterion_text: str) -> list[dict]:
    evidence = ask(
        "Below is a patient chart made of dated notes; each note starts with a header line holding its "
        f'date. Find every passage relevant to whether this criterion is met: "{criterion_text}"\n'
        "Include treatment given (type, dates, duration), the response to treatment (pain scores, no "
        "improvement, worsening) and anything that weakens the case (treatment stopped early, patient "
        "declined, noncompliance, improvement). Return JSON `items`, each with `claim` (one short "
        "plain-English statement of what the quote shows), `quote` (copied VERBATIM from the chart: one "
        "contiguous phrase or sentence, no ellipses, no edits), `note_date` (the date in the header of "
        "the note containing the quote, as MM/DD/YYYY) and `type`: `treatment_session` if the note records "
        "conservative treatment delivered that day (a therapy evaluation, session or discharge visit; a "
        "medication course started at that visit), `follow_up` if it is a visit where treatment was only "
        "reviewed (symptoms, response, orders), otherwise `other`. Include at least one item from every "
        "note that records a treatment session.\n\nCHART:\n" + chart_text,
        Evidence,
    )
    return [item.model_dump() for item in evidence.items]


def _parse_date(s: str) -> date | None:
    try:
        return datetime.strptime(s.strip(), "%m/%d/%Y").date()
    except ValueError:
        return None


def _locate(quote: str, source: str) -> list[re.Match]:
    """Every occurrence of quote in source, ignoring case and whitespace differences."""
    words = quote.split()
    return list(re.finditer(r"\s+".join(map(re.escape, words)), source, re.I)) if words else []


def _date_at(headers: list[tuple[int, date | None]], pos: int) -> date | None:
    """Date of the chart note containing position pos."""
    dates = [d for start, d in headers if start <= pos]
    return dates[-1] if dates else None


# 4
def verify(evidence: list[dict], chart_text: str, policy_text: str, criterion: dict) -> tuple[list[dict], dict | None]:
    """Plain Python, no LLM. Keep evidence whose quote is in the chart note dated note_date and check
    the policy quote is in the policy. Kept quotes are replaced by the exact source text they matched.
    Returns (kept_evidence, criterion or None if its quote is not in the policy)."""
    headers = [(m.start(), _parse_date(m[1])) for m in NOTE_HEADER.finditer(chart_text)]
    kept = []
    for item in evidence:
        claimed = _parse_date(item["note_date"])
        matches = _locate(item["quote"], chart_text)
        hit = next((m for m in matches if claimed and _date_at(headers, m.start()) == claimed), None)
        if hit:
            kept.append({**item, "quote": " ".join(hit[0].split()), "note_date": f"{claimed:%m/%d/%Y}"})
            continue
        if not matches:
            reason = "quote not found in chart"
        elif not claimed:
            reason = f"note_date {item['note_date']!r} is not MM/DD/YYYY"
        else:
            found = sorted({f"{_date_at(headers, m.start()):%m/%d/%Y}" for m in matches if _date_at(headers, m.start())})
            reason = f"quote is in note(s) dated {', '.join(found) or 'none'}, not {item['note_date']}"
        print(f"DROPPED chart quote {item['quote']!r} ({item['note_date']}): {reason}", file=sys.stderr)

    policy_matches = _locate(criterion["criterion_text"], policy_text)
    if not policy_matches:
        print(f"DROPPED policy quote {criterion['criterion_text']!r}: not found in policy", file=sys.stderr)
        return kept, None
    return kept, {**criterion, "criterion_text": " ".join(policy_matches[0][0].split())}


def _heading_above(policy_text: str, quote: str) -> str | None:
    """Nearest heading above the quote in the policy text, found in Python (the LLM's label is not used).
    ponytail: heuristic tuned to pypdf text of NIA guidelines: the first line upward that starts with a
    capital letter and cites no year. That skips bullets (PDF glyphs, 'o'), '(Author, 2019)' citation
    lines and page furniture ('©', '2—', '*'). Other PDF layouts may need their own rule."""
    matches = _locate(quote, policy_text)
    if not matches:
        return None
    above = policy_text[: max(policy_text.rfind("\n", 0, matches[0].start()), 0)]
    for line in reversed(above.splitlines()):
        line = line.strip()
        if line[:1].isupper() and not re.search(r"\b(19|20)\d\d\b", line):
            return re.sub(r"[\d,\s-]+$", "", line)  # drop trailing footnote numbers such as "7-16"
    return None


def _amount(unit: str, text: str) -> int | None:
    """Number before unit, e.g. 'six (6) weeks' or '6 weeks' -> 6."""
    m = re.search(rf"\b(\d+|{'|'.join(NUMBERS)})\s*(?:\(\d+\)\s*)?{unit}", text, re.I)
    return int(NUMBERS.get(m[1].lower(), m[1])) if m else None


# 5
def _months_before(d: date, months: int) -> date:
    """Same day of the month, `months` calendar months earlier (clamped to the month's last day)."""
    y, m = divmod(d.year * 12 + d.month - 1 - months, 12)
    return date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1]))


def decide(verified_evidence: list[dict], criterion_text: str, denial_date: date | None = None) -> dict:
    """The LLM only sorts evidence items into roles; all date arithmetic is done here in Python."""
    decision = {"supported": False, "supporting": [], "missing": [], "weeks_needed": _amount("weeks?", criterion_text)}
    if not verified_evidence:
        return {**decision, "reason": "No chart evidence survived verification.",
                "missing": ["Chart notes documenting the treatment required by the criterion"]}

    listing = "\n".join(f'{i}. [{e["note_date"]}] "{e["quote"]}" - {e["claim"]}' for i, e in enumerate(verified_evidence))
    j = ask(
        f'Criterion: "{criterion_text}"\n\nVerified chart evidence (id, note date, quote - claim):\n{listing}\n\n'
        "Return JSON: `treatment_items` = ids documenting conservative treatment delivered or under way on "
        "that note's date (therapy evaluation or session, medication started or continued), including the "
        "first and the last such note even if the patient stopped early; an id may appear in several "
        "lists; `failure_items` = ids "
        "documenting that treatment did not help (no improvement, no relief, persistent or worsening pain); "
        "`contrary_items` = ids showing the patient declined, refused or stopped treatment early by choice, "
        "did not comply, or improved meaningfully (treatment discontinued because it was not helping is "
        "failure, not contrary); `missing` = one short sentence naming evidence still needed to meet the "
        "criterion, or an empty string. Do not compute durations.",
        Judgment,
    )
    pick = lambda ids: [verified_evidence[i] for i in sorted(set(ids)) if 0 <= i < len(verified_evidence)]
    failure, contrary = pick(j.failure_items), pick(j.contrary_items)
    when = lambda e: _parse_date(e["note_date"])
    span = lambda items: (when(max(items, key=when)) - when(min(items, key=when))).days

    # Treatment weeks = first to last verified treatment_session (medication or PT). Follow-ups never count.
    sessions = sorted((e for e in verified_evidence if e.get("type") == "treatment_session"), key=when)
    weeks_needed, months = decision["weeks_needed"], _amount("months?", criterion_text)
    counted = sessions
    if months:  # the look-back window ends at the denial date
        if not denial_date:
            raise SystemExit(f"The criterion has a {months}-month window but the denial letter has no 'Date: MM/DD/YYYY' line.")
        cutoff = _months_before(denial_date, months)
        counted = [e for e in sessions if when(e) >= cutoff]

    problems, missing = [], []
    if not sessions:
        problems.append("No verified chart evidence of a conservative treatment session.")
        missing.append("Dated notes documenting conservative treatment sessions")
    else:
        days = span(sessions)
        decision["weeks"] = round(days / 7, 1)
        if weeks_needed and days < weeks_needed * 7:
            problems.append(f"Verified treatment sessions span only {days / 7:.1f} weeks "
                            f"({sessions[0]['note_date']} to {sessions[-1]['note_date']}); the criterion requires at least {weeks_needed} weeks.")
            missing.append(f"Notes documenting at least {weeks_needed} weeks of conservative treatment"
                           + (f" within the last {months} months" if months else ""))
        elif months and (not counted or (weeks_needed and span(counted) < weeks_needed * 7)):
            inside = f"only {span(counted) / 7:.1f} weeks of it falls" if counted else "none of it falls"
            problems.append(f"Verified treatment ran {sessions[0]['note_date']} to {sessions[-1]['note_date']}, but {inside} "
                            f"inside the {months}-month window before the denial date {denial_date:%m/%d/%Y} "
                            f"(window starts {cutoff:%m/%d/%Y}).")
            missing.append(f"Notes documenting conservative treatment after {cutoff:%m/%d/%Y}"
                           + (f" lasting at least {weeks_needed} weeks" if weeks_needed else ""))
        if counted:  # the span the letter states and cites
            decision.update(first=counted[0], last=counted[-1], days=span(counted))
    if not failure:
        problems.append("No verified chart evidence that conservative treatment failed.")
        missing.append("A note documenting lack of improvement after treatment")
    for e in contrary:
        problems.append(f'Chart note {e["note_date"]} says "{e["quote"]}" ({e["claim"].rstrip(".")}).')
    if problems and j.missing:
        missing.append(j.missing)

    if problems:
        return {**decision, "reason": " ".join(problems), "missing": missing}
    reason = f"Verified treatment sessions span {decision['days'] / 7:.1f} weeks with documented failure."
    # Letter cites every counted treatment session plus the outcome (failure) evidence, which may be follow-ups.
    supporting = [e for e in verified_evidence if e in counted or e in failure]
    return {**decision, "supported": True, "reason": reason, "supporting": supporting}


# 6
def draft_letter(service: str, criterion: dict, decision: dict) -> str:
    """Built only from verified evidence; every claim is followed by its citation."""
    policy = f"[Policy §{criterion['section']}]"
    cite = lambda e: f'[Chart: {e["note_date"]} "{e["quote"]}"]'
    lines = [
        "APPEAL OF PRIOR AUTHORIZATION DENIAL",
        f"Re: {service}",
        "",
        "To the Utilization Management reviewer:",
        "",
        f"We request reconsideration of the denial of {service}. The applicable criterion is "
        f'"{criterion["criterion_text"]}" {policy}. The medical record documents that it is met:',
        "",
    ]
    for e in sorted(decision["supporting"], key=lambda e: _parse_date(e["note_date"])):
        lines.append(f"- {e['claim'].rstrip('.')} {cite(e)}")
    if decision["weeks_needed"]:
        first, last = decision["first"], decision["last"]
        lines.append(f"- Conservative treatment sessions are documented from {first['note_date']} to {last['note_date']}, "
                     f"{decision['days']} days ({decision['days'] / 7:.1f} weeks), meeting the required minimum of "
                     f"{decision['weeks_needed']} weeks {cite(first)} {cite(last)} {policy}")
    lines += ["", "Because the documented conservative treatment did not relieve the patient's symptoms, "
              f"we ask that the denial be overturned and {service} be approved.", "", "Sincerely,", "Ordering provider"]
    return "\n".join(lines)


def insufficient_report(criterion: dict, decision: dict) -> str:
    return "\n".join(
        ["INSUFFICIENT DOCUMENTATION",
         f'Unmet criterion: "{criterion["criterion_text"]}" [Policy §{criterion["section"]}]',
         f"Reason: {decision['reason']}",
         "Missing evidence:"] + [f"- {m}" for m in decision["missing"]]
    )


def run(denial_path, policy_path, chart_path) -> tuple[str, dict]:
    """Returns (letter or insufficiency report, decision)."""
    denial_text = Path(denial_path).read_text(encoding="utf-8")
    chart_text = Path(chart_path).read_text(encoding="utf-8")
    policy_text = "\n".join(page.extract_text() or "" for page in PdfReader(policy_path).pages)

    denial = extract_denial_reason(denial_text)
    print(f"Denied criterion: {denial['denied_criterion']}", file=sys.stderr)
    criterion = find_policy_criterion(policy_text, denial["denied_criterion"])
    evidence = find_evidence(chart_text, criterion["criterion_text"])
    verified, criterion = verify(evidence, chart_text, policy_text, criterion)
    if criterion is None:
        raise SystemExit("Policy criterion quote is not verbatim in the policy; refusing to cite it.")
    criterion["section"] = _heading_above(policy_text, criterion["criterion_text"])
    if not criterion["section"]:
        raise SystemExit("No policy heading found above the criterion; refusing to cite a section.")
    print(f"Policy criterion: {criterion['criterion_text']} [{criterion['section']}]", file=sys.stderr)
    print(f"Evidence: {len(verified)}/{len(evidence)} quotes verified", file=sys.stderr)

    m = re.search(r"^\s*Date:\s*(\d{1,2}/\d{1,2}/\d{4})", denial_text, re.M)
    denial_date = _parse_date(m[1]) if m else None
    print(f"Denial date: {denial_date:%m/%d/%Y}" if denial_date else "Denial date: not found", file=sys.stderr)
    decision = decide(verified, criterion["criterion_text"], denial_date)
    if not decision["supported"]:
        return insufficient_report(criterion, decision), decision
    return draft_letter(denial["service"], criterion, decision), decision


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # policy text can hold characters the Windows console codepage lacks
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--denial", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--chart", required=True)
    args = parser.parse_args()
    print(run(args.denial, args.policy, args.chart)[0])
