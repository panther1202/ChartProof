"""Run the ChartProof pipeline on every blind case and save one JSON result per case.

python blind_test_set/run_blind.py

Reads inputs only through blind_pipeline_guard (cases_blind/); never opens ground_truth/.
Writes results_blind/case_XXX.json. Cases that already have a result are skipped, so a rerun
resumes at the next unrun case. Before each case it checks the OpenRouter free-model quota and
stops cleanly when fewer than 5 requests remain today. Request spacing (3.5 s, under 20/min)
comes from appeal.py itself.
"""
import contextlib
import io
import json
import os
import sys
from pathlib import Path

import requests

from blind_pipeline_guard import assert_blind_path, list_blind_inputs

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import appeal  # noqa: E402  (also loads .env)

RESULTS = HERE / "results_blind"
POLICY = HERE.parent / "data" / "policy.pdf"
MIN_FREE = 5


def free_requests_left() -> int:
    r = requests.get("https://openrouter.ai/api/v1/key",
                     headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"}, timeout=60)
    r.raise_for_status()
    return r.json()["data"]["free_model_daily_requests"]["remaining"]


class Tee(io.StringIO):
    """Keeps a copy of the pipeline's stderr diagnostics while still showing them."""

    def __init__(self, out):
        super().__init__()
        self.out = out

    def write(self, s):
        self.out.write(s)
        return super().write(s)


def run_case(case_dir: Path) -> dict:
    denial, chart = case_dir / "denial.txt", case_dir / "chart.txt"
    for p in (denial, chart):
        assert_blind_path(p)
    log = Tee(sys.stderr)
    output, decision, error = None, None, None
    with contextlib.redirect_stderr(log):
        try:
            output, decision = appeal.run(denial, POLICY, chart)
        except SystemExit as e:  # the pipeline refused: invalid JSON twice, unverifiable policy quote, no denial date
            error = str(e.code)
    lines = log.getvalue().splitlines()
    requests_sent = [l for l in lines if l.startswith("LLM request:")]
    citations = []
    if decision:  # the letter's evidence, or the counted treatment span named by an insufficiency report
        cited = decision["supporting"] + [decision[k] for k in ("first", "last") if k in decision]
        citations = [{"date": d, "quote": q} for d, q in
                     sorted({(f"{appeal._parse_date(e['note_date']):%Y-%m-%d}", e["quote"]) for e in cited})]
    return {
        "case": case_dir.name,
        "verdict": decision["supported"] if decision else None,
        "weeks": decision.get("weeks") if decision else None,
        "citations": citations,
        "dropped_quotes": [l.removeprefix("DROPPED ") for l in lines if l.startswith("DROPPED ")],
        "model": ", ".join(sorted({l.rsplit(" ", 1)[-1] for l in requests_sent})),
        "calls": len(requests_sent),
        "error": error,
        "output": output,
    }


def main():
    sys.stdout.reconfigure(encoding="utf-8")  # policy text can hold characters the Windows console codepage lacks
    sys.stderr.reconfigure(encoding="utf-8")
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise SystemExit("OPENROUTER_API_KEY is not set; the free quota can only be checked on OpenRouter.")
    RESULTS.mkdir(exist_ok=True)
    cases = sorted({p.parent for p in list_blind_inputs()})
    todo = [c for c in cases if not (RESULTS / f"{c.name}.json").exists()]
    print(f"{len(cases) - len(todo)}/{len(cases)} cases already have results.")
    for case_dir in todo:
        left = free_requests_left()
        if left < MIN_FREE:
            print(f"Stopping: {left} free requests left today (need {MIN_FREE}). Rerun to resume at {case_dir.name}.")
            return
        print(f"=== {case_dir.name} ({left} free requests left)", file=sys.stderr)
        try:
            result = run_case(case_dir)
        except Exception as e:  # provider or network failure: save nothing, so the next run retries this case
            raise SystemExit(f"{case_dir.name} failed ({type(e).__name__}: {e}); no result saved. Rerun to resume here.")
        (RESULTS / f"{case_dir.name}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"{case_dir.name}: verdict={result['verdict']} weeks={result['weeks']} calls={result['calls']}"
              + (f" error={result['error']}" if result["error"] else ""))
    print("All cases have results.")


if __name__ == "__main__":
    main()
