# agents/pdf_formatter_weasy.py
"""
WeasyPrint-based PDF renderer for the rebuild/fallback path.

Design goals:
  - ATS-safe output: standard fonts, linear reading order, no
    layout-tables, semantic headings (h1/h2/ul/li).
  - Page-count preservation: auto-scales typography so a CV that was
    1 page originally stays 1 page, and a 2-page CV stays 2 pages.
  - Optional: `is_available()` returns False when WeasyPrint / its
    native deps aren't installed, so callers fall back to ReportLab.
"""

from __future__ import annotations

import io
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from weasyprint import HTML  # type: ignore
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    _WEASY_OK = True
except Exception as _import_err:  # pragma: no cover - import guard
    HTML = None  # type: ignore
    Environment = None  # type: ignore
    _WEASY_OK = False


_TEMPLATES_DIR = Path(__file__).parent / "templates"


def is_available() -> bool:
    """True if WeasyPrint + Jinja2 can be imported."""
    return _WEASY_OK


def _env() -> "Environment":
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


# ─────────────────────────────────────────────────────────────
# CV plain-text → structured dict for the Jinja template
# ─────────────────────────────────────────────────────────────

_SECTION_ALIASES = {
    "summary":        ("summary", "professional summary", "profile", "about"),
    "experience":     ("experience", "professional experience", "work experience", "employment"),
    "projects":       ("projects", "featured projects", "selected projects"),
    "education":      ("education", "academic", "qualifications"),
    "skills":         ("skills", "key skills", "technical skills", "core skills"),
    "certifications": ("certifications", "licenses", "awards", "achievements"),
}

_DATE_RX = re.compile(
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s*\d{4}[^\n]*"
    r"|\b\d{4}\s*[-–—]\s*(?:\d{4}|Present|Current|present|current)"
    r"|\b\d{4}\s*[-–—]\s*\d{2}",
    re.IGNORECASE,
)

_BULLET_PREFIX_RX = re.compile(r"^\s*(?:[-•▪●◦*+]|\u2022|\uf0b7|[0-9]+[.)])\s+")


def _classify_heading(text: str) -> Optional[str]:
    low = (text or "").strip().lower().rstrip(":").strip()
    for key, aliases in _SECTION_ALIASES.items():
        if low in aliases:
            return key
    return None


def _is_bullet(line: str) -> bool:
    return bool(_BULLET_PREFIX_RX.match(line or ""))


def _strip_bullet(line: str) -> str:
    return _BULLET_PREFIX_RX.sub("", line or "").strip()


def _split_role_header(line: str) -> Dict[str, str]:
    """
    Split a role-header line into title + dates. Examples:
      'IBM — Test Specialist (2023-2024)' → title='IBM — Test Specialist', dates='2023-2024'
      'J.P. Morgan  June 2021-Present'    → title='J.P. Morgan', dates='June 2021-Present'
    """
    raw = (line or "").strip()
    m = _DATE_RX.search(raw)
    if not m:
        return {"title": raw, "dates": ""}
    dates = m.group(0).strip()
    title = (raw[: m.start()] + raw[m.end():]).strip(" .,-–—(|:;")
    return {"title": title or raw, "dates": dates}


def _parse_cv(cv_text: str) -> Dict[str, Any]:
    lines = [ln.rstrip() for ln in (cv_text or "").splitlines()]

    # ── 1) Header: the first non-empty lines until the first known heading.
    header_lines: List[str] = []
    idx = 0
    while idx < len(lines):
        stripped = lines[idx].strip()
        if not stripped:
            idx += 1
            continue
        if _classify_heading(stripped):
            break
        header_lines.append(stripped)
        idx += 1
        if len(header_lines) >= 5:
            break

    candidate_name = header_lines[0] if header_lines else "Candidate"
    contact_bits: List[str] = []
    for h in header_lines[1:]:
        parts = [p.strip() for p in re.split(r"[\|•·–—]|\s{2,}", h) if p.strip()]
        contact_bits.extend(parts)

    # ── 2) Remaining body: group by section.
    summary_text = ""
    sections: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    current_role: Optional[Dict[str, Any]] = None
    pending_para: List[str] = []

    def flush_para() -> None:
        nonlocal pending_para, current
        if pending_para and current is not None:
            current.setdefault("paragraphs", []).append(" ".join(pending_para).strip())
        pending_para = []

    while idx < len(lines):
        raw = lines[idx]
        stripped = raw.strip()
        idx += 1

        if not stripped:
            flush_para()
            continue

        kind = _classify_heading(stripped)
        if kind:
            flush_para()
            current_role = None
            current = {
                "kind":       kind,
                "heading":    stripped.rstrip(":").strip().title()
                              if kind != "summary" else "Professional Summary",
                "roles":      [],
                "paragraphs": [],
            }
            sections.append(current)
            continue

        # Before any section header, treat as part of header (skip).
        if current is None:
            continue

        if current["kind"] == "summary":
            summary_text = (summary_text + " " + stripped).strip() if summary_text else stripped
            continue

        if current["kind"] in ("experience", "projects"):
            if _is_bullet(stripped):
                bullet_text = _strip_bullet(stripped)
                if current_role is None:
                    # Orphan bullet before any role header — attach to a
                    # synthetic role so we don't lose content.
                    current_role = {"title": current["heading"], "dates": "", "sub": "", "bullets": []}
                    current["roles"].append(current_role)
                current_role.setdefault("bullets", []).append(bullet_text)
            else:
                # New role header OR continuation of previous role's sub-line.
                if current_role is None or current_role.get("bullets"):
                    parts = _split_role_header(stripped)
                    current_role = {
                        "title":   parts["title"],
                        "dates":   parts["dates"],
                        "sub":     "",
                        "bullets": [],
                    }
                    current["roles"].append(current_role)
                else:
                    current_role["sub"] = (current_role.get("sub") + " " + stripped).strip()
            continue

        # Skills / Education / Certifications → bullets or paragraphs.
        if _is_bullet(stripped):
            bullet_text = _strip_bullet(stripped)
            if not current["roles"]:
                current["roles"].append({"title": "", "dates": "", "sub": "", "bullets": []})
            current["roles"][-1].setdefault("bullets", []).append(bullet_text)
        else:
            pending_para.append(stripped)

    flush_para()

    # Drop a synthetic Summary "section" wrapper — template handles summary separately.
    sections = [s for s in sections if s["kind"] != "summary"]

    return {
        "candidate_name": candidate_name,
        "contact_bits":   contact_bits,
        "summary":        summary_text,
        "sections":       sections,
    }


# ─────────────────────────────────────────────────────────────
# Style profile → typography knobs
# ─────────────────────────────────────────────────────────────

def _style_knobs(style_profile: Dict[str, Any], scale: float = 1.0) -> Dict[str, Any]:
    accent = style_profile.get("header_color") or style_profile.get("accent_color") or "#2E4F7F"
    body_font   = style_profile.get("font_body")   or "Helvetica"
    header_font = style_profile.get("font_header") or style_profile.get("font_body_bold") or body_font
    body_size   = float(style_profile.get("font_size_body", 10))
    return {
        "page_size":        "A4" if style_profile.get("page_size") == "A4" else "Letter",
        "margin_left_mm":   max(8, float(style_profile.get("left_margin_mm",   18)) * scale),
        "margin_right_mm":  max(8, float(style_profile.get("right_margin_mm",  18)) * scale),
        "margin_top_mm":    max(8, float(style_profile.get("top_margin_mm",    18)) * scale),
        "margin_bottom_mm": max(8, float(style_profile.get("bottom_margin_mm", 15)) * scale),
        "accent_color":     accent,
        "body_font":        body_font,
        "header_font":      header_font,
        "body_font_size":   round(body_size * scale, 2),
        "small_font_size":  round(max(body_size - 1, 8) * scale, 2),
        "section_font_size":round((body_size + 1.5) * scale, 2),
        "name_font_size":   round((body_size + 8) * scale, 2),
    }


def _render_html(template_name: str, context: Dict[str, Any]) -> str:
    tpl = _env().get_template(template_name)
    return tpl.render(**context)


def _render_pdf_bytes(html_str: str) -> bytes:
    pdf_bytes = HTML(string=html_str).write_pdf()
    return pdf_bytes  # type: ignore[return-value]


def _count_pdf_pages(pdf_bytes: bytes) -> int:
    try:
        import fitz  # PyMuPDF
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            return doc.page_count
    except Exception:
        return 1


def _render_to_target_pages(
    template_name: str,
    context_factory,
    target_pages: int,
) -> bytes:
    """
    Render the template trying to hit `target_pages`. If the first pass
    overflows (too many pages) we shrink typography by 5% and retry, up
    to 3 times. If it under-fills, leave as-is (short CVs looking short
    is fine; trying to pad them would invent content).
    """
    if not _WEASY_OK:
        raise RuntimeError("WeasyPrint not available")
    scale = 1.0
    last_bytes = b""
    for attempt in range(4):
        html_str  = _render_html(template_name, context_factory(scale))
        last_bytes = _render_pdf_bytes(html_str)
        pages = _count_pdf_pages(last_bytes)
        if pages <= max(1, target_pages):
            return last_bytes
        # Overflow: shrink 5% and retry.
        scale *= 0.95
    return last_bytes


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────

def generate_cv_pdf_weasy(
    cv_text:        str,
    job_title:      str,
    company:        str,
    output_dir:     str,
    style_profile:  Dict[str, Any],
    target_pages:   int = 1,
) -> Optional[str]:
    """
    Render the tailored CV using WeasyPrint. Returns the PDF path on
    success, or None if WeasyPrint isn't available / rendering fails.
    `target_pages` — aim to keep the output at this page count (1 or 2).
    """
    if not _WEASY_OK:
        return None

    try:
        os.makedirs(output_dir, exist_ok=True)
        safe_co    = company.replace(" ", "_").replace("/", "-")
        safe_title = job_title.replace(" ", "_").replace("/", "-")
        filepath   = os.path.join(output_dir, f"CV_{safe_co}_{safe_title}.pdf")

        parsed = _parse_cv(cv_text)

        def _ctx(scale: float) -> Dict[str, Any]:
            ctx = _style_knobs(style_profile, scale=scale)
            ctx.update(parsed)
            return ctx

        pdf_bytes = _render_to_target_pages(
            "cv_modern.html",
            _ctx,
            target_pages=max(1, int(target_pages or 1)),
        )
        with open(filepath, "wb") as f:
            f.write(pdf_bytes)
        return filepath
    except Exception as e:
        print(f"   ⚠️  WeasyPrint CV render failed: {type(e).__name__}: {e}")
        return None


# ─────────────────────────────────────────────────────────────
# Cover letter
# ─────────────────────────────────────────────────────────────

_SALUTATION_RX = re.compile(r"^(dear[^\n]*|to whom[^\n]*)", re.IGNORECASE)
_SIGNOFF_RX    = re.compile(
    r"^(warm regards|kind regards|yours sincerely|yours faithfully|sincerely|best regards|regards)",
    re.IGNORECASE,
)


def _parse_cover_letter(cover_letter: str, candidate_name: str) -> Dict[str, Any]:
    """
    Split a flat cover-letter string into salutation / body paragraphs /
    signoff so the HTML template can style each region differently.
    """
    text = (cover_letter or "").strip()
    lines = [ln.rstrip() for ln in text.splitlines()]

    salutation = "Dear Hiring Manager"
    signoff    = "Warm Regards"

    # Detect + consume salutation.
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and _SALUTATION_RX.match(lines[0].strip()):
        salutation = lines.pop(0).strip().rstrip(",")

    # Detect signoff from the tail.
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[-1].strip() == candidate_name.strip():
        lines.pop()
        while lines and not lines[-1].strip():
            lines.pop()
    if lines and _SIGNOFF_RX.match(lines[-1].strip()):
        signoff = lines.pop().strip().rstrip(",")

    # Group remaining lines into paragraphs (blank line = boundary).
    paragraphs: List[str] = []
    buf: List[str] = []
    for ln in lines:
        if not ln.strip():
            if buf:
                paragraphs.append(" ".join(s.strip() for s in buf).strip())
                buf = []
        else:
            buf.append(ln)
    if buf:
        paragraphs.append(" ".join(s.strip() for s in buf).strip())

    return {
        "salutation":      salutation,
        "body_paragraphs": paragraphs,
        "signoff":         signoff,
    }


def generate_cover_letter_pdf_weasy(
    cover_letter:    str,
    job_title:       str,
    company:         str,
    output_dir:      str,
    style_profile:   Dict[str, Any],
    candidate_name:  str,
    contact_bits:    Optional[List[str]] = None,
) -> Optional[str]:
    if not _WEASY_OK:
        return None

    try:
        os.makedirs(output_dir, exist_ok=True)
        safe_co    = company.replace(" ", "_").replace("/", "-")
        safe_title = job_title.replace(" ", "_").replace("/", "-")
        filepath   = os.path.join(output_dir, f"CoverLetter_{safe_co}_{safe_title}.pdf")

        parts = _parse_cover_letter(cover_letter, candidate_name)

        def _ctx(scale: float) -> Dict[str, Any]:
            ctx = _style_knobs(style_profile, scale=scale)
            ctx.update({
                "candidate_name": candidate_name,
                "contact_bits":   contact_bits or [],
                "date_line":      datetime.now().strftime("%d %B %Y"),
                "recipient":      f"Hiring Team, {company}" if company else "",
                **parts,
            })
            return ctx

        # Cover letters are always single-page.
        pdf_bytes = _render_to_target_pages("cover_letter_modern.html", _ctx, target_pages=1)
        with open(filepath, "wb") as f:
            f.write(pdf_bytes)
        return filepath
    except Exception as e:
        print(f"   ⚠️  WeasyPrint cover letter render failed: {type(e).__name__}: {e}")
        return None
