"""
agents.cv_docx_to_pdf
=====================

Render an edited DOCX back to PDF (May 2026 / DOCX path).

Pipeline:    edited DOCX ──[ LibreOffice headless ]──> PDF

Why LibreOffice
---------------
LibreOffice headless renders DOCX faithfully (preserves formatting,
fonts, tables, layout) — essential for the product's format-preservation
principle. mammoth+WeasyPrint rebuilds from HTML/CSS and loses Word-
specific layout features.

Trade-offs
----------
  ✓  Preserves exact DOCX formatting (fonts, tables, layout)
  ✓  No mammoth HTML post-processing needed (glyph paragraphs handled natively)
  ✗  Adds ~350 MB to Streamlit Cloud image (fits under 1 GB limit)
  ✗  Requires libreoffice system dep (declared in packages.txt)

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
import subprocess
from typing import Optional, Tuple


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────

def render_pdf_from_docx(
    docx_path:   str,
    output_path: str,
    extra_css:   Optional[str] = None,  # unused with LibreOffice
) -> Tuple[bool, str]:
    """
    Convert `docx_path` to PDF at `output_path` using LibreOffice headless.

    Returns `(True, "")` on success or `(False, reason)` on failure.

    LibreOffice renders DOCX faithfully (preserves fonts, tables, layout),
    which is essential for the product's format-preservation principle.
    The `extra_css` parameter is unused (LibreOffice uses DOCX styles) but
    kept for API compatibility with the previous mammoth+WeasyPrint version.

    Never raises — returns `(False, reason_string)` so the caller can fall
    back to the rebuild path on any failure.
    """
    if not docx_path or not os.path.exists(docx_path):
        return False, f"DOCX file not found: {docx_path!r}"

    out_dir = os.path.dirname(output_path)
    if out_dir and not os.path.isdir(out_dir):
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception as e:
            return False, f"failed to create output dir: {type(e).__name__}: {e}"

    try:
        # LibreOffice headless: convert DOCX to PDF in the specified output dir.
        # The output filename will match the input stem with .pdf extension.
        result = subprocess.run(
            ["libreoffice", "--headless", "--convert-to", "pdf",
             "--outdir", out_dir, docx_path],
            capture_output=True,
            timeout=120,  # LibreOffice can be slow on large docs
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")[:500]
            return False, f"LibreOffice failed (exit {result.returncode}): {stderr}"
    except FileNotFoundError:
        return False, "LibreOffice not installed (add to packages.txt)"
    except subprocess.TimeoutExpired:
        return False, "LibreOffice conversion timed out (120s)"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

    # LibreOffice names the output <basename>.pdf in the output dir.
    # Rename/move it to the requested output_path if needed.
    base = os.path.splitext(os.path.basename(docx_path))[0]
    lo_output = os.path.join(out_dir, base + ".pdf")
    if not os.path.exists(lo_output):
        return False, f"LibreOffice produced no output (expected {lo_output!r})"

    if lo_output != output_path:
        try:
            os.rename(lo_output, output_path)
        except Exception as e:
            return False, f"failed to rename LibreOffice output: {type(e).__name__}: {e}"

    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        return False, "LibreOffice produced empty PDF"

    n_bytes = os.path.getsize(output_path)
    print(
        f"   🎨 cv_docx_to_pdf (LibreOffice): {os.path.basename(docx_path)} → "
        f"{os.path.basename(output_path)} ({n_bytes:,} bytes)"
    )
    return True, ""
