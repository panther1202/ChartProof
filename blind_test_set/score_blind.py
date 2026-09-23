"""Score results_blind/ against ground_truth/key.json and list every mismatch.

python blind_test_set/score_blind.py

Run it yourself, after run_blind.py; the runner never reads the key.
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results_blind"


def score(truth: dict, result: dict) -> dict:
    # ponytail: citations match on note date only (the key labels are prose, e.g. "2026-09-21 follow-up");
    # a forbidden hit means the pipeline cited that note, so read the chart to judge how it used it.
    cited = {c["date"] for c in result["citations"]}
    return {
        "verdict_ok": result["verdict"] == truth["verdict"],
        "weeks_diff": None if result["weeks"] is None else round(result["weeks"] - truth["treatment_weeks"], 1),
        "missing": [c for c in truth["required_citations"] if c[:10] not in cited],
        "forbidden": [c for c in truth["forbidden_citations"] if c[:10] in cited],
    }


def main():
    key = json.loads((HERE / "ground_truth" / "key.json").read_text(encoding="utf-8"))
    rows = []
    print(f"{'case':<9}  {'verdict exp/got':<16}  {'weeks exp/got':<14}  {'diff':>5}  {'missing req':>11}  {'forbidden cited':>15}")
    for case, truth in sorted(key.items()):
        path = RESULTS / f"{case}.json"
        if not path.exists():
            rows.append((case, truth, None, None))
            print(f"{case:<9}  not run")
            continue
        result = json.loads(path.read_text(encoding="utf-8"))
        s = score(truth, result)
        rows.append((case, truth, result, s))
        diff = "-" if s["weeks_diff"] is None else f"{s['weeks_diff']:+.1f}"
        verdicts = f"{truth['verdict']}/{result['verdict']}"
        weeks = f"{truth['treatment_weeks']}/{result['weeks']}"
        print(f"{case:<9}  {verdicts:<16}  {weeks:<14}  {diff:>5}  {len(s['missing']):>11}  {len(s['forbidden']):>15}")

    scored = [s for *_, s in rows if s]
    with_weeks = [s["weeks_diff"] for s in scored if s["weeks_diff"] is not None]
    print(f"\nTotals: {len(scored)}/{len(rows)} cases run; verdict match {sum(s['verdict_ok'] for s in scored)}/{len(scored)}; "
          f"weeks exact {with_weeks.count(0)}/{len(scored)}"
          + (f", mean |diff| {sum(map(abs, with_weeks)) / len(with_weeks):.2f} over {len(with_weeks)}" if with_weeks else "")
          + f"; missing required citations {sum(len(s['missing']) for s in scored)}; "
          f"forbidden citations cited {sum(len(s['forbidden']) for s in scored)}")

    mismatches = []
    for case, truth, result, s in rows:
        lines = []
        if s is None:
            lines.append("not run")
        else:
            if not s["verdict_ok"]:
                lines.append(f"verdict: expected {truth['verdict']}, got {result['verdict']}")
            if s["weeks_diff"] != 0:
                lines.append(f"weeks: expected {truth['treatment_weeks']}, got {result['weeks']}"
                             + ("" if s["weeks_diff"] is None else f" ({s['weeks_diff']:+.1f})"))
            lines += [f"missing required citation: {c}" for c in s["missing"]]
            lines += [f"forbidden citation cited: {c}" for c in s["forbidden"]]
            if lines and result["error"]:
                lines.append(f"pipeline error: {result['error']}")
        if lines:
            mismatches.append((case, truth, lines))

    print(f"\nMismatches: {len(mismatches)}")
    for case, truth, lines in mismatches:
        print(f"\n{case}  {(HERE / 'cases_blind' / case / 'chart.txt').relative_to(HERE.parent).as_posix()}")
        for line in lines + [f"key reason: {truth['reason']}"]:
            print(f"  {line}")


if __name__ == "__main__":
    main()
