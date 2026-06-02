"""
Bullet-delta check: are the heuristic's EXTRA bullets real content, or noise?
Prints each role's bullet TEXT from heuristic vs LLM reader, so we can see
whether promoting the LLM reader would tailor fewer REAL bullets (bad) or just
stop counting company-lines / fused fragments / sidebar junk as bullets (fine).

Usage: venv\\Scripts\\python.exe scripts\\probe_bullets.py cormac shrestha
"""
from __future__ import annotations
import os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass
os.environ.setdefault("REPLICA_LLM_READER", "1")

import agents.pdf_editor as pe                          # type: ignore
from agents.cv_structure_reader import read_outline_llm   # type: ignore

CVS = {
    "cormac":   r"D:\Projects\job-application-agent\CVs\Run25\Cormac Holleran CV 2026.pdf",
    "mohammed": r"D:\Projects\job-application-agent\CVs\Run26\Resume.pdf",
    "rishav":   r"D:\Projects\job-application-agent\CVs\Orignal Base CV\RishavSingh_ProductManagerCV.pdf",
    "shrestha": r"D:\Projects\job-application-agent\CVs\Orignal Base CV\Shrestha Ghosh_CV.pdf",
}


def dump(outline, label):
    print(f"\n----- {label} -----")
    for r in (outline.get("roles") or []):
        bts = r.get("bullets") or []
        if not bts:
            continue
        print(f"  ROLE: {(r.get('header') or '')[:55]!r}  ({len(bts)} bullets)")
        for b in bts:
            t = (b.get("text") or "").strip()
            print(f"      - {t[:95]}")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    for who in (sys.argv[1:] or ["cormac", "shrestha"]):
        cv = CVS[who]
        print("=" * 90)
        print(f" {who}")
        print("=" * 90)
        dump(pe.build_outline(cv), "HEURISTIC")
        llm = read_outline_llm(cv)
        if llm:
            dump(llm, "LLM READER")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
