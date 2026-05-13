"""
agents.cv_docx_to_pdf
=====================

Render an edited DOCX back to PDF (May 2026 / DOCX path).

Pipeline:    edited DOCX ──[ mammoth ]──> HTML  ──[ WeasyPrint ]──> PDF

Why two steps
-------------
LibreOffice headless would give the highest-fidelity DOCX→PDF, but it
adds ~400 MB to the Streamlit Cloud image (over the 1 GB limit). The
two-step path uses libraries we either already have (`weasyprint` for
WeasyPrint, which is part of the rebuild path) or are <10 MB (`mammoth`),
total <15 MB added to the install.

Trade-offs vs LibreOffice
-------------------------
  ✓  No new system deps (libreoffice / java / fontconfig)
  ✓  Works identically on Windows, macOS, and Linux
  ✓  Fits Streamlit Cloud's free tier
  ✗  Loses Word-specific layout features (e.g. text frames, vertical
     alignment within tables, custom tab stops, footnote columns)
  ✗  Fonts must be available on the host or fall back to a sans-serif

For CVs the trade-offs land in favour of mammoth+WeasyPrint:
  - CVs are 90% paragraphs + bullets + simple tables — all preserved by
    mammoth's default DOCX→HTML mapping
  - Custom fonts in CVs degrade gracefully to system fonts on the
    deployed host (which is what would happen with LibreOffice anyway
    when a font isn't installed)

Designer CV fallback
--------------------
This module is only called when the convertibility checker upstream
accepted the DOCX as parseable. For designer CVs that fail conversion,
the router goes straight to the existing WeasyPrint rebuild path
(`agents.pdf_formatter_weasy`). No regression.

Never raises. On any failure returns `(False, reason_string)` so the
caller can fall back to the rebuild path.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple


# ─────────────────────────────────────────────────────────────
# HTML post-processing: glyph paragraphs → proper list items
# ─────────────────────────────────────────────────────────────
#
# When a DOCX comes from `pdf2docx`, bullet paragraphs typically have
# Word's "Normal" style with an in-text glyph (•Started…), NOT the
# List Bullet style mammoth uses to emit <ul><li>…</li></ul>. The
# result: mammoth produces <p>•Started…</p>, which renders as a wall
# of paragraphs starting with a stray • character.
#
# We detect those paragraphs after mammoth's output and rewrite them
# into proper list items. Consecutive glyph-paragraphs become a single
# <ul> block. Same end result mammoth would have produced for a
# native-Word DOCX, just reconstructed from the in-text glyph.
_HTML_GLYPH_PARA_RX = re.compile(
    r"<p>\s*"
    r"(?:[\u2022\u00b7\u25aa\u25cb\u25a0\u2043\u2219\u25b8\u25b6]|\*|-)\s*"
    r"(.*?)</p>",
    re.IGNORECASE | re.DOTALL,
)


def _promote_glyph_paragraphs_to_lists(html: str) -> str:
    """
    Convert sequences of <p>•TEXT</p> into <ul><li>TEXT</li>...</ul>.

    Implementation: a single regex pass over the document string. We
    match every glyph-led paragraph, then run a second pass to fold
    consecutive matches into a single <ul> group. Non-glyph paragraphs
    are left untouched.
    """
    if not html or "<p>" not in html:
        return html

    # Step 1: convert every glyph-led <p>…</p> into a sentinel <li>…</li>
    converted = _HTML_GLYPH_PARA_RX.sub(r"<li>\1</li>", html)

    # Step 2: wrap runs of consecutive <li>…</li> in a single <ul>.
    # Runs may be separated by whitespace only.
    wrapped = re.sub(
        r"(?:<li>[\s\S]*?</li>\s*)+",
        lambda m: "<ul>" + m.group(0) + "</ul>",
        converted,
    )
    return wrapped


# ─────────────────────────────────────────────────────────────
# Default CSS — keeps rendered CVs ATS-friendly
# ─────────────────────────────────────────────────────────────
#
# Mammoth emits semantic HTML (h1/h2 for headings, p for body, ul/li for
# bullets, table for tables). Our CSS turns that into a CV-shaped page
# with:
#   - 1-inch margins  (matches default Word page setup)
#   - Helvetica / Arial body  (universal ATS-readable sans-serif)
#   - Tight line-height for bullets (1.3) and looser for the summary
#   - Section headings underlined or in slight uppercase  (recruiter scan)
#   - Bullet markers preserved (no flat -  /  blank-circle artefacts)
#
# Power users can override by passing `extra_css` to `render_pdf_from_docx`.

_DEFAULT_CSS = """
@page {
    size: A4;
    margin: 0.6in 0.7in 0.6in 0.7in;
}

html, body {
    font-family: Helvetica, Arial, sans-serif;
    font-size: 10.5pt;
    line-height: 1.35;
    color: #111;
}

h1 {
    font-size: 16pt;
    margin: 0 0 4pt 0;
    font-weight: 700;
}

h2 {
    font-size: 12pt;
    margin: 12pt 0 4pt 0;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    border-bottom: 0.5pt solid #888;
    padding-bottom: 1pt;
}

h3 {
    font-size: 11pt;
    margin: 8pt 0 2pt 0;
    font-weight: 700;
}

p {
    margin: 2pt 0;
}

ul {
    margin: 4pt 0 4pt 14pt;
    padding: 0;
}

li {
    margin: 1pt 0;
}

table {
    border-collapse: collapse;
    margin: 4pt 0;
    width: 100%;
}

td, th {
    padding: 2pt 4pt;
    vertical-align: top;
}

a {
    color: inherit;
    text-decoration: none;
}

/* Mammoth emits an explicit <strong>/<em> for bold and italic, both of
   which Word users will recognise. Keep the visual distinction. */
strong { font-weight: 700; }
em { font-style: italic; }
"""


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────

def render_pdf_from_docx(
    docx_path:   str,
    output_path: str,
    extra_css:   Optional[str] = None,
) -> Tuple[bool, str]:
    """
    Convert `docx_path` to PDF at `output_path`. Returns `(True, "")` on
    success or `(False, reason)` on failure.

    Two-step pipeline:
        DOCX --(mammoth)--> HTML  --(WeasyPrint)--> PDF

    Both libraries are pure-Python (mammoth) or wrap native deps we
    already require (WeasyPrint via libpango / libcairo, declared in
    `packages.txt`). If either is missing the function returns ok=False
    and the caller falls back to the rebuild path.
    """
    if not docx_path or not os.path.exists(docx_path):
        return False, f"DOCX file not found: {docx_path!r}"

    # Step 1: DOCX → HTML via mammoth. Mammoth makes a best-effort
    # mapping of Word styles to semantic HTML — Heading 1 → <h1>, List
    # Bullet → <ul><li>, bold runs → <strong>, etc. Custom style mappings
    # are possible but unnecessary for CV-shaped documents.
    try:
        import mammoth
    except Exception as e:
        return False, f"mammoth unavailable: {type(e).__name__}: {e}"

    try:
        with open(docx_path, "rb") as fh:
            result = mammoth.convert_to_html(fh)
        html_body = result.value
        if not html_body:
            return False, "mammoth produced empty HTML"
        # Post-process: pdf2docx-converted DOCX files keep bullets as
        # in-text glyphs on Normal-styled paragraphs. mammoth emits
        # those as <p>•...</p>, which renders as a wall of glyph-led
        # paragraphs instead of a proper bulleted list. Promote them.
        html_body = _promote_glyph_paragraphs_to_lists(html_body)
    except Exception as e:
        return False, f"mammoth conversion failed: {type(e).__name__}: {e}"

    # Step 2: wrap in a minimal HTML document with our CSS, render via WeasyPrint.
    try:
        from weasyprint import HTML, CSS
    except Exception as e:
        return False, f"WeasyPrint unavailable: {type(e).__name__}: {e}"

    css_text = _DEFAULT_CSS
    if extra_css:
        css_text = css_text + "\n" + extra_css

    full_html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>CV</title></head><body>"
        f"{html_body}"
        "</body></html>"
    )

    try:
        out_dir = os.path.dirname(output_path)
        if out_dir and not os.path.isdir(out_dir):
            os.makedirs(out_dir, exist_ok=True)
        HTML(string=full_html).write_pdf(
            output_path,
            stylesheets=[CSS(string=css_text)],
        )
    except Exception as e:
        return False, f"WeasyPrint render failed: {type(e).__name__}: {e}"

    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        return False, "WeasyPrint produced no PDF"

    n_bytes = os.path.getsize(output_path)
    print(
        f"   🎨 cv_docx_to_pdf: {os.path.basename(docx_path)} → "
        f"{os.path.basename(output_path)} ({n_bytes:,} bytes)"
    )
    return True, ""


def convert_docx_html(docx_path: str) -> Dict[str, Any]:
    """
    Diagnostic helper — returns the intermediate HTML mammoth produces.
    Used by `tmp_smoke_*` scripts and the diagnostics CLI; production
    paths call `render_pdf_from_docx` directly.
    """
    out: Dict[str, Any] = {"ok": False, "html": "", "messages": [], "reason": ""}
    if not os.path.exists(docx_path):
        out["reason"] = f"DOCX file not found: {docx_path!r}"
        return out
    try:
        import mammoth
        with open(docx_path, "rb") as fh:
            result = mammoth.convert_to_html(fh)
        raw_html = result.value or ""
        out["html"] = _promote_glyph_paragraphs_to_lists(raw_html)
        out["messages"] = [str(m) for m in (result.messages or [])]
        out["ok"] = True
    except Exception as e:
        out["reason"] = f"{type(e).__name__}: {e}"
    return out
