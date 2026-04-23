import fitz  # PyMuPDF
import os
import re as _re
from typing import Any, Dict, List


# Fonts whose names mark symbol/bullet glyphs — skip their spans so
# the mis-decoded "?" bullets don't leak into the body text.
_SYMBOLIC_FONT_RX = _re.compile(
    r"(symbol|wingding|webding|dingbat|marlett|bullet)",
    _re.IGNORECASE,
)


def _line_text_from_spans(line: Dict[str, Any]) -> str:
    """Render one dict-mode line to text, swapping symbol-font glyphs for •."""
    rendered = ""
    for span in line.get("spans", []):
        font = span.get("font", "") or ""
        if _SYMBOLIC_FONT_RX.search(font):
            rendered += "• "
            continue
        rendered += span.get("text", "")
    return rendered


def _cluster_lines_by_column(
    lines: List[Dict[str, Any]],
    page_width: float,
) -> List[List[Dict[str, Any]]]:
    """
    Group lines into columns based on x-start histogram. Returns a list of
    columns, left-to-right, each internally sorted top-to-bottom.

    Heuristic:
      - Bin x-starts into 20 buckets across the page width.
      - A bin with >= 10% of lines is a "peak".
      - Peaks separated by >= 2 empty-ish bins mark column boundaries.
      - If only one peak → single column.
    """
    if not lines or page_width <= 0:
        return [sorted(lines, key=lambda l: (l["bbox"][1], l["bbox"][0]))]

    # Build histogram.
    N_BINS = 20
    bins = [0] * N_BINS
    for ln in lines:
        x0 = ln["bbox"][0]
        ratio = max(0.0, min(0.9999, x0 / page_width))
        bins[int(ratio * N_BINS)] += 1

    total = sum(bins) or 1
    threshold = max(3, int(0.10 * total))

    # Find peaks above threshold.
    peaks = [i for i, c in enumerate(bins) if c >= threshold]
    if len(peaks) < 2:
        return [sorted(lines, key=lambda l: (l["bbox"][1], l["bbox"][0]))]

    # Collapse peaks that are < 3 bins apart into a single column group.
    merged_peaks: List[int] = [peaks[0]]
    for p in peaks[1:]:
        if p - merged_peaks[-1] < 3:
            continue
        merged_peaks.append(p)

    if len(merged_peaks) < 2:
        return [sorted(lines, key=lambda l: (l["bbox"][1], l["bbox"][0]))]

    # Boundaries sit at midpoints between successive peak bins.
    boundaries_px: List[float] = []
    for i in range(len(merged_peaks) - 1):
        mid_bin = (merged_peaks[i] + merged_peaks[i + 1]) / 2.0
        boundaries_px.append((mid_bin / N_BINS) * page_width)

    # Assign each line to a column.
    columns: List[List[Dict[str, Any]]] = [[] for _ in range(len(merged_peaks))]
    for ln in lines:
        x0 = ln["bbox"][0]
        col_idx = 0
        for b in boundaries_px:
            if x0 >= b:
                col_idx += 1
            else:
                break
        columns[col_idx].append(ln)

    # Sort each column top-to-bottom.
    for col in columns:
        col.sort(key=lambda l: (l["bbox"][1], l["bbox"][0]))
    # Drop empty columns (paranoia — shouldn't happen).
    columns = [c for c in columns if c]
    return columns


def _extract_page_text_columnwise(page: "fitz.Page") -> str:
    """
    Extract page text respecting multi-column reading order. For a single-
    column page this behaves like PyMuPDF's default extractor. For a 2-
    column layout it emits the left column top-to-bottom first, then the
    right column top-to-bottom — avoiding the line-interleaving that
    corrupts reading order on designer templates.
    """
    raw_lines: List[Dict[str, Any]] = []
    page_dict = page.get_text("dict")
    for block in page_dict.get("blocks", []):
        if "lines" not in block:
            continue
        for line in block["lines"]:
            txt = _line_text_from_spans(line)
            if not txt.strip():
                continue
            bbox = list(line.get("bbox") or [0, 0, 0, 0])
            raw_lines.append({"text": txt, "bbox": bbox})

    if not raw_lines:
        return ""

    columns = _cluster_lines_by_column(raw_lines, float(page.rect.width))

    parts: List[str] = []
    for col in columns:
        for ln in col:
            parts.append(ln["text"])
        # Blank line between columns so section detection doesn't merge them.
        parts.append("")
    return "\n".join(parts)


def parse_cv(cv_path):
    text = ""
    try:
        if not os.path.exists(cv_path):
            print(f"CV file not found at: {cv_path}")
            return ""

        doc = fitz.open(cv_path)
        for page in doc:
            # Column-aware extraction: preserves reading order on 2-column
            # designer templates instead of interleaving sidebar with main
            # content. Falls back to natural order on single-column CVs.
            page_text = _extract_page_text_columnwise(page)
            if page_text:
                text += page_text + "\n"
        doc.close()
        
        # Repair mis-decoded bullet prefixes ("?   thing" → "• thing") and
        # collapse excessive blank lines.
        text = _re.sub(r"(^|\n)\s*[?\uFFFD]+(?=\s{2,})\s+", r"\1• ", text)

        # Coalesce standalone bullet-glyph lines with the body that follows.
        # Many designer CVs render bullets in a symbol font that PyMuPDF
        # emits as its own line ("•\nSpearheaded ...\nLinkedIn ...").
        # Downstream parsers (`pdf_formatter_weasy._parse_cv`) expect the
        # bullet and its first body line on the same line. Without this,
        # every body line gets treated as a new role header and we lose
        # all bullet content.
        text = _re.sub(
            r"(^|\n)\s*([\u2022\u25CF\u25AA\u25A0\u25CB\u2043\u2219•●▪■○·])"
            r"[ \t]*\r?\n[ \t]*(?=\S)",
            r"\1\2 ",
            text,
        )

        text = _re.sub(r"\n{3,}", "\n\n", text).strip()
        
        print(f"CV parsed successfully: {len(text)} characters extracted")
    except Exception as e:
        print(f"Error parsing CV: {e}")
    return text.strip()


def get_cv_sections(cv_text):
    """Splits CV text into rough sections for better AI processing"""
    sections = {
        "full_text": cv_text,
        "word_count": len(cv_text.split()),
        "char_count": len(cv_text)
    }
    return sections