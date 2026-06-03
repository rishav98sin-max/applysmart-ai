"""
REAL tailoring on a sample of the real-corpus CVs (not synthetic edits).
Runs the full LLM-PRIMARY pipeline: read_outline_llm -> strategist -> re-aim
tailor (tailor_cv_diff) -> in-place apply. Then reports BOTH:
  FORMAT intact  : pages / text / borders preserved + headers untouched
  TAILORING real : how many bullets actually changed + before/after samples
so we can see format didn't break AND meaningful tailoring happened.

Usage: venv\\Scripts\\python.exe scripts\\probe_real_tailor.py
"""
from __future__ import annotations
import os, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
except Exception:
    pass
os.environ["REPLICA_LLM_PRIMARY"] = "1"
os.environ["CV_READER_SAMPLES"] = "3"
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import fitz                                                  # type: ignore
import agents.pdf_editor as pe                               # type: ignore
from agents.cv_structure_reader import read_outline_llm        # type: ignore
from agents.cv_diff_tailor import tailor_cv_diff               # type: ignore
from agents.tailor_strategist import build_tailor_strategy     # type: ignore

C = ROOT / "CVs" / "_real_corpus"
OUT = ROOT / "_r-adapt-render" / "real_tailor"; OUT.mkdir(parents=True, exist_ok=True)

JD_SWE = """Software Engineer (Backend / Full-Stack). Design, build and scale
production services and APIs; own features end-to-end; write tested, maintainable
code; collaborate across product and design. Strong in Python/Java/JS, data
structures, distributed systems, databases, CI/CD, cloud. Bonus: performance
optimisation, observability, mentoring."""

JD_DATA = """Data Scientist / Machine Learning Engineer. Build and ship ML models
to production; run experiments and A/B tests; statistical analysis and causal
inference; feature engineering; Python, SQL, PyTorch/scikit-learn, experimentation
platforms; communicate insights to stakeholders and drive data-informed decisions."""

SAMPLE = [
    ("sb2nov_resume__sourabh_bajaj_resume.pdf",                 JD_SWE,  "Software Engineer", "TechCo"),
    ("sc932_resume__ScottClarkResume.pdf",                      JD_DATA, "Data Scientist",    "DataCo"),
    ("ice1000_resume__resume.pdf",                              JD_SWE,  "Software Engineer", "TechCo"),
    ("Navin3d_Resume-To-MongoDB__Resume-Kaushik-Sathyanath.pdf", JD_SWE, "Software Engineer", "TechCo"),
    ("BazilSuhail_Resume-Dataset__CV_Rahul_Gupta.pdf",         JD_SWE,  "Software Engineer", "TechCo"),
]


def _norm(t):
    t = (t or "").lower()
    for a, b in [("’","'"),("‘","'"),("“",'"'),("”",'"'),("–","-"),("—","-"),
                 ("•"," "),("▪"," "),("○"," ")]:
        t = t.replace(a, b)
    return re.sub(r"\s+", " ", t).strip()


def _stats(p):
    d = fitz.open(p); pages = d.page_count; txt = ""; dr = 0
    for pg in d:
        txt += pg.get_text("text")
        try: dr += len(pg.get_drawings())
        except Exception: pass
    d.close(); return pages, txt, dr


def main():
    for fname, jd, title, company in SAMPLE:
        cv = str(C / fname)
        print("=" * 88); print(f" {fname[:70]}  → {title}"); print("=" * 88)
        if not os.path.exists(cv):
            print("  missing"); continue
        outline = read_outline_llm(cv, n_samples=3)
        if not (outline and outline.get("_geometry")):
            print("  LLM declined geometry — would use heuristic; skipping (want LLM path)"); continue
        geo = outline["_geometry"]
        orig_bul = {}
        for r in outline.get("roles", []):
            for i, b in enumerate(r.get("bullets") or []):
                orig_bul[(r.get("header",""), i)] = (b.get("text") or "").strip()

        strat = build_tailor_strategy(outline, jd, title, company)
        diff = tailor_cv_diff(cv_pdf_path=cv, job_description=jd, job_title=title,
                              company=company, outline=outline, strategy=strat)
        out_pdf = str(OUT / f"rt_{re.sub(r'[^A-Za-z0-9]+','_',fname)}.pdf")
        pe.apply_edits(cv, diff, out_pdf, structure_override=geo)

        # ── format intact ──
        ip, it, idr = _stats(cv); op, ot, odr = _stats(out_pdf)
        nout = _norm(ot)
        headers = [_norm(r.get("header","")) for r in outline.get("roles", []) if r.get("header")]
        hdr_ok = True
        for h in headers:
            toks = re.findall(r"[a-z0-9]{4,}", h)
            if toks and sum(1 for w in toks if w not in nout) > 0.2 * len(toks):
                hdr_ok = False
        fmt = {"pages": op == ip, "text": len(_norm(ot)) >= 0.6*len(_norm(it)),
               "borders": odr >= 0.8*max(1,idr), "headers_untouched": hdr_ok}

        # ── tailoring meaningful ──
        changed = []
        for role, entries in (diff.get("bullets") or {}).items():
            for e in entries:
                if not isinstance(e, dict): continue
                new = (e.get("text") or "").strip()
                old = orig_bul.get((role, e.get("i")), "")
                if new and _norm(new) != _norm(old):
                    changed.append((old, new))
        dbg = diff.get("_debug") or {}
        print(f"  FORMAT: " + "  ".join(f"{k}={'✅' if v else '❌'}" for k,v in fmt.items()))
        print(f"  TAILORING: {len(changed)} bullets changed; "
              f"summary_changed={bool((diff.get('summary') or '').strip())}; "
              f"surfacing={dbg.get('must_have_surfacing')}  reverts={dbg.get('bullet_reverts_count')}")
        for old, new in changed[:3]:
            print(f"      BEFORE: {old[:90]}")
            print(f"      AFTER : {new[:90]}")
        ok = all(fmt.values()) and len(changed) >= 1
        print(f"  → {'PASS (format intact + tailored)' if ok else 'CHECK'}   {out_pdf}")


if __name__ == "__main__":
    raise SystemExit(main())
