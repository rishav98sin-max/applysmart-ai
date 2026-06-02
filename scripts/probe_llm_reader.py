"""
Step-1 proof probe: LLM structure reader vs heuristic parser, on OUR corpus.

For each real CV we compare:
  - HEURISTIC  : pdf_editor.build_outline (the current bbox-heuristic parser)
  - LLM READER : cv_structure_reader.read_outline_llm (id-only Groq grouping)

We print roles / bullets / summary for BOTH so we can eyeball which one
recovers the true structure, plus the LLM validation score and whether the
geometry gate passed (= in-place replica edit would be available).

Usage:
    venv\\Scripts\\python.exe scripts\\probe_llm_reader.py
    venv\\Scripts\\python.exe scripts\\probe_llm_reader.py cormac mohammed
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

# Reader is safe to call directly, but set the flag for parity with prod.
os.environ.setdefault("REPLICA_LLM_READER", "1")

import agents.pdf_editor as pe                         # type: ignore
from agents.cv_structure_reader import read_outline_llm  # type: ignore

CVS = {
    "cormac":   r"D:\Projects\job-application-agent\CVs\Run25\Cormac Holleran CV 2026.pdf",
    "mohammed": r"D:\Projects\job-application-agent\CVs\Run26\Resume.pdf",
    "rishav":   r"D:\Projects\job-application-agent\CVs\Orignal Base CV\RishavSingh_ProductManagerCV.pdf",
    "shrestha": r"D:\Projects\job-application-agent\CVs\Orignal Base CV\Shrestha Ghosh_CV.pdf",
}


def _count(outline):
    roles = outline.get("roles") or []
    usable = [r for r in roles if (r.get("bullets") or [])]
    nb = sum(len(r.get("bullets") or []) for r in usable)
    return len(usable), nb


def _dump_roles(outline, label):
    print(f"  {label}: roles={_count(outline)[0]} bullets={_count(outline)[1]} "
          f"summary_len={len((outline.get('summary') or '').strip())}")
    for r in (outline.get("roles") or []):
        bts = r.get("bullets") or []
        if not bts:
            continue
        hdr = (r.get("header") or "").strip()
        print(f"      • [{len(bts)}b] {hdr[:70]!r}")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    which = sys.argv[1:] or list(CVS.keys())
    summary_rows = []

    for who in which:
        cv = CVS.get(who)
        print("=" * 80)
        print(f" {who}   {os.path.basename(cv) if cv else '??'}")
        print("=" * 80)
        if not cv or not os.path.exists(cv):
            print(f"  !! CV not found: {cv}")
            continue

        # ── Heuristic ──────────────────────────────────────────────
        try:
            heur = pe.build_outline(cv)
            h_roles, h_bullets = _count(heur)
            _dump_roles(heur, "HEURISTIC")
        except Exception as e:
            heur = {}
            h_roles = h_bullets = -1
            print(f"  HEURISTIC: CRASHED ({type(e).__name__}: {e})")

        print()

        # ── LLM reader ─────────────────────────────────────────────
        try:
            llm = read_outline_llm(cv, verbose=True)
        except Exception as e:
            llm = None
            print(f"  LLM READER: CRASHED ({type(e).__name__}: {e})")

        if llm:
            l_roles, l_bullets = _count(llm)
            _dump_roles(llm, "LLM READER")
            val = llm.get("_validation") or {}
            geo = "VALID (in-place avail)" if llm.get("_geometry") else "none (would rebuild)"
            print(f"      score={val.get('score')} ok={val.get('ok')} geometry={geo}")
        else:
            l_roles = l_bullets = 0
            print("  LLM READER: None (declined / validation failed)")

        summary_rows.append({
            "cv": who,
            "heuristic_roles": h_roles, "heuristic_bullets": h_bullets,
            "llm_roles": l_roles, "llm_bullets": l_bullets,
            "llm_geometry": bool(llm and llm.get("_geometry")),
            "llm_score": (llm or {}).get("_validation", {}).get("score") if llm else None,
        })
        print()

    # ── Summary table ──────────────────────────────────────────────
    print("=" * 80)
    print(" SUMMARY  (roles / bullets)")
    print("=" * 80)
    print(f"  {'CV':10} {'HEUR roles/bul':>16} {'LLM roles/bul':>16} {'geo':>6} {'score':>6}")
    for r in summary_rows:
        heur_rb = f"{r['heuristic_roles']}/{r['heuristic_bullets']}"
        llm_rb = f"{r['llm_roles']}/{r['llm_bullets']}"
        geo = "Y" if r["llm_geometry"] else "-"
        print(f"  {r['cv']:10} {heur_rb:>16} {llm_rb:>16} {geo:>6} {str(r['llm_score']):>6}")

    side = ROOT / "_r-adapt-render" / "probe_llm_reader.json"
    side.parent.mkdir(parents=True, exist_ok=True)
    side.write_text(json.dumps(summary_rows, indent=2), encoding="utf-8")
    print(f"\nsidecar: {side}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
