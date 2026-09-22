"""Download the NIA/Magellan Lumbar Spine MRI guideline to data/policy.pdf."""
from pathlib import Path

import requests

URL = "https://www.ldh.la.gov/assets/medicaid/MCPP/12.19.22/847_ACLA_Lumbar_Spine_MRI_2023_LA.pdf"
OUT = Path(__file__).parent / "data" / "policy.pdf"

if __name__ == "__main__":
    # The server returns 403 unless the User-Agent looks like a real browser.
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    r = requests.get(URL, timeout=60, headers={"User-Agent": ua})
    r.raise_for_status()
    if not r.content.startswith(b"%PDF"):
        raise SystemExit(f"Not a PDF: {URL}")
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_bytes(r.content)
    print(f"Saved {len(r.content):,} bytes to {OUT}")
