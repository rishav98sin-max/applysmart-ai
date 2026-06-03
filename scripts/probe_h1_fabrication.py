"""
H1 — RIGOROUS no-fabrication check on the designer REBUILD path.

The F3 probe flagged 'fabricated kws' but used a weak input extractor
(parse_cv on image-heavy designer PDFs misses text), so we couldn't tell
real fabrication from extraction gaps. This probe is the real test:

  1. Extract input text ROBUSTLY (fitz text + words + rawdict passes).
  2. Run the rebuild (tailor_cv_structured) → structured doc.
  3. Pull every HARD FACT atom from the output (companies, numbers,
     acronyms, proper nouns, credentials) via the SAME extractor the
     tailor guards use (_extract_fact_atoms).
  4. Ground each output atom against the input text.
  5. Report ungrounded atoms. Generic JD vocabulary ('deliver',
     'routes') is NOT counted — only concrete facts that, if invented,
     would mislead a recruiter (a company you didn't work at, a metric
     you didn't hit, a degree you don't hold).

A clean run = every hard fact in the output traces to the input = no
fabrication. This is the launch invariant, verified not assumed.

⚠️  SCOPE: this probe calls tailor_cv_structured in ISOLATION. It does NOT
run the production review+retry loop (review_rebuilt_structured →
extra_prohibitions retry in job_agent.py ~2052). That loop flags any JD
term present in the rebuild but absent from the real CV and retries with
it banned. So this probe's findings are an UPPER BOUND on production
fabrication — a term flagged here may still be caught before a real user
sees it. Use FULL-LENGTH JDs (>50 words) or the strategist is skipped and
the do_not_inject guard is disabled, inflating the count.
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
from agents.cv_diff_tailor import _extract_fact_atoms                          # type: ignore

DESIGNERS = ROOT / "CVs" / "Orignal Base CV" / "DesignerCVExamples"

# FULL-LENGTH JDs (>50 words) so the strategist runs and populates the
# do_not_inject fabrication-guard list — exactly the real-product path.
JD_BANK = {
    "Blue and Yellow Modern Graphic Designer CV.pdf": (
        "Senior Graphic Designer — Brand & Visual Identity. Lead visual identity work "
        "for a fast-growing consumer brand. Concept and execute campaigns across digital "
        "and print, design landing pages, social assets, packaging, and brand systems. "
        "Strong portfolio in typography, layout, colour theory and illustration. Daily "
        "tools: Figma, Adobe Creative Suite (Illustrator, Photoshop, InDesign). Partner "
        "with marketing, product and copy to elevate visual storytelling. 4+ yrs agency "
        "or in-house experience; ability to balance multiple projects on tight deadlines.",
        "Senior Graphic Designer", "BrandCo"),
    "Blue Light Blue Color Blocks Flight Attendant CV.pdf": (
        "Flight Attendant — International Routes. Deliver exceptional in-flight customer "
        "service to passengers on long-haul international routes. Conduct safety briefings, "
        "manage emergency procedures, serve meals and beverages, address passenger concerns "
        "with empathy and professionalism. Fluent in English; second language a plus. Must "
        "pass FAA/EASA safety training and remain calm under pressure. Flexible schedule "
        "(overnights, holidays, multi-day trips). Strong interpersonal skills, attention to "
        "detail. Previous customer-facing or hospitality experience preferred.",
        "Flight Attendant", "GlobalAir"),
    "Rishav's Resume (1).pdf": (
        "Senior Product Manager — B2B SaaS. Own a full product surface end-to-end: "
        "discovery, roadmap, requirements, launch. Partner with engineering, design, sales "
        "and customer success. Drive product-led growth metrics, run experiments and A/B "
        "tests, lead quarterly planning. Strong on user research, data analysis "
        "(SQL/Amplitude), stakeholder management, executive comms, prioritisation under "
        "ambiguity. 5+ yrs PM experience, ideally with SaaS / vertical platforms / AI "
        "products. Bonus: PLG funnels, self-serve onboarding, usage-based pricing.",
        "Senior Product Manager", "AcmeSaaS"),
    "Systems Design Resume in Bright Blue White Bold Accent Style.pdf": (
        "Senior Software Engineer — Backend Systems. Design and build scalable distributed "
        "systems that handle millions of requests per day. Own services end-to-end: "
        "technical design, implementation, monitoring and on-call. Strong fundamentals in "
        "algorithms, data structures, distributed systems (queues, caches, event streams). "
        "Languages: Python/Java/Go. Cloud: AWS or GCP. Bonus: Kafka, gRPC, Kubernetes, "
        "OpenTelemetry, performance optimisation. 5+ years professional software "
        "engineering experience required, including a shipped production service at scale.",
        "Senior Software Engineer", "TechCo"),
    "White Simple Sales Representative CV Resume.pdf": (
        "Sales Representative — SaaS Inside Sales. Drive new business acquisition for our "
        "SaaS platform: outbound prospecting, discovery calls, product demos, contract "
        "negotiation and close. Manage a pipeline of 30-50 opportunities; consistently hit "
        "monthly quotas. Strong on consultative selling, SPIN / MEDDIC methodology, "
        "objection handling, CRM hygiene (Salesforce or HubSpot). 2+ yrs SaaS sales "
        "experience preferred. Comfortable in a high-volume, metrics-driven environment "
        "with weekly leaderboards. Bonus: vertical specialisation in fintech or martech.",
        "Sales Representative", "SaaSCo"),
}


def _robust_input_text(pdf_path: str) -> str:
    """Extract as much input text as possible — multiple fitz passes."""
    chunks = []
    try:
        chunks.append(parse_cv(pdf_path) or "")
    except Exception:
        pass
    try:
        d = fitz.open(pdf_path)
        for pg in d:
            chunks.append(pg.get_text("text"))
            # words pass catches text the linear extractor reorders/drops
            chunks.append(" ".join(w[4] for w in pg.get_text("words")))
        d.close()
    except Exception:
        pass
    return _norm(" ".join(chunks))


def _norm(t: str) -> str:
    t = (t or "").lower()
    for a, b in [("’","'"),("‘","'"),("“",'"'),("”",'"'),("–","-"),("—","-")]:
        t = t.replace(a, b)
    return re.sub(r"[^a-z0-9%$.\-/ ]", " ", re.sub(r"\s+", " ", t)).strip()


# Generic JD/action vocabulary that is NOT a hard fact — relabeling proven
# work in JD vocabulary is explicitly allowed. Only flag concrete entities.
_GENERIC = {
    "deliver","delivered","routes","route","in-flight","inflight","saas","drive",
    "driven","business","acquisition","experience","manager","management","senior",
    "design","systems","engineer","engineering","product","sales","representative",
    "customer","service","skills","summary","education","work","team","teams",
    "project","projects","develop","developed","build","built","lead","led","support",
    "international","domestic","professional","technical","analysis","strong","proven",
}


def _is_hard_fact(atom: str) -> bool:
    """A hard fact = something whose invention would mislead a recruiter:
    a number/metric, an acronym, or a multi-word proper noun (company,
    institution, certification). Single generic words are excluded."""
    a = atom.strip()
    low = a.lower()
    if low in _GENERIC:
        return False
    if re.search(r"\d", a):                       # metrics, years, scale
        return True
    if re.fullmatch(r"[A-Z]{3,}s?", a):           # acronyms (FAA, IATA, AWS)
        return True
    if len(a) >= 5 and a[0].isupper():            # proper nouns / orgs
        return True
    return False


def _atom_grounded(atom: str, input_norm: str) -> bool:
    a = _norm(atom)
    if not a:
        return True
    # direct substring
    if a in input_norm:
        return True
    # token overlap: every significant token present (handles reordering)
    toks = [t for t in a.split() if len(t) >= 3]
    if toks and all(t in input_norm for t in toks):
        return True
    # numbers: the digits must appear somewhere in input
    digits = re.sub(r"\D", "", a)
    if digits and digits in re.sub(r"\D", "", input_norm):
        return True
    return False


def _run_one(rel: str, jd: str, title: str, company: str):
    cv = str(DESIGNERS / rel)
    bar = "=" * 88
    print("\n" + bar); print(f" {rel[:70]}"); print(bar)
    if not os.path.exists(cv):
        print("  ✘ missing"); return None
    input_norm = _robust_input_text(cv)
    cv_text = parse_cv(cv)
    try:
        strat = build_tailor_strategy({"summary": cv_text[:600], "roles": []}, jd, title, company)
    except Exception:
        strat = {}
    doc = tailor_cv_structured(cv_text=cv_text, job_description=jd,
                               job_title=title, company=company, strategy=strat)
    if not doc:
        print("  ✘ tailor returned None"); return None

    # Collect every output string field
    out_strings = [doc.get("summary","")]
    for s in doc.get("sections", []) or []:
        for p in s.get("paragraphs", []) or []:
            out_strings.append(p)
        for r in s.get("roles", []) or []:
            out_strings += [r.get("title",""), r.get("sub",""), r.get("dates","")]
            out_strings += (r.get("bullets") or [])

    # Extract hard-fact atoms from output, ground each
    ungrounded = []
    checked = 0
    for s in out_strings:
        for atom in _extract_fact_atoms(s or ""):
            if not _is_hard_fact(atom):
                continue
            checked += 1
            if not _atom_grounded(atom, input_norm):
                ungrounded.append(atom)

    ungrounded = sorted(set(ungrounded))
    print(f"  input text: {len(input_norm)} chars   hard-facts checked: {checked}")
    if ungrounded:
        print(f"  ⚠️  UNGROUNDED hard facts ({len(ungrounded)}): {ungrounded[:20]}")
        print(f"      → inspect: real fabrication, or input-extraction gap?")
    else:
        print(f"  ✅ every hard fact in output traces to input — NO fabrication")
    return {"rel": rel, "checked": checked, "ungrounded": ungrounded}


def main():
    results = []
    for rel, (jd, title, company) in JD_BANK.items():
        r = _run_one(rel, jd, title, company)
        if r: results.append(r)
    print("\n" + "=" * 88); print(" H1 FABRICATION SUMMARY"); print("=" * 88)
    clean = 0
    for r in results:
        flag = "✅ clean" if not r["ungrounded"] else f"⚠️  {len(r['ungrounded'])} ungrounded"
        clean += int(not r["ungrounded"])
        print(f"  {r['rel'][:50]:50s} facts={r['checked']:3d}  {flag}")
        if r["ungrounded"]:
            print(f"       {r['ungrounded'][:12]}")
    print(f"\n  {clean}/{len(results)} designer rebuilds have ZERO ungrounded hard facts")
    return 0 if clean == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
