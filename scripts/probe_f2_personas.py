"""
F2 — real re-aim tailoring on the 3 unproven personas:
  Rishav (PM)         : CVs/Orignal Base CV/RishavSingh_ProductManagerCV.pdf
  Cormac (IS Auditor) : CVs/Run25/Cormac Holleran CV 2026.pdf
  SWE  (LaTeX 2-col)  : CVs/_diverse_corpus/latex-classic-twocolumn__sb2nov_resume__sourabh_bajaj_resume.pdf

Memory says re-aim is PROVEN only on Shrestha (Account Manager). This probe
exercises re-aim on 3 different persona/layout combos and reports BOTH:
  FORMAT intact   : pages / text / borders preserved + headers untouched
  TAILORING real  : bullets changed (with before/after samples) + summary changed

Falls back to heuristic outline if LLM declines so we don't lose a run to quota.
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
# Force the launch-mode flags ON for this test.
os.environ["REPLICA_LLM_PRIMARY"] = "1"
os.environ["CV_READER_SAMPLES"]   = "1"   # n=1 keeps token cost down
os.environ["APPLYSMART_REAIM"]    = "1"
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import fitz                                                  # type: ignore
import agents.pdf_editor as pe                               # type: ignore
from agents.cv_structure_reader import read_outline_llm        # type: ignore
from agents.cv_diff_tailor import tailor_cv_diff               # type: ignore
from agents.tailor_strategist import build_tailor_strategy     # type: ignore

OUT = ROOT / "_r-adapt-render" / "f2_personas"
OUT.mkdir(parents=True, exist_ok=True)

# ── Realistic JDs (no fluff, real keywords a recruiter would write) ────────
JD_PM = """Senior Product Manager — B2B SaaS. Own a full product surface end-to-end:
discovery, roadmap, requirements, launch. Partner with engineering, design, sales
and CS. Drive product-led growth metrics, run experiments and A/B tests, lead
quarterly planning. Strong on user research, data analysis (SQL/Amplitude),
stakeholder management, executive comms, prioritisation under ambiguity. 5+ yrs
PM experience, ideally with SaaS / vertical platforms / AI products."""

JD_AUDITOR = """Internal Auditor / Risk & Compliance Manager. Plan and execute
risk-based internal audits across financial, operational, IT and compliance
domains. Engage with senior leadership, draft audit reports with actionable
recommendations, track remediation. Strong on SOX/ICFR, IFRS, risk frameworks
(COSO, ISO 31000), data analytics for audit (ACL/IDEA), regulatory reporting,
fraud investigation. CA/ACCA/CIA preferred, big-4 experience a plus."""

JD_SWE = """Senior Software Engineer — Backend / Full-Stack (Platform team). You
will design, build and scale production services and APIs that thousands of
internal teams depend on every day. Own features end-to-end from technical
design, implementation, code review, deployment to monitoring. Partner closely
with product managers, designers, SREs and security to ship reliable
infrastructure. You should have strong fundamentals in data structures and
algorithms, deep experience with distributed systems (queues, caches, event
streams), proficient in Python/Java/TypeScript and a major cloud (AWS or GCP).
We value pragmatism: write tested, observable, maintainable code; choose simple
tools over clever ones; ship iteratively. Bonus points for performance
optimisation, OpenTelemetry observability work, mentoring junior engineers, and
contributing to open-source. 5+ years of professional software engineering
experience required, preferably with experience scaling a service from 10K to
1M+ RPS."""

SAMPLE = [
    ("Orignal Base CV/RishavSingh_ProductManagerCV.pdf",
     JD_PM,       "Senior Product Manager",  "AcmeSaaS",  "Rishav (PM)"),
    ("Run25/Cormac Holleran CV 2026.pdf",
     JD_AUDITOR,  "Internal Auditor",        "BigCo",     "Cormac (Auditor)"),
    ("_diverse_corpus/latex-classic-twocolumn__sb2nov_resume__sourabh_bajaj_resume.pdf",
     JD_SWE,      "Software Engineer",       "TechCo",    "SWE (LaTeX 2-col)"),
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


def _heur_outline(cv: str):
    """Fallback to heuristic outline so this probe still runs under TPD.
    Uses build_outline_cached (the same path job_agent.py uses)."""
    try:
        s = pe.build_outline_cached(cv)
        if isinstance(s, dict) and s.get("roles"):
            s.setdefault("_geometry", s)
            return s
    except Exception as e:
        print(f"  ⚠ heuristic outline also failed: {type(e).__name__}: {e}")
    return None


def _run_one(rel: str, jd: str, title: str, company: str, label: str):
    cv = str(ROOT / "CVs" / rel)
    bar = "=" * 88
    print("\n" + bar); print(f" {label:30s}  {rel}"); print(bar)
    if not os.path.exists(cv):
        print(f"  ✘ missing file: {cv}"); return False
    # Try LLM-primary first; fall back to heur on decline.
    outline = None
    try:
        outline = read_outline_llm(cv, n_samples=1)
    except Exception as e:
        print(f"  ⚠ LLM reader exception: {type(e).__name__}: {str(e)[:120]}")
    parser_label = "LLM"
    if not (outline and outline.get("roles") and outline.get("_geometry")):
        print("  ↩ LLM declined / no geometry — using heuristic outline")
        outline = _heur_outline(cv); parser_label = "heur"
        if not outline:
            print("  ✘ no outline available"); return False
    geo = outline.get("_geometry") or outline

    orig_bul = {}
    for r in outline.get("roles", []):
        for i, b in enumerate(r.get("bullets") or []):
            orig_bul[(r.get("header",""), i)] = (b.get("text") or "").strip()
    orig_summary = (outline.get("summary") or "").strip()
    # Diagnose summary path: re-aim returns None for summary <80 chars (cv_reaim.py:262).
    print(f"  outline summary: {'present (' + str(len(orig_summary)) + ' chars)' if orig_summary else 'MISSING — re-aim will skip summary track'}")

    try:
        strat = build_tailor_strategy(outline, jd, title, company)
    except Exception as e:
        print(f"  ✘ strategy failed: {type(e).__name__}: {str(e)[:160]}"); return False
    try:
        diff = tailor_cv_diff(cv_pdf_path=cv, job_description=jd,
                              job_title=title, company=company,
                              outline=outline, strategy=strat)
    except Exception as e:
        print(f"  ✘ tailor failed: {type(e).__name__}: {str(e)[:160]}"); return False

    out_pdf = str(OUT / f"f2_{re.sub(r'[^A-Za-z0-9]+','_',label)}.pdf")
    try:
        pe.apply_edits(cv, diff, out_pdf, structure_override=geo)
    except Exception as e:
        print(f"  ✘ apply failed: {type(e).__name__}: {str(e)[:160]}"); return False

    # FORMAT INTACT
    ip, it, idr = _stats(cv); op, ot, odr = _stats(out_pdf)
    nout = _norm(ot)
    headers = [_norm(r.get("header","")) for r in outline.get("roles", []) if r.get("header")]
    hdr_ok = True
    for h in headers:
        toks = re.findall(r"[a-z0-9]{4,}", h)
        if toks and sum(1 for w in toks if w not in nout) > 0.2 * len(toks):
            hdr_ok = False
    fmt = {"pages": op == ip,
           "text>=60%": len(_norm(ot)) >= 0.6*len(_norm(it)),
           "borders>=80%": odr >= 0.8*max(1, idr),
           "headers_untouched": hdr_ok}

    # TAILORING REAL
    changed = []
    for role, entries in (diff.get("bullets") or {}).items():
        for e in entries:
            if not isinstance(e, dict): continue
            new = (e.get("text") or "").strip()
            old = orig_bul.get((role, e.get("i")), "")
            if new and _norm(new) != _norm(old):
                changed.append((old, new))
    new_summary = (diff.get("summary") or "").strip()
    summary_changed = bool(new_summary) and _norm(new_summary) != _norm(orig_summary)
    dbg = diff.get("_debug") or {}

    print(f"  parser path : {parser_label}")
    print(f"  FORMAT      : " + "  ".join(f"{k}={'OK' if v else 'FAIL'}" for k,v in fmt.items()))
    print(f"  TAILORING   : {len(changed)} bullets changed  |  summary_changed={summary_changed}")
    print(f"  RE-AIM debug: surfacing={dbg.get('must_have_surfacing')}  reverts={dbg.get('bullet_reverts_count')}")
    for i, (old, new) in enumerate(changed[:3]):
        print(f"     [{i+1}] BEFORE: {old[:90]}")
        print(f"         AFTER : {new[:90]}")
    if summary_changed:
        print(f"     SUMMARY BEFORE: {orig_summary[:120]}")
        print(f"     SUMMARY AFTER : {new_summary[:120]}")

    fmt_ok = all(fmt.values())
    tailor_ok = len(changed) >= 4   # arbitrary "meaningfully tailored" floor
    verdict = "PASS" if (fmt_ok and tailor_ok) else ("CHECK — partial")
    print(f"  → {verdict}   out={out_pdf}")
    return fmt_ok and tailor_ok


def main():
    results = []
    for rel, jd, title, company, label in SAMPLE:
        ok = _run_one(rel, jd, title, company, label)
        results.append((label, ok))
    print("\n" + "=" * 88)
    print(" F2 SUMMARY")
    print("=" * 88)
    for label, ok in results:
        print(f"  {label:30s}  {'✅ PASS' if ok else '⚠️  CHECK'}")
    overall = sum(1 for _, ok in results if ok)
    print(f"\n  {overall}/{len(results)} personas PASS (format+tailoring)")
    return 0 if overall == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
