from pathlib import Path

BLIND_ROOT = Path(__file__).resolve().parent / "cases_blind"
FORBIDDEN_ROOT = Path(__file__).resolve().parent / "ground_truth"

def assert_blind_path(path):
    p = Path(path).resolve()
    forbidden = FORBIDDEN_ROOT.resolve()
    if p == forbidden or forbidden in p.parents:
        raise RuntimeError("BLIND PIPELINE GUARD: ground_truth access is forbidden.")
    if BLIND_ROOT.resolve() not in p.parents and p != BLIND_ROOT.resolve():
        raise RuntimeError("BLIND PIPELINE GUARD: input must come from cases_blind.")

def list_blind_inputs():
    assert_blind_path(BLIND_ROOT)
    return sorted(BLIND_ROOT.glob("case_*/[dc]*.txt"))
