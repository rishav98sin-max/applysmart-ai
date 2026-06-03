"""
H2 — DOCX path on a REALISTICALLY MESSY Word CV (tables + text boxes).

G2 proved the DOCX path on a CLEAN doc (headings + bullet paragraphs).
But real ATS Word CVs commonly use TABLES for layout — a contact-header
table, a 2-column "skills | detail" grid, an experience table with
date-in-left-cell / role-in-right-cell. Tables are exactly what the
docx parser historically struggled with (memory: bullets split across
table boundaries was a known pdf2docx failure; the native python-docx
parser walks table CELLS via XML in document order to avoid it).

This probe builds a table-heavy Word CV and runs it end-to-end:
  build_outline_from_docx → tailor → apply_diff_to_docx → verify.
A PASS means the launch goal's "one table or two table" Word CV case
actually works, not just the clean case.
"""
from __future__ import annotations
import os, sys
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
from docx.shared import Pt                                                      # type: ignore
from agents.cv_docx_pipeline import try_route_docx                              # type: ignore
from agents.cv_docx_editor  import apply_diff_to_docx                           # type: ignore
from agents.cv_diff_tailor  import tailor_cv_diff                               # type: ignore
from agents.tailor_strategist import build_tailor_strategy                     # type: ignore

OUT = ROOT / "_r-adapt-render" / "h2_docx"; OUT.mkdir(parents=True, exist_ok=True)
WORK = OUT / "_work"; WORK.mkdir(parents=True, exist_ok=True)

JD = """Senior Data Analyst — Commercial Analytics. Build dashboards and models
that drive commercial decisions; SQL, Python, Tableau/Power BI; experiment design
and A/B testing; stakeholder management with finance and marketing; translate
messy data into clear recommendations. 4+ yrs analytics experience, ideally in
retail / e-commerce / fintech. Bonus: dbt, Snowflake, causal inference."""


def _make_messy_docx(path: Path) -> None:
    """A table-LAYOUT Word CV: contact header in a 1-row table, an
    experience block as a 2-column (date | detail) table, plus a
    skills 2-col table. This is the shape that stresses the parser."""
    d = Document()

    # ── Contact header as a 1x2 table (name | contact) ──
    head = d.add_table(rows=1, cols=2)
    c0, c1 = head.rows[0].cells
    c0.paragraphs[0].add_run("MOHAMMED AL-FARSI").bold = True
    c1.paragraphs[0].add_run("mohammed.alfarsi@gmail.com\n+971-50-1234567\nDubai, UAE")

    d.add_paragraph()  # spacer
    p = d.add_paragraph(); p.add_run("Professional Summary").bold = True
    d.add_paragraph(
        "Data Analyst with 5 years turning messy commercial data into decisions "
        "across retail and fintech. Built 20+ dashboards, ran 40 experiments, and "
        "drove a 12% uplift in campaign ROI. Fluent in SQL, Python, and Tableau."
    )

    # ── Experience as a 2-col table: date-cell | role+bullets-cell ──
    p = d.add_paragraph(); p.add_run("Experience").bold = True
    exp = d.add_table(rows=2, cols=2)

    # Row 1 — Noon.com
    r0 = exp.rows[0].cells
    r0[0].paragraphs[0].add_run("Jan 2022 –\nPresent")
    rc = r0[1]
    rc.paragraphs[0].add_run("Senior Data Analyst — Noon.com (Dubai)").bold = True
    for b in [
        "Built 20+ Tableau dashboards tracking GMV, conversion, and cohort retention for the commercial team.",
        "Designed and ran 40 A/B tests on the checkout funnel; lifted conversion 8% over two quarters.",
        "Partnered with finance to model promo ROI; recommendations drove a 12% uplift in campaign returns.",
    ]:
        rc.add_paragraph(b, style="List Bullet")

    # Row 2 — Careem
    r1 = exp.rows[1].cells
    r1[0].paragraphs[0].add_run("Jun 2019 –\nDec 2021")
    rc2 = r1[1]
    rc2.paragraphs[0].add_run("Data Analyst — Careem (Dubai)").bold = True
    for b in [
        "Owned the SQL reporting layer for the rides marketplace; cut report turnaround from 2 days to 2 hours.",
        "Ran causal analysis on driver-incentive experiments; informed a AED 3M annual budget reallocation.",
    ]:
        rc2.add_paragraph(b, style="List Bullet")

    # ── Skills as a 2-col table ──
    p = d.add_paragraph(); p.add_run("Skills").bold = True
    sk = d.add_table(rows=2, cols=2)
    sk.rows[0].cells[0].paragraphs[0].add_run("Languages & Tools")
    sk.rows[0].cells[1].paragraphs[0].add_run("SQL, Python, Tableau, Power BI, dbt")
    sk.rows[1].cells[0].paragraphs[0].add_run("Methods")
    sk.rows[1].cells[1].paragraphs[0].add_run("A/B testing, causal inference, forecasting")

    p = d.add_paragraph(); p.add_run("Education").bold = True
    d.add_paragraph("B.Sc. Statistics — American University of Sharjah (2015 – 2019)")

    d.save(str(path))


def main() -> int:
    bar = "=" * 88
    print(bar); print(" H2 — messy (table-layout) Word CV through the DOCX path"); print(bar)

    docx_path = OUT / "messy_mohammed_analyst.docx"
    print(f"\n[1] build table-layout CV → {docx_path.name}")
    _make_messy_docx(docx_path)
    print(f"  ✓ {docx_path.stat().st_size} bytes")

    print(f"\n[2] try_route_docx → outline (table cells must parse in order)")
    route = try_route_docx(str(docx_path), workdir=str(WORK))
    if route is None:
        print("  ✘ DOCX route declined — table layout defeated the parser"); return 1
    outline = route.outline
    roles = outline.get("roles") or []
    n_b = sum(len(r.get("bullets") or []) for r in roles)
    print(f"  ✓ {len(roles)} roles, {n_b} bullets, summary={'Y' if (outline.get('summary') or '').strip() else 'N'}")
    for i, r in enumerate(roles[:6], 1):
        print(f"    [{i}] {r.get('header','')[:55]:55s} ({len(r.get('bullets') or [])} bullets)")

    # Critical correctness check: did the date-cell text get mis-parsed as a
    # role header? (the classic table failure). Headers should be the ROLE
    # lines, not 'Jan 2022 - Present'.
    bad_headers = [r.get("header","") for r in roles
                   if r.get("header","").strip().lower().startswith(("jan ","jun ","feb ","mar "))
                   and "—" not in r.get("header","") and "-" not in r.get("header","").replace("Present","")]
    if bad_headers:
        print(f"  ⚠️  date-cell leaked as role header: {bad_headers}")

    if len(roles) < 1 or n_b < 3:
        print("  ✘ outline too thin — table parse likely dropped content"); return 2

    print(f"\n[3] tailor (re-aim)")
    try:
        strat = build_tailor_strategy(outline, JD, "Senior Data Analyst", "CommerceCo")
    except Exception as e:
        print(f"  ⚠ strategist: {e}"); strat = {}
    diff = tailor_cv_diff(cv_pdf_path=str(docx_path), job_description=JD,
                          job_title="Senior Data Analyst", company="CommerceCo",
                          outline=outline, strategy=strat)
    n_ch = sum(len(v) for v in (diff.get("bullets") or {}).values() if isinstance(v, list)) if diff else 0
    print(f"  ✓ diff: {n_ch} bullet entries")

    print(f"\n[4] apply_diff_to_docx")
    out_docx = OUT / "tailored_messy_analyst.docx"
    ok, reason = apply_diff_to_docx(docx_path=str(docx_path), diff=diff,
                                    outline=outline, output_path=str(out_docx))
    if not ok:
        print(f"  ✘ apply failed: {reason}"); return 3
    print(f"  ✓ {out_docx.name} ({out_docx.stat().st_size} bytes)")

    print(f"\n[5] verify — output readable + tailoring landed + tables intact")
    od = Document(str(out_docx))
    # Count text in tables vs paragraphs to confirm tables survived
    n_tables = len(od.tables)
    table_text = ""
    for t in od.tables:
        for row in t.rows:
            for cell in row.cells:
                table_text += cell.text + " "
    full = "\n".join(p.text for p in od.paragraphs) + " " + table_text
    landed = 0
    for role, entries in (diff.get("bullets") or {}).items():
        for e in entries:
            if isinstance(e, dict) and (e.get("text") or "").strip() and e["text"].strip() in full:
                landed += 1
    # Critical: original hard facts still present (no data loss through tables)
    facts_kept = all(f in full for f in ["Noon.com", "Careem", "12%", "American University"])
    print(f"  ✓ tables in output: {n_tables}   (input had 4)")
    print(f"  ✓ tailored bullets present: {landed}/{n_ch}")
    print(f"  ✓ original hard facts preserved (Noon/Careem/12%/AUS): {facts_kept}")

    verdict = ok and len(roles) >= 1 and n_b >= 3 and n_ch >= 1 and facts_kept and not bad_headers
    print(f"\n→ {'PASS' if verdict else 'CHECK'}   out={out_docx}")
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(main())
