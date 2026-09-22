# ChartProof

Drafts prior-auth appeals where every claim is a verified quote from the chart.

Reads a denial letter, the payer policy PDF and a patient chart, then prints one of two things:

- an appeal letter in which every claim is followed by `[Chart: <date> "<verbatim quote>"]` or `[Policy §<section>]`, or
- `INSUFFICIENT DOCUMENTATION`, naming the unmet criterion and the evidence that is missing.

Every quote the LLM returns is checked against the source text in plain Python before it is used. Dropped quotes, and the reason each one was dropped, are printed to stderr. Date arithmetic (for example, "at least six weeks of treatment") is done in Python.

LLM: provider-agnostic via OpenRouter; tested on `nvidia/nemotron-3-super-120b-a12b:free`. Without `OPENROUTER_API_KEY` it calls Gemini (`gemini-3.8-flash`) through the google-genai SDK.

All files under `cases/` are synthetic test data.

## Setup (Python 3.11)

```bash
python -m venv .venv
source .venv/Scripts/activate # macOS/Linux: source .venv/bin/activate
pip install google-genai pypdf pydantic pytest requests
export GEMINI_API_KEY=...     # or put GEMINI_API_KEY=... in .env (gitignored)
python fetch_policy.py        # downloads the NIA Lumbar Spine MRI guideline to data/policy.pdf
```

The policy PDF is copyrighted, so `*.pdf` is gitignored. You can pass any policy PDF with `--policy`.

Optional environment variables (also read from `.env`):

- `GEMINI_MODEL`: use a different Gemini model when the default `gemini-3.8-flash` is overloaded (503).
- `OPENROUTER_API_KEY`: if set, calls go through OpenRouter instead of the google-genai SDK, using `google/<GEMINI_MODEL>`.
- `OPENROUTER_MODEL`: any full OpenRouter model ID, for example `nvidia/nemotron-3-super-120b-a12b:free`.

## Run

```bash
python appeal.py --denial cases/supported/denial.txt --policy data/policy.pdf --chart cases/supported/chart.txt
```

The five cases:

- `cases/supported`: 7 weeks of PT, no improvement, pain 7/10.
- `cases/insufficient`: 3 weeks of PT, then the patient declined further PT.
- `cases/messy`: abbreviated notes with evidence spread across 4 notes, plus one unrelated note.
- `cases/borderline_under`: PT for 5 weeks 5 days, then a physician follow-up at about 6.5 weeks reporting no improvement.
- `cases/borderline_exact`: PT for exactly 6.0 weeks, no improvement.

End-to-end results so far:

| Case | Expected | Actual | Model | Date |
| --- | --- | --- | --- | --- |
| `supported` | appeal letter | superseded (pre-decide() change): not completed: Gemini 503 "high demand", then the free-tier daily quota (429) and OpenRouter out of credit (402) | gemini-3.8-flash, 3.7-flash, 3-flash-preview | 2026-09-22 |
| `insufficient` | `INSUFFICIENT DOCUMENTATION` | superseded (pre-decide() change): `INSUFFICIENT DOCUMENTATION`: 1.0 week of treatment verified, and the patient declined further PT; 9/9 quotes verified | gemini-3.8-flash (via OpenRouter) | 2026-09-22 |
| `messy` | appeal letter citing the right notes | superseded (pre-decide() change): appeal letter citing the 01/12, 01/19, 03/02 and 03/05 notes; 5/5 quotes verified | gemini-3-flash-preview (google-genai) | 2026-09-22 |
| `supported` | appeal letter | superseded (pre-decide() change): appeal letter; 4/4 quotes verified, 0 dropped; treatment span 03/02–04/22 (7.3 weeks); pytest passed | nvidia/nemotron-3-super-120b-a12b:free (OpenRouter) | 2026-09-22 |
| `insufficient` | `INSUFFICIENT DOCUMENTATION` | superseded (pre-decide() change): `INSUFFICIENT DOCUMENTATION`; 5/5 quotes verified, 0 dropped; reason: 2.0 weeks verified (1.0 in the pytest run), and the patient declined further PT; pytest passed | nvidia/nemotron-3-super-120b-a12b:free (OpenRouter) | 2026-09-22 |
| `messy` | appeal letter citing the right notes | superseded (pre-decide() change): appeal letter citing the 01/12, 01/19, 03/02 and 03/05 notes, skipping the unrelated 02/10 note; 4/4 quotes verified, 0 dropped; pytest passed | nvidia/nemotron-3-super-120b-a12b:free (OpenRouter) | 2026-09-22 |
| `supported` | appeal letter; 7.0 weeks | appeal letter; 7.0 weeks (sessions 03/02–04/20); 4/4 quotes verified, 0 dropped; pytest passed | nvidia/nemotron-3-super-120b-a12b:free (OpenRouter) | 2026-09-22 |
| `insufficient` | `INSUFFICIENT DOCUMENTATION`; 3.0 weeks | `INSUFFICIENT DOCUMENTATION`; 3.0 weeks (sessions 02/02–02/23), and the patient declined further PT; 7/7 quotes verified, 0 dropped; pytest passed | nvidia/nemotron-3-super-120b-a12b:free (OpenRouter) | 2026-09-22 |
| `borderline_under` | `INSUFFICIENT DOCUMENTATION`; 5.7 weeks | `INSUFFICIENT DOCUMENTATION`; 5.7 weeks (sessions 01/05–02/14; the 02/19 follow-up is not counted); 4/4 quotes verified, 0 dropped; pytest passed | nvidia/nemotron-3-super-120b-a12b:free (OpenRouter) | 2026-09-22 |
| `borderline_exact` | appeal letter; 6.0 weeks | appeal letter; 6.0 weeks (sessions 03/02–04/13); 4/4 quotes verified, 0 dropped; pytest passed | nvidia/nemotron-3-super-120b-a12b:free (OpenRouter) | 2026-09-22 |
| `messy` | appeal letter citing the right notes | first run crashed on its first call (OpenRouter returned HTTP 200 with no `choices`), so the verdict test failed; the citation test's rerun produced an appeal letter, 7.0 weeks (sessions 01/12–03/02), citing the 01/12, 03/02 and 03/05 notes; 4/4 quotes verified, 0 dropped | nvidia/nemotron-3-super-120b-a12b:free (OpenRouter) | 2026-09-22 |

Superseded rows ran before `decide()` counted only verified `treatment_session` dates. Before that change it relied on the model's own list of treatment notes, which undercounted `insufficient` (1–2 weeks instead of 3) and counted a follow-up visit in `supported` (7.3 weeks instead of 7.0). The `insufficient` chart's 02/23 note now records that a session took place that day, so the true span is exactly 3.0 weeks.

Chart notes must each start with a header line such as `=== 03/02/2026 | ... ===`. The note date is read from that line and used to check each quote's `note_date`.

## Test

```bash
pytest
```

The `verify()` and section-heading tests run offline. The end-to-end tests call the LLM, so they are skipped unless `GEMINI_API_KEY` or `OPENROUTER_API_KEY` is set and `data/policy.pdf` exists.
