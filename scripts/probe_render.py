"""
Render-verify the LLM-PRIMARY replica path end-to-end.

Runs the REAL pipeline with REPLICA_LLM_PRIMARY behaviour (LLM reader →
tailor → in-place apply_edits via LLM geometry), produces an actual tailored
PDF, and checks it is NOT corrupted vs the original:
  - same page count
  - text still extractable (no massive content loss)
  - borders/vector drawings preserved (no collapsed boxes)
  - the tailored bullet text actually landed in the PDF

Usage: venv\\Scripts\\python.exe scripts\\probe_render.py cormac shrestha
"""
from __future__ import annotations
import os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass
os.environ["REPLICA_LLM_PRIMARY"] = "1"   # exercise the path under test
os.environ["CV_READER_SAMPLES"] = "3"     # consensus → stable geometry (matches prod wiring)

import fitz                                              # type: ignore
import agents.pdf_editor as pe                           # type: ignore
from agents.cv_structure_reader import read_outline_llm    # type: ignore
from agents.cv_diff_tailor import tailor_cv_diff            # type: ignore
from agents.tailor_strategist import build_tailor_strategy  # type: ignore
from run_live_tailor import FIXTURES                        # type: ignore

OUT = ROOT / "_r-adapt-render"
OUT.mkdir(parents=True, exist_ok=True)


def _norm(t):
    t = (t or "").lower()
    for a, b in [("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"'),
                 ("–", "-"), ("—", "-"), ("•", " "), ("▪", " "), ("○", " ")]:
        t = t.replace(a, b)
    import re as _re
    return _re.sub(r"\s+", " ", t).strip()


def _pdf_stats(path):
    doc = fitz.open(path)
    pages = doc.page_count
    text = ""
    draws = 0
    for p in doc:
        text += p.get_text("text")
        try:
            draws += len(p.get_drawings())
        except Exception:
            pass
    doc.close()
    return pages, text, draws


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    overall_ok = True
    for who in (sys.argv[1:] or ["cormac", "shrestha"]):
        fx = FIXTURES[who]
        cv, jd, title, company = fx["cv"], fx["jd"], fx["title"], fx["company"]
        out_pdf = str(OUT / f"render_{who}_llmprimary.pdf")
        print("=" * 84)
        print(f" {who}   ({title} @ {company})")
        print("=" * 84)

        # 1) LLM-primary outline (what job_agent now builds when flag on)
        outline = read_outline_llm(cv)
        if not (outline and outline.get("_geometry")):
            print("  ❌ LLM reader returned no geometry — would fall back to heuristic.")
            overall_ok = False
            continue
        geo = outline["_geometry"]
        print(f"  outline: {len(outline['roles'])} roles, "
              f"_source={outline.get('_source')}, geometry roles={len(geo.get('roles') or [])}")

        # 2) strategist + tailor (real LLM)
        strat = build_tailor_strategy(outline, jd, title, company)
        diff = tailor_cv_diff(cv_pdf_path=cv, job_description=jd, job_title=title,
                              company=company, outline=outline, strategy=strat)
        edited = [(e.get("text") or "").strip()
                  for r in (diff.get("bullets") or {}).values()
                  for e in r if isinstance(e, dict) and (e.get("text") or "").strip()]
        print(f"  tailored bullets: {len(edited)}")

        # 3) in-place apply via LLM geometry (structure_override) — the wiring
        report = pe.apply_edits(cv, diff, out_pdf, structure_override=geo)

        # 4) verify output vs original
        op, ot, od = _pdf_stats(cv)
        np_, nt, nd = _pdf_stats(out_pdf)
        _nt = _norm(nt)
        landed = sum(1 for e in edited if _norm(e)[:24] and _norm(e)[:24] in _nt)
        checks = {
            "pages preserved":   np_ == op,
            "text not lost":     len(nt) >= 0.6 * max(1, len(ot)),
            "borders preserved": nd >= 0.8 * max(1, od),
            "edits landed":      (landed >= max(1, len(edited) // 2)) if edited else True,
        }
        print(f"  original : pages={op} text={len(ot)}c drawings={od}")
        print(f"  output   : pages={np_} text={len(nt)}c drawings={nd}  edits_landed={landed}/{len(edited)}")
        for k, v in checks.items():
            print(f"    {'✅' if v else '❌'} {k}")
        ok = all(checks.values())
        overall_ok = overall_ok and ok
        print(f"  → {'PASS' if ok else 'FAIL'}   ({out_pdf})")

    print("\n" + ("✅ RENDER-VERIFY PASS" if overall_ok else "❌ RENDER-VERIFY FAIL"))
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
