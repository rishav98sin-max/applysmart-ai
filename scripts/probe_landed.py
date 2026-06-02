"""Decisive edits-landed check on the SAVED render outputs (no LLM).
For each CV, how many of the ORIGINAL (heuristic) bullets survive VERBATIM in
the tailored output? Many verbatim = edits didn't land. Few = edits landed
(and the prefix-check in probe_render was a special-char false alarm)."""
from __future__ import annotations
import os, re, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
except Exception:
    pass
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import fitz                          # type: ignore
import agents.pdf_editor as pe       # type: ignore

CVS = {
    "cormac":   r"D:\Projects\job-application-agent\CVs\Run25\Cormac Holleran CV 2026.pdf",
    "shrestha": r"D:\Projects\job-application-agent\CVs\Orignal Base CV\Shrestha Ghosh_CV.pdf",
}
OUT = ROOT / "_r-adapt-render"


def norm(t):
    t = (t or "").lower()
    for a, b in [("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"'),
                 ("–", "-"), ("—", "-"), ("•", " "), ("▪", " "),
                 ("○", " ")]:
        t = t.replace(a, b)
    return re.sub(r"\s+", " ", t).strip()


for who in (sys.argv[1:] or ["cormac", "shrestha"]):
    cv = CVS[who]
    out_pdf = OUT / f"render_{who}_llmprimary.pdf"
    if not out_pdf.exists():
        print(f"{who}: no saved output"); continue
    doc = fitz.open(out_pdf)
    out_text = norm("".join(p.get_text("text") for p in doc)); doc.close()
    bullets = [norm(b["text"]) for r in pe.build_outline(cv).get("roles", [])
               for b in (r.get("bullets") or []) if len(norm(b["text"])) > 25]
    verbatim = sum(1 for b in bullets if b in out_text)
    changed = len(bullets) - verbatim
    print(f"{who:9}  original bullets={len(bullets)}  verbatim(unchanged)={verbatim}  "
          f"changed(tailored)={changed}  -> {'EDITS LANDED' if changed >= max(1, len(bullets)//3) else 'edits did NOT land'}")
