# agents/cv_style_agent.py
#
# Analyzes the uploaded CV PDF and builds a style profile (fonts, sizes, margins,
# colours) so ReportLab output can mirror the original as closely as possible.

from __future__ import annotations

from collections import Counter
from statistics import median
from typing import Any, Dict, List, Tuple

import fitz  # PyMuPDF

from agents.pdf_formatter import extract_cv_style


def _map_pdf_font_to_reportlab(pdf_font_name: str) -> Tuple[str, str]:
    """
    Map a PDF font name to ReportLab's built-in Type1 families (always available).
    Returns (regular, bold) base font names for ParagraphStyle.
    """
    name = (pdf_font_name or "").lower()

    if any(x in name for x in ("times", "georgia", "garamond", "serif")):
        return "Times-Roman", "Times-Bold"
    if any(x in name for x in ("courier", "mono", "consolas")):
        return "Courier", "Courier-Bold"
    # Default: Helvetica family (sans)
    return "Helvetica", "Helvetica-Bold"


def _collect_span_stats(page: fitz.Page) -> Tuple[List[float], List[str], List[float]]:
    sizes: List[float] = []
    fonts: List[str] = []
    left_edges: List[float] = []

    blocks = page.get_text("dict").get("blocks", [])
    for block in blocks:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                sz = float(span.get("size") or 10)
                fn = str(span.get("font") or "")
                sizes.append(sz)
                if fn:
                    fonts.append(fn)
                bbox = span.get("bbox")
                if bbox:
                    left_edges.append(float(bbox[0]))

    return sizes, fonts, left_edges


def build_style_profile(pdf_path: str) -> Dict[str, Any]:
    """
    Merge colour/header detection from extract_cv_style with font and margin hints
    from the first page's text spans.
    """
    profile: Dict[str, Any] = dict(extract_cv_style(pdf_path))

    profile.setdefault("font_body", "Helvetica")
    profile.setdefault("font_body_bold", "Helvetica-Bold")
    profile.setdefault("font_header", "Helvetica-Bold")
    profile.setdefault("left_margin_mm", 18.0)
    profile.setdefault("right_margin_mm", 18.0)
    profile.setdefault("top_margin_mm", 18.0)
    profile.setdefault("bottom_margin_mm", 15.0)
    profile.setdefault("body_leading", None)

    try:
        doc = fitz.open(pdf_path)
        page = doc[0]

        sizes, fonts, left_edges = _collect_span_stats(page)

        if fonts:
            common = Counter(fonts).most_common(1)[0][0]
            body_r, body_b = _map_pdf_font_to_reportlab(common)
            profile["font_body"] = body_r
            profile["font_body_bold"] = body_b
            profile["font_header"] = body_b

        if sizes:
            med = float(median(sizes))
            mx = max(sizes)
            # Body: near median; header bump already in extract_cv_style
            profile["font_size_body"] = max(9, min(12, int(round(med))))
            if mx > med + 1:
                profile["font_size_header"] = max(
                    profile.get("font_size_header", 14),
                    int(round(mx)),
                )
            # Line spacing ~ 1.2–1.35 × font size (ReportLab leading)
            profile["body_leading"] = round(profile["font_size_body"] * 1.25, 1)

        if left_edges:
            # PyMuPDF points → mm (1 pt ≈ 0.352778 mm)
            left_pt = min(left_edges)
            left_mm = left_pt * 0.352778
            profile["left_margin_mm"] = float(max(12.0, min(32.0, left_mm)))

        doc.close()
    except Exception as e:
        print(f"   ⚠️  cv_style_agent: extra style hints failed ({e}) — using base profile")

    lm = float(profile.get("left_margin_mm") or 18.0)
    print(
        f"   📐 Style profile: body={profile.get('font_body')} "
        f"{profile.get('font_size_body')}pt, margins L≈{lm:.1f}mm"
    )
    return profile
