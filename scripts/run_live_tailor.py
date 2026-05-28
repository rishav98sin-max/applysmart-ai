"""
Live end-to-end tailoring run (strategist + diff-tailor, real LLM calls).
Uses a real JD scraped from the web. Prints the strategy, the tailored
summary + bullets, the alignment delta, and any reverts so we can read
the actual output quality of the fact-anchored free-rewrite approach.

Usage:
    venv\\Scripts\\python.exe scripts\\run_live_tailor.py mohammed
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Load .env so the LLM keys are available.
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

import agents.pdf_editor        as pe   # type: ignore
import agents.cv_diff_tailor    as cdt  # type: ignore
from agents.tailor_strategist import build_tailor_strategy  # type: ignore

# Real Hays Data Analyst JD (Belfast, energy/sustainability) scraped
# from hays.co.uk job-search, May 2026.
JD_MOHAMMED = """\
Data Analyst (SQL/Power BI) — Belfast
An energy and sustainability organisation is seeking a Data Analyst to join
their Belfast team working on large-scale energy programmes, analysing
high-volume data for insights that inform low-carbon strategy and policy
decisions.

Key Responsibilities:
- Analyse and interpret large, complex datasets from live energy programmes
- Produce reports and dashboards for internal teams, regulators, and external
  stakeholders
- Identify trends and performance insights that drive operational improvements
- Translate findings into briefings for senior stakeholders
- Support evidence-based decision-making across commercial and policy initiatives

Required Skills:
- 3+ years' experience in a Data Analyst, Research Analyst or similar role
- Advanced Excel proficiency
- Power BI, SQL or similar reporting/analytics tools
- Strong data interpretation and presentation abilities
- Report writing and stakeholder communication expertise
- Exposure to cloud data platforms (Azure, Microsoft Fabric) is an advantage
"""

FIXTURES = {
    "mohammed": {
        "cv":    r"D:\Projects\job-application-agent\CVs\Run26\Resume.pdf",
        "jd":    JD_MOHAMMED,
        "title": "Data Analyst",
        "company": "Hays (Energy & Sustainability)",
    },
}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    who = sys.argv[1] if len(sys.argv) > 1 else "mohammed"
    fx = FIXTURES[who]
    cv, jd, title, company = fx["cv"], fx["jd"], fx["title"], fx["company"]

    print("=" * 78)
    print(f" LIVE TAILOR — {who}  ({title} @ {company})")
    print("=" * 78)

    outline = pe.build_outline_cached(cv)
    orig_summary = (outline.get("summary") or "").strip()
    print(f"\nORIGINAL SUMMARY:\n  {orig_summary}\n")

    # ── Strategist (live) ────────────────────────────────────────────
    print("--- running strategist (live LLM) ---")
    strategy = build_tailor_strategy(outline, jd, title, company)
    print(f"\nnarrative_angle: {strategy.get('narrative_angle')}")
    print(f"jd_thesis:       {strategy.get('jd_thesis')}")
    pri = strategy.get("jd_priorities") or {}
    print(f"jd_priorities.must_have:    {pri.get('must_have')}")
    print(f"jd_priorities.nice_to_have: {pri.get('nice_to_have')}")
    print(f"hot_zone: {strategy.get('hot_zone')}")
    print("\nbullet_strategy (post score+cap):")
    for role, entries in (strategy.get("bullet_strategy") or {}).items():
        rw = [e for e in entries if isinstance(e, dict) and e.get("action") == "rewrite_verb_led"]
        if not rw:
            continue
        print(f"  role: {role[:55]}")
        for e in rw:
            print(f"    i={e.get('i')} score={e.get('jd_relevance_score')} "
                  f"lead={e.get('lead_with')!r} kw={e.get('jd_keyword')!r}")

    # ── Diff-tailor (live) ───────────────────────────────────────────
    print("\n--- running diff-tailor (live LLM) ---")
    diff = cdt.tailor_cv_diff(
        cv_pdf_path=cv, job_description=jd, job_title=title, company=company,
        outline=outline, strategy=strategy,
    )

    print("\n" + "=" * 78)
    print(" TAILORED OUTPUT")
    print("=" * 78)
    new_sum = (diff.get("summary") or "").strip()
    print(f"\nTAILORED SUMMARY:\n  {new_sum}")
    if new_sum == orig_summary:
        print("  (unchanged / reverted to original)")

    print("\nTAILORED BULLETS (only rewrites shown):")
    for role, entries in (diff.get("bullets") or {}).items():
        shown = [e for e in entries if isinstance(e, dict) and isinstance(e.get("text"), str) and e["text"].strip()]
        if not shown:
            continue
        print(f"\n  role: {role[:60]}")
        # Map original text by index for before/after.
        orig_map = {}
        for r in outline.get("roles", []):
            if r["header"].strip().lower() == role.strip().lower():
                for i, b in enumerate(r["bullets"]):
                    orig_map[i] = (b["text"] if isinstance(b, dict) else str(b))
        for e in shown:
            i = e["i"]
            print(f"    [i={i}]")
            print(f"      BEFORE: {orig_map.get(i, '?')}")
            print(f"      AFTER : {e['text']}")

    dbg = diff.get("_debug") or {}
    print("\n--- diagnostics ---")
    print(f"  jd_alignment: {dbg.get('jd_alignment')}")
    print(f"  low_jd_lift:  {dbg.get('low_jd_lift', False)}")
    print(f"  summary_reverts: {dbg.get('summary_reverts')}")
    print(f"  bullet_reverts_count: {dbg.get('bullet_reverts_count')}")
    print(f"  all_reverted: {dbg.get('all_reverted')}")

    side = ROOT / "_r-adapt-render" / f"live_{who}_diff.json"
    side.parent.mkdir(parents=True, exist_ok=True)
    side.write_text(json.dumps({"strategy": strategy, "diff": diff}, indent=2, default=str), encoding="utf-8")
    print(f"\nsidecar: {side}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
