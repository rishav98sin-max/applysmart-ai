"""
agents.cv_docx_editor
=====================

Apply a tailor-produced diff to a DOCX file (May 2026 / DOCX path).

The diff shape (from `cv_diff_tailor.tailor_cv_diff`) is:

    {
        "summary": "new summary text",          # "" or absent → keep original
        "bullets": {
            "Role Header Match Key": [
                {"i": 0, "text": "new bullet text"},   # rewrite bullet 0
                {"i": 1, "text": None},                # keep bullet 1
                {"i": 2},                              # keep bullet 2 (text omitted)
                ...
            ],
            ...
        },
        "skills_order": [],   # always empty per current policy
    }

Anchors flow through the outline as `_anchor` (per role / per bullet) and
`_summary_anchors` (list — one summary may span multiple paragraphs).
We use these to relocate paragraphs in the on-disk DOCX without re-parsing.

Run preservation policy
-----------------------
A paragraph can contain multiple runs (each with its own bold/italic/font/
size/colour). When we rewrite a bullet's text we:

  1.  Capture the FIRST non-empty run's formatting (the "primary" style).
  2.  Replace all runs with a single run carrying the new text + primary
      formatting.

This loses mid-paragraph formatting variations (e.g. a bolded sub-phrase
inside a bullet) but preserves what matters for CV rendering: paragraph
style (List Bullet / List Paragraph), alignment, font family, base font
size, and colour. The trade-off matches the PDF in-place editor's
behaviour, which also loses sub-bullet style variation.

Summary handling
----------------
A summary often spans multiple paragraphs (e.g. one prose sentence per
line, or a header line followed by a body). We collapse the new summary
text into the FIRST summary paragraph and CLEAR the rest. This avoids
length-mismatch artefacts (e.g. shipping a 3-paragraph summary block
when the new summary is one sentence).

Bullet reorder support
----------------------
v1 keeps bullets in their original document order — same as the PDF
in-place editor. The reorder field is read but only the indices' order
of TEXT REWRITES is honoured (paragraph order in the DOCX stays put).
This matches the current product behaviour where reorder hasn't
demonstrated quality gains over targeted rewrites.

Never raises. On any failure returns `(False, reason_string)` so the
caller can fall back to the rebuild path.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple


# ─────────────────────────────────────────────────────────────
# Run / paragraph mutation helpers
# ─────────────────────────────────────────────────────────────

def _first_non_empty_run_index(runs: List[Any]) -> int:
    """
    Return the index into `runs` of the first run whose text is non-empty.
    Falls back to 0 if every run is blank. Caller MUST pass a single
    snapshot of `list(paragraph.runs)` and reuse the returned index against
    that same list — `paragraph.runs` returns fresh proxy objects on every
    access, so identity checks across two calls do NOT match (the bug that
    made every rewrite silently blank out the paragraph in the May 13
    smoke test).
    """
    for i, r in enumerate(runs):
        if (r.text or "").strip():
            return i
    return 0


def _replace_paragraph_text(paragraph: Any, new_text: str) -> None:
    """
    Replace the paragraph's visible text while preserving paragraph-level
    formatting (style, alignment, indent) and the FIRST run's font/bold/
    italic/colour properties.

    Implementation notes:
      - We don't simply set `paragraph.text = "..."` because that
        path destroys every run, including their font properties.
      - We don't delete runs naively because some runs carry properties
        (e.g. tab stops) that other runs reference.
      - Instead: rewrite the first non-empty run's text, then blank out
        every subsequent run's text. This leaves the run XML intact while
        showing only the new text.
      - We work with a SINGLE snapshot of `paragraph.runs` and index by
        position. `paragraph.runs` returns fresh proxy objects each call,
        so `proxy_a is proxy_b` is False even for the same underlying XML.
    """
    if paragraph is None:
        return
    runs = list(paragraph.runs)
    if not runs:
        # Empty paragraph — add a single run with the new text.
        paragraph.add_run(new_text)
        return
    primary_idx = _first_non_empty_run_index(runs)
    runs[primary_idx].text = new_text
    for i, r in enumerate(runs):
        if i == primary_idx:
            continue
        r.text = ""


def _blank_paragraph(paragraph: Any) -> None:
    """Set every run in the paragraph to empty text (visible result: blank line)."""
    if paragraph is None:
        return
    for r in paragraph.runs:
        r.text = ""


def _index_paragraphs(doc: Any) -> Dict[int, Any]:
    """
    Build {global_paragraph_index → paragraph_object} matching the same
    walk order used by `cv_docx_parser._iter_body_paragraphs`. Used to
    look up paragraphs by their outline `_anchor` value.
    """
    out: Dict[int, Any] = {}
    idx = 0
    for p in doc.paragraphs:
        out[idx] = p
        idx += 1
    for tbl in doc.tables:
        for row in tbl.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    out[idx] = p
                    idx += 1
    return out


# ─────────────────────────────────────────────────────────────
# Diff application
# ─────────────────────────────────────────────────────────────

def apply_diff_to_docx(
    docx_path:   str,
    diff:        Dict[str, Any],
    outline:     Dict[str, Any],
    output_path: str,
) -> Tuple[bool, str]:
    """
    Open `docx_path`, apply `diff` (matched against `outline`'s anchors),
    save to `output_path`. Returns `(True, "")` on success or
    `(False, reason)` on any failure (file not found, no python-docx,
    write error, etc.).

    The function is intentionally fault-tolerant:
      - Missing role anchors (e.g. tailor renamed the role and our match
        already failed upstream) are skipped, not fatal.
      - Out-of-range bullet indices are skipped.
      - A `None` / missing `text` on a bullet means "keep original" and
        leaves the paragraph untouched.

    Output is byte-true to the input format (DOCX), so the caller can
    then route through `cv_docx_to_pdf.render_pdf` to produce the final
    PDF.
    """
    if not docx_path or not os.path.exists(docx_path):
        return False, f"DOCX file not found: {docx_path!r}"

    try:
        import docx as _docx_lib
    except Exception as e:
        return False, f"python-docx unavailable: {type(e).__name__}: {e}"

    try:
        doc = _docx_lib.Document(docx_path)
    except Exception as e:
        return False, f"Failed to open DOCX: {type(e).__name__}: {e}"

    paragraphs = _index_paragraphs(doc)
    if not paragraphs:
        return False, "DOCX has no paragraphs"

    summary_text = (diff.get("summary") or "").strip()
    summary_anchors: List[int] = outline.get("_summary_anchors") or []

    edits_applied: int = 0
    edits_skipped: int = 0

    # ── 1. Summary ──────────────────────────────────────────
    # If the diff carries a non-empty summary AND we know which paragraphs
    # the original summary occupies, rewrite the first one with the new
    # text and clear any continuation paragraphs.
    if summary_text and summary_anchors:
        first_idx = summary_anchors[0]
        first_para = paragraphs.get(first_idx)
        if first_para is not None:
            _replace_paragraph_text(first_para, summary_text)
            edits_applied += 1
        # Clear any continuation paragraphs (rare — most summaries are one
        # paragraph) so we don't ship stale prose alongside the new summary.
        for cont_idx in summary_anchors[1:]:
            cont_para = paragraphs.get(cont_idx)
            if cont_para is not None:
                _blank_paragraph(cont_para)

    # ── 2. Bullets ──────────────────────────────────────────
    # The diff's `bullets` dict is keyed by role header. We've already
    # done the fuzzy role-key matching in `_sanitise_diff` (cv_diff_tailor),
    # so the keys here align 1:1 with `outline["roles"][k]["header"]`.
    # Iterate the outline's roles and look up the matching diff entry.
    bullets_diff: Dict[str, Any] = diff.get("bullets") or {}
    if isinstance(bullets_diff, dict):
        # Build a lowercase header → diff-entries map for case-tolerant lookup.
        diff_by_header_l: Dict[str, List[Dict[str, Any]]] = {
            str(k).strip().lower(): v
            for k, v in bullets_diff.items()
            if isinstance(v, list)
        }
        for role in outline.get("roles", []):
            header = (role.get("header") or "").strip()
            header_l = header.lower()
            entries = diff_by_header_l.get(header_l)
            if not entries:
                continue
            role_bullets = role.get("bullets") or []
            n_bullets = len(role_bullets)
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                try:
                    i = int(entry.get("i"))
                except (TypeError, ValueError):
                    continue
                if i < 0 or i >= n_bullets:
                    edits_skipped += 1
                    continue
                new_text = entry.get("text")
                if not isinstance(new_text, str) or not new_text.strip():
                    # Caller marked this bullet "keep original" — no-op.
                    continue
                # Locate the actual DOCX paragraph for bullet i.
                bullet_obj = role_bullets[i]
                if not isinstance(bullet_obj, dict):
                    edits_skipped += 1
                    continue
                anchor = bullet_obj.get("_anchor")
                if not isinstance(anchor, int):
                    edits_skipped += 1
                    continue
                target_para = paragraphs.get(anchor)
                if target_para is None:
                    edits_skipped += 1
                    continue
                _replace_paragraph_text(target_para, new_text.strip())
                edits_applied += 1

    # ── 3. Save ─────────────────────────────────────────────
    try:
        # Ensure output directory exists.
        out_dir = os.path.dirname(output_path)
        if out_dir and not os.path.isdir(out_dir):
            os.makedirs(out_dir, exist_ok=True)
        doc.save(output_path)
    except Exception as e:
        return False, f"Failed to save DOCX: {type(e).__name__}: {e}"

    print(
        f"   ✏️  cv_docx_editor: applied={edits_applied} skipped={edits_skipped} "
        f"→ {os.path.basename(output_path)}"
    )
    return True, ""
