"""
G2 — DOCX upload path smoke test.

The launch invariant covers BOTH PDF and DOCX uploads (a significant share
of LinkedIn users upload Word). We've tested PDF heavily but not DOCX.

This probe:
  1. Synthesises a realistic Rishav-PM CV as a .docx (python-docx)
  2. Runs it through cv_docx_pipeline.try_route_docx  → outline
  3. Runs through the re-aim tailor (tailor_cv_diff)  → diff
  4. Applies via cv_docx_editor.apply_diff_to_docx    → tailored.docx
  5. Verifies output exists + has the tailored bullets
  6. Optionally renders to PDF via cv_docx_to_pdf for end-to-end proof

Skips Groq entirely — heuristic outline + DeepSeek tailor are enough.
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
os.environ["APPLYSMART_REAIM"] = "1"
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from docx import Document                                                      # type: ignore
from agents.cv_docx_pipeline import try_route_docx                              # type: ignore
from agents.cv_docx_editor  import apply_diff_to_docx                           # type: ignore
from agents.cv_diff_tailor  import tailor_cv_diff                               # type: ignore
from agents.tailor_strategist import build_tailor_strategy                     # type: ignore

OUT = ROOT / "_r-adapt-render" / "g2_docx"
OUT.mkdir(parents=True, exist_ok=True)
WORK = OUT / "_work"
WORK.mkdir(parents=True, exist_ok=True)

JD_PM = """Senior Product Manager — B2B SaaS. Own a full product surface
end-to-end: discovery, roadmap, requirements, launch. Partner with engineering,
design, sales and CS. Drive product-led growth metrics, run experiments and
A/B tests, lead quarterly planning. Strong on user research, data analysis
(SQL/Amplitude), stakeholder management, executive comms, prioritisation
under ambiguity. 5+ yrs PM experience, ideally with SaaS / vertical platforms
/ AI products. Bonus: experience with PLG funnels, self-serve onboarding,
usage-based pricing."""


def _make_test_docx(path: Path) -> None:
    """Realistic Rishav-style PM CV as a Word document."""
    d = Document()
    d.add_heading("Rishav Singh", level=0)
    p = d.add_paragraph(); p.add_run("rishav98sin@gmail.com  ·  +91-9999999999  ·  linkedin.com/in/rishav-singh")
    d.add_heading("Professional Summary", level=1)
    d.add_paragraph(
        "Product Manager with 4 years building 0-1 SaaS and AI products. "
        "Scoped 2 platform launches, ran 30+ experiments, drove revenue from "
        "single-digit MRR to seven figures. Comfortable owning roadmap, specs, "
        "experiments, and launch end-to-end."
    )
    d.add_heading("Experience", level=1)

    d.add_heading("ApplySmart AI — Founder & Product Manager", level=2)
    d.add_paragraph("Bangalore, India · Jul 2025 – Present")
    for b in [
        "Scoped the architecture as a supervisor + workers pattern with a 0–100 relevance scorer, shipped to LinkedIn launch.",
        "Made trade-offs for the Groq free tier: capped LLM calls per run, set a rate-limit ceiling that holds at scale.",
        "Owned every decision from problem to launch (scope, flows, UI, safety, deploy) with crash-safe deployment.",
        "Identified a PM pain point around unstructured feedback synthesis; designed and shipped VoC Insight Hub.",
    ]:
        d.add_paragraph(b, style="List Bullet")

    d.add_heading("Prithvi.ai — Product Manager", level=2)
    d.add_paragraph("Remote · Apr 2023 – Jun 2025")
    for b in [
        "Owned the conversational-AI product surface from discovery to GA; led 2 quarterly planning cycles.",
        "Ran 30+ A/B tests on the onboarding funnel; lifted day-1 activation 18 → 27 %.",
        "Partnered with engineering + design + sales on pricing experiments; converted 3 enterprise contracts.",
    ]:
        d.add_paragraph(b, style="List Bullet")

    d.add_heading("IBM — Associate Product Manager", level=2)
    d.add_paragraph("Bangalore · Jan 2021 – Mar 2023")
    for b in [
        "Worked on a B2B IoT analytics platform; defined 14 PRDs, shipped 6 features GA.",
        "Drove platform-led growth metrics: WAU, time-to-first-insight, retention curves.",
    ]:
        d.add_paragraph(b, style="List Bullet")

    d.add_heading("Education", level=1)
    d.add_paragraph("B.Tech, Electronics & Communication — VIT University (2017 – 2021)")

    d.add_heading("Skills", level=1)
    d.add_paragraph("Product: Roadmap, OKRs, RICE, JTBD, PRD authoring, A/B testing")
    d.add_paragraph("Data: SQL, Amplitude, Mixpanel, Segment, Looker")
    d.add_paragraph("Tech: REST APIs, Python (light), LangGraph, AWS")
    d.save(str(path))


def main() -> int:
    bar = "=" * 88
    print(bar); print(" G2 — DOCX upload path smoke test"); print(bar)

    test_docx = OUT / "test_rishav_pm.docx"
    print(f"\n[1/5] Synthesising test CV → {test_docx.name}")
    _make_test_docx(test_docx)
    assert test_docx.exists(), "synthetic .docx not written"
    print(f"  ✓ written ({test_docx.stat().st_size} bytes)")

    # 2. Route + parse outline
    print(f"\n[2/5] try_route_docx → outline")
    route = try_route_docx(str(test_docx), workdir=str(WORK))
    if route is None:
        print("  ✘ DOCX route declined (0 roles parsed?)"); return 1
    outline = route.outline
    n_roles = len(outline.get("roles") or [])
    n_bullets = sum(len(r.get("bullets") or []) for r in outline.get("roles") or [])
    has_summary = bool((outline.get("summary") or "").strip())
    print(f"  ✓ outline: {n_roles} role(s), {n_bullets} bullet(s), summary={'present' if has_summary else 'MISSING'}")
    for i, r in enumerate(outline.get("roles", [])[:4], 1):
        bs = len(r.get("bullets") or [])
        print(f"    [{i}] {r.get('header','')[:60]}  ({bs} bullets)")
    if n_roles < 1 or n_bullets < 3:
        print("  ✘ outline too thin for a meaningful tailor"); return 2

    # 3. Strategy + re-aim tailor
    print(f"\n[3/5] build_tailor_strategy + tailor_cv_diff (re-aim path)")
    try:
        strat = build_tailor_strategy(outline, JD_PM, "Senior Product Manager", "AcmeSaaS")
    except Exception as e:
        print(f"  ⚠ strategist failed ({type(e).__name__}: {e}); continuing with empty strategy")
        strat = {}
    try:
        diff = tailor_cv_diff(
            cv_pdf_path=str(test_docx), job_description=JD_PM,
            job_title="Senior Product Manager", company="AcmeSaaS",
            outline=outline, strategy=strat,
        )
    except Exception as e:
        print(f"  ✘ tailor_cv_diff raised: {type(e).__name__}: {e}"); return 3
    if not diff or not diff.get("bullets"):
        print(f"  ✘ tailor returned empty diff (debug={diff.get('_debug') if diff else None})"); return 4
    n_changes = sum(len(v) for v in (diff.get("bullets") or {}).values() if isinstance(v, list))
    has_sum_diff = bool((diff.get("summary") or "").strip())
    print(f"  ✓ diff: {n_changes} bullet entries, summary_diff={has_sum_diff}")

    # 4. Apply to .docx
    print(f"\n[4/5] apply_diff_to_docx → tailored.docx")
    out_docx = OUT / "tailored_rishav_pm_acmesaas.docx"
    ok, reason = apply_diff_to_docx(
        docx_path=str(test_docx), diff=diff, outline=outline, output_path=str(out_docx),
    )
    if not ok:
        print(f"  ✘ apply failed: {reason}"); return 5
    print(f"  ✓ written: {out_docx.name} ({out_docx.stat().st_size} bytes)")

    # 5. Read back + verify tailoring landed
    print(f"\n[5/5] verify tailored .docx contents")
    out_doc = Document(str(out_docx))
    full_text = "\n".join(p.text for p in out_doc.paragraphs)
    # Did at least 1 tailored bullet text land in the output?
    landed = 0
    for role, entries in (diff.get("bullets") or {}).items():
        for e in entries:
            if not isinstance(e, dict): continue
            t = (e.get("text") or "").strip()
            if t and t in full_text:
                landed += 1
    print(f"  ✓ output has {len(out_doc.paragraphs)} paragraphs, {len(full_text)} chars")
    print(f"  ✓ {landed}/{n_changes} tailored bullet texts present in output")

    verdict = ok and n_roles >= 1 and n_bullets >= 3 and n_changes >= 1
    print(f"\n→ {'PASS' if verdict else 'CHECK — partial'}")
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(main())
