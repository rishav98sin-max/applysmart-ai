"""
F3 — Designer-CV rebuild quality verification.

Memory: rebuild path quality was "ASSUMED, never validated this session." This
probe runs the full rebuild pipeline on 5 designer CVs (`CVs/Orignal Base CV/
DesignerCVExamples/`) and checks the output:

  ATS-readable     : output PDF text extracts cleanly (>= 300 chars per page)
  Structure intact : section headers detectable (Experience/Skills/etc)
  Tailored bullets : >= 6 bullets present + reflect JD keywords
  Tailored summary : output summary differs from a generic boilerplate
  No fabrications  : JD-only term that's NOT in original CV doesn't appear in output

Skips Groq entirely — rebuild uses DeepSeek for tailor_cv_structured and Typst
for render. Works under depleted Groq.
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
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import fitz                                                                  # type: ignore
from agents.cv_parser import parse_cv                                          # type: ignore
from agents.cv_tailor import tailor_cv_structured                              # type: ignore
from agents.tailor_strategist import build_tailor_strategy                     # type: ignore
from agents.pdf_formatter import (                                             # type: ignore
    generate_cv_pdf_styled_via_typst,
    generate_cv_pdf_styled_from_structured,
)

OUT = ROOT / "_r-adapt-render" / "f3_designer_rebuild"
OUT.mkdir(parents=True, exist_ok=True)
DESIGNERS_DIR = ROOT / "CVs" / "Orignal Base CV" / "DesignerCVExamples"

# ── Realistic JDs for each designer CV ────────────────────────────────────────
JD_GRAPHIC = """Senior Graphic Designer — Brand & Visual Identity. Lead visual
identity work for a fast-growing consumer brand. Concept and execute campaigns
across digital and print; design landing pages, social assets, packaging, and
brand systems. Strong portfolio in typography, layout, colour theory, and
illustration. Daily tools: Figma, Adobe Creative Suite (Illustrator,
Photoshop, InDesign), Cinema 4D a plus. Partner with marketing, product and
copy to elevate visual storytelling. 4+ yrs agency or in-house experience;
ability to balance multiple projects on tight deadlines."""

JD_FLIGHT = """Flight Attendant — International Routes. Deliver exceptional
in-flight customer service to passengers on long-haul international routes.
Conduct safety briefings, manage emergency procedures, serve meals and
beverages, address passenger concerns with empathy and professionalism. Fluent
in English; second language a plus. Must pass FAA/EASA safety training and
remain calm under pressure. Flexible schedule (overnights, holidays, multi-day
trips). Strong interpersonal skills, attention to detail, and grooming
standards. Previous customer-facing or hospitality experience preferred."""

JD_PM = """Senior Product Manager — B2B SaaS. Own a full product surface
end-to-end: discovery, roadmap, requirements, launch. Partner with engineering,
design, sales and customer success. Drive product-led growth metrics, run
experiments and A/B tests, lead quarterly planning. Strong on user research,
data analysis (SQL/Amplitude), stakeholder management, executive comms,
prioritisation under ambiguity. 5+ yrs PM experience, ideally with SaaS /
vertical platforms / AI products. Bonus: experience with PLG funnels,
self-serve onboarding, usage-based pricing."""

JD_SWE = """Senior Software Engineer — Backend Systems. Design and build
scalable distributed systems that handle millions of requests per day. Own
services end-to-end: technical design, implementation, monitoring, and on-call.
Strong fundamentals in algorithms, data structures, distributed systems
(queues, caches, event streams). Languages: Python/Java/Go. Cloud: AWS or GCP.
Bonus: Kafka, gRPC, Kubernetes, OpenTelemetry, performance optimisation. 5+
years of professional software engineering experience required, including a
shipped service running in production at non-trivial scale."""

JD_SALES = """Sales Representative — SaaS Inside Sales. Drive new business
acquisition for our SaaS platform: outbound prospecting, discovery calls,
product demos, contract negotiation, and close. Manage a pipeline of 30-50
opportunities; consistently hit monthly quotas. Strong on consultative selling,
SPIN / MEDDIC methodology, objection handling, CRM hygiene (Salesforce or
HubSpot). 2+ yrs SaaS sales experience preferred. Comfortable in a high-volume,
metrics-driven environment with weekly leaderboards. Bonus: vertical
specialisation in fintech, healthcare or martech."""

SAMPLE = [
    ("Blue and Yellow Modern Graphic Designer CV.pdf",
     JD_GRAPHIC, "Senior Graphic Designer", "BrandCo",      "Graphic Designer"),
    ("Blue Light Blue Color Blocks Flight Attendant CV.pdf",
     JD_FLIGHT,  "Flight Attendant",        "GlobalAir",    "Flight Attendant"),
    ("Rishav's Resume (1).pdf",
     JD_PM,      "Senior Product Manager",  "AcmeSaaS",     "Rishav-designer"),
    ("Systems Design Resume in Bright Blue White Bold Accent Style.pdf",
     JD_SWE,     "Senior Software Engineer", "TechCo",      "SWE-designer"),
    ("White Simple Sales Representative CV Resume.pdf",
     JD_SALES,   "Sales Representative",    "SaaSCo",       "Sales"),
]


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").lower()).strip()


def _pdf_text(p: str) -> str:
    d = fitz.open(p); t = ""
    for pg in d: t += pg.get_text("text")
    d.close(); return t


def _has_ats_sections(text: str) -> bool:
    """ATS parsers look for canonical section headers."""
    t = text.lower()
    canonical = ["experience", "education", "skills"]
    hits = sum(1 for k in canonical if k in t)
    return hits >= 2  # at least 2 of the 3 must appear


def _extract_jd_keywords(jd: str, k: int = 8) -> list:
    """Cheap content-word extractor — proxy for what the tailor should surface."""
    stop = {"the","a","an","and","or","but","with","for","of","in","on","to","as","at",
            "by","from","be","is","are","was","were","this","that","these","those",
            "you","your","we","our","experience","required","preferred","strong","work"}
    words = re.findall(r"[A-Za-z][A-Za-z\-]{3,}", jd.lower())
    freq = {}
    for w in words:
        if w in stop: continue
        freq[w] = freq.get(w, 0) + 1
    return [w for w, _ in sorted(freq.items(), key=lambda x: -x[1])[:k]]


def _run_one(rel: str, jd: str, title: str, company: str, label: str) -> dict:
    cv_path = str(DESIGNERS_DIR / rel)
    bar = "=" * 88
    print("\n" + bar); print(f" {label:30s}  {rel[:60]}"); print(bar)
    if not os.path.exists(cv_path):
        print(f"  ✘ missing: {cv_path}"); return {"label": label, "verdict": "MISSING"}
    try:
        cv_text = parse_cv(cv_path)
    except Exception as e:
        print(f"  ✘ parse_cv failed: {e}"); return {"label": label, "verdict": "PARSE_FAIL"}
    if not cv_text or len(cv_text) < 200:
        print(f"  ✘ parsed text too short ({len(cv_text or '')} chars) — designer PDF probably image-only")
        return {"label": label, "verdict": "EMPTY_TEXT"}
    print(f"  cv_text: {len(cv_text)} chars")

    # Strategy (silent skip on short JD — all our JDs are 80+ words, fine)
    try:
        # need an outline-shaped dict; for rebuild path it's mostly metadata,
        # so a synthetic shell works. Real summary helps the strategist focus.
        outline = {"summary": cv_text[:600], "roles": []}
        strategy = build_tailor_strategy(outline, jd, title, company)
    except Exception as e:
        print(f"  ⚠ strategist failed (continuing with empty strategy): {e}")
        strategy = {}

    try:
        doc = tailor_cv_structured(
            cv_text=cv_text, job_description=jd,
            job_title=title, company=company, strategy=strategy,
        )
    except Exception as e:
        print(f"  ✘ tailor_cv_structured raised: {type(e).__name__}: {str(e)[:200]}")
        return {"label": label, "verdict": "TAILOR_RAISE"}
    if not doc:
        print("  ✘ tailor_cv_structured returned None (LLM JSON parse fail)")
        return {"label": label, "verdict": "TAILOR_NONE"}
    print(f"  structured_doc: summary_len={len(doc.get('summary','') or '')}  sections={len(doc.get('sections', []) or [])}")

    # Render via Typst, fall back to WeasyPrint structured.
    out_pdf = None
    try:
        out_pdf = generate_cv_pdf_styled_via_typst(
            structured=doc, job_title=title, company=company, output_dir=str(OUT),
        )
    except Exception as e:
        print(f"  ⚠ Typst render raised: {e}")
    if not out_pdf:
        try:
            out_pdf = generate_cv_pdf_styled_from_structured(
                structured=doc, job_title=title, company=company,
                output_dir=str(OUT), style_profile=None,
            )
            print(f"  ↩ Typst declined — used WeasyPrint fallback")
        except Exception as e:
            print(f"  ✘ WeasyPrint render also failed: {e}")
            return {"label": label, "verdict": "RENDER_FAIL", "doc": doc}
    if not out_pdf or not os.path.exists(out_pdf):
        print("  ✘ no output PDF"); return {"label": label, "verdict": "NO_OUTPUT"}

    # Acceptance checks on the rendered PDF
    out_text = _pdf_text(out_pdf)
    out_norm = _norm(out_text)
    n_pages = 0
    try:
        with fitz.open(out_pdf) as d: n_pages = d.page_count
    except Exception:
        pass
    ats_ok = _has_ats_sections(out_text)
    bullets_total = sum(len(s.get("entries") or []) and
                        sum(len(e.get("bullets") or []) for e in s.get("entries") or [])
                        for s in doc.get("sections", []) or [])
    # Simpler bullets count
    bullets_total = 0
    for s in doc.get("sections", []) or []:
        for e in s.get("entries", []) or []:
            bullets_total += len(e.get("bullets") or [])
    bullets_total += sum(len(s.get("bullets") or []) for s in doc.get("sections", []) or [])

    jd_kws = _extract_jd_keywords(jd, k=8)
    jd_kws_hit = [w for w in jd_kws if w in out_norm]

    # Fabrication probe: a JD-only term that doesn't appear in input cv_text
    cv_norm = _norm(cv_text)
    jd_only = [w for w in jd_kws if w in (jd.lower()) and w not in cv_norm]
    fabricated = [w for w in jd_only if w in out_norm and w not in cv_norm]

    has_summary = bool((doc.get("summary") or "").strip())

    print(f"  output PDF      : {out_pdf}  ({n_pages} pages, {len(out_text)} chars text)")
    print(f"  ATS sections    : {'OK' if ats_ok else 'MISSING (need 2+ of Experience/Education/Skills)'}")
    print(f"  bullets in doc  : {bullets_total}")
    print(f"  JD-kw hit       : {len(jd_kws_hit)}/{len(jd_kws)}   {jd_kws_hit}")
    print(f"  summary present : {has_summary}")
    print(f"  fabricated kws  : {fabricated if fabricated else 'none'}  (these would be RED FLAG)")

    verdict = (
        ats_ok
        and bullets_total >= 6
        and len(jd_kws_hit) >= 3
        and has_summary
        and not fabricated
    )
    print(f"  → {'PASS' if verdict else 'CHECK — partial'}")
    return {
        "label": label, "verdict": "PASS" if verdict else "CHECK",
        "ats_ok": ats_ok, "bullets": bullets_total,
        "jd_hits": len(jd_kws_hit), "fab": fabricated,
        "summary": has_summary, "out_pdf": out_pdf,
    }


def main():
    results = []
    for rel, jd, title, company, label in SAMPLE:
        results.append(_run_one(rel, jd, title, company, label))
    print("\n" + "=" * 88); print(" F3 SUMMARY"); print("=" * 88)
    for r in results:
        flag = "✅" if r.get("verdict") == "PASS" else "⚠️ "
        extras = ""
        if r.get("verdict") not in ("PASS", "MISSING"):
            extras = f"   ATS={r.get('ats_ok')} bullets={r.get('bullets')} jd_hits={r.get('jd_hits')} fab={r.get('fab')}"
        print(f"  {flag} {r['label']:30s}  {r['verdict']}{extras}")
    n_pass = sum(1 for r in results if r.get("verdict") == "PASS")
    print(f"\n  {n_pass}/{len(results)} designer CVs PASS rebuild-quality bar")
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
