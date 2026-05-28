"""
Adaptability probe — run the Run 26 fixes against multiple real CVs.

For each (input CV, Langfuse.jsonl) pair, this script:
  1. Loads every cv_diff_tailor observation's output from the Langfuse log.
  2. Runs the new validators (concrete-term / title / duplicate / sector)
     on the recorded LLM summary output to see whether our new guards
     would have caught regressions in this CV too.
  3. Runs apply_edits with the recorded diff against the SOURCE CV and
     checks the output for phantom-HR / content-loss regressions.
  4. Reports per-CV pass/fail across both axes.

Read-only against inputs; writes to _r-adapt-render/.
"""
from __future__ import annotations
import io
import json
import os
import shutil
import sys
import contextlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import fitz                          # type: ignore
import agents.pdf_editor       as pe   # type: ignore
import agents.cv_diff_tailor   as cdt  # type: ignore

OUT_DIR = ROOT / "_r-adapt-render"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Test fixtures: (label, source PDF, Langfuse jsonl OR None for synthetic)
FIXTURES: List[Tuple[str, str, Optional[str]]] = [
    (
        "Run26 Mohammed (Data Analyst)",
        r"D:\Projects\job-application-agent\CVs\Run26\Resume.pdf",
        r"D:\Projects\job-application-agent\CVs\Run26\Langfuse.jsonl",
    ),
    (
        "Cormac (IS Auditor) — synthetic no-op",
        r"D:\Projects\job-application-agent\CVs\Run25\Cormac Holleran CV 2026.pdf",
        None,   # Run25 log was pre-batch-21 (mis-routed to rebuild); use synthetic
    ),
    (
        "Run24 Rishav (PM)",
        r"D:\Projects\job-application-agent\CVs\Orignal Base CV\RishavSingh_ProductManagerCV.pdf",
        r"D:\Projects\job-application-agent\CVs\Run 24\1779129645145-lf-events-export-cmokjdwoh09piad07jdowu4el.jsonl",
    ),
    (
        "Shrestha (Acct Exec, 2-col layout) — synthetic no-op",
        r"D:\Projects\job-application-agent\CVs\Orignal Base CV\Shrestha Ghosh_CV.pdf",
        None,   # No Langfuse — generates a no-op diff to exercise layout
    ),
]


def _synthetic_noop_diff(outline: dict) -> dict:
    """Build a diff that touches one bullet per role with a near-identical
    rewrite — exercises the redact + re-insert path without changing
    content. Catches crashes on layouts the Run 26 fixes haven't tested."""
    bullets = {}
    for r in outline.get("roles", []) or []:
        bs = r.get("bullets") or []
        if not bs:
            continue
        entries = []
        for i, b in enumerate(bs):
            t = (b.get("text") if isinstance(b, dict) else str(b)) or ""
            if i == 0 and t:
                # Trivial rewrite of bullet 0: drop trailing period only
                entries.append({"i": 0, "text": t.rstrip(".").strip()})
            else:
                entries.append({"i": i})
        bullets[r.get("header") or ""] = entries
    return {
        "summary": outline.get("summary") or "",
        "bullets": bullets,
        "skills_order": [],
    }


def _load_diff_outputs(jsonl_path: str) -> List[Dict[str, Any]]:
    diffs: List[Dict[str, Any]] = []
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                obs = json.loads(line)
            except Exception:
                continue
            if obs.get("name") != "cv_diff_tailor":
                continue
            out = obs.get("output")
            if isinstance(out, str):
                try:
                    out = json.loads(out)
                except Exception:
                    continue
            if isinstance(out, dict):
                diffs.append(out)
    return diffs


def _horizontal_line_keys(pdf_path: str, y_low: float, y_high: float):
    """Return the set of (y, x0, x1, width) tuples for every visible black
    horizontal line in the band. Visibility key = position + width.
    Duplicates at the same key are collapsed (same as visual rendering)."""
    d = fitz.open(pdf_path)
    keys = set()
    for page in d:
        for dr in page.get_drawings() or []:
            color = dr.get("color")
            w = round(dr.get("width", 0) or 0, 2)
            for it in dr.get("items", []) or []:
                if it[0] != "l":
                    continue
                p1, p2 = it[1], it[2]
                if abs(p2.y - p1.y) < 0.5 and y_low <= p1.y < y_high:
                    if color and color != (1, 1, 1):
                        key = (
                            round(p1.y, 1),
                            round(min(p1.x, p2.x), 1),
                            round(max(p1.x, p2.x), 1),
                            w,
                        )
                        keys.add(key)
    d.close()
    return keys


def _net_visible_new_lines(src_pdf: str, out_pdf: str) -> int:
    """Lines in out_pdf at positions NOT present in src_pdf. Duplicates
    of existing source lines (same y/x/width) are collapsed — they render
    visually identical to the source, so they don't count as regressions."""
    src_keys = _horizontal_line_keys(src_pdf, 0, 1000)
    out_keys = _horizontal_line_keys(out_pdf, 0, 1000)
    return len(out_keys - src_keys)


def _bullet_text_summary(pdf_path: str) -> List[Tuple[str, int, List[str]]]:
    """Return (role_header, n_bullets, [bullet_texts_truncated]) per role."""
    out: List[Tuple[str, int, List[str]]] = []
    sections = pe.extract_structure(pdf_path)
    for sec in sections:
        if sec.get("type") not in ("experience", "projects"):
            continue
        for role in pe._role_blocks(sec):
            bullets = role.get("bullet_groups") or []
            texts = [(b.get("text") or "")[:80] for b in bullets]
            out.append((role.get("header_text") or "(no header)", len(bullets), texts))
    return out


def _validators_report(orig_summary: str, new_summary: str, outline: dict,
                       cv_text: str = "") -> Dict[str, Any]:
    """Run the four new validators on the LLM's actual summary."""
    if not new_summary or not orig_summary:
        return {"skipped": "missing_orig_or_new_summary"}
    return {
        "concrete_terms_missing": cdt._check_concrete_terms_preserved(
            orig_summary, new_summary
        ),
        "title_escalation": cdt._title_escalation(orig_summary, new_summary),
        "duplicate_phrase": cdt._check_summary_duplicate_phrase(new_summary),
        "sectors_fabricated": cdt._check_sector_fabrication(
            orig_summary, new_summary, outline, cv_text
        ),
        "extracted_orig_terms": cdt._extract_concrete_summary_terms(orig_summary),
        "extracted_orig_title": cdt._extract_summary_title_clause(orig_summary),
        "extracted_new_title":  cdt._extract_summary_title_clause(new_summary),
    }


def _run_layout_probe(label: str, source_pdf: str, diff: dict,
                      work_dir: Path) -> Dict[str, Any]:
    """Apply the recorded diff to the source CV with our new pdf_editor
    code and detect physical regressions (phantom HR, wiped bullets)."""
    src = Path(source_pdf)
    work_dir.mkdir(parents=True, exist_ok=True)
    work_pdf = work_dir / f"{src.stem}_work.pdf"
    out_pdf  = work_dir / f"{src.stem}_edited.pdf"
    shutil.copyfile(src, work_pdf)

    os.environ["APPLYSMART_DEBUG_BULLETS"] = "0"
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            report = pe.apply_edits(str(work_pdf), diff, str(out_pdf))
    except Exception as exc:
        return {"crashed": repr(exc), "stdout": buf.getvalue()[:2000]}
    stdout = buf.getvalue()

    # Compare drawings: only count NET NEW visible lines (at positions
    # absent from source). Same-position duplicates render identically
    # so they don't count as regressions.
    net_new_hr = _net_visible_new_lines(source_pdf, str(out_pdf))

    # Compare bullet structure: any bullet whose text shrank to <30% of
    # source = likely content-loss regression.
    src_struct = _bullet_text_summary(source_pdf)
    out_struct = _bullet_text_summary(str(out_pdf))
    shrunk: List[str] = []
    for (rh_s, n_s, txts_s), (rh_o, n_o, txts_o) in zip(src_struct, out_struct):
        if rh_s != rh_o:
            continue
        for j, (ts, to) in enumerate(zip(txts_s, txts_o)):
            if len(ts) > 30 and len(to) < int(len(ts) * 0.3):
                shrunk.append(f"  • {rh_s[:50]!r}/i={j}: src({len(ts)}c) → out({len(to)}c)")

    return {
        "net_new_horizontal_lines": net_new_hr,
        "applied_rewrites": (report.get("applied", {}).get("bullets", {}) or {}).get("rewrites", 0),
        "skipped_count": len(report.get("skipped") or []),
        "trimmed_count": len(report.get("trimmed") or []),
        "summary_revert_reasons": [
            r.get("reason") for r in (report.get("_debug", {}) or {}).get("summary_reverts", [])
        ],
        "content_loss_bullets": shrunk,
        "out_pdf": str(out_pdf),
    }


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print("=" * 78)
    print(" Adaptability probe — Run 26 fixes against 3 real CVs")
    print("=" * 78)

    overall: List[Dict[str, Any]] = []
    for label, src_pdf, lf_path in FIXTURES:
        print(f"\n\n=== {label} ===")
        if not Path(src_pdf).exists():
            print(f"  ⚠️  source CV not found: {src_pdf}")
            continue
        outline = pe.build_outline_cached(src_pdf)
        orig_summary = (outline.get("summary") or "").strip()
        cv_text = ""

        # Current routing verdict from the live classifier — clarifies
        # whether the Run 26 changes are even on this CV's path.
        try:
            verdict = pe.detect_replica_compatibility(src_pdf)
            routes_to = "replica" if verdict.get("compatible") else "rebuild"
            print(f"  Current routing verdict: {routes_to}  ({verdict.get('reason')!r})")
            if routes_to == "rebuild":
                print(f"  NOTE: this CV routes to rebuild in production — the Run 26")
                print(f"  pdf_editor / diff-tailor changes don't run on its live output.")
        except Exception as exc:
            print(f"  ⚠️  routing classifier crashed: {exc!r}")

        if lf_path is None:
            diff = _synthetic_noop_diff(outline)
            print(f"  Synthetic no-op diff (no Langfuse) — layout-path exercise only.")
        elif not Path(lf_path).exists():
            print(f"  ⚠️  langfuse file not found: {lf_path}")
            continue
        else:
            diffs = _load_diff_outputs(lf_path)
            if not diffs:
                print("  ⚠️  no cv_diff_tailor entries in langfuse — skipping")
                continue
            diff = diffs[-1]  # last attempt is the one shipped
            print(f"  Loaded {len(diffs)} cv_diff_tailor obs; using last (final).")

        # ── Layer 3 validators on the recorded LLM summary ───────────────
        new_summary = (diff.get("summary") or "").strip()
        vrep = _validators_report(orig_summary, new_summary, outline, cv_text)
        print("\n  --- summary validators (would NEW guards have fired?) ---")
        print(f"    orig title clause: {vrep.get('extracted_orig_title')!r}")
        print(f"    new  title clause: {vrep.get('extracted_new_title')!r}")
        print(f"    orig concrete terms: {vrep.get('extracted_orig_terms')}")
        for k in ("concrete_terms_missing", "title_escalation",
                  "duplicate_phrase", "sectors_fabricated"):
            v = vrep.get(k)
            mark = "🚩 FIRES" if v else "✓ pass"
            print(f"    {mark}  {k}: {v}")

        # ── Physical layout probe with new pdf_editor ────────────────────
        work_dir = OUT_DIR / Path(src_pdf).stem.replace(" ", "_")
        lr = _run_layout_probe(label, src_pdf, diff, work_dir)
        print("\n  --- layout probe (apply_edits with new code) ---")
        if "crashed" in lr:
            print(f"    💥 CRASHED: {lr['crashed']}")
            print("    stdout tail:", lr.get("stdout", "")[-400:])
        else:
            print(f"    net_new_horizontal_lines: {lr['net_new_horizontal_lines']}"
                  + ("  🚨" if lr['net_new_horizontal_lines'] > 0 else "  ✓"))
            print(f"    applied_rewrites: {lr['applied_rewrites']}")
            print(f"    trimmed:          {lr['trimmed_count']}")
            print(f"    skipped:          {lr['skipped_count']}")
            print(f"    content_loss_bullets: {len(lr['content_loss_bullets'])}"
                  + ("  🚨" if lr['content_loss_bullets'] else "  ✓"))
            for s in lr['content_loss_bullets'][:6]:
                print(s)
            print(f"    output: {lr['out_pdf']}")

        overall.append({
            "label":     label,
            "validators": vrep,
            "layout":    lr,
        })

    print("\n\n" + "=" * 78)
    print(" SUMMARY")
    print("=" * 78)
    for o in overall:
        lbl = o["label"]
        v   = o["validators"]
        lr  = o["layout"]
        fired = [k for k in ("concrete_terms_missing", "title_escalation",
                              "duplicate_phrase", "sectors_fabricated")
                 if v.get(k)]
        phys_ok = (
            not lr.get("crashed")
            and lr.get("net_new_horizontal_lines", 0) == 0
            and not lr.get("content_loss_bullets")
        )
        print(f"  • {lbl}")
        print(f"      validators that would fire (catches LLM defects): "
              f"{', '.join(fired) or 'none'}")
        print(f"      physical layout: "
              f"{'✓ clean' if phys_ok else '🚨 regression'}")

    side = OUT_DIR / "adapt_report.json"
    side.write_text(json.dumps(overall, indent=2, default=str), encoding="utf-8")
    print(f"\nSidecar: {side}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
