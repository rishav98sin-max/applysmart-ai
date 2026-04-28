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
    "summary":        ("summary", "professional summary", "profile", "about",
                       "career summary", "executive summary",
                       "summary of qualifications"),
    "experience":     ("experience", "professional experience", "work experience",
                       "employment", "career history", "work history",
                       "employment history", "professional background"),
    "projects":       ("projects", "featured projects", "selected projects",
                       "personal projects", "self projects", "self-projects",
                       "side projects", "side-projects", "key projects",
                       "notable projects", "recent projects", "academic projects",
                       "independent projects", "relevant projects",
                       "own projects", "passion projects", "portfolio",
                       "project experience", "project work"),
    "education":      ("education", "academic", "qualifications",
                       "academic background", "academic qualifications",
                       "degrees", "academic credentials"),
    "skills":         ("skills", "key skills", "technical skills", "core skills",
                       "expertise", "competencies", "professional skills",
                       "core competencies", "technical expertise",
                       "areas of expertise"),
    "certifications": ("certifications", "licenses", "awards", "achievements",
                       "professional certifications", "credentials",
                       "honors", "honors & awards", "honours", "awards & honors"),
}

# Apr 28 follow-up: regex fallback for project headings that don't exactly
# match a phrase in the alias table. Captures arbitrary qualifiers like
# "Independent", "Open Source", "Capstone", "Research", etc. before
# "Project(s)", which the static alias list can't enumerate exhaustively.
# Pattern matches lines like:
#   "Capstone Projects", "Open-Source Projects", "Research Project",
#   "Side Project Highlights", "Major Projects" — all classified as projects.
_PROJECTS_HEADING_RX = re.compile(
    r"^\s*([a-z][a-z\-]*[\s\-]+){0,3}projects?\s*(highlights|portfolio|showcase)?\s*:?\s*$",
    re.IGNORECASE,
)


_DATE_RX = re.compile(
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s*\d{4}[^\n]*"
    r"|\b\d{4}\s*[-–—]\s*(?:\d{4}|Present|Current|present|current)"
    r"|\b\d{4}\s*[-–—]\s*\d{2}",
    re.IGNORECASE,
)

_BULLET_PREFIX_RX = re.compile(r"^\s*(?:[-•▪●◦*+]|\u2022|\uf0b7|[0-9]+[.)])\s+")

# Matches a bullet glyph (with or without trailing whitespace) at the start
# of a line — used to recognise continuation lines vs new bullets.
_BULLET_GLYPH_RX = re.compile(r"^\s*(?:[-•▪●◦*+]|\u2022|\uf0b7|[0-9]+[.)])")


def _classify_heading(text: str) -> Optional[str]:
    low = (text or "").strip().lower().rstrip(":").strip()
    for key, aliases in _SECTION_ALIASES.items():
        if low in aliases:
            return key
    # Apr 28 follow-up: regex fallback for project-section headings whose
    # exact phrasing isn't in the static alias table (e.g. "Capstone
    # Projects", "Open-Source Projects", "Side Project Highlights"). Length
    # cap (<=40 chars) prevents misclassifying a paragraph that happens to
    # contain the word "projects" inline.
    if len(low) <= 40 and _PROJECTS_HEADING_RX.match(low):
        return "projects"
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


_EMAIL_RX = re.compile(r"[\w\.\+-]+@[\w\.-]+\.[A-Za-z]{2,}")
_PHONE_RX = re.compile(r"(?:\+?\d[\d\s\-()]{6,}\d)")
_URL_RX   = re.compile(r"https?://\S+|www\.\S+|linkedin\.com/\S+", re.IGNORECASE)
_NAME_LIKE_RX = re.compile(
    r"^[A-Z][A-Za-z'’.\-]+(?:\s+[A-Z][A-Za-z'’.\-]+){1,4}$"
)


# Words that look name-shaped (Title Case + 2 tokens) but are actually
# section labels from designer templates. Treat as non-names.
_NAME_BLOCKLIST = {
    "scholastic record", "work experience", "professional experience",
    "work history", "key skills", "technical skills", "core skills",
    "core competencies", "awards achievements", "awards and achievements",
    "new business", "project experience", "business development",
}


def _looks_like_name(text: str) -> bool:
    """Best-effort name detector: 2-5 Title-Case tokens, no digits/@/urls."""
    s = (text or "").strip()
    if not s or len(s) > 60:
        return False
    if _EMAIL_RX.search(s) or _PHONE_RX.search(s) or _URL_RX.search(s):
        return False
    if any(c.isdigit() for c in s):
        return False
    # Reject ALL-CAPS strings (e.g. 'SCHOLASTIC RECORD') — real names are
    # typically Title Case, never all upper.
    letters = [c for c in s if c.isalpha()]
    if letters and all(c.isupper() for c in letters):
        return False
    low = re.sub(r"[^a-z ]+", " ", s.lower()).strip()
    low = re.sub(r"\s+", " ", low)
    if low in _NAME_BLOCKLIST:
        return False
    return bool(_NAME_LIKE_RX.match(s))


def _coalesce_bullet_bodies(lines: List[str]) -> List[str]:
    """
    Many CVs wrap bullet bodies across 2-3 lines. After the cv_parser step
    that joins standalone bullet glyphs with their first body line, we still
    see continuation lines with no bullet marker, e.g.

        • Spearheaded the development and evolution of integrated marketing
        LinkedIn, Instagram, YouTube — ensuring alignment with brand
        business objectives.

        • Planned and executed robust, platform-specific content calendars

    The downstream section parser expects one bullet per line. Without
    this coalescing it treats each continuation line as a new role header.

    Rule: a non-empty, non-bullet line that directly follows a bullet line
    (or a previous continuation) and is NOT a recognised section heading
    is merged into the preceding bullet. A blank line ends the merge.
    """
    out: List[str] = []
    in_bullet = False
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            out.append(raw)
            in_bullet = False
            continue
        is_bullet_line = bool(_BULLET_GLYPH_RX.match(stripped))
        is_heading     = _classify_heading(stripped) is not None
        if (
            in_bullet
            and not is_bullet_line
            and not is_heading
        ):
            # Merge into last output line.
            if out:
                out[-1] = (out[-1].rstrip() + " " + stripped).rstrip()
            else:
                out.append(raw)
            continue
        out.append(raw)
        in_bullet = is_bullet_line
    return out


def _parse_cv(cv_text: str) -> Dict[str, Any]:
    lines = [ln.rstrip() for ln in (cv_text or "").splitlines()]

    # Pre-pass: merge wrapped bullet bodies into single-line bullets.
    lines = _coalesce_bullet_bodies(lines)

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
        if len(header_lines) >= 8:
            break

    # Prefer a real name line over whatever happens to come first (often an
    # email, phone or tagline on designer templates).
    candidate_name = "Candidate"
    for h in header_lines:
        if _looks_like_name(h):
            candidate_name = h
            break
    if candidate_name == "Candidate" and header_lines:
        # Fallback: first header line if it at least isn't an email/phone.
        first = header_lines[0]
        if not (_EMAIL_RX.search(first) or _PHONE_RX.search(first)):
            candidate_name = first
        else:
            # Derive a display name from the local-part of the email as a
            # last resort — better than literally printing the address.
            m = _EMAIL_RX.search(first)
            if m:
                local = m.group(0).split("@", 1)[0]
                # Strip ALL digits (emails often embed birth years mid-name),
                # replace separators with spaces, collapse whitespace.
                local = re.sub(r"[._\-]+", " ", local)
                local = re.sub(r"\d+", " ", local)
                local = re.sub(r"\s+", " ", local).strip()
                if local:
                    candidate_name = local.title()

    contact_bits: List[str] = []
    for h in header_lines:
        if h == candidate_name:
            continue
        parts = [p.strip() for p in re.split(r"[\|•·–—]|\s{2,}", h) if p.strip()]
        for p in parts:
            # Filter out designer-template noise that isn't contact info.
            if re.match(r"(?i)^(scholastic|work\s+experience|\d+\s*years?|female|male|dob[:\s])", p):
                continue
            contact_bits.append(p)

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

    # ── Section-kind repair pass ─────────────────────────────
    # A common designer-template pattern is to write "PROFESSIONAL EXPERIENCE"
    # directly above the summary paragraph (and then "WORK EXPERIENCE" above
    # the actual role list). When that happens, the first experience-kind
    # section contains no bullets, just a paragraph — which is really a
    # summary. Promote it, so the template renders it in the right place.
    if not summary_text:
        for s in list(sections):
            if s["kind"] != "experience":
                continue
            has_bullets = any(
                len(r.get("bullets") or []) > 0 for r in s.get("roles", [])
            )
            has_paragraphs = bool(
                (s.get("paragraphs") or [])
                or any((r.get("title") or "").strip() for r in s.get("roles", []))
            )
            if has_bullets or not has_paragraphs:
                continue
            # Collect all paragraph-like text from this pseudo-section —
            # include role.title AND role.sub, which together hold the
            # soft-wrapped summary body after the section repair pass.
            collected: List[str] = []
            for p in (s.get("paragraphs") or []):
                if p.strip():
                    collected.append(p.strip())
            for r in s.get("roles", []):
                t = (r.get("title") or "").strip()
                if t:
                    collected.append(t)
                sub = (r.get("sub") or "").strip()
                if sub:
                    collected.append(sub)
            promoted = " ".join(collected).strip()
            if promoted:
                summary_text = promoted
                sections.remove(s)
                break

    # Drop a synthetic Summary "section" wrapper — template handles summary separately.
    sections = [s for s in sections if s["kind"] != "summary"]

    # ── Enforce canonical ATS order:
    #    Experience → Projects → Education → Certifications/Achievements → Skills.
    # Skills sits AFTER certifications because recruiters skim the narrative
    # blocks (experience→education→achievements) first, and the keyword-dense
    # skills line is most useful as the closing reference, not mid-document.
    _ORDER = {"experience": 0, "projects": 1, "education": 2, "certifications": 3, "skills": 4}
    sections.sort(key=lambda s: _ORDER.get(s.get("kind", ""), 99))

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

    # P7-followup (Apr 28): salutation reverted to "Dear Hiring Manager"
    # per user preference. If the cover-letter generator emits a different
    # one (e.g. "Dear Sarah" when the JD includes a hiring manager name),
    # the salutation regex below will capture and pass it through.
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

        # P7 (Apr 28): new layout.
        #   • company  — top-left header (replaces the old sender/name block)
        #   • subject  — single-line "Application for <Role>" / "Application
        #                for <Role> at <Company>"
        # No longer passed: candidate_name (top), contact_bits, date_line,
        # recipient. The candidate's name appears only in the signoff block.
        subject_line = (
            f"Application for {job_title}" if job_title else "Application"
        )
        if company and job_title:
            # Slightly more specific phrasing when both are known
            subject_line = f"Application for {job_title} at {company}"

        def _ctx(scale: float) -> Dict[str, Any]:
            ctx = _style_knobs(style_profile, scale=scale)
            ctx.update({
                "candidate_name": candidate_name,
                "company":        company or "",
                "subject_line":   subject_line,
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
