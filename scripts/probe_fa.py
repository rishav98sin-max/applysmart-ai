"""Pin the Cormac Finance-Administrator header-wipe: dump that role's header
vs bullets (text + source-line bbox + bullet flag) so we see whether the
'Finance Administrator' line is parsed as a HEADER or as an editable BULLET."""
from __future__ import annotations
import os, sys
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
from agents.cv_structure_reader import read_outline_llm  # type: ignore

CV = r"D:\Projects\job-application-agent\CVs\Run25\Cormac Holleran CV 2026.pdf"
out = read_outline_llm(CV, n_samples=3)
geo = {r.get("header_text",""): r for r in (out.get("_geometry") or {}).get("roles", [])}
for r in out.get("roles", []):
    h = r.get("header","")
    if "financ" not in h.lower() and "mater" not in h.lower() and "administrat" not in h.lower():
        continue
    print("ROLE header:", repr(h))
    g = geo.get(h, {})
    hl = g.get("header_line") or {}
    print("  header_line text:", repr((hl.get("text") or ""))[:80], " bbox:", hl.get("bbox"))
    for i, b in enumerate(r.get("bullets") or []):
        bg = (g.get("bullet_groups") or [])
        src = (bg[i].get("lines") if i < len(bg) and bg[i].get("lines") else [{}])
        print(f"  bullet[{i}] text: {repr(b.get('text',''))[:80]}")
        for s in src:
            print(f"        src bbox={s.get('bbox')} page={s.get('page')}")
