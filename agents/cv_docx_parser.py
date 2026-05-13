"""
agents.cv_docx_parser
=====================

DOCX → outline parser (May 2026 / DOCX path).

Produces the same outline shape as `pdf_editor.build_outline` so the
downstream tailor / strategist / reviewer pipeline doesn't know or care
whether the CV came from a PDF or a Word document:

    {
        "summary": "...",
        "roles":   [
            {
                "header":  "Company, Title  Dates",
                "section": "experience" | "projects",
                "bullets": [{"text": "...", "length": int}, ...],
            },
            ...
        ],
        "skills":  ["Python", "React", ...],
    }

Each role and bullet ALSO carries an internal `_anchor` field with the
paragraph index that produced it. `cv_docx_editor.py` uses these anchors
to rewrite text in the original document while preserving all formatting
(font, bold, italic, colour, indent). cv_diff_tailor / tailor_strategist
ignore unknown fields, so the anchors flow through without side-effects.

Why DOCX parsing is structural (not coordinate-based)
------------------------------------------------------
DOCX is a tree of Paragraph → Run elements with explicit style names.
We classify each paragraph using three signals, in order of confidence:

1.  Numbering / list-style → BULLET
2.  Section keyword + heading style or short bold paragraph → SECTION_HEADER
3.  Anything else inside an experience/projects section → ROLE_HEADER

This is far more robust than the PDF path's x-coordinate / font-size
heuristics. The trade-off is that DOCX has no fixed page layout, so we
can't reason about page-breaks — but the tailor doesn't need to, because
text edits in a DOCX never cause overflow in the first place.

Never raises. On any parse failure returns an empty outline; downstream
treats that as "no edits possible" and falls back to the rebuild path.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple


# ─────────────────────────────────────────────────────────────
# Section / heading detection
# ─────────────────────────────────────────────────────────────

# Reuse the same section regex the validator and PDF outline parser use,
# so a CV that converts cleanly between formats gets the same section
# boundaries regardless of source. Kept locally rather than imported to
# avoid a circular dependency.
_SECTION_RX = re.compile(
    r"^\s*(summary|profile|objective|about(\s+me)?|"
    r"experience|professional\s+experience|work\s+experience|employment\s*history|"
    r"education|academic(\s+achievements?|\s+background)?|"
    r"skills?|technical\s+skills?|core\s+competenc(?:y|ies)|"
    r"projects?|featured\s+projects?|portfolio|"
    r"certifications?|awards?|publications?|languages?)\s*:?\s*$",
    re.IGNORECASE,
)

# Map matched section keyword → canonical section type used by the
# downstream pipeline. `pdf_editor.build_outline` emits these same labels.
_SECTION_CANONICAL: Dict[str, str] = {
    "summary": "summary", "profile": "summary",
    "objective": "summary", "about": "summary", "about me": "summary",
    "experience": "experience",
    "professional experience": "experience",
    "work experience": "experience",
    "employment history": "experience",
    "projects": "projects", "featured projects": "projects",
    "portfolio": "projects",
    "skills": "skills", "technical skills": "skills",
    "core competency": "skills", "core competencies": "skills",
    "education": "education",
    "academic achievements": "education",
    "academic background": "education",
    "certifications": "certifications", "certification": "certifications",
    "awards": "awards", "award": "awards",
    "publications": "publications", "publication": "publications",
    "languages": "languages", "language": "languages",
}

# A paragraph is a "list / bullet" paragraph when any of these hold.
# Conservative — false negatives only mean we treat the bullet as a role
# header (the editor will then refuse to rewrite it, which is safe).
_BULLET_STYLE_HINTS = ("list bullet", "list paragraph", "bullet", "list number")

# Date pattern in role headers ("Jan 2024 – Present", "2021-2023").
# Used to distinguish role headers from prose paragraphs when the
# CV doesn't use heading styles.
_DATE_HINT_RX = re.compile(
    r"(?:\d{4}|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
    r"\s*\.?\s*\d{2,4})",
    re.IGNORECASE,
)

# Bullet glyphs that may appear inside paragraph text (some templates
# embed the bullet character rather than using list numbering).
_BULLET_GLYPH_RX = re.compile(r"^\s*[\u2022\u00b7\u25aa\u25cb\u25a0\u2043\u2219\u25b8\u25b6\-\*]\s+")


# ─────────────────────────────────────────────────────────────
# Paragraph classification helpers
# ─────────────────────────────────────────────────────────────

def _is_bullet_paragraph(paragraph: Any) -> bool:
    """True if this paragraph is rendered as a bulleted/numbered list item."""
    # 1. Style name hints (most reliable for Word-generated DOCX).
    try:
        style_name = (paragraph.style.name or "").lower() if paragraph.style else ""
    except Exception:
        style_name = ""
    if any(hint in style_name for hint in _BULLET_STYLE_HINTS):
        return True

    # 2. Explicit numbering definition in the paragraph properties.
    try:
        ppr = paragraph._p.pPr
        if ppr is not None and ppr.numPr is not None:
            return True
    except Exception:
        pass

    # 3. Leading bullet glyph in the text itself (templates that "fake" bullets).
    text = (paragraph.text or "").strip()
    if text and _BULLET_GLYPH_RX.match(text):
        return True

    return False


def _strip_leading_bullet_glyph(text: str) -> str:
    """If text starts with a bullet glyph, drop it (and the following space)."""
    return _BULLET_GLYPH_RX.sub("", text, count=1).strip()


def _is_section_header(text: str) -> Optional[str]:
    """
    Return the canonical section type ("summary"/"experience"/...) if `text`
    looks like a section header line. Otherwise None.

    The match is intentionally strict: the WHOLE paragraph must be the
    section label (optionally with trailing punctuation). Otherwise a
    sentence like "I have strong skills in Python" would falsely trigger
    a "skills" section break mid-prose.
    """
    if not text:
        return None
    m = _SECTION_RX.match(text.strip())
    if not m:
        return None
    keyword = m.group(1).lower().strip()
    return _SECTION_CANONICAL.get(keyword)


def _paragraph_is_bold_heading(paragraph: Any) -> bool:
    """
    Heuristic: paragraph "looks like" a role header even without using a
    Heading style. True when every non-empty run is bold AND the text is
    relatively short (< 200 chars) AND no leading bullet glyph.

    Some CVs (especially conversions from PDFs) emit role headers as
    bold paragraphs styled "Normal". This catches them.
    """
    text = (paragraph.text or "").strip()
    if not text or len(text) > 200:
        return False
    runs = list(paragraph.runs) if hasattr(paragraph, "runs") else []
    if not runs:
        return False
    bold_chars = 0
    total_chars = 0
    for run in runs:
        rt = (run.text or "")
        if not rt.strip():
            continue
        total_chars += len(rt.strip())
        if run.bold:
            bold_chars += len(rt.strip())
    if total_chars == 0:
        return False
    return (bold_chars / total_chars) >= 0.8


# ─────────────────────────────────────────────────────────────
# Paragraph walk — flatten body + tables in document order
# ─────────────────────────────────────────────────────────────

def _iter_body_paragraphs(doc: Any) -> List[Tuple[int, Any]]:
    """
    Return `[(global_index, paragraph), ...]` in document order. Includes
    paragraphs inside top-level table cells (but NOT nested tables — those
    are extremely rare in CVs and the rebuild path handles them).

    The global_index is a stable identifier the editor uses to locate the
    same paragraph on re-open.
    """
    out: List[Tuple[int, Any]] = []
    idx = 0
    # python-docx exposes doc.element.body.iter() which gives us paragraphs
    # in true document order (paragraphs nested in tables included). But
    # mixing tables/paragraphs is fragile; instead we walk
    # doc.paragraphs then doc.tables — this matches the order MOST simple
    # CVs use (body text first, sidebar tables second).
    for p in doc.paragraphs:
        out.append((idx, p))
        idx += 1
    for tbl in doc.tables:
        for row in tbl.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    out.append((idx, p))
                    idx += 1
    return out


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────

def build_outline_from_docx(docx_path: str) -> Dict[str, Any]:
    """
    Parse a DOCX file and return an outline matching the shape of
    `agents.pdf_editor.build_outline`. Never raises — returns an empty
    outline on any failure so the caller can fall back to the rebuild path.

    `roles[i]["_anchor"]` and `roles[i]["bullets"][j]["_anchor"]` contain
    the paragraph indices from `_iter_body_paragraphs`. `cv_docx_editor`
    uses these to relocate paragraphs in the on-disk DOCX without
    re-parsing the outline.
    """
    # `_summary_anchors` and `_skills_anchors` are internal fields the
    # editor uses to relocate the summary / skills paragraphs without
    # re-parsing. cv_diff_tailor and tailor_strategist only read
    # `summary` / `roles` / `skills`, so unknown fields pass through harmlessly.
    out: Dict[str, Any] = {
        "summary": "", "roles": [], "skills": [],
        "_summary_anchors": [], "_skills_anchors": [],
    }

    if not docx_path or not os.path.exists(docx_path):
        return out

    try:
        import docx as _docx_lib
        doc = _docx_lib.Document(docx_path)
    except Exception:
        return out

    paragraphs = _iter_body_paragraphs(doc)
    if not paragraphs:
        return out

    # Phase 1: classify every paragraph.
    #
    # Each entry: {
    #   "idx": int (anchor),
    #   "text": str,
    #   "kind": "section" | "bullet" | "header_like" | "prose",
    #   "section": Optional[str]  (only set for kind="section"),
    # }
    classified: List[Dict[str, Any]] = []
    for idx, p in paragraphs:
        text = (p.text or "").strip()
        if not text:
            continue
        section = _is_section_header(text)
        if section:
            classified.append({
                "idx": idx, "text": text,
                "kind": "section", "section": section,
            })
            continue
        if _is_bullet_paragraph(p):
            classified.append({
                "idx": idx,
                "text": _strip_leading_bullet_glyph(text),
                "kind": "bullet", "section": None,
            })
            continue
        if _paragraph_is_bold_heading(p):
            classified.append({
                "idx": idx, "text": text,
                "kind": "header_like", "section": None,
            })
            continue
        classified.append({
            "idx": idx, "text": text,
            "kind": "prose", "section": None,
        })

    # Phase 2: walk classified stream, grouping by section.
    #
    # State machine:
    #   - On "section": flush any in-progress role; switch current section.
    #   - In "summary"/"profile" section: accumulate prose lines into summary.
    #   - In "experience"/"projects" section: header_like / prose-with-date
    #     starts a new role; subsequent bullets attach to that role.
    #   - In "skills" section: accumulate prose into a skills text blob,
    #     then split on common delimiters.
    current_section: str = "preamble"   # before any section header
    current_role: Optional[Dict[str, Any]] = None
    summary_chunks: List[str] = []
    skills_chunks: List[str] = []

    def _flush_role() -> None:
        nonlocal current_role
        if current_role is not None and current_role.get("bullets"):
            out["roles"].append(current_role)
        current_role = None

    # Pre-section fallback: the first prose paragraph(s) before any explicit
    # section header are often a Summary block (templates that skip the
    # "Summary" label). Capture them into a pre-section buffer; if the CV
    # never has an explicit Summary header, we'll use them.
    pre_section_prose: List[Tuple[int, str]] = []   # (anchor, text)

    for entry in classified:
        kind = entry["kind"]
        text = entry["text"]
        idx = entry["idx"]

        if kind == "section":
            _flush_role()
            current_section = entry["section"] or "other"
            continue

        if current_section == "preamble":
            # Skip name/email-style top-of-CV header lines (short, often
            # contain "@" or phone numbers). Save short prose lines as
            # possible summary candidates.
            if len(text) > 40 and "@" not in text and not re.search(r"\+?\d[\d\s().-]{6,}", text):
                pre_section_prose.append((idx, text))
            continue

        if current_section == "summary":
            if kind in ("prose", "header_like", "bullet"):
                summary_chunks.append(text)
                out["_summary_anchors"].append(idx)
            continue

        if current_section in ("experience", "projects"):
            if kind == "header_like":
                _flush_role()
                current_role = {
                    "header":  text,
                    "section": current_section,
                    "bullets": [],
                    "_anchor": idx,
                }
            elif kind == "prose":
                # A prose paragraph in an experience section may be a
                # role header that wasn't styled bold (especially after
                # PDF→DOCX conversion). Use date-presence as the tell.
                if _DATE_HINT_RX.search(text):
                    _flush_role()
                    current_role = {
                        "header":  text,
                        "section": current_section,
                        "bullets": [],
                        "_anchor": idx,
                    }
                # Otherwise: orphan prose. Attach to current role's
                # header as a sub-line (rare; templates with location/
                # description lines between header and bullets). We do
                # NOT treat it as a bullet because it'd confuse the
                # tailor's bullet count.
                elif current_role is not None:
                    current_role["header"] = (
                        current_role["header"].rstrip() + " " + text
                    ).strip()
            elif kind == "bullet":
                if current_role is None:
                    # Bullet without a header — synthesise a placeholder.
                    # This shouldn't happen on well-formed CVs but we
                    # prefer "salvage what we can" to "drop everything".
                    current_role = {
                        "header":  "(role)",
                        "section": current_section,
                        "bullets": [],
                        "_anchor": idx,
                    }
                current_role["bullets"].append({
                    "text":    text,
                    "length":  len(text),
                    "_anchor": idx,
                })
            continue

        if current_section == "skills":
            if kind in ("prose", "bullet", "header_like"):
                skills_chunks.append(text)
                out["_skills_anchors"].append(idx)
            continue

        # Other sections (education / certifications / etc.) are recorded
        # but not surfaced — the tailor doesn't edit them in v1.

    _flush_role()

    # Summary fallback: if we never hit an explicit Summary section but
    # we did capture some pre-section prose, use that.
    if not summary_chunks and pre_section_prose:
        summary_chunks = [text for _idx, text in pre_section_prose]
        out["_summary_anchors"] = [idx for idx, _text in pre_section_prose]

    out["summary"] = " ".join(s.strip() for s in summary_chunks).strip()

    # Skills: flatten and split on common delimiters. If the skills text
    # looks "categorised" (contains "Foo: bar, baz" pattern), keep it as
    # a single string so the tailor knows not to reorder — same policy as
    # pdf_editor.build_outline.
    skills_text = " ".join(s.strip() for s in skills_chunks).strip()
    if skills_text:
        if re.search(r"\b[A-Z][A-Za-z &/]{2,20}:\s", skills_text):
            out["skills"] = []   # categorised — reorder disabled
        else:
            items = [
                s.strip() for s in re.split(r"[,;|\u2022\u00b7]", skills_text)
                if s.strip()
            ]
            out["skills"] = items

    return out


def outline_anchors(outline: Dict[str, Any]) -> List[Tuple[str, int]]:
    """
    Convenience helper: flatten an outline into a list of
    `(kind, paragraph_index)` pairs. Used by `cv_docx_editor` to validate
    that the document hasn't shifted between parse and edit.

    Kinds: "role_header", "bullet". Summary anchors are intentionally
    excluded because the summary is treated as a contiguous block.
    """
    pairs: List[Tuple[str, int]] = []
    for role in outline.get("roles", []):
        anc = role.get("_anchor")
        if isinstance(anc, int):
            pairs.append(("role_header", anc))
        for b in role.get("bullets", []):
            banc = b.get("_anchor") if isinstance(b, dict) else None
            if isinstance(banc, int):
                pairs.append(("bullet", banc))
    return pairs
