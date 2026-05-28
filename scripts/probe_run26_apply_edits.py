"""
Probe — Run 26 (Mohammed Humaam) — diagnose why 5 strategist-planned bullet
rewrites in the EY GDS Advanced Data Analyst role silently dropped from the
output PDF.

Read-only against the input artifacts; writes a working PDF copy + edited
output into _r26render/. No LLM calls. Does NOT modify pdf_editor.py — uses
monkey-patching + APPLYSMART_DEBUG_BULLETS=1 + stdout capture to surface
revert reasons.

Usage (from repo root):
    venv\\Scripts\\python.exe scripts\\probe_run26_apply_edits.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import fitz  # type: ignore  # noqa: E402
import agents.pdf_editor as pe  # noqa: E402

RUN_DIR  = ROOT / "CVs" / "Run26"
RESUME   = RUN_DIR / "Resume.pdf"
LANGFUSE = RUN_DIR / "Langfuse.jsonl"
OUT_DIR  = ROOT / "_r26render"
OUT_DIR.mkdir(parents=True, exist_ok=True)
WORK_PDF = OUT_DIR / "Resume_work.pdf"
OUT_PDF  = OUT_DIR / "probe_output.pdf"

TARGET_HEADER_PREFIX = "Ernst and Young Global Delivery Services"


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def _load_second_diff() -> dict:
    diffs: list = []
    with LANGFUSE.open("r", encoding="utf-8") as f:
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
    assert len(diffs) >= 2, f"expected >=2 cv_diff_tailor, got {len(diffs)}"
    return diffs[1]


def _find_target_role(sections):
    for sec in sections:
        if sec.get("type") not in ("experience", "projects"):
            continue
        for role in pe._role_blocks(sec):
            if (role.get("header_text") or "").startswith(TARGET_HEADER_PREFIX):
                return sec, role
    return None, None


def _find_diff_role_key(diff):
    for k in (diff.get("bullets") or {}).keys():
        if k.startswith(TARGET_HEADER_PREFIX):
            return k
    return None


def _slot_rect_extended(b_lines, all_page_lines, page):
    """Replicate apply_edits' rect-extension logic so the predicted fit-width
    matches what apply_edits actually computes."""
    body_rect, has_inline_glyph, body_true = pe._bullet_body_rect(b_lines)
    if body_rect is None:
        return None, has_inline_glyph
    own_y_min = body_rect.y0 - 0.5
    own_y_max = body_rect.y1 + 0.5
    other_lines = [
        ln for ln in all_page_lines
        if not (own_y_min <= ln["bbox"][1] <= own_y_max)
    ]
    body_rect.x1 = max(
        body_rect.x1,
        pe._measured_right_margin(other_lines, page.rect.width),
    )
    text_y0, text_y1 = body_rect.y0, body_rect.y1
    prev_y1 = max(
        (ln["bbox"][3] for ln in other_lines if ln["bbox"][3] <= text_y0 + 0.5),
        default=text_y0 - 6.0,
    )
    next_y0 = pe._next_y0_below(body_rect, other_lines, page.rect.height)
    gap_above = max(0.0, text_y0 - prev_y1)
    body_rect.y0 = text_y0 - min(gap_above / 2.0, 6.0)
    gap_below = max(0.0, next_y0 - text_y1)
    body_rect.y1 = text_y1 + min(gap_below / 2.0, 6.0)
    return body_rect, has_inline_glyph


def main() -> int:
    # Force UTF-8 on stdout/stderr so Windows cp1252 doesn't choke on
    # arrows / em-dashes / bullet glyphs in the source CV text.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print("=" * 78)
    print(" Mohammed Run 26 probe — apply_edits diagnostic")
    print("=" * 78)
    print(f"Resume:   {RESUME}")
    print(f"Langfuse: {LANGFUSE}")
    print(f"Outdir:   {OUT_DIR}")

    # ── 1. Load diff #2 ────────────────────────────────────────────────────
    diff = _load_second_diff()
    diff_role_key = _find_diff_role_key(diff)
    assert diff_role_key, "EY GDS Advanced role key not found in diff #2"
    diff_entries = diff["bullets"][diff_role_key]
    planned = [e for e in diff_entries if isinstance(e, dict) and e.get("text")]
    planned_idx = sorted(e["i"] for e in planned)
    print(f"\ndiff#2 role key (raw):\n  {diff_role_key!r}")
    print(f"diff#2 planned rewrites (i): {planned_idx}")
    print(f"diff#2 summary len:          {len(diff.get('summary') or '')}")

    # ── 2. Compact outline (build_outline_cached) — for sanity ─────────────
    outline_compact = pe.build_outline_cached(str(RESUME))
    print(f"\nbuild_outline_cached: summary_len={len(outline_compact.get('summary') or '')} "
          f"roles={len(outline_compact.get('roles') or [])} "
          f"skills={len(outline_compact.get('skills') or [])}")
    for r in (outline_compact.get("roles") or []):
        print(f"  • {r['header'][:80]!r}  ({len(r.get('bullets') or [])} bullets)")

    # ── 3. Slot-level extraction (mirrors apply_edits) ─────────────────────
    sections = pe.extract_structure(str(RESUME))
    target_sec, target_role = _find_target_role(sections)
    assert target_role, "EY GDS Advanced role not found in extracted sections"
    bullets = target_role.get("bullet_groups") or []
    print(f"\nTarget role (extracted): {target_role['header_text'][:100]!r}")
    print(f"  bullet count: {len(bullets)}")
    for i, b in enumerate(bullets):
        marker = " ←" if i in {0, 1, 3, 7, 9} else ""
        print(f"    i={i:>2}  ({len(b.get('text') or ''):>3}c)  {(b.get('text') or '')[:90]}{marker}")

    # Outline-index sanity: does build_outline_cached agree with _role_blocks?
    print("\nOutline-vs-role_blocks index alignment check:")
    target_compact = next(
        (r for r in (outline_compact.get("roles") or [])
         if r["header"].startswith(TARGET_HEADER_PREFIX)),
        None,
    )
    if target_compact is None:
        print("  ⚠️  outline_compact has no matching role — index map cannot be cross-checked")
    else:
        cb = target_compact.get("bullets") or []
        print(f"  outline_compact bullets={len(cb)}   role_blocks bullets={len(bullets)}")
        mismatches = []
        for i in range(min(len(cb), len(bullets))):
            a = _norm(cb[i].get("text") or "")
            b = _norm(bullets[i].get("text") or "")
            if a != b:
                mismatches.append((i, a[:60], b[:60]))
        if mismatches:
            print(f"  ⚠️  {len(mismatches)} mismatching indices:")
            for i, a, b in mismatches[:6]:
                print(f"     i={i}:")
                print(f"        outline:    {a}")
                print(f"        role_block: {b}")
        else:
            print("  ✅ indices align 1:1 between outline and _role_blocks")

    # ── 4. Per-bullet slot analysis (predict before edits) ─────────────────
    print("\n" + "=" * 78)
    print(" PER-BULLET SLOT ANALYSIS (prediction — no PDF edits yet)")
    print("=" * 78)

    doc = fitz.open(str(RESUME))
    page_lines_cache: dict = {}

    def _page_lines(idx):
        if idx not in page_lines_cache:
            page_lines_cache[idx] = pe._all_lines_on_page(sections, idx)
        return page_lines_cache[idx]

    section_glyph = pe._detect_section_bullet_glyph(target_sec)
    predict: dict = {}

    for e in planned:
        i = e["i"]
        rec = {
            "i": i,
            "rewrite_text": pe._normalize_for_ats(e["text"]),
            "orig_text": "",
            "orig_len": 0,
            "rewrite_len": 0,
            "delta_pct": None,
            "font": "",
            "font_size": None,
            "is_symbolic_font": False,
            "rect_w": None,
            "rect_h": None,
            "measured_gap": None,
            "has_inline_glyph": None,
            "predicted_fit": None,
            "predicted_reason": "",
        }
        if i >= len(bullets):
            rec["predicted_reason"] = f"INDEX_OOB (only {len(bullets)} bullets in role)"
            predict[i] = rec
            continue

        bullet = bullets[i]
        b_lines = bullet.get("lines") or []
        orig_text = bullet.get("text") or ""
        rec["orig_text"] = orig_text
        rec["orig_len"] = len(orig_text)
        rec["rewrite_len"] = len(rec["rewrite_text"])
        if rec["orig_len"]:
            rec["delta_pct"] = (rec["rewrite_len"] - rec["orig_len"]) * 100.0 / rec["orig_len"]

        if not b_lines:
            rec["predicted_reason"] = "NO_LINES_IN_BULLET"
            predict[i] = rec
            continue

        page_idx = b_lines[0]["page"]
        page = doc[page_idx]
        all_pl = _page_lines(page_idx)
        body_rect, has_inline_glyph = _slot_rect_extended(b_lines, all_pl, page)
        ref = pe._pick_body_span(b_lines)
        font_name = (ref or {}).get("font") or ""
        fit_size = float((ref or {}).get("size", 10) or 10) if ref else 10.0
        measured_gap = pe._measure_line_gap(b_lines)

        if has_inline_glyph:
            insert_text = f"{section_glyph}   {rec['rewrite_text']}"
        else:
            insert_text = rec["rewrite_text"]

        fits = pe._estimate_text_fits(
            insert_text,
            body_rect.width if body_rect else 0.0,
            body_rect.height if body_rect else 0.0,
            fit_size, measured_gap,
            font_name=pe._pick_builtin(ref) if ref else None,
        ) if body_rect else False
        symbolic = pe._is_symbolic_font_name(font_name)

        rec.update({
            "font": font_name,
            "font_size": fit_size,
            "is_symbolic_font": symbolic,
            "rect_w": body_rect.width if body_rect else None,
            "rect_h": body_rect.height if body_rect else None,
            "measured_gap": measured_gap,
            "has_inline_glyph": has_inline_glyph,
            "predicted_fit": fits,
        })
        if symbolic:
            rec["predicted_reason"] = "FONT_SYMBOLIC_PICK_FAILS_TEXT_CARRY"
        elif not fits:
            rec["predicted_reason"] = "SLOT_TOO_TIGHT (estimate)"
        else:
            rec["predicted_reason"] = "OK (predicted to apply)"
        predict[i] = rec

        print(f"\n• bullet i={i}")
        print(f"    orig    ({rec['orig_len']}c): {orig_text[:120]}")
        print(f"    rewrite ({rec['rewrite_len']}c, Δ={rec['delta_pct']:+.1f}%): "
              f"{rec['rewrite_text'][:120]}")
        print(f"    font={font_name!r}  size={fit_size:.1f}  symbolic={symbolic}")
        print(f"    rect_w={rec['rect_w']:.1f}  rect_h={rec['rect_h']:.1f}  "
              f"measured_gap={measured_gap:.3f}  has_inline_glyph={has_inline_glyph}")
        print(f"    section_glyph={section_glyph!r}  insert_len={len(insert_text)}")
        print(f"    predicted_fit={fits}  → {rec['predicted_reason']}")

    doc.close()

    # ── 5. Run apply_edits with verbose logs, capture stdout ───────────────
    print("\n" + "=" * 78)
    print(" RUNNING apply_edits (APPLYSMART_DEBUG_BULLETS=1)")
    print("=" * 78)
    os.environ["APPLYSMART_DEBUG_BULLETS"] = "1"
    shutil.copyfile(RESUME, WORK_PDF)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        report = pe.apply_edits(str(WORK_PDF), diff, str(OUT_PDF))
    captured = buf.getvalue()
    print(captured)

    print("---REPORT (json) ---")
    print(json.dumps(report, indent=2, default=str)[:4000])
    print()

    # ── 6. Diff input vs output text per bullet to determine "applied" ─────
    new_sections = pe.extract_structure(str(OUT_PDF))
    _, new_role = _find_target_role(new_sections)
    new_bullets = (new_role or {}).get("bullet_groups") or []

    # Parse skipped reasons by index from report + captured stdout.
    reasons: dict = {}
    pat = re.compile(
        r"bullet i=(\d+).*?("
        r"too long for (?:its )?slot|did not fit|duplicates an earlier rewrite|"
        r"spans pages|spans \d+ pages)",
        re.IGNORECASE,
    )
    for s in (report.get("skipped") or []):
        m = pat.search(s)
        if m:
            reasons.setdefault(int(m.group(1)), m.group(2))
    for line in captured.splitlines():
        m = pat.search(line)
        if m:
            reasons.setdefault(int(m.group(1)), m.group(2))

    # ── 7. Final table ─────────────────────────────────────────────────────
    print("=" * 78)
    print(" FINAL TABLE — 5 planned bullets × {planned, applied, reject_reason}")
    print("=" * 78)
    print(f"{'i':>2} | {'Δ%':>6} | {'applied?':<9} | reason")
    print("-" * 78)
    rows = []
    for e in sorted(planned, key=lambda x: x["i"]):
        i = e["i"]
        p = predict.get(i, {})
        orig = p.get("orig_text", "")
        rewr = p.get("rewrite_text", "")
        new_text = (new_bullets[i].get("text") if i < len(new_bullets) else "") or ""
        no, nr, nn = _norm(orig), _norm(rewr), _norm(new_text)
        if nn == nr:
            applied = "YES"
        elif nn == no:
            applied = "NO"
        elif not nn:
            applied = "BLANK"
        else:
            applied = "PARTIAL"
        reason = reasons.get(i)
        if not reason:
            reason = "applied (no revert)" if applied == "YES" else f"unknown — predicted: {p.get('predicted_reason','?')}"
        delta = p.get("delta_pct")
        delta_str = f"{delta:+5.1f}" if isinstance(delta, (int, float)) else "    ?"
        print(f"{i:>2} | {delta_str:>6} | {applied:<9} | {reason}")
        rows.append({
            "i": i, "applied": applied, "reason": reason,
            "orig": orig, "rewrite": rewr, "now_pdf": new_text,
            "delta_pct": delta,
            "predicted_fit": p.get("predicted_fit"),
            "predicted_reason": p.get("predicted_reason"),
            "font": p.get("font"),
            "is_symbolic_font": p.get("is_symbolic_font"),
            "rect_w": p.get("rect_w"),
            "rect_h": p.get("rect_h"),
        })

    print("\nPer-bullet detail (orig / rewrite / now-in-pdf):")
    for r in rows:
        print(f"\n  i={r['i']}  applied={r['applied']}")
        print(f"    orig    : {r['orig'][:110]}")
        print(f"    rewrite : {r['rewrite'][:110]}")
        print(f"    now-pdf : {r['now_pdf'][:110]}")

    # Drop a JSON sidecar for follow-up scripting.
    side = OUT_DIR / "probe_report.json"
    side.write_text(json.dumps({
        "diff_role_key": diff_role_key,
        "extracted_role_header": target_role.get("header_text"),
        "extracted_bullet_count": len(bullets),
        "rows": rows,
        "report": report,
    }, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote sidecar: {side}")
    print(f"Wrote output PDF: {OUT_PDF}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
