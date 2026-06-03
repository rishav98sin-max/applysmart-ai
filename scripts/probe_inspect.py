"""Show exactly what happened to the Finance-Admin region: dump page-0 text
lines (y0, text) for the ORIGINAL Cormac vs the harness OUTPUT, y in [715,800]."""
from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import fitz  # type: ignore

ORIG = r"D:\Projects\job-application-agent\CVs\Run25\Cormac Holleran CV 2026.pdf"
OUTP = ROOT / "_r-adapt-render" / "invariant" / "inv_Cormac_Holleran_CV_2026_pdf.pdf"


def dump(path, lo=715, hi=800):
    print(f"\n=== {Path(path).name} (page0, y {lo}-{hi}) ===")
    doc = fitz.open(path)
    pg = doc[0]
    rows = []
    for b in pg.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            t = "".join(s.get("text", "") for s in ln.get("spans", [])).strip()
            y0 = ln["bbox"][1]
            if t and lo <= y0 <= hi:
                rows.append((round(y0, 1), round(ln["bbox"][0], 1), t))
    for y0, x0, t in sorted(rows):
        print(f"  y={y0:<7} x={x0:<7} {t[:75]}")
    doc.close()


dump(ORIG)
if OUTP.exists():
    dump(str(OUTP))
else:
    print(f"\n(output not found: {OUTP})")
