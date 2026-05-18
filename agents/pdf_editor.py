# agents/pdf_editor.py
#
# In-place PDF editor for CV tailoring.
#
# Strategy: keep the uploaded PDF as the canvas. Only modify text that the
# tailor explicitly changed:
#   * summary paragraph (rewritten)
#   * bullet order within each role (reordered, wording preserved)
#   * skills list order (reordered)
#
# Everything else (fonts, colours, lines, tables, page size, spacing,
# italics, headers, footers) is untouched because we never rebuild the page.

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional
import hashlib

import fitz  # PyMuPDF


# ─────────────────────────────────────────────────────────────
# OUTLINE CACHE (P2-1: May 2026)
# ─────────────────────────────────────────────────────────────
# Simple in-memory cache for CV outlines to avoid re-parsing the
# same PDF multiple times when tailoring to multiple jobs.
# Cache key: (pdf_path, file_mtime, file_size) - invalidates when
# the file changes. This is safe for single-process usage; for
# multi-process, a Redis or file-based cache would be needed.

# Run-17 audit fix #13: cap the cache at 32 entries with simple FIFO
# eviction. The previous unbounded dict grew per upload on long-running
# Streamlit Cloud instances where the same process serves many users.
# Outlines are ~5-50KB each, so 32 entries = 160KB-1.6MB worst case.
#
# Run 19 audit fix #35: add threading.Lock + deepcopy on cache hit. The
# previous unlocked dict allowed RuntimeError during concurrent iteration
# (TAILOR_JOB_CONCURRENCY=2). Worse: returned outlines were shared by
# reference — when one thread attached _megabullet_subidx etc. to bullets,
# a concurrent job parsing the same source PDF saw the mutated outline.
import copy as _copy
import threading as _threading
_OUTLINE_CACHE_MAX = 32
_outline_cache: Dict[tuple, tuple] = {}  # cache_key -> (outline, mtime, size)
_outline_cache_lock = _threading.Lock()


def _evict_outline_cache_if_full() -> None:
    """FIFO eviction when the cache grows past _OUTLINE_CACHE_MAX.

    MUST be called with _outline_cache_lock held.
    """
    while len(_outline_cache) > _OUTLINE_CACHE_MAX:
        # Pop the oldest entry. Python 3.7+ dicts preserve insertion order.
        oldest_key = next(iter(_outline_cache))
        _outline_cache.pop(oldest_key, None)


def _get_cache_key(pdf_path: str) -> tuple:
    """Generate a cache key based on file path, mtime, and size."""
    try:
        stat = os.stat(pdf_path)
        return (pdf_path, stat.st_mtime, stat.st_size)
    except OSError:
        return (pdf_path, 0, 0)


def build_outline_cached(pdf_path: str) -> Dict[str, Any]:
    """
    Wrapper around build_outline that uses an in-memory cache.
    Returns the cached outline if the PDF hasn't changed, otherwise
    parses the PDF and updates the cache.
    """
    cache_key = _get_cache_key(pdf_path)

    # Run 19 audit fix #35: locked read + deepcopy on hit so concurrent
    # jobs don't mutate each other's outlines.
    with _outline_cache_lock:
        cached = _outline_cache.get(cache_key)
    if cached is not None:
        cached_outline, cached_mtime, cached_size = cached
        try:
            stat = os.stat(pdf_path)
            if stat.st_mtime == cached_mtime and stat.st_size == cached_size:
                # Deep-copy so per-job mutations (e.g. _megabullet_subidx
                # on bullets, _continuation_anchors blanking) don't leak
                # into the cached canonical outline shared by other threads.
                return _copy.deepcopy(cached_outline)
        except OSError:
            pass  # File error, fall through to re-parse

    # Parse the PDF and cache the result under the lock.
    outline = build_outline(pdf_path)
    fresh_key = _get_cache_key(pdf_path)
    try:
        stat = os.stat(pdf_path)
        with _outline_cache_lock:
            _outline_cache[fresh_key] = (outline, stat.st_mtime, stat.st_size)
            _evict_outline_cache_if_full()
    except OSError:
        pass  # Still return the outline even if we can't get stats

    # Return a deep copy so the caller's mutations don't pollute the cache.
    return _copy.deepcopy(outline)


# ─────────────────────────────────────────────────────────────
# FONT HANDLING
# ─────────────────────────────────────────────────────────────

# PyMuPDF built-in (Base14) font aliases, picked by family + bold/italic.
_BUILTIN = {
    ("helv",    False, False): "helv",
    ("helv",    True,  False): "hebo",
    ("helv",    False, True ): "heit",
    ("helv",    True,  True ): "hebi",
    ("times",   False, False): "tiro",
    ("times",   True,  False): "tibo",
    ("times",   False, True ): "tiit",
    ("times",   True,  True ): "tibi",
    ("courier", False, False): "cour",
    ("courier", True,  False): "cobo",
    ("courier", False, True ): "coit",
    ("courier", True,  True ): "cobi",
}


def _font_key(span: dict) -> tuple:
    name  = (span.get("font") or "").lower()
    flags = int(span.get("flags", 0))
    bold  = bool(flags & 16) or "bold"   in name
    italic = bool(flags & 2) or "italic" in name or "oblique" in name
    if any(x in name for x in ("times", "georgia", "garamond", "serif")):
        family = "times"
    elif any(x in name for x in ("courier", "mono", "consolas")):
        family = "courier"
    else:
        family = "helv"
    return family, bold, italic


def _pick_builtin(span: dict) -> str:
    return _BUILTIN[_font_key(span)]


_SUBSET_PREFIX_RX = re.compile(r"^[A-Z]{6}\+")


def _is_subset_font(basefont: str) -> bool:
    return bool(_SUBSET_PREFIX_RX.match(basefont or ""))


def _find_font_xref(doc: fitz.Document, wanted_name: str) -> Optional[int]:
    """
    Find the xref of an embedded TTF whose PostScript/base name matches
    `wanted_name`.

    Preference order:
      1. Exact NON-subset match (full font — safest, has all glyphs).
      2. Substring NON-subset match.
      3. Exact SUBSET match (e.g. 'BAAAAA+Calibri').
      4. Substring SUBSET match.

    Subset fonts (May 2026 — Shrestha CV fix): previously skipped
    entirely because their glyph tables only cover characters that
    appeared in the original document. But Word / Google Docs / "Print
    to PDF" exports subset EVERY font — so for those CVs (the majority)
    skipping subsets meant we ALWAYS fell back to Base14 Helvetica,
    visibly changing the body font. In practice a tailored rewrite is a
    paraphrase that reuses the same character set as the original, so
    the subset font usually CAN render it. We now return subset xrefs
    too; the caller (`_install_original_font`) runs `_font_can_render`
    on the actual new text and falls back to Base14 only when the
    subset genuinely lacks a needed glyph.
    """
    w = (wanted_name or "").lower()
    if not w:
        return None
    exact_full: Optional[int] = None
    sub_full:   Optional[int] = None
    exact_subset: Optional[int] = None
    sub_subset:   Optional[int] = None
    for pi in range(doc.page_count):
        for entry in doc.get_page_fonts(pi):
            # entry: (xref, ext, type, basefont, refname, encoding)
            xref = entry[0]
            base = str(entry[3] or "")
            is_subset = _is_subset_font(base)
            base_l = base.lower()
            base_clean = base_l.split("+")[-1]
            if w == base_clean or w == base_l:
                if is_subset:
                    if exact_subset is None:
                        exact_subset = xref
                else:
                    exact_full = xref
                    break
            elif w in base_clean:
                if is_subset:
                    if sub_subset is None:
                        sub_subset = xref
                elif sub_full is None:
                    sub_full = xref
        if exact_full is not None:
            break
    return exact_full or sub_full or exact_subset or sub_subset


# Cache of (doc_id, xref) -> installed font alias on each page.
# We install per-page because PyMuPDF requires page-level insert_font().

def _font_can_render(buf: bytes, text: str) -> bool:
    """
    True iff the TTF/CID font in `buf` has a glyph for every non-whitespace
    character in `text`. PyMuPDF otherwise silently substitutes .notdef
    (glyph 0) on missing chars — text renders as NULLs / `?`.
    """
    try:
        font = fitz.Font(fontbuffer=buf)
    except Exception:
        return False
    # Extra verification for the space glyph: a subset font may keep the
    # U+0020 character mapping but strip its advance-width metrics. When
    # that happens, PyMuPDF renders spaces as zero-width marks which
    # visually look like NBSPs running every word together. Detect this
    # by requiring the space glyph to have a positive advance, and force
    # fallback to Base14 when it does not.
    try:
        sp_advance = font.glyph_advance(0x20)
        if not sp_advance or sp_advance <= 0:
            return False
    except Exception:
        return False
    # Skip line-break whitespace (they are structural, not rendered).
    for ch in set(text):
        if ch in ("\n", "\t", "\r"):
            continue
        cp = ord(ch)
        try:
            if not font.has_glyph(cp):
                return False
            # Apr 28 follow-up: bullets-as-`?` bug.
            #
            # Subsetted embedded fonts (extremely common in CVs exported from
            # Word / Google Docs / "Print to PDF") frequently keep a cmap
            # entry for a non-ASCII codepoint while STRIPPING the glyph
            # outline + advance-width data for it. `has_glyph` then returns
            # True (cmap-only check) but PyMuPDF's `insert_textbox` ends up
            # drawing `.notdef`, which renders as `?` in most readers.
            #
            # The bullet character (U+2022 •) is the canonical victim of
            # this: every CV in the wild has cmap support for it, but only
            # ~30% of subsetted fonts retain real glyph data after the PDF
            # producer's font-subsetter strips "unused" glyphs.
            #
            # Catch this by verifying `glyph_advance > 0` for any non-ASCII
            # char — ASCII letters/digits always survive subsetting (they're
            # used in body text), so we only spend cycles on the suspect
            # range. Failing this check here forces the caller to fall
            # through to the Base14 path, which renders U+2022 reliably
            # via WinAnsi 0x95.
            if cp >= 0x80:
                adv = font.glyph_advance(cp)
                if not adv or adv <= 0:
                    return False
                # Apr 29 follow-up: advance-only check is INSUFFICIENT.
                # Some fonts retain the cmap entry AND the advance-width
                # metric for a codepoint while stripping the actual glyph
                # outline data (.notdef). The advance check passes but
                # PyMuPDF still draws .notdef at render time → renders
                # as `?` in readers. Observed concretely on Calibri-
                # subsetted Word→PDF exports for U+2022 (•): the bullet
                # character has positive advance metrics but zero-area
                # outline, because Word's PDF exporter never asked the
                # subsetter to retain it (the original document used
                # Symbol-font \uf0b7 for bullets, not U+2022).
                #
                # Verify the glyph has a non-empty bbox as a second-line
                # defence. Real glyphs have a positive-area bbox; .notdef
                # / outline-stripped glyphs have a zero-area bbox even
                # when the advance is set.
                #
                # Wrapped in try/except because `glyph_bbox` is not in
                # all PyMuPDF versions; on older builds we fall through
                # to the existing advance-only check rather than break.
                try:
                    bbox = font.glyph_bbox(cp)
                    if bbox is None:
                        return False
                    if hasattr(bbox, "x0"):
                        w = float(bbox.x1) - float(bbox.x0)
                        h = float(bbox.y1) - float(bbox.y0)
                    else:
                        w = float(bbox[2]) - float(bbox[0])
                        h = float(bbox[3]) - float(bbox[1])
                    if w <= 0 or h <= 0:
                        return False
                except AttributeError:
                    pass  # PyMuPDF too old for glyph_bbox — keep advance-only check
                except Exception:
                    return False
        except Exception:
            return False
    return True


def _install_original_font(
    doc:  fitz.Document,
    page: fitz.Page,
    span: dict,
    installed: Dict[int, str],
    text: Optional[str] = None,
) -> Optional[str]:
    """
    Try to register the embedded TTF of `span`'s font on `page` and return the
    alias to use in insert_textbox. Returns None if not possible (bad encoding,
    extraction failure, etc.).
    """
    name = str(span.get("font") or "")
    if not name or _is_symbolic_font_name(name):
        return None
    xref = _find_font_xref(doc, name)
    if xref is None:
        return None
    alias = installed.get(xref)
    if alias:
        # Already registered on SOME page — must also register on this page.
        buf = installed.get(f"buf:{xref}")
        if text is not None and isinstance(buf, (bytes, bytearray)):
            if not _font_can_render(bytes(buf), text):
                return None   # font lacks glyphs for the new text
        try:
            page.insert_font(fontname=alias, fontbuffer=buf)
            return alias
        except Exception:
            return None
    try:
        info = doc.extract_font(xref)
        # PyMuPDF returns (basefont, ext, type, buffer) as tuple — layout varies by version.
        buf = None
        for item in info:
            if isinstance(item, (bytes, bytearray)) and len(item) > 200:
                buf = bytes(item)
                break
        if not buf:
            return None
        # Glyph-coverage pre-check: avoid fonts that silently substitute
        # .notdef for chars not in their subset.
        if text is not None and not _font_can_render(buf, text):
            return None
        alias = f"emb{xref}"
        page.insert_font(fontname=alias, fontbuffer=buf)
        installed[xref] = alias
        installed[f"buf:{xref}"] = buf
        return alias
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# Bundled metric-compatible clone fonts (agents/fonts/)
# ─────────────────────────────────────────────────────────────
# When the CV's own embedded font cannot be reused, a clone matched to the
# original family beats Base14: Base14 already approximates Arial / Times /
# Courier acceptably, but does NOT resemble Calibri or Cambria — the two
# common Word fonts. Each entry maps an original-family keyword to
# (clone basename, the ORIGINAL family's ascender). The ascender matters
# because a clone shares glyph WIDTHS with the original (text wraps the
# same) but NOT vertical metrics — the original's ascender must drive
# baseline placement so re-inserted text lands where the original sat.

_FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

_CLONE_FONTS: Dict[str, Tuple[str, float]] = {
    "calibri": ("Carlito", 0.75),   # Carlito — metric-compatible with Calibri
    "cambria": ("Caladea", 0.95),   # Caladea — metric-compatible with Cambria
}


def _bundled_clone_font(
    span:  dict,
    text:  str,
    cache: Optional[Dict[Any, Any]] = None,
) -> Optional[Tuple["fitz.Font", float]]:
    """
    Return (clone fitz.Font, original-family ascender) for `span`'s font
    family, or None when no bundled clone covers it. The clone is a full
    font, so it also rescues a subset original that lacked a glyph.
    """
    name = str(span.get("font") or "").lower()
    entry = next((v for k, v in _CLONE_FONTS.items() if k in name), None)
    if entry is None:
        return None
    base, orig_ascender = entry
    flags  = int(span.get("flags", 0) or 0)
    bold   = bool(flags & 16) or "bold" in name
    italic = bool(flags & 2)  or "italic" in name or "oblique" in name
    style  = ("BoldItalic" if bold and italic else
              "Bold" if bold else "Italic" if italic else "Regular")
    fname  = f"{base}-{style}.ttf"
    ck     = f"clonefont:{fname}"
    if cache is not None and isinstance(cache.get(ck), fitz.Font):
        return cache[ck], orig_ascender
    path = os.path.join(_FONTS_DIR, fname)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as fh:
            buf = fh.read()
    except Exception:
        return None
    # Full clone fonts cover Latin CV text + standard punctuation; this
    # guards only the rare exotic glyph the clone happens to lack.
    if not _font_can_render(buf, text):
        return None
    try:
        font = fitz.Font(fontbuffer=buf)
    except Exception:
        return None
    if cache is not None:
        cache[ck] = font
    return font, orig_ascender


# ATS hygiene: re-inserted LLM text occasionally carries Unicode that
# trips older resume parsers — smart quotes, en/em dashes, ellipsis,
# non-breaking spaces, zero-width characters. Map them to plain ASCII.
# Applied ONLY to LLM-generated edits, never to restored original text.
_ATS_NORMALIZE = {
    0x201c: '"', 0x201d: '"', 0x201e: '"', 0x201f: '"',
    0x2018: "'", 0x2019: "'", 0x201a: "'", 0x201b: "'",
    0x2013: "-", 0x2014: "-",
    0x2026: "...",
    0x00a0: " ",
    0x200b: "", 0x200c: "", 0x200d: "", 0x2060: "", 0xfeff: "",
}


def _normalize_for_ats(text: str) -> str:
    """Convert ATS-hostile Unicode in re-inserted LLM text to plain ASCII."""
    if not text or not isinstance(text, str):
        return text
    return text.translate(_ATS_NORMALIZE)


def _int_color_to_rgb(c: int) -> tuple:
    r = (c >> 16) & 0xFF
    g = (c >> 8)  & 0xFF
    b = c         & 0xFF
    return (r / 255.0, g / 255.0, b / 255.0)


# ─────────────────────────────────────────────────────────────
# SECTION HEADING DETECTION
# ─────────────────────────────────────────────────────────────

_HEADINGS: Dict[str, re.Pattern] = {
    "summary": re.compile(
        r"^\s*(professional\s+summary|career\s+summary|executive\s+summary|summary|profile)\s*$",
        re.I,
    ),
    "experience": re.compile(
        r"^\s*(professional\s+experience|work\s+experience|employment\s+history|"
        r"relevant\s+experience|experience)\s*$",
        re.I,
    ),
    "education": re.compile(
        r"^\s*(education|academic\s+achievements|academic\s+background)\s*$",
        re.I,
    ),
    "skills": re.compile(
        r"^\s*(technical\s+skills|core\s+skills|skills)\s*$",
        re.I,
    ),
    "projects": re.compile(
        # P0 (Apr 28): added "personal projects" + other common variants.
        # The previous regex missed "Personal Projects" which is one of the
        # most common project-section headings on CVs. The miss caused the
        # entire projects block (often 200-300+ words of project bullets)
        # to be absorbed into the preceding "Professional Summary" section,
        # which then exploded the summary word-count baseline (e.g. 80 →
        # 385 words) and broke the in-place tailor's summary slot, causing
        # visible layout damage on the rendered PDF (role headers got
        # clipped to "A" / "Cl" fragments because the rewritten summary
        # overflowed into them).
        #
        # Apr 28 follow-up: generalised to ANY single qualifier word + the
        # word "projects?" (with optional ":" or "highlights/portfolio/
        # showcase" suffix). Captures arbitrary user variants we can't
        # enumerate exhaustively (capstone projects, research projects,
        # passion projects, capstone-projects, academic projects, etc.).
        # The 3-word qualifier cap and per-word [a-z\-]+ pattern keep us
        # from matching paragraph text that happens to contain "projects".
        r"^\s*(?:[a-z][a-z\-]*[\s\-]+){0,3}projects?\s*"
        r"(?:highlights|portfolio|showcase)?\s*:?\s*$",
        re.I,
    ),
    "certifications": re.compile(
        r"^\s*(certifications?|certificates?)\s*$",
        re.I,
    ),
    # Adaptive parsing fix (May 2026 — Shrestha CV evidence): recognise
    # AWARDS / ACHIEVEMENTS / HONORS as a distinct section. Without this
    # entry, the awards block on a Genesis-style CV silently flows into
    # whatever section preceded it (usually "projects"), corrupting that
    # role's bullet list. Variants observed in real ATS-friendly CVs:
    #   "Awards", "Awards & Achievements", "Achievements",
    #   "Honors & Awards", "Recognition", "Awards and Honors"
    # The optional prefix word (Honors / Recognition / Notable) plus the
    # optional " & <other>" suffix captures the common templates without
    # false-matching prose like "Achievements I'm proud of".
    "awards": re.compile(
        r"^\s*(?:honors?\s*(?:&|and)\s*|notable\s+|"
        r"key\s+|selected\s+|recognition\s*[:&]?\s*)?"
        r"(awards?|achievements?|honors?|recognitions?|accomplishments?)"
        r"(?:\s*(?:&|and)\s*(?:achievements?|honors?|awards?|recognitions?))?"
        r"\s*:?\s*$",
        re.I,
    ),
}


def _classify_heading(text: str) -> Optional[str]:
    t = (text or "").strip()
    if not t or len(t) > 60:
        return None
    for kind, rx in _HEADINGS.items():
        if rx.match(t):
            # May 2026 (Run 12 fix): reject the bare singular "Project"
            # (no qualifier word, no colon, no plural). On Shrestha-style
            # CVs the word "Project" appears as a sub-section label INSIDE
            # an experience role (labelling the project the candidate was
            # responsible for). Letting the projects regex match it splits
            # the role mid-bullet and corrupts the outline. Any legitimate
            # project section heading is either plural ("Projects"),
            # qualified ("Personal Projects"), or punctuated ("Project:").
            if kind == "projects" and t.strip().lower() == "project":
                return None
            return kind
    return None


# P0-followup (Apr 28): handle the case where PyMuPDF returns a heading
# merged with the following line's content as a single string. Observed
# repeatedly on tight-spacing CVs:
#
#   "Personal Projects ApplySmart AI | Agentic AI Product Jan 2026 - Present"
#
# arrives as ONE extracted line because the visual gap between the heading
# and the next paragraph is small enough that PyMuPDF clusters them into
# the same "line". The strict `^...$` regex in `_classify_heading` then
# misses the heading completely, and the entire projects block flows into
# whatever section came before (typically Summary), exploding its word
# count from ~80 to ~385 and breaking the in-place tailor's summary slot.
#
# This helper detects the merged case specifically for the projects family
# of headings (where it has been seen in the wild) and returns the heading
# text and remainder so the caller can split the line into two logical
# lines: the heading + the first content line of the projects section.
#
# Scope is intentionally narrow:
#   • Only PROJECTS headings (Personal/Side/Notable/Selected/etc.).
#     We do NOT extend this to summary/experience/education because the
#     regex would false-positive on prose like "Education taught me..."
#     or "Experience working with..." in body text.
#   • Only when the remainder looks like a project subtitle: contains a
#     pipe, em/en-dash, year (19xx/20xx), or month-year pattern. Without
#     these signals we'd risk splitting bullets like "Personal projects
#     taught me to ship end-to-end".
_MERGED_PROJECT_HEADING_RX = re.compile(
    r"^\s*("
    r"featured\s+projects?|key\s+projects?|"
    r"personal\s+projects?|side\s+projects?|notable\s+projects?|"
    r"selected\s+projects?|independent\s+projects?|"
    r"open[\s\-]source\s+projects?"
    r")\s+(?=\S)",
    re.I,
)
_PROJECT_SUBTITLE_HINT_RX = re.compile(
    r"[|–—]"                                              # pipe / en-dash / em-dash
    r"|\b(?:19|20)\d{2}\b"                                # 19xx / 20xx year
    r"|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"  # month
    r"\w*\.?\s+\d{4}\b",
    re.I,
)


def _try_split_merged_heading(text: str) -> Optional[tuple]:
    """
    Detect "<known projects heading> <project subtitle...>" merged into one
    extraction line. Returns (kind, heading_text, remainder_text) on a
    confident match, else None.
    """
    if not text or len(text) > 400:
        return None
    m = _MERGED_PROJECT_HEADING_RX.match(text)
    if not m:
        return None
    heading_text = m.group(1).strip()
    remainder    = text[m.end():].strip()
    if not remainder or len(remainder) < 4:
        return None
    if not _PROJECT_SUBTITLE_HINT_RX.search(remainder):
        return None
    return ("projects", heading_text, remainder)


# Union of bullet-like chars we recognise as markers:
#   - ASCII dash / asterisk
#   \u2022 • (unicode bullet)   \u00b7 · (middle dot)
#   \u2043 \u2219 \u25aa \u25cb \u25e6 \u25cf \u25a0 (other shapes)
#   \uf0b7 (Symbol-font bullet, PUA — used by Word/Print-to-PDF)
#   \uf0a7 \uf076 \uf0d8 (Wingdings-style bullets in PUA)
_BULLET_CHARS = (
    "-*"
    "\u2022\u00b7\u2043\u2219"
    "\u25aa\u25cb\u25e6\u25cf\u25a0"
    "\uf0b7\uf0a7\uf076\uf0d8"
)

# Adaptive bullet-glyph detection (May 2026 \u2014 Shrestha CV evidence).
# Different CVs use different bullet characters (\u2022 U+2022 / \u25aa U+25AA /
# \u00b7 U+00B7 / \u25cb U+25CB / etc.). When we re-render bullets after a rewrite,
# we should use the SAME glyph the original PDF used so the visual look
# is preserved. Asterisk fallback (* substituting for the original glyph
# in some embedded-font fallback paths) was visible to the user as a
# clear format break vs the input PDF.
_GLYPHIC_BULLET_CHARS = (
    "\u2022\u00b7\u2043\u2219"
    "\u25aa\u25cb\u25e6\u25cf\u25a0"
    "\uf0b7\uf0a7\uf076\uf0d8"
)


def _capture_borders_in_rect(
    page: "fitz.Page",
    rect: "fitz.Rect",
) -> List[Dict[str, Any]]:
    """
    Record straight border lines (table borders, section divider rules)
    that pass through `rect`, so they can be re-drawn after a redaction
    wipes them.

    May 2026 (Shrestha + Rishav): clipping the redact rect away from
    borders shrank it below the bullet text and broke text fitting. The
    robust approach is the opposite — redact freely, then restore any
    border the redaction crossed by re-drawing it at its exact original
    geometry / colour / width.

    Captures standalone "l" segments and the four edges of "re"
    rectangles. A line counts as "in the rect" when it crosses the
    rect's span (full original endpoints kept so the redraw is exact).
    """
    saved: List[Dict[str, Any]] = []
    if page is None or rect is None:
        return saved
    try:
        drawings = page.get_drawings() or []
    except Exception:
        return saved
    for d in drawings:
        color = d.get("color")
        width = d.get("width") or 0.75
        for item in d.get("items", []) or []:
            try:
                op = item[0]
            except (TypeError, IndexError):
                continue
            segs: List[Tuple[float, float, float, float]] = []
            if op == "l":
                try:
                    p1, p2 = item[1], item[2]
                    segs.append((float(p1.x), float(p1.y),
                                 float(p2.x), float(p2.y)))
                except Exception:
                    continue
            elif op == "re":
                try:
                    r = item[1]
                    rx0, ry0 = float(r.x0), float(r.y0)
                    rx1, ry1 = float(r.x1), float(r.y1)
                except Exception:
                    continue
                segs = [
                    (rx0, ry0, rx1, ry0),  # top edge
                    (rx0, ry1, rx1, ry1),  # bottom edge
                    (rx0, ry0, rx0, ry1),  # left edge
                    (rx1, ry0, rx1, ry1),  # right edge
                ]
            for (x1, y1, x2, y2) in segs:
                is_h = abs(y1 - y2) <= 2.0
                is_v = abs(x1 - x2) <= 2.0
                if not (is_h or is_v):
                    continue
                if is_h:
                    if not (rect.y0 - 1.0 <= y1 <= rect.y1 + 1.0):
                        continue
                    xlo, xhi = min(x1, x2), max(x1, x2)
                    if xlo >= rect.x1 or xhi <= rect.x0:
                        continue
                else:
                    if not (rect.x0 - 1.0 <= x1 <= rect.x1 + 1.0):
                        continue
                    ylo, yhi = min(y1, y2), max(y1, y2)
                    if ylo >= rect.y1 or yhi <= rect.y0:
                        continue
                saved.append({
                    "p1": (x1, y1), "p2": (x2, y2),
                    "color": color, "width": width,
                })
    return saved


def _redraw_borders(page: "fitz.Page", saved: List[Dict[str, Any]]) -> None:
    """Re-draw border segments captured by `_capture_borders_in_rect`."""
    for b in saved or []:
        try:
            page.draw_line(
                fitz.Point(*b["p1"]), fitz.Point(*b["p2"]),
                color=b.get("color") or (0, 0, 0),
                width=b.get("width") or 0.75,
            )
        except Exception:
            pass


def _horizontal_borders_intersecting_rect(
    page: "fitz.Page",
    rect: "fitz.Rect",
    tolerance: float = 1.5,
) -> List[float]:
    """
    Return Y positions of horizontal line segments drawn on `page` that
    intersect `rect`'s horizontal range. Mirror of
    `_vertical_borders_intersecting_rect` for the bottom-border case.
    """
    if page is None or rect is None:
        return []
    horizontals: List[float] = []
    try:
        drawings = page.get_drawings() or []
    except Exception:
        return []
    for d in drawings:
        for item in d.get("items", []) or []:
            try:
                op = item[0]
            except (TypeError, IndexError):
                continue
            if op == "l":
                try:
                    p1, p2 = item[1], item[2]
                    x1, y1 = float(p1.x), float(p1.y)
                    x2, y2 = float(p2.x), float(p2.y)
                except Exception:
                    continue
                if abs(y1 - y2) > tolerance:
                    continue  # not horizontal
                x_lo, x_hi = (x1, x2) if x1 < x2 else (x2, x1)
                if x_lo >= rect.x1 or x_hi <= rect.x0:
                    continue
                horizontals.append((y1 + y2) / 2.0)
            elif op == "re":
                try:
                    r = item[1]
                    rx0, ry0, rx1, ry1 = float(r.x0), float(r.y0), float(r.x1), float(r.y1)
                except Exception:
                    continue
                if abs(ry1 - ry0) <= tolerance:
                    if rx0 < rect.x1 and rx1 > rect.x0:
                        horizontals.append((ry0 + ry1) / 2.0)
                else:
                    if rx0 < rect.x1 and rx1 > rect.x0:
                        horizontals.append(ry0)
                        horizontals.append(ry1)
    return sorted(set(horizontals))


def _vertical_borders_intersecting_rect(
    page: "fitz.Page",
    rect: "fitz.Rect",
    tolerance: float = 1.5,
) -> List[float]:
    """
    Return X positions of vertical line segments (table borders / column
    separators) drawn on `page` that intersect `rect`'s vertical range.

    Used by apply_edits to clip the redaction rect so the white fill
    doesn't paint over table borders. Without this, 2-column bordered
    layouts (e.g. Shrestha's WORK EXPERIENCE table) lose their right
    border line whenever we rewrite bullets inside the right column.

    Tolerance allows for hairline lines that aren't perfectly vertical
    (rounded coordinates) — anything within `tolerance` pt of vertical
    counts.
    """
    if page is None or rect is None:
        return []
    verticals: List[float] = []
    try:
        drawings = page.get_drawings() or []
    except Exception:
        return []
    for d in drawings:
        for item in d.get("items", []) or []:
            try:
                op = item[0]
            except (TypeError, IndexError):
                continue
            if op == "l":
                # Line segment: ("l", Point start, Point end)
                try:
                    p1, p2 = item[1], item[2]
                    x1, y1 = float(p1.x), float(p1.y)
                    x2, y2 = float(p2.x), float(p2.y)
                except Exception:
                    continue
                if abs(x1 - x2) > tolerance:
                    continue  # not vertical
                y_lo, y_hi = (y1, y2) if y1 < y2 else (y2, y1)
                # Must intersect rect's Y range
                if y_lo >= rect.y1 or y_hi <= rect.y0:
                    continue
                verticals.append((x1 + x2) / 2.0)
            elif op == "re":
                # Rectangle: ("re", Rect)
                try:
                    r = item[1]
                    rx0, ry0, rx1, ry1 = float(r.x0), float(r.y0), float(r.x1), float(r.y1)
                except Exception:
                    continue
                # Treat as four edges; record the vertical ones
                if abs(rx1 - rx0) <= tolerance:
                    # Degenerate rect (vertical line)
                    if ry0 < rect.y1 and ry1 > rect.y0:
                        verticals.append((rx0 + rx1) / 2.0)
                else:
                    # Real rectangle — its left + right edges are verticals.
                    # Only count edges that intersect our rect Y-range.
                    if ry0 < rect.y1 and ry1 > rect.y0:
                        verticals.append(rx0)
                        verticals.append(rx1)
    return sorted(set(verticals))


def _estimate_text_fits(
    text:        str,
    rect_width:  float,
    rect_height: float,
    fontsize:    float,
    line_gap:    float,
) -> bool:
    """
    Estimate (without touching the page) whether `text` will fit inside a
    box of `rect_width` × `rect_height` at `fontsize`. Used as a PRE-CHECK
    so apply_edits never redacts a bullet whose rewrite won't fit — which
    would leave a blank gap when both the rewrite AND the original-text
    restore fail PyMuPDF's conservative measurement.

    Greedy word-wrap line count × the TIGHTEST line gap the editor will
    try (line_gap × 0.88, matching `_insert_fitted`'s gap ladder floor).
    If the text fits even at the tightest spacing, the real insertion is
    guaranteed a gap that works. Conservative by ~1pt.
    """
    if not text or not text.strip():
        return True
    if rect_width <= 0 or rect_height <= 0:
        return False
    try:
        font = fitz.Font("helv")
    except Exception:
        return True  # cannot measure — assume fits, let insertion decide
    words = text.split()
    if not words:
        return True
    try:
        space_w = font.text_length(" ", fontsize)
    except Exception:
        return True
    n_lines = 1
    cur_w = 0.0
    for w in words:
        try:
            ww = font.text_length(w, fontsize)
        except Exception:
            ww = len(w) * fontsize * 0.5
        if cur_w <= 0:
            cur_w = ww
        elif cur_w + space_w + ww <= rect_width:
            cur_w += space_w + ww
        else:
            n_lines += 1
            cur_w = ww
    # helv is ~8-10% wider than typical CV body fonts (Calibri/Liberation),
    # so this line count is a slight over-estimate — that's the safe
    # direction (skip rather than redact-and-fail).
    tightest_gap = max(1.0, line_gap * 0.88)
    needed_h = n_lines * fontsize * tightest_gap
    return needed_h <= rect_height + 1.0


def _bullet_body_rect(
    lines: List[Dict[str, Any]],
) -> Tuple[Optional["fitz.Rect"], bool, Optional["fitz.Rect"]]:
    """
    Compute the bounding rect of ONE bullet's BODY text — the
    text-carrying spans only, EXCLUDING a separate-span bullet glyph
    (e.g. a Wingdings ▪ rendered in its own span at a different X).

    Returns (rect, has_inline_glyph, true_rect):
      rect             — padded fitz.Rect of the body text (drives the
                         redaction), or None if empty.
      has_inline_glyph — True when the bullet glyph is embedded in the
                         first text-carrying span ("• Started…" as one
                         span, single-column CVs). The caller must then
                         re-prepend the glyph because redacting the body
                         rect also wipes that inline glyph. False when
                         the glyph is a separate span (2-column layouts
                         like Shrestha's Wingdings ▪) — that glyph sits
                         outside this rect and stays untouched.
      true_rect        — un-padded body-text rect, so the caller places
                         re-inserted text at the exact original origin.
    """
    text_boxes: List[List[float]] = []
    has_inline_glyph = False
    first_text_span_checked = False
    for ln in lines:
        spans = ln.get("spans") or []
        if spans:
            for sp in spans:
                if not _span_is_text_carrying(sp):
                    continue
                bb = sp.get("bbox")
                if bb:
                    text_boxes.append(list(bb))
                if not first_text_span_checked:
                    first_text_span_checked = True
                    sp_text = (sp.get("text") or "").lstrip()
                    if sp_text and sp_text[0] in _BULLET_CHARS:
                        has_inline_glyph = True
        else:
            bb = ln.get("bbox")
            if bb:
                text_boxes.append(list(bb))
            if not first_text_span_checked:
                first_text_span_checked = True
                ln_text = (ln.get("text") or "").lstrip()
                if ln_text and ln_text[0] in _BULLET_CHARS:
                    has_inline_glyph = True
    if not text_boxes:
        text_boxes = [list(ln["bbox"]) for ln in lines if ln.get("bbox")]
    if not text_boxes:
        return None, False, None
    # Padded rect drives the redaction; the un-padded rect lets the
    # caller place re-inserted text at the body's TRUE origin (the pad
    # would otherwise shift every edited bullet up-and-left by 1pt).
    return (_union_rect(text_boxes, pad=1.0), has_inline_glyph,
            _union_rect(text_boxes, pad=0.0))


def _detect_section_bullet_glyph(section: Dict[str, Any]) -> str:
    """
    Detect the bullet glyph the original PDF used for this section.
    Returns the most common glyph found, or U+2022 (\u2022) as a safe default.

    Method (two-pass):
      Pass 1: standalone glyph-only lines (e.g. PyMuPDF extracted "\u25aa"
        on its own line because the body text was at a different X)
      Pass 2: glyph-prefixed lines (e.g. "\u25aa Led communication...")
    """
    from collections import Counter
    glyphs: List[str] = []

    for ln in section.get("lines", []) or []:
        text = (ln.get("text") or "").strip()
        if not text:
            continue
        # Standalone glyph line: short text whose every non-space
        # character is a bullet glyph.
        if len(text) <= 3:
            chars = [c for c in text if not c.isspace()]
            if chars and all(c in _GLYPHIC_BULLET_CHARS for c in chars):
                glyphs.append(chars[0])
                continue
        # Glyph-prefixed line: first non-space char is a bullet glyph.
        first = text.lstrip()[:1]
        if first and first in _GLYPHIC_BULLET_CHARS:
            glyphs.append(first)

    if glyphs:
        return Counter(glyphs).most_common(1)[0][0]
    # Fallback: U+2022 (\u2022) renders cleanly in most CV fonts.
    return "\u2022"
_BULLET_START_RX = re.compile(rf"^\s*[{re.escape(_BULLET_CHARS)}]")
_BULLET_STRIP_RX = re.compile(rf"^[{re.escape(_BULLET_CHARS)}]+\s*")

# Extraction-failure marker: when PyMuPDF can't decode a bullet glyph from
# a custom symbol font it often returns '?' or U+FFFD followed by 2+ spaces.
# Strip that pattern so it doesn't end up as literal "?   " in the output.
_BAD_BULLET_PREFIX_RX = re.compile(r"^\s*[?\uFFFD]+(?=\s{2,})\s+")


def _is_bullet(text: str) -> bool:
    t = text or ""
    if _BULLET_START_RX.match(t):
        return True
    # Fallback: extraction-failed bullet glyph rendered as "?   " or "\uFFFD   "
    return bool(_BAD_BULLET_PREFIX_RX.match(t))


def _strip_bullet(text: str) -> str:
    """Remove leading bullet character(s) and following whitespace."""
    t = (text or "").lstrip()
    # First strip any mis-extracted bullet marker ("?   " / "\uFFFD   ").
    t = _BAD_BULLET_PREFIX_RX.sub("", t)
    return _BULLET_STRIP_RX.sub("", t).strip()


# ─────────────────────────────────────────────────────────────
# STRUCTURE EXTRACTION
# ─────────────────────────────────────────────────────────────

def _protected_table_zones(page: fitz.Page) -> List[fitz.Rect]:
    """
    Detect tables on the page and return their bounding rects. Any line whose
    bbox intersects one of these zones is considered factual, non-editable
    content (education grids, scholastic records, skills matrices, contact
    header tables) and is excluded from `_collect_page_lines`.

    Rationale: PyMuPDF reads a table cell as an independent text span at its
    (x, y) origin. Our global (y, x) sort merges every cell in a row into one
    horizontal string, destroying the grid before the LLM ever sees it. The
    downstream rebuild/redact step then flattens the table visually too. By
    dropping table lines before extraction, the original table is preserved
    visually (we never touch it) and the LLM outline stays clean.

    Fails soft: older PyMuPDF versions without `find_tables` return [].
    """
    try:
        finder = getattr(page, "find_tables", None)
        if finder is None:
            return []
        found = finder()
        tables = getattr(found, "tables", None) or list(found)
        zones: List[fitz.Rect] = []
        # Cap real data tables at ~30% of page height. `find_tables()` is a
        # heuristic that frequently classifies the decorative borders around
        # a whole experience section on designer CVs (Canva/Novoresume) as a
        # "table". Protecting those would strip every bullet on the page.
        # Real education/scholastic/skills grids are short (2-5 rows).
        page_h = float(page.rect.height) or 1.0
        page_w = float(page.rect.width) or 1.0
        MAX_H_RATIO = 0.30
        for t in tables:
            bbox = getattr(t, "bbox", None)
            if bbox is None:
                continue
            try:
                r = fitz.Rect(*bbox)
            except Exception:
                continue
            if (r.y1 - r.y0) / page_h > MAX_H_RATIO:
                continue  # too tall → decorative box, not a data table
            if (r.x1 - r.x0) / page_w < 0.15:
                continue  # absurdly narrow → probably a misdetect
            zones.append(r)
        return zones
    except Exception:
        # find_tables throws on malformed PDFs; treat as "no tables".
        return []


def _bbox_intersects_any(bbox: List[float], zones: List[fitz.Rect]) -> bool:
    if not zones:
        return False
    try:
        r = fitz.Rect(*bbox)
    except Exception:
        return False
    for z in zones:
        # Use centre-point containment plus rect intersection so we catch
        # both tall cells and narrow multi-line cells.
        cx = (r.x0 + r.x1) / 2.0
        cy = (r.y0 + r.y1) / 2.0
        if z.contains(fitz.Point(cx, cy)) or r.intersects(z):
            return True
    return False


# Module-level counter so callers (apply_edits) can report how many lines
# were filtered as table cells, for observability.
_LAST_EXTRACT_STATS: Dict[str, int] = {"table_lines_filtered": 0, "tables_detected": 0}


def _collect_page_lines(page: fitz.Page, pi: int) -> List[Dict[str, Any]]:
    """
    Return a y-sorted list of lines on a page, each with merged spans. Lines
    whose text strips to empty are kept and flagged as `is_marker=True` (these
    are the invisible bullet-glyphs that 'Print to PDF' PDFs emit via symbol
    fonts).

    Lines whose bbox falls inside a detected table zone are dropped entirely
    — see `_protected_table_zones` for rationale.
    """
    table_zones = _protected_table_zones(page)
    if table_zones:
        _LAST_EXTRACT_STATS["tables_detected"] += len(table_zones)

    raw: List[Dict[str, Any]] = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text  = "".join(s.get("text", "") for s in spans)
            bbox  = list(line["bbox"])
            if _bbox_intersects_any(bbox, table_zones):
                _LAST_EXTRACT_STATS["table_lines_filtered"] += 1
                continue
            raw.append({
                "text":      text,
                "bbox":      bbox,
                "spans":     spans,
                "page":      pi,
                "is_marker": not text.strip(),
            })
    raw.sort(key=lambda r: (round(r["bbox"][1], 1), round(r["bbox"][0], 1)))

    # Merge lines at near-identical y (same visual row): concatenate text,
    # union the bbox, keep spans in x-order. This fixes 'role header + date'
    # appearing as two separate lines at the same y. We walk back past any
    # marker lines that sit between same-y visual neighbours.
    merged: List[Dict[str, Any]] = []
    prev_nm: Optional[Dict[str, Any]] = None
    Y_TOL = 1.2
    for ln in raw:
        if (
            not ln["is_marker"]
            and prev_nm is not None
            and abs(ln["bbox"][1] - prev_nm["bbox"][1]) <= Y_TOL
        ):
            prev_nm["text"]  = (prev_nm["text"] + "   " + ln["text"]).strip()
            prev_nm["spans"] = prev_nm["spans"] + ln["spans"]
            prev_nm["bbox"]  = [
                min(prev_nm["bbox"][0], ln["bbox"][0]),
                min(prev_nm["bbox"][1], ln["bbox"][1]),
                max(prev_nm["bbox"][2], ln["bbox"][2]),
                max(prev_nm["bbox"][3], ln["bbox"][3]),
            ]
            continue
        merged.append(ln)
        if not ln["is_marker"]:
            prev_nm = ln
    return merged


def extract_structure(pdf_path: str) -> List[Dict[str, Any]]:
    """
    Walk a PDF and return an ordered list of sections:
      {type, heading, page, heading_bbox, lines: [{text, bbox, spans, page,
        is_marker, preceded_by_marker}, ...]}
    Section types: header, summary, experience, projects, education, skills,
    certifications, other.
    """
    doc = fitz.open(pdf_path)
    try:
        sections: List[Dict[str, Any]] = []
        current: Dict[str, Any] = {
            "type":    "header",
            "heading": "",
            "page":    0,
            "heading_bbox": None,
            "lines":   [],
        }
        marker_pending = False
        for pi, page in enumerate(doc):
            for ln in _collect_page_lines(page, pi):
                if ln["is_marker"]:
                    marker_pending = True
                    continue
                text = ln["text"].strip()
                kind = _classify_heading(text)
                if kind:
                    sections.append(current)
                    current = {
                        "type":         kind,
                        "heading":      text,
                        "page":         pi,
                        "heading_bbox": list(ln["bbox"]),
                        "lines":        [],
                    }
                    marker_pending = False
                    continue

                # P0-followup: handle merged-heading lines like
                #   "Personal Projects ApplySmart AI | Agentic AI Product Jan 2026 - Present"
                # by splitting into a synthetic heading + remainder content line.
                split = _try_split_merged_heading(text)
                if split is not None:
                    merged_kind, heading_text, remainder = split
                    sections.append(current)
                    current = {
                        "type":         merged_kind,
                        "heading":      heading_text,
                        "page":         pi,
                        "heading_bbox": list(ln["bbox"]),
                        "lines":        [],
                    }
                    # The remainder text shares the same source bbox/spans as
                    # the original merged line. Down-stream consumers rely on
                    # `bbox` being present, so we reuse it; this is cosmetically
                    # imperfect (the remainder visually starts mid-line) but it
                    # keeps the line a valid editable target.
                    current["lines"].append({
                        "text":  remainder,
                        "bbox":  ln["bbox"],
                        "spans": ln["spans"],
                        "page":  pi,
                        "preceded_by_marker": marker_pending,
                    })
                    marker_pending = False
                    continue

                entry = {
                    "text":  text,
                    "bbox":  ln["bbox"],
                    "spans": ln["spans"],
                    "page":  pi,
                    "preceded_by_marker": marker_pending,
                }
                current["lines"].append(entry)
                marker_pending = False
        sections.append(current)
    finally:
        doc.close()
    return sections


def _line_is_bold(line: Dict[str, Any]) -> bool:
    for s in line.get("spans", []):
        flags = int(s.get("flags", 0))
        name  = (s.get("font") or "").lower()
        if (flags & 16) or ("bold" in name):
            return True
    return False


def _line_is_italic(line: Dict[str, Any]) -> bool:
    for s in line.get("spans", []):
        flags = int(s.get("flags", 0))
        name  = (s.get("font") or "").lower()
        if (flags & 2) or ("italic" in name) or ("oblique" in name):
            return True
    return False


def _role_blocks(section: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Split an experience/projects section into role blocks.

    Role header = bold line, not a bullet.
    Bullets are detected by 3 tiered signals (any is sufficient to mark a
    bullet START):
      1. Line text begins with -, *, \u2022, or \u00b7
      2. Line is flagged `preceded_by_marker` (invisible bullet glyph just
         above it — common in 'Print to PDF' CVs)
      3. Fallback: first content line after the role header, or lines whose
         y-gap from previous line exceeds median inter-line gap * 1.3
    """
    lines = list(section.get("lines", []))
    roles: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None

    # Pre-compute y-gaps between consecutive content lines for fallback splits.
    gaps: List[float] = []
    for a, b in zip(lines, lines[1:]):
        gaps.append(max(0.0, b["bbox"][1] - a["bbox"][3]))
    sorted_gaps = sorted(g for g in gaps if g > 0)
    median_gap = sorted_gaps[len(sorted_gaps) // 2] if sorted_gaps else 0.0
    bullet_gap_threshold = median_gap * 1.3 if median_gap > 0 else 0.0

    prev_line: Optional[Dict[str, Any]] = None
    prev_was_bullet_text: bool = False
    # Track the leftmost x0 seen for any role header in this section.
    # Real role headers all sit at (or very near) the same left margin;
    # a "bold" line indented well to the right of that margin is virtually
    # always a wrap continuation of an indented bullet whose body text
    # also happens to be rendered in a bold font (common in marketing /
    # comms CVs — see Shrestha-style layout, May 2026 Run 12 diagnosis).
    header_baseline_x0: Optional[float] = None
    # Adaptive parsing: per-role flag set when any glyph-prefixed bullet
    # has been seen. If the role NEVER sees a glyph (paragraph-style
    # bullet section), we activate sentence-boundary paragraph detection
    # for that role's bullets.
    role_had_glyph_bullet: Dict[int, bool] = {}

    for ln in lines:
        text = ln["text"]
        bold = _line_is_bold(ln)
        italic = _line_is_italic(ln) and not bold

        explicit_bullet = _is_bullet(text)
        marker_bullet   = bool(ln.get("preceded_by_marker"))
        gap = 0.0
        if prev_line is not None:
            gap = max(0.0, ln["bbox"][1] - prev_line["bbox"][3])

        # Role header: bold, not bullet. (We deliberately do NOT require
        # `not marker_bullet` here — a stray marker from a previous visual row
        # must not stop us from recognising a real bold role header.)
        #
        # May 2026 (Run 12 fix): two extra guards prevent bullet wrap-lines
        # from being misclassified as fresh role headers when the bullet body
        # text is rendered in bold (Shrestha-style marketing CVs):
        #
        #   (1) Continuation guard — if the previous line was bullet body
        #       text, this line was NOT preceded by a bullet marker, the
        #       y-gap is within normal inter-line spacing, AND its x0 sits
        #       within 40pt of the previous bullet's first-line x0, then it
        #       is a wrap continuation of that bullet, not a new role.
        #
        #   (2) Indent guard — once we have observed a real role header in
        #       this section, any subsequent bold non-bullet line whose x0
        #       is more than 15pt to the right of that baseline cannot be a
        #       role header. CVs do not indent role headers; an indented
        #       bold line is bullet body emphasis.
        is_continuation = (
            prev_was_bullet_text
            and not marker_bullet
            and prev_line is not None
            and abs(ln["bbox"][0] - prev_line["bbox"][0]) <= 40
            and (median_gap == 0.0 or gap <= median_gap * 1.3)
        )
        # Indent guard: a bold line indented well past the established role
        # baseline is most likely bullet body emphasis, NOT a new role —
        # UNLESS the line itself carries a strong role-header signal
        # (date pattern or Company–Role em-dash). Multi-page CVs sometimes
        # render later role headers at a different left margin (Shrestha:
        # Ogilvy at x≈24, Genesis BCW at x≈107 on page 2). The signal-bypass
        # makes the guard layout-tolerant while still rejecting indented
        # bullet-body bold lines that lack a header signal.
        line_has_header_signal = bool(
            _DATE_HINT_RX.search(text) or _COMPANY_DASH_RX.search(text)
        )
        indent_blocks_header = (
            header_baseline_x0 is not None
            and ln["bbox"][0] > header_baseline_x0 + 15
            and not line_has_header_signal
        )
        if (
            bold
            and not explicit_bullet
            and not is_continuation
            and not indent_blocks_header
        ):
            # Adaptive parsing fix (May 2026 — Shrestha CV evidence):
            # Same-Y role-header merge. Some CVs render the role-header
            # as TWO visual fragments at the same baseline Y but at
            # opposite ends of the page:
            #     "Ogilvy – Senior Account Executive"   at x≈24  (left)
            #     "October 2024 – Present"              at x≈460 (right)
            # PyMuPDF extracts these as two separate lines because of the
            # large horizontal gap. The previous parser created TWO roles
            # for one visual line, then later silently dropped the left
            # half (no bullets attached) leaving only the date as the
            # role header — catastrophic for downstream LLM matching.
            #
            # Fix: if we're about to create a new role and the previous
            # role we just created (a) has no bullets yet, (b) is at the
            # same Y as this line (±5pt), and (c) this line is to the
            # right of the previous header's right edge, merge this line
            # into the previous role's header instead. Use a 4-space
            # separator so the merged text looks natural in logs.
            prev_role = roles[-1] if roles else None
            if (
                prev_role is not None
                and not prev_role.get("bullet_groups")
                and prev_role.get("header_line") is not None
                and abs(ln["bbox"][1] - prev_role["header_line"]["bbox"][1]) <= 5
                and ln["bbox"][0] > prev_role["header_line"]["bbox"][2]
            ):
                prev_role["header_text"] = (
                    prev_role["header_text"].rstrip()
                    + "    "
                    + text.strip()
                )
                pbb = prev_role["header_line"]["bbox"]
                prev_role["header_line"] = {
                    **prev_role["header_line"],
                    "bbox": [
                        min(pbb[0], ln["bbox"][0]),
                        min(pbb[1], ln["bbox"][1]),
                        max(pbb[2], ln["bbox"][2]),
                        max(pbb[3], ln["bbox"][3]),
                    ],
                }
                prev_was_bullet_text = False
                prev_line = ln
                continue

            cur = {
                "header_text":   text,
                "header_line":   ln,
                "bullet_groups": [],
                "sub_lines":     [],
            }
            roles.append(cur)
            if header_baseline_x0 is None or ln["bbox"][0] < header_baseline_x0:
                header_baseline_x0 = ln["bbox"][0]
            prev_was_bullet_text = False
            prev_line = ln
            continue

        # Italic sub-title directly under a fresh role header (e.g. job title)
        if italic and cur is not None and not cur["bullet_groups"]:
            cur["sub_lines"].append(ln)
            prev_was_bullet_text = False
            prev_line = ln
            continue

        # Bullet decision
        if cur is None:
            # Bullets before any role header — stash into a synthetic role.
            cur = {
                "header_text":   "",
                "header_line":   None,
                "bullet_groups": [],
                "sub_lines":     [],
            }
            roles.append(cur)

        # Adaptive paragraph-bullet detection (May 2026 — Shrestha CV).
        # When the current role has NO glyph-prefixed bullets yet (pure
        # paragraph-style section like Shrestha's "Projects"), use
        # sentence-boundary + capital-verb-start as the bullet separator.
        # This catches "Conducted research. Collaborated with teams.
        # Crafted presentations." as 3 bullets instead of 1 merged.
        cur_id = id(cur)
        had_glyph_in_role = role_had_glyph_bullet.get(cur_id, False)
        if explicit_bullet or marker_bullet:
            role_had_glyph_bullet[cur_id] = True
        is_paragraph_bullet_start = (
            not had_glyph_in_role
            and cur["bullet_groups"]
            and prev_line is not None
            and bool(_SENTENCE_END_RX.search(prev_line.get("text", "")))
            and bool(_PARAGRAPH_BULLET_START_RX.match(text))
            # Don't fire on lines that are themselves glyph-prefixed —
            # the explicit_bullet path will handle them.
            and not explicit_bullet
            and not marker_bullet
        )

        is_new_bullet = (
            explicit_bullet
            or marker_bullet
            or is_paragraph_bullet_start
            or not cur["bullet_groups"]   # first content line of role starts a bullet
            or (bullet_gap_threshold > 0 and gap > bullet_gap_threshold)
        )

        if is_new_bullet:
            _append_bullet(cur, ln)
            prev_was_bullet_text = True
        else:
            if cur["bullet_groups"]:
                _attach_continuation(cur, ln)
            else:
                cur["sub_lines"].append(ln)

        prev_line = ln

    # ── Sidebar-label filter (May 2026 — adaptive parsing) ────────────
    # On 2-column layouts, the LEFT sidebar holds project labels
    # ("Project Experience: SAP Labs", "B2B, B2C & D2C") and the RIGHT
    # column holds the real bullets. PyMuPDF interleaves them by Y, so
    # the line-walk above can promote a left-column label to bullet[0]
    # of a role (seen in Shrestha's Genesis role: bullet[0] was the
    # 28-char "Project Experience: SAP Labs" label, the 9 real bullets
    # followed). Filter bullets whose first-line X is a left-side
    # outlier from the role's dominant bullet X cluster.
    _filter_sidebar_bullets(roles)

    # ── Empty-bullet filter (May 2026 — adaptive parsing) ─────────────
    # Drop bullet groups whose text is empty / sub-3-char (caused by
    # standalone bullet-glyph lines like "▪" extracted on their own by
    # PyMuPDF). MUST happen here at the source so build_outline AND
    # apply_edits see the SAME bullets — otherwise the LLM's diff
    # indices (computed against build_outline's view) misalign with
    # apply_edits' raw view, sending rewrites to the wrong bullet slot.
    for role in roles:
        bullet_groups = role.get("bullet_groups") or []
        role["bullet_groups"] = [
            b for b in bullet_groups
            if (b.get("text") or "").strip()
            and len((b.get("text") or "").strip()) >= 3
        ]

    # ── Post-merge fragmented roles (May 2026 / Run 12 fix) ─────────────
    # On 2-column layouts (left column = sub-section labels like
    # "Experience:" / "SAP Labs" / "India", right column = bullets), the
    # parser's top-down line iteration interleaves the columns and
    # promotes left-column labels to fake role headers. We post-process
    # by merging any "role" whose header lacks ALL real-role signals
    # (no date pattern, no italic sub-title, no company-style em-dash
    # phrase) into the previous role. This collapses the noise back
    # into the right place without needing full column detection.
    return _merge_fragmented_roles(roles)


def _filter_sidebar_bullets(roles: List[Dict[str, Any]]) -> None:
    """
    Drop bullets that are actually left-sidebar labels misclassified by
    the parser. Mutates `roles` in place.

    Heuristic (only fires when 2-column layout is detected):
      - Role must have ≥3 bullets (need enough X data to cluster).
      - First-line X positions must span ≥50pt across the role's
        bullets (proves a 2-column gap, not just minor wrap variance).
      - Then: drop bullets whose first-line X is >30pt LEFT of the
        median bullet first-line X.

    Single-column CVs (e.g. Rishav's): all bullets at similar X → the
    span check is False → no filtering applied. Guaranteed no-op on
    well-formed single-column layouts.
    """
    for role in roles:
        bullets = role.get("bullet_groups") or []
        if len(bullets) < 3:
            continue
        xs = [
            b["lines"][0]["bbox"][0]
            for b in bullets
            if b.get("lines") and b["lines"]
        ]
        if len(xs) < 3:
            continue
        if max(xs) - min(xs) < 50:
            continue  # single-column layout — skip

        # Median X = dominant bullet column.
        xs_sorted = sorted(xs)
        median_x = xs_sorted[len(xs_sorted) // 2]

        kept: List[Dict[str, Any]] = []
        dropped: List[tuple] = []
        for b in bullets:
            if not b.get("lines") or not b["lines"]:
                kept.append(b)
                continue
            bx = b["lines"][0]["bbox"][0]
            # Only filter LEFT outliers (sidebar labels); right outliers
            # are usually wrap variance or right-aligned dates.
            if median_x - bx > 30:
                dropped.append((round(bx, 0), (b.get("text") or "")[:60]))
                continue
            kept.append(b)

        if dropped:
            role["bullet_groups"] = kept
            print(
                f"   🔍 sidebar filter: dropped {len(dropped)} bullet(s) "
                f"from role {role.get('header_text','')[:40]!r} at X outliers "
                f"(median X={median_x:.0f}). Dropped: {dropped[:3]}"
            )


_DATE_HINT_RX = re.compile(
    r"\b("
    r"(?:19|20)\d{2}"                                    # 1995, 2024
    r"|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"   # month names
    r"|january|february|march|april|june|july|august"
    r"|september|october|november|december"
    r"|present|current|now|today"
    r")\b",
    re.I,
)
# Match space-padded em/en dash between words, e.g. "Company \u2014 Role" or
# "Manager \u2013 Director". Hyphens (and dashes WITHOUT surrounding spaces) are
# excluded on purpose: they appear inside ordinary words ("on-time",
# "data-driven", "2020-2023") and produced false positives that promoted
# CV continuation lines (e.g. "Client: ... (on-time delivery)") to fake role
# headers. Date ranges like "2020 \u2013 2023" are detected by _DATE_HINT_RX
# separately, so we don't lose those signals by tightening this pattern.
_COMPANY_DASH_RX = re.compile(r"\w+\s+[\u2013\u2014]\s+\w+")

# Adaptive parsing (May 2026 \u2014 Shrestha CV evidence): some CVs render
# bullets as plain paragraphs (no `\u25aa` / `\u2022` / `-` glyph), separated only
# by sentence boundaries. The parser's default logic treats them as a
# single merged bullet, losing per-bullet anchors. When a role has no
# glyph-prefixed bullets yet, we activate paragraph detection: a new
# bullet starts when the previous line ended with sentence terminator
# AND the current line starts with a recognisable past-tense / present-
# participle action verb. This list is comprehensive enough to cover
# most ATS-friendly Word-style CVs without false-positive matches on
# wrap continuations (which rarely start with these verbs).
_PARAGRAPH_BULLET_START_RX = re.compile(
    r"^(?:"
    r"Built|Led|Drove|Delivered|Managed|Owned|Designed|"
    r"Developed|Implemented|Created|Authored|Defined|Established|"
    r"Architected|Engineered|Shipped|Launched|Coordinated|"
    r"Orchestrated|Spearheaded|Partnered|Collaborated|Improved|"
    r"Reduced|Increased|Achieved|Generated|Identified|Resolved|"
    r"Negotiated|Facilitated|Conducted|Analysed|Analyzed|"
    r"Streamlined|Translated|Drafted|Wrote|Researched|Initiated|"
    r"Crafted|Capitalised|Capitalized|Directed|Provided|Utilised|"
    r"Utilized|Influenced|Trained|Mentored|Supervised|Received|"
    r"Captured|Negotiated|Drafted|Spear-?headed|Negotiated|"
    r"Designing|Building|Owning|Leading|Delivering|Managing|"
    r"Driving|Authoring|Defining|Establishing|Coordinating|"
    r"Synthesised|Synthesized|Generated|Maintained|Influenced"
    r")\b"
)
_SENTENCE_END_RX = re.compile(r"[.!?;]\s*$")


def _role_header_has_signal(role: Dict[str, Any]) -> bool:
    """
    True if `role`'s header (or its italic sub_lines) carries a real
    role-header signal: a date / month / 'present', or a Company–Role
    em-dash phrase, or any italic sub-line (typical CV job-title styling).
    """
    hdr = (role.get("header_text") or "").strip()
    if not hdr:
        return False
    if _DATE_HINT_RX.search(hdr):
        return True
    if _COMPANY_DASH_RX.search(hdr):
        return True
    # Italic sub_lines under a header are almost always a job-title
    # subtitle on real role headers.
    if role.get("sub_lines"):
        return True
    # Fallback: if any sub_line carries a date hint, accept.
    for sl in role.get("sub_lines") or []:
        if _DATE_HINT_RX.search(sl.get("text") or ""):
            return True
    return False


def _merge_fragmented_roles(roles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Walk roles in order. The FIRST role is always kept (it is what the
    section heading decided). For each subsequent role, if its header
    has no real-role signal, merge its bullet_groups + sub_lines into
    the most recent kept role. Otherwise, keep it as a new role.

    This preserves all bullets — they just attach to the right role.
    """
    if not roles:
        return roles
    kept: List[Dict[str, Any]] = [roles[0]]
    for r in roles[1:]:
        if _role_header_has_signal(r):
            kept.append(r)
        else:
            anchor = kept[-1]
            anchor["bullet_groups"].extend(r.get("bullet_groups") or [])
            anchor["sub_lines"].extend(r.get("sub_lines") or [])
    return kept


def _append_bullet(role: Dict[str, Any], line: Dict[str, Any]) -> None:
    text = _strip_bullet(line["text"])
    role["bullet_groups"].append({
        "lines": [line],
        "text":  text,
        "total_char_length": len(text),
    })


def _attach_continuation(role: Dict[str, Any], line: Dict[str, Any]) -> None:
    last = role["bullet_groups"][-1]
    first_x = last["lines"][0]["bbox"][0]
    # Continuation lines share x-indent with first bullet line (tolerant 40pt).
    if abs(line["bbox"][0] - first_x) <= 40:
        last["lines"].append(line)
        continuation_text = line["text"].strip()
        last["text"] += " " + continuation_text
        last["total_char_length"] += len(continuation_text) + 1  # +1 for the space


# ─────────────────────────────────────────────────────────────
# OUTLINE — what the tailor LLM sees
# ─────────────────────────────────────────────────────────────

def _infer_summary_from_header(sections: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Some CVs omit a 'SUMMARY' heading: the summary paragraph sits inside the
    implicit 'header' section right after the name+contact lines.

    Heuristic: take the trailing run of body-text lines (>= 3 lines or >= 150
    chars total) whose first span has size < 14 pt (i.e. not the giant name
    line) and x0 near the left margin.
    """
    header = next((s for s in sections if s["type"] == "header"), None)
    if not header or not header["lines"]:
        return None
    lines = header["lines"]

    # Walk backwards collecting body-text lines until we hit name-sized text.
    trailing: List[Dict[str, Any]] = []
    for ln in reversed(lines):
        first = ln["spans"][0] if ln.get("spans") else {}
        sz = float(first.get("size", 10))
        if sz >= 14:            # Name / title — stop here.
            break
        trailing.append(ln)
    trailing.reverse()

    if not trailing:
        return None
    total_chars = sum(len(ln["text"]) for ln in trailing)
    if len(trailing) < 2 or total_chars < 120:
        return None  # probably just contact line — no summary.

    # Drop contact-like leading lines (contain @, phone digits, URLs).
    contact_rx = re.compile(r"(@|linkedin\.com|https?://|\+?\d[\d\s\-]{6,})", re.I)
    while trailing and contact_rx.search(trailing[0]["text"]):
        trailing.pop(0)
    if not trailing:
        return None

    return {
        "type":    "summary",
        "heading": "",
        "page":    trailing[0]["page"],
        "heading_bbox": None,
        "lines":   trailing,
        "synthetic": True,
    }


def build_outline(pdf_path: str) -> Dict[str, Any]:
    """
    Return a compact, LLM-friendly view of the CV:
      {
        "summary": "...",
        "roles":   [{"header": "...", "section": "experience", "bullets": [...]}, ...],
        "skills":  ["..."],
      }
    """
    sections = extract_structure(pdf_path)
    out: Dict[str, Any] = {"summary": "", "roles": [], "skills": []}

    sum_sec = next((s for s in sections if s["type"] == "summary"), None)
    if sum_sec is None:
        sum_sec = _infer_summary_from_header(sections)
    if sum_sec:
        out["summary"] = " ".join(ln["text"] for ln in sum_sec["lines"]).strip()

    for sec in sections:
        t = sec["type"]
        if t in ("experience", "projects"):
            for r in _role_blocks(sec):
                # Empty bullets are filtered inside _role_blocks now (so
                # apply_edits and build_outline see identical indices).
                if not r.get("bullet_groups"):
                    continue
                out["roles"].append({
                    "header":  r["header_text"].strip(),
                    "section": t,
                    "bullets": [
                        {"text": b["text"], "length": b.get("total_char_length", len(b["text"]))}
                        for b in r["bullet_groups"]
                    ],
                })
        elif t == "skills":
            # If the CV uses categorised skills (e.g. "Product: ... Delivery: ..."),
            # splitting by commas jumbles them. Detect & skip in that case.
            text = " ".join(ln["text"] for ln in sec["lines"])
            if re.search(r"\b[A-Z][A-Za-z &/]{2,20}:\s", text):
                out["skills"] = []   # categorised — reorder disabled for v1
            else:
                items = [s.strip() for s in re.split(r"[,;|\u2022\u00b7]", text) if s.strip()]
                out["skills"] = items
    return out


# ─────────────────────────────────────────────────────────────
# EDITING PRIMITIVES
# ─────────────────────────────────────────────────────────────

def _measure_line_gap(lines: List[Dict[str, Any]], fallback: float = 1.15) -> float:
    """
    Return the median baseline-to-baseline distance of `lines`, divided by the
    reference fontsize. This is the 'lineheight' multiplier PyMuPDF uses when
    we re-insert text — matching the original's natural spacing.

    Falls back to 1.15 if the lines are too few to measure.
    """
    if not lines or len(lines) < 2:
        return fallback
    # Gather baselines (y1 of bbox ~= descent) sorted ascending.
    ys = sorted({round(ln["bbox"][1], 2) for ln in lines if ln.get("bbox")})
    if len(ys) < 2:
        return fallback
    deltas = [ys[i+1] - ys[i] for i in range(len(ys) - 1) if ys[i+1] - ys[i] > 0]
    if not deltas:
        return fallback
    deltas.sort()
    median = deltas[len(deltas) // 2]
    # Determine reference fontsize (first body-text span on any line).
    sz = 0.0
    for ln in lines:
        for sp in ln.get("spans", []):
            if float(sp.get("size", 0)) > 0:
                sz = float(sp["size"])
                break
        if sz > 0:
            break
    if sz <= 0:
        return fallback
    multi = median / sz
    # Clamp to sane bounds so a freak measurement can't blow up the layout.
    # The original rect was sized for the measured spacing, so in theory we
    # could use any value — but PyMuPDF occasionally needs slack, and a too-
    # generous gap causes overlap with the next section. 1.30 is the safe cap.
    return max(1.0, min(multi, 1.30))


def _union_rect(bboxes: List[List[float]], pad: float = 1.0) -> fitz.Rect:
    x0 = min(b[0] for b in bboxes) - pad
    y0 = min(b[1] for b in bboxes) - pad
    x1 = max(b[2] for b in bboxes) + pad
    y1 = max(b[3] for b in bboxes) + pad
    return fitz.Rect(x0, y0, x1, y1)


# ─────────────────────────────────────────────────────────────
# Data-driven rect boundaries (Apr 28 follow-up — replaces
# hardcoded `+50` / `width-40` geometry that broke role headers
# whenever a user's CV spacing didn't match the author's template)
# ─────────────────────────────────────────────────────────────

def _all_lines_on_page(sections: List[Dict[str, Any]], page_idx: int) -> List[Dict[str, Any]]:
    """Flatten every line on `page_idx` across all sections, ordered by y0."""
    out: List[Dict[str, Any]] = []
    for sec in sections:
        # heading line (if any) — also a real line on the page
        hb = sec.get("heading_bbox")
        if hb and sec.get("page") == page_idx:
            out.append({
                "bbox": hb,
                "text": sec.get("heading", ""),
                "_kind": "heading",
                "_section_type": sec.get("type"),
            })
        for ln in sec.get("lines", []):
            if ln.get("page") != page_idx:
                continue
            out.append({
                "bbox": ln["bbox"],
                "text": ln.get("text", ""),
                "_kind": "line",
                "_section_type": sec.get("type"),
            })
    out.sort(key=lambda r: (round(r["bbox"][1], 2), round(r["bbox"][0], 2)))
    return out


def _next_y0_below(
    rect: fitz.Rect,
    lines: List[Dict[str, Any]],
    page_height: float,
    bottom_margin: float = 36.0,
) -> float:
    """
    Find the y-coordinate of the FIRST content line whose top sits below
    `rect.y1`. Returns that y0 (so callers can set `rect.y1 = result - small_gap`
    and never overflow into the next element).

    Falls back to `page_height - bottom_margin` when nothing follows on the
    page (i.e. this is the last block before the footer).

    This is the data-driven replacement for the old `rect.y1 + 50` magic
    number that would clip role headers whenever the CV's natural spacing
    happened to be tighter than 50pt.
    """
    candidates = [
        ln["bbox"][1]
        for ln in lines
        if ln["bbox"][1] > rect.y1 + 0.5  # strictly below current rect
    ]
    if candidates:
        return min(candidates)
    return max(rect.y1, page_height - bottom_margin)


def _measured_right_margin(
    lines: List[Dict[str, Any]],
    page_width: float,
    fallback: float = 40.0,
) -> float:
    """
    Return the x1-coordinate of the rightmost CONTENT on the page.

    This is the data-driven replacement for the old `page.rect.width - 40`
    margin assumption — it reads the CV's actual right-edge from where text
    already sits, instead of imposing a fixed 40pt right margin that breaks
    on CVs designed with wider/narrower margins.

    Excludes lines that sit very close to the page right edge (likely
    decorative / header artefacts) by clamping to the 95th percentile of
    observed x1 values when sample size is large enough.
    """
    if not lines:
        return page_width - fallback
    xs = sorted(ln["bbox"][2] for ln in lines)
    if len(xs) >= 5:
        # 95th percentile of content right-edges. A few outlier-wide lines
        # (e.g. a header rule line) shouldn't dictate the wrap margin for
        # the body block we're editing.
        idx = max(0, int(0.95 * len(xs)) - 1)
        return xs[idx]
    return xs[-1]


def detect_replica_compatibility(pdf_path: str) -> Dict[str, Any]:
    """
    Sniff the PDF's layout to decide whether the in-place replica path is
    likely to produce a clean output, or whether we should bail out and use
    the rebuild path immediately.

    Returns a dict:
      {
        "compatible":   bool,
        "reason":       "single-column" | "multi-column" |
                        "image-heavy" | "scanned" | "empty",
        "n_columns":    int,
        "image_ratio":  float,
        "n_text_lines": int,
      }

    Apr 28 follow-up: prevents replica corruption on designer / two-column /
    image-based CVs that we'd otherwise attempt to edit in-place and ship
    visibly broken (e.g. cross-column text bleed, font-substitution clipping,
    extracted-text gaps from image-only pages). When `compatible=False` the
    caller should skip `apply_pdf_edits` entirely and route to the rebuild
    path.
    """
    result: Dict[str, Any] = {
        "compatible":   True,
        "reason":       "single-column",
        "n_columns":    1,
        "image_ratio":  0.0,
        "n_text_lines": 0,
    }
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        result["compatible"] = False
        result["reason"] = f"open-failed: {type(e).__name__}"
        return result

    try:
        if doc.page_count == 0:
            result["compatible"] = False
            result["reason"] = "empty"
            return result

        # We only need the first 1-2 pages — most replica-incompatible CVs
        # show their layout pattern on page 1 alone.
        pages_to_check = min(2, doc.page_count)
        all_x0:     List[float] = []
        n_lines     = 0
        image_area  = 0.0
        page_area   = 0.0

        for pi in range(pages_to_check):
            page = doc[pi]
            page_area += float(page.rect.width) * float(page.rect.height)

            # Image area
            try:
                for img in page.get_images(full=True):
                    # Each img tuple: (xref, smask, width, height, ...)
                    rects = page.get_image_rects(img[0]) or []
                    for r in rects:
                        image_area += float(r.width) * float(r.height)
            except Exception:
                pass

            # Text lines and their x0 positions
            for block in page.get_text("dict")["blocks"]:
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    spans = line.get("spans", [])
                    text = "".join(s.get("text", "") for s in spans).strip()
                    if not text:
                        continue
                    x0 = float(line["bbox"][0])
                    all_x0.append(x0)
                    n_lines += 1

        result["n_text_lines"] = n_lines
        result["image_ratio"]  = round(image_area / page_area, 3) if page_area > 0 else 0.0

        # Scanned / image-only PDF — almost no extractable text
        if n_lines < 8:
            result["compatible"] = False
            result["reason"] = "scanned"
            return result

        # Image-heavy designer template — treat like rebuild candidate
        if result["image_ratio"] > 0.30:
            result["compatible"] = False
            result["reason"] = "image-heavy"
            return result

        # Column detection: cluster x0 values into buckets of width 25pt and
        # count how many buckets contain >= 15% of all lines. A single-column
        # CV has one dominant cluster; a two-column CV has two.
        if all_x0:
            buckets: Dict[int, int] = {}
            for x in all_x0:
                key = int(x // 25)
                buckets[key] = buckets.get(key, 0) + 1
            min_for_col = max(3, int(0.15 * n_lines))
            dominant = [b for b, c in buckets.items() if c >= min_for_col]
            # Cluster adjacent buckets — a wrapped paragraph spans 1-2
            # adjacent buckets but it's still ONE column. Group buckets
            # within 2 of each other.
            if dominant:
                dominant.sort()
                groups = [[dominant[0]]]
                for b in dominant[1:]:
                    if b - groups[-1][-1] <= 2:
                        groups[-1].append(b)
                    else:
                        groups.append([b])
                result["n_columns"] = len(groups)
                if len(groups) >= 2:
                    result["compatible"] = False
                    result["reason"] = "multi-column"
                    return result

        return result
    finally:
        try:
            doc.close()
        except Exception:
            pass


def _is_symbolic_font_name(name: str) -> bool:
    """
    Heuristic: fonts named Symbol/Wingdings/Dingbats/ZapfDingbats etc. carry
    non-Latin glyph tables and must never be used as the reference font for
    inserting Latin text.
    """
    n = (name or "").lower()
    return any(k in n for k in (
        "symbol", "wingding", "dingbat", "webding", "mtextra",
    ))


def _span_is_text_carrying(span: dict) -> bool:
    """
    True if this span actually carries body text (not just a bullet glyph).
    We strip bullet characters and check for any remaining non-whitespace.
    """
    t = (span.get("text") or "")
    stripped = _strip_bullet(t).strip()
    if not stripped:
        return False
    if _is_symbolic_font_name(span.get("font") or ""):
        return False
    return True


def _pick_body_span(lines: List[Dict[str, Any]]) -> Optional[dict]:
    """
    Find a body-text span across `lines` to use as the style reference
    for re-inserting tailored text.

    Adaptive fix (May 2026 — Shrestha CV evidence): prefer NON-BOLD
    spans. Many CV templates bold the FIRST few words of each bullet for
    emphasis ("Defined and executed integrated digital strategy..."
    starts bold, wrap lines are regular weight). The old logic returned
    the first text-carrying span — the bold one — and `_insert_fitted`
    then rendered the entire rewrite in bold. The recruiter sees every
    bullet body bolded, which looks broken.

    New policy:
      1. Collect every text-carrying span across all lines.
      2. Prefer the first NON-bold one (matches body wrap-line style).
      3. Fall back to the first text-carrying span (bold or otherwise)
         if no non-bold span exists (rare; all-bold sections).
      4. Last resort: the very first span we saw (preserves the old
         "always return something" contract).
    """
    first_any: Optional[dict] = None
    first_textcarrying: Optional[dict] = None
    for ln in lines:
        for s in ln.get("spans") or []:
            if first_any is None:
                first_any = s
            if not _span_is_text_carrying(s):
                continue
            if first_textcarrying is None:
                first_textcarrying = s
            # Check bold-ness: PDF font flags bit 16 = bold, OR font name
            # contains "bold". Skip; we want non-bold.
            font_name = (s.get("font") or "").lower()
            is_bold = bool(int(s.get("flags", 0)) & 16) or "bold" in font_name
            if not is_bold:
                return s
    return first_textcarrying or first_any


def _first_span_of_lines(lines: List[Dict[str, Any]]) -> Optional[dict]:
    """Kept for backwards compatibility; prefers body-text spans."""
    return _pick_body_span(lines)


def _summaries_equivalent(a: str, b: str) -> bool:
    """
    True when two summary strings carry the same content modulo whitespace,
    case, and typographic dash variants. Used by apply_edits to short-circuit
    the redact-and-insert path when a credential / identity guard has
    reverted a rewritten summary back to the original PDF text — in which
    case redacting and re-inserting the same text can silently fail
    (tighter rect than the original draw region) and leave the summary
    block blank.
    """
    def _norm(s: str) -> str:
        if not s:
            return ""
        # Unify typographic dashes, collapse whitespace, lower-case.
        s2 = s.replace("\u2013", "-").replace("\u2014", "-")
        return " ".join(s2.lower().split())
    return _norm(a) == _norm(b)


def _apply_summary_edit(
    doc:         "fitz.Document",
    sections:    List[Dict[str, Any]],
    sum_sec:     Dict[str, Any],
    new_summary: str,
    font_cache:  Dict[int, str],
    report:      Dict[str, Any],
) -> None:
    """
    Redact the existing summary block and draw `new_summary` in its place.

    Safety net (May 2026 / Run 12 fix): if the final insert_textbox call
    returns fontsize=0 (couldn't fit at any size in the shrink ladder),
    re-draw the ORIGINAL summary text instead of leaving an empty box.
    Protects against the silent-empty-summary regression seen when the
    reverted summary is nominally different but geometrically equivalent
    to the original.
    """
    # ── P4 (Apr 28): Summary overflow guard ────────
    # Reject dangerously long rewrites BEFORE touching the PDF.
    orig_text = " ".join(ln["text"] for ln in sum_sec["lines"]).strip()
    orig_words = len(orig_text.split())
    new_words  = len(new_summary.split())
    overflow_ratio = (new_words / orig_words) if orig_words >= 10 else 0.0
    if overflow_ratio > 1.4:
        msg = (
            f"summary: rewrite is {new_words} words vs "
            f"{orig_words} original ({overflow_ratio:.1f}\u00d7) "
            f"\u2014 rejected to prevent layout overflow"
        )
        print(f"   \U0001f6e1\ufe0f  pdf_editor: {msg}")
        report["skipped"].append(msg)
        report.setdefault("_debug", {})["summary_overflow_rejected"] = {
            "orig_words": orig_words,
            "new_words":  new_words,
            "ratio":      round(overflow_ratio, 2),
        }
        return

    # P1-1 (May 2026): Cross-page summary guard. When the summary spans
    # multiple pages, redacting only the first page's bbox leaves the
    # tail orphaned on page 2 with no matching insert. Skip the in-place
    # edit and let the caller (rebuild path) regenerate the CV cleanly.
    summary_pages = {ln["page"] for ln in sum_sec["lines"]}
    if len(summary_pages) > 1:
        msg = (
            f"summary: spans {len(summary_pages)} pages "
            f"({sorted(summary_pages)}) — in-place edit cannot safely "
            f"redact across page boundaries; skipping rewrite."
        )
        print(f"   \u26a0\ufe0f  pdf_editor: {msg}")
        report["skipped"].append(msg)
        report.setdefault("_debug", {})["summary_cross_page"] = {
            "pages": sorted(summary_pages),
        }
        return

    page_idx = sum_sec["lines"][0]["page"]
    page = doc[page_idx]
    rect = _union_rect([ln["bbox"] for ln in sum_sec["lines"]], pad=1.5)

    # Data-driven rect boundaries (exclude self from right-margin measure).
    page_lines = _all_lines_on_page(sections, page_idx)
    own_y_min = min(ln["bbox"][1] for ln in sum_sec["lines"]) - 0.5
    own_y_max = max(ln["bbox"][3] for ln in sum_sec["lines"]) + 0.5
    other_lines = [
        ln for ln in page_lines
        if not (own_y_min <= ln["bbox"][1] <= own_y_max)
    ]
    rect.x1 = max(rect.x1, _measured_right_margin(other_lines, page.rect.width))
    # Next content line strictly below the summary's TRUE text bottom.
    # Measuring from the un-padded bottom matters: _union_rect's 1.5pt
    # pad can otherwise reach into a heading sitting flush under the
    # summary (e.g. "Personal Projects" on tightly-spaced CVs), which
    # _next_y0_below would then skip — and the rect would redact it away.
    summary_bottom = max(ln["bbox"][3] for ln in sum_sec["lines"])
    next_tops = [ln["bbox"][1] for ln in other_lines
                 if ln["bbox"][1] > summary_bottom + 0.3]
    next_y0 = min(next_tops) if next_tops else (page.rect.height - 36.0)
    # Extend y1 down into the whitespace below the summary (up to 8pt —
    # invisible, gives _insert_fitted room to land the rewrite) but
    # HARD-CAP it 0.5pt short of the next line so adjacent content is
    # never touched by the redaction.
    rect.y1 = min(rect.y1 + 8.0, next_y0 - 0.5)

    ref = _first_span_of_lines(sum_sec["lines"])
    if ref is None:
        report["skipped"].append("summary: no reference span")
        return

    measured = _measure_line_gap(sum_sec["lines"])
    summary_first_top = min(ln["bbox"][1] for ln in sum_sec["lines"])
    summary_first_left = min(ln["bbox"][0] for ln in sum_sec["lines"])
    summary_right = max(ln["bbox"][2] for ln in sum_sec["lines"])
    # Match the original's alignment: justified summaries are re-justified,
    # ragged-left ones stay left-aligned.
    summary_align = 3 if _is_block_justified(sum_sec["lines"]) else 0
    # Capture any border / divider lines inside the redact rect so they
    # can be re-drawn after the white-fill redaction wipes them (e.g.
    # the page-frame border on Shrestha-style 2-column layouts, or the
    # rule under the "Professional Summary" heading).
    saved_borders = _capture_borders_in_rect(page, rect)
    _redact_rect(page, rect)
    sz = _insert_fitted(
        page, rect, new_summary, ref, align=summary_align,
        line_gap=measured, orig_first_top=summary_first_top,
        orig_left=summary_first_left, orig_right=summary_right,
        doc=doc, font_cache=font_cache,
    )
    _redraw_borders(page, saved_borders)
    if sz > 0:
        report["applied"]["summary"] = {"fontsize": sz, "line_gap": round(measured, 3)}
        return

    # ── Silent-empty guard (May 2026 / Run 12 fix) ─────────────────
    # insert_textbox returned rc<0 at every size in the shrink ladder.
    # Rather than ship an empty redacted rectangle, restore the ORIGINAL
    # summary text so the PDF at worst carries unchanged content.
    print(
        "   \u26a0\ufe0f  pdf_editor: summary rewrite did not fit at any "
        "font size; restoring original summary to avoid empty box."
    )
    sz_fallback = _insert_fitted(
        page, rect, orig_text, ref, align=summary_align,
        line_gap=measured, orig_first_top=summary_first_top,
        orig_left=summary_first_left, orig_right=summary_right,
        doc=doc, font_cache=font_cache,
    )
    _redraw_borders(page, saved_borders)
    if sz_fallback > 0:
        report["applied"]["summary"] = {
            "fontsize": sz_fallback,
            "line_gap": round(measured, 3),
            "fallback": "restored_original",
        }
    else:
        # Absolute worst case: mention in report so the caller can
        # surface a UI warning. The PDF will have an empty summary box.
        report["skipped"].append("summary: rewrite AND fallback both overflowed")
        report.setdefault("_debug", {})["summary_empty_fallback"] = True


def _redact_rect(page: fitz.Page, rect: fitz.Rect) -> None:
    page.add_redact_annot(rect, fill=(1, 1, 1))
    # Keep images/graphics; only remove text+fill.
    page.apply_redactions(
        images=fitz.PDF_REDACT_IMAGE_NONE,
        graphics=fitz.PDF_REDACT_LINE_ART_NONE,
    )


def _wrap_to_width(
    text: str, font: fitz.Font, fontsize: float, max_width: float,
) -> List[str]:
    """
    Greedy word-wrap `text` to `max_width` points, measured with the real
    font. Explicit newlines are honoured as hard breaks. A single word
    wider than `max_width` is kept on its own line (not split mid-word).
    """
    out: List[str] = []
    for para in (text or "").split("\n"):
        words = para.split()
        if not words:
            out.append("")
            continue
        cur = words[0]
        for w in words[1:]:
            trial = f"{cur} {w}"
            try:
                fits = font.text_length(trial, fontsize) <= max_width
            except Exception:
                fits = len(trial) * fontsize * 0.5 <= max_width
            if fits:
                cur = trial
            else:
                out.append(cur)
                cur = w
        out.append(cur)
    return out


def _is_block_justified(lines: List[Dict[str, Any]]) -> bool:
    """
    True when the original block's lines are flush-right (justified) — the
    non-last lines all end at nearly the same x1. Needs >=3 lines: a 1-2
    line block cannot be told apart from a ragged-left one.
    """
    rows = [ln["bbox"] for ln in lines if ln.get("bbox")]
    if len(rows) < 3:
        return False
    x1s = [b[2] for b in rows[:-1]]          # every line except the last
    return (max(x1s) - min(x1s)) <= 2.5


def _render_block_textwriter(
    page:      fitz.Page,
    rect:      fitz.Rect,
    text:      str,
    font:      fitz.Font,
    fontsize:  float,
    color:     tuple,
    pitch:     float,
    first_top: float,
    left:      float,
    align:     int = 0,
    ascender:  Optional[float] = None,
    right:     Optional[float] = None,
) -> bool:
    """
    Word-wrap `text` to the slot width (`rect.x1` - `left`) and draw it
    with a fitz.TextWriter: every glyph in `font` at exactly `fontsize`,
    consecutive baselines spaced by exactly `pitch` points, the first
    baseline pinned so the block starts where the original first line did
    (`first_top` is that line's bbox-top, `left` its bbox-left).

    Replaces page.insert_textbox, whose `lineheight` parameter does not
    map linearly to rendered pitch on PyMuPDF 1.27 — passing the measured
    multiplier rendered every edited block ~25% too tight.

    Returns True on success; False — WITHOUT drawing — when the wrapped
    block is taller than `rect` (the caller must then revert the region).

    `ascender` overrides the baseline-placement ascender — needed when
    `font` is a metric-compatible CLONE of the original (the clone shares
    glyph widths but not vertical metrics, so the original family's
    ascender must place the baseline where the original text sat).
    """
    eff_right = right if right is not None else rect.x1
    avail_w   = max(1.0, eff_right - left)
    lines = _wrap_to_width(text, font, fontsize, avail_w)
    if not any(ln.strip() for ln in lines):
        return False
    asc      = float(ascender) if ascender is not None else float(font.ascender)
    desc_abs = abs(float(font.descender))
    n        = len(lines)
    baseline0    = first_top + asc * fontsize
    block_bottom = baseline0 + (n - 1) * pitch + desc_abs * fontsize
    # Hard reject: an over-long rewrite must not spill past its slot.
    if block_bottom > rect.y1 + 1.0:
        return False
    # Last non-empty line — a justified paragraph leaves it ragged (left);
    # only the lines above are stretched to the full width.
    last_real = max((i for i, ln in enumerate(lines) if ln.strip()), default=-1)
    sp_w = font.text_length(" ", fontsize)
    tw = fitz.TextWriter(page.rect)
    for i, ln in enumerate(lines):
        if not ln:
            continue
        y = baseline0 + i * pitch
        # align 3 = justified: stretch inter-word gaps so the line spans
        # the full width (every line except the last real one).
        if align == 3 and i != last_real:
            words = ln.split()
            if len(words) > 1:
                wlens   = [font.text_length(w, fontsize) for w in words]
                natural = sum(wlens) + sp_w * (len(words) - 1)
                extra   = avail_w - natural
                if extra > 0:
                    gap = sp_w + extra / (len(words) - 1)
                    xx  = left
                    last_wi = len(words) - 1
                    for wi, w in enumerate(words):
                        # Keep a real space char in each piece (except the
                        # last word) — word-by-word placement alone fuses
                        # the words in the extracted text layer, which
                        # breaks ATS parsing and copy-paste.
                        piece = w if wi == last_wi else w + " "
                        tw.append((xx, y), piece, font=font, fontsize=fontsize)
                        xx += wlens[wi] + gap
                    continue
        x = left
        if align in (1, 2):
            try:
                line_w = font.text_length(ln, fontsize)
            except Exception:
                line_w = 0.0
            if align == 1:        # centred
                x = left + max(0.0, (avail_w - line_w) / 2.0)
            else:                 # right-aligned
                x = max(left, eff_right - line_w)
        tw.append((x, y), ln, font=font, fontsize=fontsize)
    tw.write_text(page, color=color)
    return True


def _insert_fitted(
    page:      fitz.Page,
    rect:      fitz.Rect,
    text:      str,
    ref_span:  dict,
    align:     int = 0,
    line_gap:  float = 1.05,  # matches typical CV line-spacing (~1.07).
    doc:       Optional[fitz.Document] = None,
    font_cache: Optional[Dict[int, str]] = None,
    orig_first_top: Optional[float] = None,
    orig_left: Optional[float] = None,
    orig_right: Optional[float] = None,
) -> float:
    """
    Insert `text` into `rect` in ref_span's exact style — original
    embedded font, original size, original line pitch — using a
    fitz.TextWriter with manual word-wrapping (see _render_block_textwriter).

    99% format mode (final architecture): NO font shrinkage, NO line-gap
    compression. The text is drawn at the original span's font size and
    at the original block's measured line pitch. If the rewrite wraps to
    more lines than the slot can hold, this returns 0 and the caller MUST
    revert the region to its original text — the hard-reject path that
    preserves typography exactly.

    Why TextWriter and not page.insert_textbox: insert_textbox owns its
    own line-wrapping and its `lineheight` parameter does not map linearly
    to rendered pitch on PyMuPDF 1.27 (it folds in the font ascender), so
    passing the measured multiplier rendered every edited block ~25% too
    tight ("text looks sticked together"). TextWriter places each baseline
    at an exact y.

    `orig_first_top` is the original block's first-line bbox-top; the
    re-inserted text's first baseline is pinned to it so the edited block
    begins exactly where the original did.

    Returns the fontsize used (== base_size) on success, or 0 on overflow.
    """
    base_size = float(ref_span.get("size", 10) or 10)
    color     = _int_color_to_rgb(int(ref_span.get("color", 0)))

    # Original block line pitch, in points. `line_gap` is the multiplier
    # measured by _measure_line_gap (median baseline distance / fontsize),
    # so `pitch` reproduces the original's natural line spacing exactly.
    pitch = max(1.0, float(line_gap)) * base_size

    # First-line bbox-top of the original block. Callers pass it from the
    # original line geometry; fall back to the summary/skills rect padding
    # (_union_rect uses pad=1.5) when it is not supplied.
    if orig_first_top is None:
        orig_first_top = rect.y0 + 1.5
    # Left edge of the original text. Callers pass the original block's
    # bbox-left; fall back to rect.x0 (already the true left for the
    # bullet path, whose rect is not padded on the x-axis).
    if orig_left is None:
        orig_left = rect.x0

    # ── Attempt 1: original embedded font (preserves look + unicode) ──
    if doc is not None and font_cache is not None:
        alias = _install_original_font(doc, page, ref_span, font_cache, text=text)
        if alias:
            emb_font = None
            try:
                xref = int(str(alias)[3:])          # alias is f"emb{xref}"
                cached = font_cache.get(f"fontobj:{xref}")
                if isinstance(cached, fitz.Font):
                    emb_font = cached
                else:
                    buf = font_cache.get(f"buf:{xref}")
                    if isinstance(buf, (bytes, bytearray)):
                        emb_font = fitz.Font(fontbuffer=bytes(buf))
                        font_cache[f"fontobj:{xref}"] = emb_font
            except Exception:
                emb_font = None
            if emb_font is not None:
                ok = _render_block_textwriter(
                    page, rect, text, emb_font, base_size, color,
                    pitch, orig_first_top, orig_left, align,
                    right=orig_right,
                )
                # A non-None alias guarantees the embedded font installed
                # and covers every glyph. Fit or not, do NOT fall through
                # to the Base14 path: swapping the candidate's font (e.g.
                # Calibri -> Helvetica) renders visibly wrong. On overflow
                # return 0 so the caller keeps the ORIGINAL text, already
                # in the correct font / size / pitch.
                return base_size if ok else 0.0
            # Embedded buffer unexpectedly unavailable — fall through.

    # ── Attempt 1.5: bundled metric-compatible clone ──
    # The CV's own font could not be reused. Before Base14 (which does
    # not resemble Calibri / Cambria), try a bundled clone matched to the
    # original family — same glyph widths, near-identical look.
    clone = _bundled_clone_font(ref_span, text, font_cache)
    if clone is not None:
        clone_font, clone_ascender = clone
        ok = _render_block_textwriter(
            page, rect, text, clone_font, base_size, color,
            pitch, orig_first_top, orig_left, align,
            ascender=clone_ascender, right=orig_right,
        )
        return base_size if ok else 0.0

    # ── Attempt 2: built-in Base14 (PyMuPDF mapping) ──
    # PyMuPDF's built-in Latin-1 Base14 fonts silently render chars outside
    # the WinAnsi table as `?`. Map the common typographic glyphs we see in
    # real CVs (arrows, en/em dashes, curly quotes, ellipsis, bullets) to
    # ASCII/WinAnsi equivalents so the output stays legible.
    _UNICODE_FALLBACK = {
        # Note: U+2022 (•) is NOT downgraded — it sits in WinAnsi at 0x95 and
        # all Base14 Latin-1 fonts render it correctly. Previous code mapped
        # it to U+00B7 (·), which caused inconsistent bullet glyphs between
        # sections whenever the embedded-font path fell back here.
        "\u2192": "->",       # →      → ->
        "\u2190": "<-",       # ←      → <-
        "\u2194": "<->",      # ↔
        "\u2013": "-",        # –  en-dash
        "\u2014": "-",        # —  em-dash (WinAnsi has 0x97 but rendering is patchy)
        "\u2026": "...",      # …  ellipsis
        "\u201c": '"', "\u201d": '"',   # curly double quotes
        "\u2018": "'", "\u2019": "'",   # curly single quotes
        "\u00d7": "x",        # ×
    }
    builtin = _BUILTIN[_font_key(ref_span)]
    safe_text = text
    for src, dst in _UNICODE_FALLBACK.items():
        if src in safe_text:
            safe_text = safe_text.replace(src, dst)
    # Bullet-glyph normalisation in the Base14 fallback path.
    #
    # Apr 30 (Run-2 evidence): the previous policy ("keep U+2022 because
    # Base14 helv maps it at WinAnsi 0x95") proved unreliable on the
    # PyMuPDF build we ship — every tailored CV across two test runs
    # rendered • as `?` in the bullet column. Some Base14 builds don't
    # honour the WinAnsi 0x95 mapping for U+2022; PyMuPDF then draws
    # .notdef, which most readers display as `?`.
    #
    # PyMuPDF's `insert_textbox` returns rc>=0 for "the text fit" — it
    # does NOT tell us whether any glyph was substituted with .notdef. So
    # we cannot detect-and-retry; we must downgrade pre-emptively.
    #
    # New policy: in the Base14 fallback path, ALWAYS downgrade U+2022
    # to U+00B7 (·, middle dot). U+00B7 is at WinAnsi 0xB7 which is the
    # standard Latin-1 position and IS reliably mapped on every Base14
    # build we've tested. Visual cost: a slightly smaller dot than the
    # original • for bullets in REWRITTEN sections only — original bullets
    # in untouched sections remain • (they are never re-rendered, just
    # left in the PDF's original text layer). This is strictly better
    # than the current `?` outcome.
    for ch in _BULLET_CHARS:
        if ch in ("-", "*"):
            continue
        # Map every bullet-shape char (including U+2022) to · in this
        # fallback path.
        safe_text = safe_text.replace(ch, "\u00b7")
    try:
        b14_font = fitz.Font(builtin)
    except Exception:
        return 0.0
    ok = _render_block_textwriter(
        page, rect, safe_text, b14_font, base_size, color,
        pitch, orig_first_top, orig_left, align,
        right=orig_right,
    )
    return base_size if ok else 0.0


# ─────────────────────────────────────────────────────────────
# TOP-LEVEL API
# ─────────────────────────────────────────────────────────────

def apply_edits(
    pdf_path:    str,
    edits:       Dict[str, Any],
    output_path: str,
) -> Dict[str, Any]:
    """
    Apply structured edits to the PDF in-place and save to output_path.

    edits = {
      "summary":      "new summary text" | None,
      "bullets":      {
                        "<role header>": [
                           {"i": <orig_idx>, "text": "new wording" | None},
                           ...
                        ],
                        ...
                      }
                      (legacy: also accepts [<orig_idx>, ...] — equivalent
                      to each index with text=None),
      "skills_order": ["Skill1", "Skill2", ...] | None,
    }

    Behaviour:
      - Indices not listed in the per-role order are DROPPED from the PDF.
      - When "text" is provided, the bullet is re-rendered with that wording;
        when None/absent, the original bullet text is preserved.

    Returns a report: {"applied": {...}, "skipped": [...]}.
    """
    doc = fitz.open(pdf_path)
    report: Dict[str, Any] = {"applied": {}, "skipped": []}
    font_cache: Dict[int, str] = {}
    # Reset per-run extract stats so the report reflects THIS job only.
    _LAST_EXTRACT_STATS["table_lines_filtered"] = 0
    _LAST_EXTRACT_STATS["tables_detected"] = 0
    try:
        sections = extract_structure(pdf_path)

        # ── SUMMARY ────────────────────────────────────────────
        new_summary = _normalize_for_ats((edits.get("summary") or "").strip())
        if new_summary:
            sum_sec = next((s for s in sections if s["type"] == "summary"), None)
            if sum_sec is None:
                sum_sec = _infer_summary_from_header(sections)
            if sum_sec and sum_sec["lines"]:
                # ── Trivial-summary short-circuit (May 2026 / Run 12 fix) ──
                # When the credential / identity / foreign-term guards in
                # cv_diff_tailor revert a rewritten summary to the original
                # text, `new_summary` arrives equal to the existing PDF
                # text. The redact-then-insert path that follows can fail
                # to fit the same text into a slightly tighter rect (the
                # next-sibling y1 cap shrinks the box by 2pt vs. the
                # natural draw region), leaving the summary block redacted
                # but empty — see Run 12 Shrestha output. Skip the whole
                # path when the summary is unchanged: zero risk of empty
                # box, zero token cost.
                _orig_for_compare = " ".join(
                    ln["text"] for ln in sum_sec["lines"]
                ).strip()
                if _summaries_equivalent(new_summary, _orig_for_compare):
                    report["applied"]["summary"] = {"skipped": "unchanged"}
                else:
                    _apply_summary_edit(
                        doc, sections, sum_sec, new_summary,
                        font_cache, report,
                    )
                # Drop into the unchanged branch. Both paths leave the
                # original PDF intact in the trivial case; the rewrite
                # path runs only when there's a real text delta.
                pass
            else:
                report["skipped"].append("summary: section not found")

        # ── BULLETS ────────────────────────────────────────────
        bullet_edits: Dict[str, Any] = edits.get("bullets") or {}
        if bullet_edits:
            applied_roles: List[str] = []
            n_rewrites_total = 0
            n_dropped_total  = 0
            exp_sections = [s for s in sections if s["type"] in ("experience", "projects")]
            for sec in exp_sections:
                for role in _role_blocks(sec):
                    header = (role.get("header_text") or "").strip()
                    if not header:
                        continue
                    order = _match_role_order(header, bullet_edits)
                    if order is None:
                        continue
                    bullets = role.get("bullet_groups") or []
                    if not bullets:
                        continue

                    # Normalise order entries to [{"i": int, "text": str|None}, ...].
                    # Accepts legacy [int, int, ...] and new [{"i":..,"text":..}, ...].
                    normalised: List[Dict[str, Any]] = []
                    seen: set = set()
                    for item in order:
                        if isinstance(item, dict):
                            try:
                                idx = int(item.get("i"))
                            except (TypeError, ValueError):
                                continue
                            text = item.get("text")
                            if not isinstance(text, str) or not text.strip():
                                text = None
                        else:
                            try:
                                idx = int(item)
                                text = None
                            except (TypeError, ValueError):
                                continue
                        if 0 <= idx < len(bullets) and idx not in seen:
                            normalised.append({"i": idx, "text": text})
                            seen.add(idx)

                    if not normalised:
                        continue

                    # Issue 5 (May 2026): dedupe rewrites with >70% token
                    # overlap. The LLM occasionally produces two rewrites
                    # for different bullet slots whose content is near-
                    # identical (e.g. "Capitalised on digital trends..." at
                    # i=4 AND i=7). Keep the FIRST occurrence as rewrite;
                    # revert subsequent duplicates to original text.
                    _DUP_OVERLAP_THRESHOLD = 0.70
                    rewrite_token_sets: List[set] = []
                    for e in normalised:
                        if e["text"] is None:
                            rewrite_token_sets.append(set())
                            continue
                        tokens = {
                            t for t in re.split(r"[^a-z0-9]+", e["text"].lower())
                            if len(t) >= 3
                        }
                        # Compare against earlier rewrites in this role.
                        is_duplicate = False
                        for prev_tokens in rewrite_token_sets:
                            if not prev_tokens or not tokens:
                                continue
                            overlap = len(tokens & prev_tokens) / max(
                                1, min(len(tokens), len(prev_tokens))
                            )
                            if overlap >= _DUP_OVERLAP_THRESHOLD:
                                is_duplicate = True
                                break
                        if is_duplicate:
                            print(
                                f"   ⚠️  apply_edits: bullet i={e['i']} in "
                                f"role {header[:40]!r} duplicates an earlier "
                                f"rewrite (token overlap ≥{_DUP_OVERLAP_THRESHOLD}) "
                                f"— reverting to original."
                            )
                            e["text"] = None
                            rewrite_token_sets.append(set())
                        else:
                            rewrite_token_sets.append(tokens)

                    # Issue 6 (May 2026): auto-include missing bullet indices
                    # as text=null instead of silently dropping them. The
                    # tailor LLM occasionally returns only the indices it
                    # rewrote (e.g. [0, 1, 4, 7, 11]) — the legacy behaviour
                    # was to DROP bullets 2, 3, 5, 6, 8, 9, 10, 12, 13 from
                    # the PDF entirely. For the Shrestha+Ogilvy case this
                    # silently lost "Led a team of copywriters, designers,
                    # and executives..." which is critical evidence of
                    # Manager-level experience. New rule: any bullet index
                    # not explicitly listed by the LLM is preserved at its
                    # ORIGINAL position with text=null (keep original).
                    listed_indices = {e["i"] for e in normalised}
                    auto_added: List[int] = []
                    for i in range(len(bullets)):
                        if i not in listed_indices:
                            normalised.append({"i": i, "text": None})
                            auto_added.append(i)
                    if auto_added:
                        # Re-sort so original order is preserved when the
                        # LLM didn't explicitly request a reorder.
                        normalised.sort(key=lambda e: e["i"])
                        print(
                            f"   🛡️  apply_edits: auto-added {len(auto_added)} "
                            f"missing bullet indices {auto_added} in role "
                            f"{header[:40]!r} (LLM didn't list them — "
                            f"keeping their original text)."
                        )

                    # Detect no-op: same order as original AND no rewrites.
                    trivial = (
                        len(normalised) == len(bullets)
                        and all(
                            e["i"] == i and e["text"] is None
                            for i, e in enumerate(normalised)
                        )
                    )
                    if trivial:
                        continue

                    # P1-1 (May 2026): Cross-page bullet group guard. When
                    # the bullet block straddles a page break, a single
                    # _union_rect on page A leaves any bullet lines on
                    # page B orphaned + un-redacted, while the rewritten
                    # text gets squeezed onto page A only. Skip the
                    # rewrite for that role and surface in the report so
                    # the reviewer can decide whether to retry or accept.
                    bullet_pages = {
                        ln["page"] for b in bullets for ln in b["lines"]
                    }
                    if len(bullet_pages) > 1:
                        msg = (
                            f"role {header[:40]!r}: bullet group spans "
                            f"{len(bullet_pages)} pages "
                            f"({sorted(bullet_pages)}) — in-place edit "
                            f"cannot redact across page boundary; "
                            f"keeping role original."
                        )
                        print(f"   \u26a0\ufe0f  pdf_editor: {msg}")
                        report["skipped"].append(msg)
                        continue

                    page_idx = bullets[0]["lines"][0]["page"]
                    page = doc[page_idx]
                    page_lines = _all_lines_on_page(sections, page_idx)

                    # PER-BULLET IN-PLACE INSERTION (May 2026 rewrite).
                    # Each rewritten bullet is redacted + re-inserted in
                    # ITS OWN rect, instead of redacting the whole role
                    # block and re-flowing every bullet through one shared
                    # textbox. The old block approach collapsed every
                    # bullet to the leftmost X (broke 2-column layouts),
                    # used one line-gap for the whole block, forced a
                    # single inserted glyph, and re-rendered even unchanged
                    # bullets. Per-bullet insertion keeps each bullet at its
                    # own X / Y / line-gap, leaves the original bullet glyph
                    # untouched, and SKIPS unchanged bullets entirely
                    # (perfect fidelity for kept bullets).
                    if os.getenv("APPLYSMART_FORCE_BULLET_DOT") == "1":
                        bullet_char = "·"
                    else:
                        bullet_char = _detect_section_bullet_glyph(sec)

                    bullet_ref = _pick_body_span(
                        [ln for b in bullets for ln in b["lines"]]
                    )
                    if bullet_ref is None:
                        continue

                    n_rewrites_role = 0
                    role_applied = False
                    for e in normalised:
                        # Unchanged bullet — leave the PDF untouched.
                        if e["text"] is None:
                            continue
                        new_btext = _normalize_for_ats(e["text"])
                        if not isinstance(new_btext, str) or not new_btext.strip():
                            continue
                        bullet = bullets[e["i"]]
                        b_lines = bullet.get("lines") or []
                        if not b_lines:
                            continue
                        # Skip bullets that straddle a page break — one
                        # rect can't redact across pages.
                        if len({ln["page"] for ln in b_lines}) > 1:
                            report.setdefault("skipped", []).append(
                                f"bullets/{header}: bullet i={e['i']} spans "
                                f"pages — kept original"
                            )
                            continue

                        body_rect, has_inline_glyph, body_true = _bullet_body_rect(b_lines)
                        if body_rect is None:
                            continue

                        # Extend the rect to this bullet's full vertical
                        # SLOT: up to the midpoint of the gap above, down
                        # to the midpoint of the gap below. PyMuPDF's
                        # insert_textbox measurement runs ~0.5-2pt more
                        # conservative than the original text's occupied
                        # height — without this slack every multi-line
                        # rewrite was rejected by a hair. Taking half of
                        # each neighbouring gap is invisible (the gap is
                        # whitespace) and never reaches a neighbour's text.
                        # Capture the bullet's TRUE text bounds before the
                        # slot extension — the reference for border clipping
                        # (a divider line just below the text must be
                        # protected even though it sits inside the padded,
                        # extended rect).
                        text_y0 = body_rect.y0
                        text_y1 = body_rect.y1

                        own_y_min = body_rect.y0 - 0.5
                        own_y_max = body_rect.y1 + 0.5
                        other_lines = [
                            ln for ln in page_lines
                            if not (own_y_min <= ln["bbox"][1] <= own_y_max)
                        ]
                        body_rect.x1 = max(
                            body_rect.x1,
                            _measured_right_margin(other_lines, page.rect.width),
                        )
                        # Nearest line ABOVE (its bottom edge) and BELOW
                        # (its top edge), excluding this bullet's own lines.
                        prev_y1 = max(
                            (ln["bbox"][3] for ln in other_lines
                             if ln["bbox"][3] <= text_y0 + 0.5),
                            default=text_y0 - 6.0,
                        )
                        next_y0 = _next_y0_below(
                            body_rect, other_lines, page.rect.height
                        )
                        # Extend to this bullet's slot: half the gap on each
                        # side (whitespace, invisible). Clamp ≤6pt so we
                        # never swallow a neighbouring line.
                        gap_above = max(0.0, text_y0 - prev_y1)
                        body_rect.y0 = text_y0 - min(gap_above / 2.0, 6.0)
                        gap_below = max(0.0, next_y0 - text_y1)
                        body_rect.y1 = text_y1 + min(gap_below / 2.0, 6.0)

                        # Inline glyph -> must re-prepend (original glyph is
                        # inside the redact rect). Separate glyph -> leave
                        # it; the original sits outside body_rect untouched.
                        if has_inline_glyph:
                            insert_text  = f"{bullet_char}   {new_btext}"
                            restore_text = f"{bullet_char}   {bullet['text']}"
                        else:
                            insert_text  = new_btext
                            restore_text = bullet["text"]

                        measured = _measure_line_gap(b_lines)
                        ref = _pick_body_span(b_lines) or bullet_ref
                        fit_size = float(ref.get("size", 10) or 10) if ref else 10.0

                        if os.getenv("APPLYSMART_DEBUG_BULLETS") == "1":
                            print(
                                f"   bullet i={e['i']}: rect "
                                f"x0={body_rect.x0:.1f} y0={body_rect.y0:.1f} "
                                f"x1={body_rect.x1:.1f} y1={body_rect.y1:.1f} "
                                f"w={body_rect.width:.1f} h={body_rect.height:.1f} | "
                                f"n_lines={len(b_lines)} measured_gap={measured:.3f} | "
                                f"insert_len={len(insert_text)} ref_size={fit_size}"
                            )

                        # PRE-CHECK: only redact if the rewrite will fit.
                        # Skipping a non-fitting rewrite leaves the original
                        # bullet PERFECTLY intact (no redaction, no blank
                        # gap). This is what prevents the empty-bullet bug.
                        if not _estimate_text_fits(
                            insert_text, body_rect.width, body_rect.height,
                            fit_size, measured,
                        ):
                            report.setdefault("skipped", []).append(
                                f"bullets/{header}: bullet i={e['i']} rewrite "
                                f"too long for its slot — kept original (untouched)"
                            )
                            print(
                                f"   pdf_editor: bullet i={e['i']} in "
                                f"{header[:40]!r} rewrite too long for slot — "
                                f"kept original (untouched, no redaction)"
                            )
                            continue

                        # Capture any table/divider border lines that pass
                        # through the redact rect, so we can re-draw them
                        # after the white-fill redaction wipes them.
                        saved_borders = _capture_borders_in_rect(page, body_rect)

                        _redact_rect(page, body_rect)
                        sz_b = _insert_fitted(
                            page, body_rect, insert_text, ref, align=0,
                            line_gap=measured,
                            orig_first_top=body_true.y0, orig_left=body_true.x0,
                            doc=doc, font_cache=font_cache,
                        )
                        _redraw_borders(page, saved_borders)
                        if not sz_b or sz_b <= 0:
                            # Estimate passed but PyMuPDF still rejected
                            # (rare). Restore the original — it is no longer
                            # than the rewrite, so it fits — and never leave
                            # the bullet blank.
                            restored = _insert_fitted(
                                page, body_rect, restore_text, ref, align=0,
                                line_gap=measured,
                                orig_first_top=body_true.y0, orig_left=body_true.x0,
                                doc=doc, font_cache=font_cache,
                            )
                            _redraw_borders(page, saved_borders)
                            if not restored or restored <= 0:
                                # Absolute last resort: force-draw the
                                # original ignoring the fit return code so
                                # the bullet is never blank.
                                try:
                                    page.insert_textbox(
                                        body_rect, restore_text,
                                        fontsize=fit_size, fontname="helv",
                                        align=0, lineheight=1.0,
                                    )
                                    _redraw_borders(page, saved_borders)
                                except Exception:
                                    pass
                            report.setdefault("skipped", []).append(
                                f"bullets/{header}: bullet i={e['i']} rewrite "
                                f"did not fit — kept original"
                            )
                            print(
                                f"   pdf_editor: bullet i={e['i']} in "
                                f"{header[:40]!r} did not fit — kept original"
                            )
                        else:
                            n_rewrites_role += 1
                            role_applied = True

                    n_rewrites_total += n_rewrites_role
                    if role_applied:
                        applied_roles.append(header)
            if applied_roles:
                report["applied"]["bullets"] = {
                    "roles":     applied_roles,
                    "rewrites":  n_rewrites_total,
                    "dropped":   n_dropped_total,
                }

        # ── SKILLS ─────────────────────────────────────────────
        skills_order: List[str] = edits.get("skills_order") or []
        if skills_order:
            sk_sec = next((s for s in sections if s["type"] == "skills"), None)
            if sk_sec and sk_sec["lines"]:
                page_idx = sk_sec["lines"][0]["page"]
                page = doc[page_idx]
                rect = _union_rect([ln["bbox"] for ln in sk_sec["lines"]], pad=1.5)
                # Apr 28 follow-up: data-driven margins (see bullets path
                # above for full rationale). Skills section is usually last
                # so y1 typically extends to the page bottom margin via
                # _next_y0_below's fallback.
                page_lines = _all_lines_on_page(sections, page_idx)
                own_y_min = min(ln["bbox"][1] for ln in sk_sec["lines"]) - 0.5
                own_y_max = max(ln["bbox"][3] for ln in sk_sec["lines"]) + 0.5
                own_x_min = min(ln["bbox"][0] for ln in sk_sec["lines"])
                other_lines = [
                    ln for ln in page_lines
                    if not (own_y_min <= ln["bbox"][1] <= own_y_max)
                ]
                rect.x1 = max(rect.x1, _measured_right_margin(other_lines, page.rect.width))
                next_y0 = _next_y0_below(rect, other_lines, page.rect.height)
                rect.y1 = max(rect.y1, next_y0 - 2.0)
                ref = _first_span_of_lines(sk_sec["lines"])
                if ref is not None:
                    reordered = ", ".join(s.strip() for s in skills_order if s.strip())
                    _redact_rect(page, rect)
                    # Run 19 audit fix #34: also check the return value on
                    # the skills path. Same fallback pattern.
                    sz_sk = _insert_fitted(
                        page, rect, reordered, ref, align=0,
                        orig_first_top=own_y_min + 0.5, orig_left=own_x_min,
                        doc=doc, font_cache=font_cache,
                    )
                    if not sz_sk or sz_sk <= 0:
                        # Reordered skills didn't fit — restore original.
                        original_skills = ", ".join(
                            (ln.get("text") or "").strip()
                            for ln in sk_sec["lines"]
                            if (ln.get("text") or "").strip()
                        )
                        _insert_fitted(
                            page, rect, original_skills, ref, align=0,
                            orig_first_top=own_y_min + 0.5, orig_left=own_x_min,
                            doc=doc, font_cache=font_cache,
                        )
                        report["skipped"].append(
                            "skills: reorder did not fit (restored original)"
                        )
                    else:
                        report["applied"]["skills"] = True
                else:
                    report["skipped"].append("skills: no reference span")
            else:
                report["skipped"].append("skills: section not found")

        doc.save(output_path, deflate=True, garbage=0)
    finally:
        doc.close()

    # Surface table-protection stats for the UI / Mixpanel.
    report["tables"] = {
        "detected": _LAST_EXTRACT_STATS["tables_detected"],
        "lines_filtered": _LAST_EXTRACT_STATS["table_lines_filtered"],
    }
    return report


def _match_role_order(
    header: str,
    bullet_edits: Dict[str, List[int]],
) -> Optional[List[int]]:
    """Tolerant match: exact → startswith → substring."""
    h = header.strip().lower()
    # Exact / case-insensitive
    for k, v in bullet_edits.items():
        if k.strip().lower() == h:
            return v
    # Startswith either direction
    for k, v in bullet_edits.items():
        kl = k.strip().lower()
        if h.startswith(kl) or kl.startswith(h):
            return v
    # Substring
    for k, v in bullet_edits.items():
        kl = k.strip().lower()
        if kl and kl in h:
            return v
    return None
