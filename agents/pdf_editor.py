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

import re
from typing import Any, Dict, List, Optional

import fitz  # PyMuPDF


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
    Find the xref of a full (non-subsetted) embedded TTF whose PostScript/base
    name matches `wanted_name`. Subset fonts (e.g. 'BAAAAA+LiberationSans') are
    skipped because their glyph tables only contain characters that already
    appeared in the original document; inserting new characters with them
    produces .notdef / NULL glyphs.
    """
    w = (wanted_name or "").lower()
    if not w:
        return None
    # Prefer exact, non-subset match on base-clean; never use subsets.
    exact_hit: Optional[int] = None
    sub_hit:   Optional[int] = None
    for pi in range(doc.page_count):
        for entry in doc.get_page_fonts(pi):
            # entry: (xref, ext, type, basefont, refname, encoding)
            xref = entry[0]
            base = str(entry[3] or "")
            if _is_subset_font(base):
                continue  # subset — can't safely reuse
            base_l = base.lower()
            base_clean = base_l.split("+")[-1]
            if w == base_clean or w == base_l:
                exact_hit = xref
                break
            if w in base_clean and sub_hit is None:
                sub_hit = xref
        if exact_hit is not None:
            break
    return exact_hit if exact_hit is not None else sub_hit


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
        r"^\s*(featured\s+projects?|key\s+projects?|"
        r"personal\s+projects?|side\s+projects?|notable\s+projects?|"
        r"selected\s+projects?|independent\s+projects?|"
        r"open[\s\-]source\s+projects?|projects?)\s*$",
        re.I,
    ),
    "certifications": re.compile(
        r"^\s*(certifications?|certificates?)\s*$",
        re.I,
    ),
}


def _classify_heading(text: str) -> Optional[str]:
    t = (text or "").strip()
    if not t or len(t) > 60:
        return None
    for kind, rx in _HEADINGS.items():
        if rx.match(t):
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
        if bold and not explicit_bullet:
            cur = {
                "header_text":   text,
                "header_line":   ln,
                "bullet_groups": [],
                "sub_lines":     [],
            }
            roles.append(cur)
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

        is_new_bullet = (
            explicit_bullet
            or marker_bullet
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

    return roles


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
    Find the first span across `lines` that is a body-text span (not a bullet
    glyph and not a symbolic font). Falls back to the very first span if none
    qualifies, so we always return *something*.
    """
    first_any: Optional[dict] = None
    for ln in lines:
        for s in ln.get("spans") or []:
            if first_any is None:
                first_any = s
            if _span_is_text_carrying(s):
                return s
    return first_any


def _first_span_of_lines(lines: List[Dict[str, Any]]) -> Optional[dict]:
    """Kept for backwards compatibility; prefers body-text spans."""
    return _pick_body_span(lines)


def _redact_rect(page: fitz.Page, rect: fitz.Rect) -> None:
    page.add_redact_annot(rect, fill=(1, 1, 1))
    # Keep images/graphics; only remove text+fill.
    page.apply_redactions(
        images=fitz.PDF_REDACT_IMAGE_NONE,
        graphics=fitz.PDF_REDACT_LINE_ART_NONE,
    )


def _insert_fitted(
    page:      fitz.Page,
    rect:      fitz.Rect,
    text:      str,
    ref_span:  dict,
    align:     int = 0,
    line_gap:  float = 1.05,  # matches typical CV line-spacing (~1.07).
    doc:       Optional[fitz.Document] = None,
    font_cache: Optional[Dict[int, str]] = None,
) -> float:
    """
    Insert `text` into `rect` using the ref_span's style. Tries to reuse the
    original embedded font first; on failure falls back to a built-in Base14
    font and substitutes any unicode bullets with ASCII dashes (so renders
    never come out as `?`).

    Progressively shrinks fontsize on overflow. Returns the fontsize used, or 0
    if even the smallest size couldn't fit.
    """
    base_size = float(ref_span.get("size", 10))
    color     = _int_color_to_rgb(int(ref_span.get("color", 0)))

    # Keep overflow margin tight so `insert_textbox` returns rc<0 (and we
    # shrink fontsize) the moment text would spill into the NEXT section
    # below. A fat overflow margin causes visible overlap with adjacent
    # content — the exact bug we're avoiding.
    work_rect = fitz.Rect(rect.x0, rect.y0, rect.x1, rect.y1 + max(2.0, base_size * 0.4))

    # More aggressive shrinkage ladder (floor of 6pt enforced below).
    sizes = [base_size - 0.5 * i for i in range(10)]

    # ── Attempt 1: original embedded font (preserves look + unicode) ──
    if doc is not None and font_cache is not None:
        alias = _install_original_font(doc, page, ref_span, font_cache, text=text)
        if alias:
            for sz in sizes:
                if sz < 6:
                    break
                try:
                    rc = page.insert_textbox(
                        work_rect, text,
                        fontsize=sz, fontname=alias, color=color,
                        align=align, lineheight=max(1.0, line_gap),
                    )
                except Exception:
                    rc = -1
                    break
                if rc >= 0:
                    return sz

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
    # Any remaining bullet glyphs → middle dot. U+2022 (•) is intentionally
    # kept because Base14 Latin-1 maps it at WinAnsi 0x95; downgrading it
    # caused visible inconsistency when some sections used the embedded-font
    # path (kept •) and others fell through here (became ·).
    for ch in _BULLET_CHARS:
        if ch in ("-", "*", "\u00b7", "\u2022"):
            continue
        safe_text = safe_text.replace(ch, "\u00b7")
    for sz in sizes:
        if sz < 6:
            break
        rc = page.insert_textbox(
            work_rect, safe_text,
            fontsize=sz, fontname=builtin, color=color,
            align=align, lineheight=max(1.0, line_gap),
        )
        if rc >= 0:
            return sz
    return 0.0


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
        new_summary = (edits.get("summary") or "").strip()
        if new_summary:
            sum_sec = next((s for s in sections if s["type"] == "summary"), None)
            if sum_sec is None:
                sum_sec = _infer_summary_from_header(sections)
            if sum_sec and sum_sec["lines"]:
                # ── P4 (Apr 28): Summary overflow guard ────────
                # If the rewritten summary is much longer than the original
                # block, applying it via _insert_fitted() will overflow into
                # the next section's text spans (role headers below get
                # clipped to fragments — the "A" / "Cl" damage seen in the
                # Barden run on Apr 27). Reject the summary edit cleanly
                # so the supervisor's overflow signal can route to the
                # rebuild path or trigger a stricter retry.
                #
                # Threshold 1.4×: legitimate tailored summaries can grow
                # 10-30% naturally (added JD verbs, foregrounded facts).
                # Beyond 1.4× we're in disaster territory (e.g. 80→385
                # words from the Personal-Projects parser bug).
                orig_text = " ".join(
                    ln["text"] for ln in sum_sec["lines"]
                ).strip()
                orig_words = len(orig_text.split())
                new_words = len(new_summary.split())
                # Only enforce when we have a meaningful original to
                # compare against — very short originals (<10 words) are
                # often placeholder/empty summaries on stub CVs.
                overflow_ratio = (new_words / orig_words) if orig_words >= 10 else 0.0
                if overflow_ratio > 1.4:
                    msg = (
                        f"summary: rewrite is {new_words} words vs "
                        f"{orig_words} original ({overflow_ratio:.1f}\u00d7) "
                        f"\u2014 rejected to prevent layout overflow"
                    )
                    print(f"   \U0001f6e1\ufe0f  pdf_editor: {msg}")
                    report["skipped"].append(msg)
                    # Surface in the report's debug counters so the
                    # supervisor can detect it via best_review._debug
                    report.setdefault("_debug", {})["summary_overflow_rejected"] = {
                        "orig_words": orig_words,
                        "new_words": new_words,
                        "ratio": round(overflow_ratio, 2),
                    }
                else:
                    page = doc[sum_sec["lines"][0]["page"]]
                    rect = _union_rect([ln["bbox"] for ln in sum_sec["lines"]], pad=1.5)
                    # Extend rect to right page margin for wrap room.
                    rect.x1 = max(rect.x1, page.rect.width - 40)
                    ref = _first_span_of_lines(sum_sec["lines"])
                    if ref is not None:
                        measured = _measure_line_gap(sum_sec["lines"])
                        _redact_rect(page, rect)
                        sz = _insert_fitted(
                            page, rect, new_summary, ref, align=0,
                            line_gap=measured,
                            doc=doc, font_cache=font_cache,
                        )
                        report["applied"]["summary"] = {"fontsize": sz, "line_gap": round(measured, 3)}
                    else:
                        report["skipped"].append("summary: no reference span")
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

                    page = doc[bullets[0]["lines"][0]["page"]]
                    all_boxes = [ln["bbox"] for b in bullets for ln in b["lines"]]
                    rect = _union_rect(all_boxes, pad=1.5)
                    # Extend to right page margin for wrap room — without this,
                    # rewritten (often longer) bullets are constrained to the
                    # narrowest original bullet's right edge, producing ugly
                    # orphaned words on follow-on lines. Mirrors the summary
                    # path on line ~840.
                    rect.x1 = max(rect.x1, page.rect.width - 40)
                    # Extend bottom to preserve content below edited section
                    # This prevents cutting off awards, co-curricular, etc.
                    rect.y1 = min(rect.y1 + 50, page.rect.height)
                    # Pick a body-text span (NOT the symbol-font bullet glyph) as
                    # style reference, scanning across all bullet lines.
                    ref = _pick_body_span(
                        [ln for b in bullets for ln in b["lines"]]
                    )
                    if ref is None:
                        continue
                    # Always use standard bullet character (•) for consistent rendering
                    # This prevents "?" artifacts from special symbol fonts that don't embed properly
                    bullet_char = "\u2022"

                    # Build new bullet block, using rewrite text when provided.
                    lines_out: List[str] = []
                    n_rewrites_role = 0
                    for e in normalised:
                        original = bullets[e["i"]]["text"]
                        use_text = e["text"] if e["text"] is not None else original
                        # Ensure use_text is a string (defensive against LLM returning dict)
                        if not isinstance(use_text, str):
                            use_text = str(use_text)
                        if e["text"] is not None:
                            n_rewrites_role += 1
                        lines_out.append(f"{bullet_char}   {use_text}")
                    new_text = "\n".join(lines_out)

                    dropped_this_role = len(bullets) - len(normalised)
                    n_rewrites_total += n_rewrites_role
                    n_dropped_total  += dropped_this_role

                    measured = _measure_line_gap(
                        [ln for b in bullets for ln in b["lines"]]
                    )
                    _redact_rect(page, rect)
                    _insert_fitted(
                        page, rect, new_text, ref, align=0,
                        line_gap=measured,
                        doc=doc, font_cache=font_cache,
                    )
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
                page = doc[sk_sec["lines"][0]["page"]]
                rect = _union_rect([ln["bbox"] for ln in sk_sec["lines"]], pad=1.5)
                rect.x1 = max(rect.x1, page.rect.width - 40)
                ref = _first_span_of_lines(sk_sec["lines"])
                if ref is not None:
                    reordered = ", ".join(s.strip() for s in skills_order if s.strip())
                    _redact_rect(page, rect)
                    _insert_fitted(
                        page, rect, reordered, ref, align=0,
                        doc=doc, font_cache=font_cache,
                    )
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
