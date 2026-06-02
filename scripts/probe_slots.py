"""Diagnose the Shrestha revert: how many LLM-geometry bullets are SINGLE-LINE
slots (which a wrapped-bullet rewrite would overflow)? Contrast with Cormac."""
from __future__ import annotations
import os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
except Exception:
    pass
from agents.cv_structure_reader import read_outline_llm  # type: ignore

CVS = {
    "cormac":   r"D:\Projects\job-application-agent\CVs\Run25\Cormac Holleran CV 2026.pdf",
    "shrestha": r"D:\Projects\job-application-agent\CVs\Orignal Base CV\Shrestha Ghosh_CV.pdf",
}
for who in (sys.argv[1:] or ["shrestha", "cormac"]):
    out = read_outline_llm(CVS[who])
    geo = (out or {}).get("_geometry") or {}
    print("=" * 70); print(who); print("=" * 70)
    one = multi = 0
    for r in geo.get("roles") or []:
        for bg in r.get("bullet_groups") or []:
            n = len(bg.get("lines") or [])
            tl = len(bg.get("text") or "")
            if n <= 1:
                one += 1
            else:
                multi += 1
            print(f"  lines={n}  textlen={tl}  {((bg.get('text') or '')[:55])!r}")
    print(f"  --> single-line slots={one}  multi-line slots={multi}")
