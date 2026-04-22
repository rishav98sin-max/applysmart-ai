# agents/pdf_formatter.py

import os
import re
from typing import List, Optional, Tuple

import fitz  # PyMuPDF
from unidecode import unidecode
from reportlab.lib.pagesizes import A4, letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, black, white
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer,
    HRFlowable, Table, TableStyle, KeepTogether
)
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
# ─────────────────────────────────────────────────────────────
# TEXT SANITISER — fixes ALL ? marks
# ─────────────────────────────────────────────────────────────

def _safe_text(text: str) -> str:
    """
    Convert ALL unicode to safe ASCII using unidecode.
    Handles: em dashes, smart quotes, bullets, accented chars, arrows etc.
    Then escapes XML special chars for ReportLab.
    Examples:
        —  →  -       "  →  "       •  →  *
        é  →  e       ×  →  x       →  →  ->
    """
    if not text:
        return ""
    cleaned = unidecode(str(text))
    return (cleaned
            .replace("&",  "&amp;")
            .replace("<",  "&lt;")
            .replace(">",  "&gt;")
            .replace('"',  "&quot;")
            .replace("'",  "&#39;"))


# ─────────────────────────────────────────────────────────────
# STYLE EXTRACTOR
# ─────────────────────────────────────────────────────────────

def extract_cv_style(pdf_path: str) -> dict:
    style = {
        "primary_color":    "#2C3E50",
        "accent_color":     "#2980B9",
        "page_size":        "A4",
        "has_color_header": False,
        "header_color":     "#2C3E50",
        "font_size_body":   10,
        "font_size_header": 14,
    }

    try:
        doc   = fitz.open(pdf_path)
        page  = doc[0]

        color_counts = {}
        blocks = page.get_text("dict")["blocks"]

        for block in blocks:
            if block.get("type") == 0:
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        color_int = span.get("color", 0)
                        size      = span.get("size", 10)

                        r = (color_int >> 16) & 0xFF
                        g = (color_int >> 8)  & 0xFF
                        b = color_int         & 0xFF
                        hex_color = f"#{r:02X}{g:02X}{b:02X}"

                        if hex_color not in ("#000000", "#FFFFFF", "#FEFEFE", "#010101"):
                            color_counts[hex_color] = color_counts.get(hex_color, 0) + 1

                        if size > style["font_size_header"]:
                            style["font_size_header"] = int(size)

        if color_counts:
            primary = max(color_counts, key=color_counts.get)
            style["primary_color"] = primary
            sorted_colors = sorted(color_counts, key=color_counts.get, reverse=True)
            style["accent_color"] = sorted_colors[1] if len(sorted_colors) > 1 else primary

        drawings   = page.get_drawings()
        for d in drawings:
            rect = d.get("rect")
            if rect and rect[1] < 80:
                fill = d.get("fill")
                if fill and fill != (1, 1, 1):
                    style["has_color_header"] = True
                    r = int(fill[0] * 255)
                    g = int(fill[1] * 255)
                    b = int(fill[2] * 255)
                    style["header_color"] = f"#{r:02X}{g:02X}{b:02X}"
                    style["primary_color"] = style["header_color"]
                    break

        width  = page.rect.width
        height = page.rect.height
        style["page_size"] = "A4" if abs(height - 842) < 20 else "Letter"
        doc.close()

    except Exception as e:
        print(f"Style extraction warning: {e} — using defaults")

    return style


# ─────────────────────────────────────────────────────────────
# BUILD STYLES
# ─────────────────────────────────────────────────────────────

def _build_styles(style_profile: dict):
    primary = HexColor(style_profile["primary_color"])
    accent  = HexColor(style_profile["accent_color"])

    fb  = style_profile.get("font_body", "Helvetica")
    fbb = style_profile.get("font_body_bold", "Helvetica-Bold")
    fh  = style_profile.get("font_header", fbb)
    fs  = float(style_profile.get("font_size_body", 10))
    fsh = float(style_profile.get("font_size_header", max(fs + 1, 11)))
    lead = float(style_profile.get("body_leading") or round(fs * 1.22, 1))

    styles = {
        "name": ParagraphStyle(
            "CandidateName",
            fontName=fh,
            fontSize=max(fsh, 16),
            textColor=white if style_profile["has_color_header"] else primary,
            alignment=TA_LEFT,
            spaceAfter=2,
        ),
        "contact": ParagraphStyle(
            "Contact",
            fontName=fb,
            fontSize=max(fs - 1, 8),
            textColor=white if style_profile["has_color_header"] else HexColor("#555555"),
            alignment=TA_LEFT,
            spaceAfter=0,
        ),
        "section_heading": ParagraphStyle(
            "SectionHeading",
            fontName=fbb,
            fontSize=min(fsh, 12),
            textColor=primary,
            alignment=TA_LEFT,
            spaceBefore=10,
            spaceAfter=3,
        ),
        "job_title": ParagraphStyle(
            "JobTitle",
            fontName=fbb,
            fontSize=fs,
            textColor=black,
            alignment=TA_LEFT,
            spaceBefore=6,
            spaceAfter=1,
        ),
        "job_title_right": ParagraphStyle(
            "JobTitleRight",
            fontName=fbb,
            fontSize=fs,
            textColor=black,
            alignment=TA_RIGHT,
            spaceBefore=6,
            spaceAfter=1,
        ),
        "bullet": ParagraphStyle(
            "Bullet",
            fontName=fb,
            fontSize=fs,
            textColor=HexColor("#333333"),
            alignment=TA_JUSTIFY,
            leftIndent=12,
            spaceBefore=1,
            spaceAfter=1,
            leading=lead,
        ),
        "body": ParagraphStyle(
            "Body",
            fontName=fb,
            fontSize=fs,
            textColor=HexColor("#333333"),
            alignment=TA_JUSTIFY,
            spaceAfter=4,
            leading=lead,
        ),
        "skills_tag": ParagraphStyle(
            "SkillsTag",
            fontName=fb,
            fontSize=min(fs + 0.5, 11),
            textColor=HexColor("#333333"),
            alignment=TA_LEFT,
            spaceAfter=3,
        ),
    }
    return styles, primary, accent


# ─────────────────────────────────────────────────────────────
# CV SECTION PARSER
# ─────────────────────────────────────────────────────────────

def _strip_header_markdown(line: str) -> str:
    s = line.strip()
    s = re.sub(r"^#+\s*", "", s)
    s = re.sub(r"^\*+\s*|\s*\*+$", "", s).strip()
    return s


def _match_section_type(line: str) -> Optional[str]:
    """
    Map a standalone heading line to a logical section. Uses full phrases first
    so job titles like 'Technical Product Specialist' are never mistaken for sections.
    """
    raw = _strip_header_markdown(line)
    if not raw or len(raw) > 88:
        return None
    if raw.startswith(("-", "•", "·")) or re.match(r"^\d+[\.)]\s", raw):
        return None

    hl = raw.lower().strip()

    # Longest / most specific first
    exact = [
        ("professional summary", "summary"),
        ("career summary", "summary"),
        ("executive summary", "summary"),
        ("summary", "summary"),
        ("featured projects", "projects"),
        ("key projects", "projects"),
        ("projects", "projects"),
        ("professional experience", "experience"),
        ("work experience", "experience"),
        ("employment history", "experience"),
        ("relevant experience", "experience"),
        ("experience", "experience"),
        ("academic achievements", "education"),
        ("academic background", "education"),
        ("education", "education"),
        ("technical skills", "skills"),
        ("core skills", "skills"),
        ("skills", "skills"),
        ("certifications", "certifications"),
        ("certificates", "certifications"),
    ]
    for phrase, stype in exact:
        if hl == phrase:
            return stype

    # Soft match: heading is phrase + optional short suffix (e.g. extra words)
    soft = [
        ("professional summary", "summary"),
        ("featured projects", "projects"),
        ("professional experience", "experience"),
        ("academic achievements", "education"),
    ]
    for phrase, stype in soft:
        if hl.startswith(phrase) and len(hl) <= len(phrase) + 20:
            return stype

    return None


def _parse_cv_sections(cv_text: str) -> List[Tuple[str, List[str], str]]:
    """
    Returns list of (section_type, lines, heading_display).
    heading_display is the cleaned heading text from the CV for PDF titles.
    """
    sections: List[Tuple[str, List[str], str]] = []
    current_section: Optional[str] = None
    current_heading = ""
    current_lines: list = []

    for line in cv_text.split("\n"):
        stripped = line.strip()
        if not stripped:
            if current_lines is not None:
                current_lines.append("")
            continue

        sec = _match_section_type(stripped)
        if sec:
            if current_section is not None and current_lines:
                sections.append((current_section, current_lines[:], current_heading))
            current_section = sec
            current_heading = _strip_header_markdown(stripped).upper()
            current_lines = []
        else:
            if current_section is None:
                current_section = "header"
                current_heading = ""
            current_lines.append(stripped)

    if current_section is not None and current_lines:
        sections.append((current_section, current_lines[:], current_heading))

    return sections


def _xml_escape(s: str) -> str:
    if not s:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _render_section_heading(story, title: str, styles, primary):
    story.append(Paragraph(_safe_text(title), styles["section_heading"]))
    story.append(HRFlowable(width="100%", thickness=1, color=primary, spaceAfter=4))


def _render_skills_body(lines: list, story, styles, primary):
    """Skills with optional categories like Product:, AI:, Tools: — not one pipe-blob."""
    cat_re = re.compile(
        r"^\*?\*?\s*([A-Za-z][A-Za-z0-9\s/&+]{0,48})\s*:\s*(.*)$"
    )
    for line in lines:
        line = line.strip()
        if not line:
            story.append(Spacer(1, 2))
            continue
        if line.upper().startswith("SKILL") and len(line) < 22:
            continue
        m = cat_re.match(line)
        if m and m.group(1).strip().lower() not in ("http", "www"):
            cat, rest = m.group(1).strip(), m.group(2).strip()
            inner = f"<b>{_xml_escape(cat)}:</b> {_xml_escape(unidecode(rest))}"
            story.append(Paragraph(inner, styles["body"]))
            continue
        if line.startswith(("-", "•", "*", "·")):
            t = line.lstrip("-•*· ").strip()
            story.append(
                Paragraph(_safe_text(f"- {t}"), styles["bullet"])
            )
        else:
            story.append(Paragraph(_safe_text(line), styles["body"]))


def _looks_like_academic_table(lines: list) -> bool:
    blob = "\n".join(lines)
    if "|" not in blob:
        return False
    if re.search(r"year\s*\|.*degree|degree\s*\|.*university", blob, re.I):
        return True
    if re.search(r"\d{4}\s*[-–]\s*\d{4}\s*\|", blob):
        return True
    return False


def _render_academic_table(lines: list, story, styles, primary) -> None:
    """Renders Year | Degree | Institution | Grade style blocks as a bordered table."""
    raw_lines = [ln.strip() for ln in lines if ln.strip()]
    rows_raw: List[List[str]] = []
    header_row: Optional[List[str]] = None

    for ln in raw_lines:
        if "|" not in ln:
            continue
        parts = [p.strip() for p in ln.split("|")]
        if len(parts) < 3:
            continue
        joined = " ".join(parts).lower()
        if "year" in joined and "degree" in joined:
            header_row = parts
            continue
        rows_raw.append(parts)

    if not rows_raw:
        for line in lines:
            if line.strip():
                story.append(Paragraph(_safe_text(line.strip()), styles["body"]))
        return

    ncol = max(len(r) for r in rows_raw + ([header_row] if header_row else []))
    if header_row:
        header_row = header_row + [""] * (ncol - len(header_row))
    norm = [r + [""] * (ncol - len(r)) for r in rows_raw]

    data_rows = []
    if header_row:
        data_rows.append(
            [Paragraph(_safe_text(c), styles["job_title"]) for c in header_row[:ncol]]
        )
    data_rows.extend(
        [[Paragraph(_safe_text(c), styles["body"]) for c in row[:ncol]] for row in norm]
    )

    w = (170 * mm) / float(ncol)
    t = Table(data_rows, colWidths=[w] * ncol, repeatRows=1 if header_row else 0)
    t.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.5, black),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(t)
    story.append(Spacer(1, 3 * mm))


def _render_experience_block(lines: list, story, styles, primary):
    """Experience / projects: roles, bullets; handles lines with trailing dates."""
    date_tail = re.compile(
        r"^(.+?)\s{2,}((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}.*|"
        r"\d{4}\s*[—–-]\s*(?:Present|\d{4}).*|(?:Present|\d{4}).*)$",
        re.I,
    )

    current_role = None
    current_bullets: list = []

    def flush_role(role, bullets):
        if not role:
            return
        r = role.strip()
        left, right = None, None
        m = date_tail.match(r)
        if m:
            left, right = m.group(1).strip(), m.group(2).strip()
        else:
            m2 = re.search(
                r"^(.+?)\s{2,}((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}.*|"
                r"\d{4}\s*[—–\-]\s*(?:Present|\d{4}).*)$",
                r,
                re.I,
            )
            if m2:
                left, right = m2.group(1).strip(), m2.group(2).strip()

        if left and right:
            row = Table(
                [
                    [
                        Paragraph(_safe_text(left), styles["job_title"]),
                        Paragraph(_safe_text(right), styles["job_title_right"]),
                    ]
                ],
                colWidths=["72%", "28%"],
            )
            row.setStyle(
                TableStyle(
                    [
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                    ]
                )
            )
            items = [row]
        else:
            items = [Paragraph(_safe_text(r), styles["job_title"])]

        for b in bullets:
            if b.strip():
                items.append(
                    Paragraph(
                        _safe_text(f"- {b.lstrip('-*•').strip()}"),
                        styles["bullet"],
                    )
                )
        story.append(KeepTogether(items))
        story.append(Spacer(1, 2 * mm))

    for line in lines:
        if not line.strip():
            continue
        line = line.strip()
        is_bullet = line.startswith(("-", "*", "•")) or re.match(r"^[•·]\s", line)
        has_date_right = bool(
            re.search(
                r"\s{2,}((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}|\d{4}\s*[—–\-])",
                line,
                re.I,
            )
        )
        looks_role = (
            (("|" in line or "—" in line or "–" in line) and len(line) < 240 and not is_bullet)
            or bool(date_tail.match(line))
            or has_date_right
        )

        if is_bullet:
            current_bullets.append(line)
        elif looks_role and not is_bullet:
            flush_role(current_role, current_bullets)
            current_role = line
            current_bullets = []
        else:
            if current_role:
                current_bullets.append(line)
            else:
                current_role = line

    flush_role(current_role, current_bullets)


# Default PDF headings when the CV text does not carry an explicit title line.
_DEFAULT_SECTION_TITLE = {
    "summary": "PROFESSIONAL SUMMARY",
    "experience": "PROFESSIONAL EXPERIENCE",
    "education": "ACADEMIC ACHIEVEMENTS",
    "skills": "SKILLS",
    "projects": "FEATURED PROJECTS",
    "certifications": "CERTIFICATIONS",
}


def _display_section_title(section_type: str, heading_from_cv: str) -> str:
    h = (heading_from_cv or "").strip()
    if h:
        return h
    return _DEFAULT_SECTION_TITLE.get(
        section_type, section_type.replace("_", " ").upper()
    )


# ─────────────────────────────────────────────────────────────
# CV PDF GENERATOR
# ─────────────────────────────────────────────────────────────

# Prefer the HTML+CSS (WeasyPrint) renderer for the rebuild path — it
# produces ATS-safe output that looks closer to a proper CV than the
# ReportLab flow. ReportLab stays as a safety net for dev environments
# where WeasyPrint's native deps (libpango, cairo) can't be installed.
def _try_weasy_cv(
    cv_text: str,
    job_title: str,
    company: str,
    output_dir: str,
    style_profile: dict,
) -> Optional[str]:
    try:
        from agents import pdf_formatter_weasy as _weasy
    except Exception as e:
        print(f"   ⚠️  pdf_formatter_weasy import failed: {e}")
        return None
    if not _weasy.is_available():
        return None
    target = int(style_profile.get("page_count") or 1)
    return _weasy.generate_cv_pdf_weasy(
        cv_text=cv_text,
        job_title=job_title,
        company=company,
        output_dir=output_dir,
        style_profile=style_profile,
        target_pages=max(1, target),
    )


def generate_cv_pdf_styled(
    cv_text: str,
    job_title: str,
    company: str,
    output_dir: str,
    style_profile: dict
) -> str:
    # 1) Try WeasyPrint / HTML-CSS route first.
    weasy_path = _try_weasy_cv(
        cv_text=cv_text,
        job_title=job_title,
        company=company,
        output_dir=output_dir,
        style_profile=style_profile,
    )
    if weasy_path and os.path.exists(weasy_path) and os.path.getsize(weasy_path) > 0:
        print(f"   🎨 CV rendered via WeasyPrint → {os.path.basename(weasy_path)}")
        return weasy_path
    # 2) Fall back to the legacy ReportLab renderer below.
    safe_co = company.replace(" ", "_").replace("/", "-")
    safe_title = job_title.replace(" ", "_").replace("/", "-")
    filepath = os.path.join(output_dir, f"CV_{safe_co}_{safe_title}.pdf")

    lm = float(style_profile.get("left_margin_mm", 18))
    rm = float(style_profile.get("right_margin_mm", 18))
    tm = float(style_profile.get("top_margin_mm", 18))
    bm = float(style_profile.get("bottom_margin_mm", 15))
    if style_profile.get("has_color_header"):
        tm = min(tm, 16.0)

    page_size = A4 if style_profile.get("page_size") == "A4" else letter
    doc = SimpleDocTemplate(
        filepath,
        pagesize     = page_size,
        leftMargin   = lm * mm,
        rightMargin  = rm * mm,
        topMargin    = tm * mm,
        bottomMargin = bm * mm,
    )

    styles, primary, _ = _build_styles(style_profile)
    story: List = []
    sections = _parse_cv_sections(cv_text)

    # Enforce canonical ATS order: header → summary → experience → projects
    # → education → skills → certifications → others. "header" always first.
    _ORDER = {
        "header": 0, "summary": 1, "experience": 2, "projects": 3,
        "education": 4, "skills": 5, "certifications": 6,
    }
    sections = sorted(sections, key=lambda s: _ORDER.get(s[0], 99))

    fb = style_profile.get("font_body", "Helvetica")
    fs = float(style_profile.get("font_size_body", 10))
    fh = style_profile.get("font_header", style_profile.get("font_body_bold", "Helvetica-Bold"))

    for section_type, lines, sec_heading in sections:

        # ── HEADER ────────────────────────────────────────────
        if section_type == "header":
            if style_profile["has_color_header"]:
                header_color = HexColor(style_profile["header_color"])
                name_line    = lines[0] if lines else "Candidate"
                contact_line = " | ".join(lines[1:3]) if len(lines) > 1 else ""
                header_data  = [[
                    Paragraph(_safe_text(name_line),    styles["name"]),
                    Paragraph(_safe_text(contact_line), styles["contact"]),
                ]]
                header_table = Table(header_data, colWidths=["60%", "40%"])
                header_table.setStyle(TableStyle([
                    ("BACKGROUND",   (0,0), (-1,-1), header_color),
                    ("LEFTPADDING",  (0,0), (-1,-1), 14),
                    ("RIGHTPADDING", (0,0), (-1,-1), 14),
                    ("TOPPADDING",   (0,0), (-1,-1), 14),
                    ("BOTTOMPADDING",(0,0), (-1,-1), 14),
                    ("VALIGN",       (0,0), (-1,-1), "MIDDLE"),
                ]))
                story.append(header_table)
                story.append(Spacer(1, 8*mm))
            else:
                name_line = lines[0] if lines else "Candidate"
                contact_line = ""
                if len(lines) > 1:
                    contact_line = " • ".join(
                        x.strip() for x in lines[1:] if x.strip()
                    )
                name_style = ParagraphStyle(
                    "NameCenter",
                    fontName=fh,
                    fontSize=max(fs + 6, 16),
                    textColor=primary,
                    alignment=TA_CENTER,
                    spaceAfter=4,
                )
                story.append(Paragraph(_safe_text(name_line), name_style))
                if contact_line:
                    c_style = ParagraphStyle(
                        "ContactCenter",
                        fontName=fb,
                        fontSize=max(fs - 1, 9),
                        textColor=HexColor("#333333"),
                        alignment=TA_CENTER,
                        spaceAfter=6,
                    )
                    story.append(Paragraph(_safe_text(contact_line), c_style))
                story.append(HRFlowable(width="100%", thickness=0.5, color=primary, spaceAfter=6))
                story.append(Spacer(1, 2*mm))

        # ── SUMMARY ───────────────────────────────────────────
        elif section_type == "summary":
            title = _display_section_title("summary", sec_heading)
            _render_section_heading(story, title, styles, primary)
            summary_text = " ".join(l for l in lines if l)
            story.append(Paragraph(_safe_text(summary_text), styles["body"]))

        # ── EXPERIENCE ────────────────────────────────────────
        elif section_type == "experience":
            title = _display_section_title("experience", sec_heading)
            _render_section_heading(story, title, styles, primary)
            _render_experience_block(lines, story, styles, primary)

        # ── PROJECTS ────────────────────────────────────────────
        elif section_type == "projects":
            title = _display_section_title("projects", sec_heading)
            _render_section_heading(story, title, styles, primary)
            _render_experience_block(lines, story, styles, primary)

        # ── EDUCATION ─────────────────────────────────────────
        elif section_type == "education":
            title = _display_section_title("education", sec_heading)
            _render_section_heading(story, title, styles, primary)
            if _looks_like_academic_table(lines):
                _render_academic_table(lines, story, styles, primary)
            else:
                for line in lines:
                    if not line.strip():
                        story.append(Spacer(1, 2*mm))
                        continue
                    story.append(Paragraph(_safe_text(line.strip()), styles["body"]))

        # ── SKILLS ────────────────────────────────────────────
        elif section_type == "skills":
            title = _display_section_title("skills", sec_heading)
            _render_section_heading(story, title, styles, primary)
            _render_skills_body(lines, story, styles, primary)

        # ── CERTIFICATIONS & OTHER ────────────────────────────
        else:
            title = _display_section_title(section_type, sec_heading)
            _render_section_heading(story, title, styles, primary)
            for line in lines:
                if not line:
                    story.append(Spacer(1, 2*mm))
                elif line.startswith(("-", "*", "•")):
                    story.append(Paragraph(
                        _safe_text(f"- {line.lstrip('-*•').strip()}"),
                        styles["bullet"],
                    ))
                else:
                    story.append(Paragraph(_safe_text(line), styles["body"]))

    doc.build(story)
    return filepath


# ─────────────────────────────────────────────────────────────
# COVER LETTER PDF GENERATOR
# ─────────────────────────────────────────────────────────────

def _parse_cover_letter_structure(
    cover_letter: str,
    candidate_name: str,
) -> tuple:
    """
    Expects: Dear Hiring Manager + body + Warm Regards + name.
    Returns (salutation, body_text, closing_line, signer_name).
    """
    text = (cover_letter or "").strip()
    sal = "Dear Hiring Manager"

    m = re.match(
        r"(?is)^\s*Dear\s+Hiring\s+Manager\s*\n+(.*?)\n+\s*Warm\s+Regards\s*\n+\s*(.+?)\s*$",
        text,
        re.DOTALL,
    )
    if m:
        body, signer = m.group(1).strip(), m.group(2).strip()
        return sal, body, "Warm Regards", signer

    # Fallback: strip any LLM salutation/sign-off heuristically
    body = text
    body = re.sub(
        r"(?is)^\s*(dear|to)\s+[^,\n]+,?\s*\n+",
        "",
        body,
        count=1,
    )
    body = re.sub(
        r"(?is)\n+\s*(yours\s+sincerely|sincerely|best regards|kind regards|regards)[^\n]*\n+.*$",
        "",
        body,
    )
    return sal, body.strip(), "Warm Regards", candidate_name.strip()


def _try_weasy_cl(
    cover_letter: str,
    job_title: str,
    company: str,
    output_dir: str,
    style_profile: dict,
    candidate_name: str,
) -> Optional[str]:
    try:
        from agents import pdf_formatter_weasy as _weasy
    except Exception as e:
        print(f"   ⚠️  pdf_formatter_weasy import failed: {e}")
        return None
    if not _weasy.is_available():
        return None
    contact_bits = style_profile.get("contact_bits") or []
    return _weasy.generate_cover_letter_pdf_weasy(
        cover_letter   = cover_letter,
        job_title      = job_title,
        company        = company,
        output_dir     = output_dir,
        style_profile  = style_profile,
        candidate_name = candidate_name,
        contact_bits   = contact_bits,
    )


def generate_cover_letter_pdf_styled(
    cover_letter:   str,
    job_title:      str,
    company:        str,
    output_dir:     str,
    style_profile:  dict,
    candidate_name: str = "Candidate",
) -> str:
    # Prefer WeasyPrint HTML/CSS rendering; fall back to ReportLab.
    weasy_path = _try_weasy_cl(
        cover_letter   = cover_letter,
        job_title      = job_title,
        company        = company,
        output_dir     = output_dir,
        style_profile  = style_profile,
        candidate_name = candidate_name,
    )
    if weasy_path and os.path.exists(weasy_path) and os.path.getsize(weasy_path) > 0:
        print(f"   🎨 Cover letter rendered via WeasyPrint → {os.path.basename(weasy_path)}")
        return weasy_path

    safe_co    = company.replace(" ", "_").replace("/", "-")
    safe_title = job_title.replace(" ", "_").replace("/", "-")
    filepath   = os.path.join(output_dir, f"CoverLetter_{safe_co}_{safe_title}.pdf")

    lm = float(style_profile.get("left_margin_mm", 25))
    rm = float(style_profile.get("right_margin_mm", 25))
    tm = float(style_profile.get("top_margin_mm", 25))
    bm = float(style_profile.get("bottom_margin_mm", 25))

    page_size = A4 if style_profile.get("page_size") == "A4" else letter
    doc = SimpleDocTemplate(
        filepath,
        pagesize     = page_size,
        leftMargin   = lm * mm,
        rightMargin  = rm * mm,
        topMargin    = tm * mm,
        bottomMargin = bm * mm,
    )

    styles, primary, _ = _build_styles(style_profile)
    fb = style_profile.get("font_body", "Helvetica")
    fbb = style_profile.get("font_body_bold", "Helvetica-Bold")
    fs = float(style_profile.get("font_size_body", 10)) + 1.0
    lead = float(style_profile.get("body_leading") or round(fs * 1.25, 1))

    sal, body_text, close_line, signer = _parse_cover_letter_structure(
        cover_letter, candidate_name
    )

    sal_style = ParagraphStyle(
        "CLSal",
        fontName=fbb,
        fontSize=fs,
        textColor=black,
        alignment=TA_LEFT,
        spaceAfter=8,
    )
    body_style = ParagraphStyle(
        "CLBody",
        fontName=fb,
        fontSize=fs,
        textColor=HexColor("#333333"),
        alignment=TA_JUSTIFY,
        leading=lead,
        spaceAfter=10,
    )
    close_style = ParagraphStyle(
        "CLClose",
        fontName=fb,
        fontSize=fs,
        textColor=black,
        alignment=TA_LEFT,
        spaceBefore=12,
        spaceAfter=4,
    )
    name_style = ParagraphStyle(
        "CLSign",
        fontName=fbb,
        fontSize=fs,
        textColor=black,
        alignment=TA_LEFT,
        spaceAfter=0,
    )

    story = [
        Paragraph(_safe_text(sal), sal_style),
    ]

    for chunk in body_text.split("\n\n"):
        chunk = chunk.strip()
        if chunk:
            story.append(Paragraph(_safe_text(chunk), body_style))

    story.append(Paragraph(_safe_text(close_line), close_style))
    story.append(Paragraph(_safe_text(signer), name_style))

    doc.build(story)
    return filepath