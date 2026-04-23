"""
agents.cv_validator
===================

Pre-flight compatibility check for uploaded CVs. Run this BEFORE the
expensive agent pipeline so obviously-unsupported inputs (scanned PDFs,
password-protected files, empty documents, etc.) fail fast with a
human-readable explanation instead of producing garbage output.

Design
------
The checker is layered from "definitely broken" → "likely degraded":

    ERRORS   (block the run)
      - PDF unreadable / corrupt
      - Password-protected
      - No text layer (scanned / image-only)
      - Empty or near-empty text (< 200 chars)
      - Absurd file size (> 25 MB) / page count (> 12)

    WARNINGS (allow the run, surface to user)
      - Low ASCII ratio → probably non-English CV
      - No recognised section headers (Summary / Experience / Education / Skills)
      - No bullet-looking characters detected
      - Very short (< 400 chars) — might be a portfolio blurb, not a CV

    DETAILS  (for the run snapshot + UI diagnostics panel)
      - page count, char count, section hits, bullet count,
        ASCII ratio, dominant font family

The result is a `ValidationReport` dataclass safe to serialise to JSON
for the run snapshot.

Never raises.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

import fitz   # PyMuPDF


# ─────────────────────────────────────────────────────────────
# Tunables
# ─────────────────────────────────────────────────────────────

MIN_TEXT_CHARS        = 200
ADVISED_TEXT_CHARS    = 400
MAX_FILE_BYTES        = 7 * 1024 * 1024
MAX_PAGES             = 12
MIN_ASCII_RATIO       = 0.70
MIN_SECTION_HITS      = 2

# Multi-column layout heuristics.
# x_start positions are bucketed into 10 bins across the page width. A
# bucket counts as a "column peak" when it holds at least this fraction
# of all line starts AND is a local maximum against its neighbours.
COLUMN_PEAK_MIN_RATIO = 0.10
COLUMN_GAP_MIN_BINS   = 2  # peaks need at least this many empty-ish bins between them

# Designer-template tells (case-insensitive). These appear in sidebars of
# templated CVs (Canva / Novoresume / MS Publisher style) but virtually
# never in clean ATS CVs.
_DESIGNER_SIDEBAR_TOKENS = (
    r"\bDOB\b",
    r"\bdate of birth\b",
    r"\bmarital status\b",
    r"\bnationality\b",
    r"\b(?:male|female|gender)\b",
    r"\bSCHOLASTIC\s+RECORD\b",
)
_DESIGNER_SIDEBAR_RX = re.compile(
    "|".join(_DESIGNER_SIDEBAR_TOKENS),
    re.IGNORECASE,
)

# Standard section labels we know how to parse downstream. Used both for
# the compatibility check here and by the diff tailor / pdf editor.
_SECTION_RX = re.compile(
    r"\b("
    r"summary|profile|objective|about(\s+me)?|"
    r"experience|professional\s+experience|work\s+experience|employment|"
    r"education|academic(\s+achievements?|\s+background)?|"
    r"skills?|technical\s+skills?|core\s+competenc(?:y|ies)|"
    r"projects?|featured\s+projects?|portfolio|"
    r"certifications?|awards?|publications?|languages?"
    r")\b",
    re.IGNORECASE,
)

# Any of the common bullet glyphs — if none show up we can't do our bullet
# reorder edit in-place (we fall back to the rebuild path).
_BULLET_CHARS = "\u2022\u00b7\u25aa\u25cb\u25a0\u2043\u2219-*"


# ─────────────────────────────────────────────────────────────
# Result types
# ─────────────────────────────────────────────────────────────

@dataclass
class ValidationReport:
    """Structured outcome of `validate_cv()`. Safe to JSON-dump."""
    ok:         bool                = True
    score:      int                 = 100           # 0..100 compatibility estimate
    errors:     List[str]           = field(default_factory=list)
    warnings:   List[str]           = field(default_factory=list)
    details:    Dict[str, Any]      = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ─────────────────────────────────────────────────────────────
# Column layout detector
# ─────────────────────────────────────────────────────────────

def _detect_column_layout(doc: "fitz.Document") -> Dict[str, Any]:
    """
    Estimate whether the PDF uses a multi-column / designer-template
    layout. Counts the x-start position of every text line across all
    pages, bins them into 10 horizontal buckets, and looks for multiple
    local peaks separated by at least COLUMN_GAP_MIN_BINS empty-ish bins.

    Returns:
      {
        "columns":      int,   # 1 = single column, 2+ = multi-column
        "has_sidebar":  bool,  # True if the smaller peak looks sidebar-sized
        "x_bins":       list,  # raw histogram for debugging
      }

    Heuristic only — safe to over-warn on borderline 2-column CVs since the
    user keeps agency to proceed.
    """
    x_starts: List[float] = []
    page_widths: List[float] = []

    try:
        for page in doc:
            page_widths.append(float(page.rect.width))
            for block in page.get_text("dict").get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    bbox = line.get("bbox") or []
                    if len(bbox) < 2:
                        continue
                    spans = line.get("spans", [])
                    txt = "".join(s.get("text", "") for s in spans).strip()
                    if not txt:
                        continue
                    x_starts.append(float(bbox[0]))
    except Exception:
        return {"columns": 1, "has_sidebar": False, "x_bins": []}

    if not x_starts or not page_widths:
        return {"columns": 1, "has_sidebar": False, "x_bins": []}

    mean_width = sum(page_widths) / len(page_widths)
    if mean_width <= 0:
        return {"columns": 1, "has_sidebar": False, "x_bins": []}

    # Bin x-starts into 10 buckets across the page width.
    bins = [0] * 10
    for x in x_starts:
        ratio = max(0.0, min(0.9999, x / mean_width))
        bins[int(ratio * 10)] += 1

    total = sum(bins) or 1
    threshold = max(3, int(COLUMN_PEAK_MIN_RATIO * total))

    # Find peaks: local maxima above threshold.
    peaks: List[int] = []
    for i, count in enumerate(bins):
        if count < threshold:
            continue
        left  = bins[i - 1] if i > 0            else 0
        right = bins[i + 1] if i < len(bins) - 1 else 0
        if count >= left and count >= right:
            peaks.append(i)

    # Require at least COLUMN_GAP_MIN_BINS bin gap between peaks to count
    # as truly separate columns (rejects small wiggles from justified text).
    def _filter_gap(ps: List[int]) -> List[int]:
        if not ps:
            return []
        kept = [ps[0]]
        for p in ps[1:]:
            if p - kept[-1] >= COLUMN_GAP_MIN_BINS:
                kept.append(p)
        return kept

    peaks = _filter_gap(sorted(peaks))
    columns = len(peaks) if len(peaks) >= 2 else 1

    has_sidebar = False
    if columns >= 2:
        # "Sidebar" = a short peak in bins 0-2 that's materially smaller
        # than the tallest peak (the main content column).
        leftmost = peaks[0]
        tallest  = max(bins[p] for p in peaks)
        if leftmost <= 2 and bins[leftmost] < tallest * 0.7:
            has_sidebar = True

    return {
        "columns":     columns,
        "has_sidebar": has_sidebar,
        "x_bins":      bins,
    }


# ─────────────────────────────────────────────────────────────
# Validator
# ─────────────────────────────────────────────────────────────

def validate_cv(cv_path: str) -> ValidationReport:
    """
    Run the compatibility check. Never raises.

    Returns a ValidationReport where:
      - `ok` is False iff the file is unusable (errors present).
      - `score` is a heuristic 0..100 — 100 = ideal, 60-90 = degraded but
        usable, <60 = expect noticeable quality drop.
    """
    report = ValidationReport()

    # ── 1. File-level checks ─────────────────────────────────
    if not cv_path or not os.path.exists(cv_path):
        report.errors.append(f"CV file not found at {cv_path!r}.")
        report.ok = False
        report.score = 0
        return report

    try:
        size = os.path.getsize(cv_path)
    except OSError as e:
        report.errors.append(f"Cannot read CV file: {e}")
        report.ok = False
        report.score = 0
        return report

    report.details["file_bytes"] = size
    if size > MAX_FILE_BYTES:
        report.errors.append(
            f"File is too large ({size / 1_048_576:.1f} MB). "
            f"Maximum supported size is {MAX_FILE_BYTES / 1_048_576:.0f} MB."
        )
        report.ok = False
        report.score = 0
        return report

    # ── 2. Open with PyMuPDF ─────────────────────────────────
    doc: Optional[fitz.Document] = None
    try:
        doc = fitz.open(cv_path)
    except Exception as e:
        report.errors.append(
            f"PDF is unreadable (corrupt or unsupported variant): "
            f"{type(e).__name__}: {e}"
        )
        report.ok = False
        report.score = 0
        return report

    try:
        # Password-protected?
        if doc.needs_pass or getattr(doc, "is_encrypted", False):
            report.errors.append(
                "This PDF is password-protected. Please export an unprotected "
                "copy and upload again."
            )
            report.ok = False
            report.score = 0
            return report

        n_pages = doc.page_count
        report.details["page_count"] = n_pages
        if n_pages == 0:
            report.errors.append("PDF has no pages.")
            report.ok = False
            report.score = 0
            return report
        if n_pages > MAX_PAGES:
            report.warnings.append(
                f"PDF has {n_pages} pages — unusually long. Processing will "
                f"still run but the agent is tuned for 1-4 page CVs."
            )
            report.score -= 10

        # ── 3. Text layer + content checks ──────────────────
        try:
            full_text = "\n".join(p.get_text("text") or "" for p in doc)
        except Exception as e:
            report.errors.append(f"Failed to extract text from PDF: {e}")
            report.ok = False
            report.score = 0
            return report

        char_count = len(full_text.strip())
        report.details["char_count"] = char_count

        if char_count < MIN_TEXT_CHARS:
            report.errors.append(
                "No readable text found in this PDF — it looks like a SCANNED "
                "or image-only document. The agent needs a text-based CV. "
                "Please export your CV from Word / Docs / LaTeX rather than "
                "photographing or scanning it."
            )
            report.ok = False
            report.score = 0
            return report

        if char_count < ADVISED_TEXT_CHARS:
            report.warnings.append(
                f"CV is very short ({char_count} chars). The tailor agent "
                f"works best on a full-length CV with measurable outcomes."
            )
            report.score -= 15

        # ── 4. Language / character-set heuristic ───────────
        ascii_chars  = sum(1 for c in full_text if ord(c) < 128)
        ascii_ratio  = ascii_chars / max(1, len(full_text))
        report.details["ascii_ratio"] = round(ascii_ratio, 3)
        if ascii_ratio < MIN_ASCII_RATIO:
            report.warnings.append(
                "This CV contains a lot of non-ASCII characters — it may be "
                "in a language other than English. The agent's section "
                "detection is English-only; expect degraded output."
            )
            report.score -= 20

        # ── 5. Section detection dry-run ────────────────────
        section_hits = len(set(
            m.group(0).lower() for m in _SECTION_RX.finditer(full_text)
        ))
        report.details["section_hits"] = section_hits
        if section_hits < MIN_SECTION_HITS:
            report.warnings.append(
                f"Only {section_hits} standard CV section header(s) detected. "
                f"The agent may fall back to rebuilding the CV from text "
                f"(visual layout will change)."
            )
            report.score -= 15

        # ── 6. Bullet detection dry-run ─────────────────────
        bullet_count = sum(1 for c in full_text if c in _BULLET_CHARS)
        report.details["bullet_count"] = bullet_count
        if bullet_count < 3:
            report.warnings.append(
                "Few bullet points detected. The agent reorders bullets per "
                "role to emphasise JD-relevant experience; if bullets aren't "
                "a distinct layer, that optimisation will be skipped."
            )
            report.score -= 10

        # ── 7. Font diversity (informational) ───────────────
        try:
            fonts = {
                f[3] for p in doc for f in p.get_fonts(full=False) if f
            }
            report.details["fonts"] = sorted(list(fonts))[:10]
        except Exception:
            pass

        # ── 8. Multi-column / designer-template layout ──────
        try:
            layout = _detect_column_layout(doc)
        except Exception:
            layout = {"columns": 1, "has_sidebar": False, "x_bins": []}
        report.details["columns"]     = layout["columns"]
        report.details["has_sidebar"] = layout["has_sidebar"]

        sidebar_tokens_hit = bool(_DESIGNER_SIDEBAR_RX.search(full_text))
        report.details["designer_sidebar_tokens"] = sidebar_tokens_hit

        # Also treat "3+ distinct fonts" as a weak designer-template signal
        # (ATS-friendly CVs usually stick to 1-2 font families).
        unique_fonts = len(report.details.get("fonts", []))
        font_heavy   = unique_fonts >= 4
        report.details["font_heavy"] = font_heavy

        if layout["columns"] >= 2 or layout["has_sidebar"] or sidebar_tokens_hit:
            bits = []
            if layout["has_sidebar"]:
                bits.append("a left-hand sidebar column")
            elif layout["columns"] >= 2:
                bits.append(f"{layout['columns']} columns of text")
            if sidebar_tokens_hit:
                bits.append("personal-details fields (e.g. DOB, scholastic record)")
            if font_heavy:
                bits.append(f"{unique_fonts} distinct fonts")
            detail = " and ".join(bits) if bits else "a non-standard layout"
            report.warnings.append(
                "This CV appears to use a **designer / multi-column template** "
                f"({detail}). The agent's tailoring and PDF rebuild expect a "
                "single-column, ATS-style layout. Expect sections to be "
                "reordered or renamed, bullets to lose their original position, "
                "and the visual design to change. For best results, upload a "
                "single-column export (Word / Docs 'Simple' template, or the "
                "LaTeX 'moderncv' default)."
            )
            # Penalty grows with severity: 15 per tell, capped at 35.
            penalty = 15 + (10 if layout["has_sidebar"] else 0) + \
                      (10 if sidebar_tokens_hit else 0)
            report.score -= min(35, penalty)

    finally:
        try:
            doc.close()
        except Exception:
            pass

    # Finalise.
    report.score = max(0, min(100, report.score))
    report.ok    = not report.errors
    return report


# ─────────────────────────────────────────────────────────────
# Convenience: one-line check used by the Streamlit app
# ─────────────────────────────────────────────────────────────

def describe_report(report: ValidationReport) -> str:
    """One-line human-readable summary for logs / toast messages."""
    if not report.ok:
        return f"✗ blocked ({len(report.errors)} error(s))"
    if report.warnings:
        return f"~ compatibility {report.score}/100 ({len(report.warnings)} warning(s))"
    return f"✓ compatibility {report.score}/100"
