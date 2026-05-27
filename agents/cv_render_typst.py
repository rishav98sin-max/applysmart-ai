"""
agents/cv_render_typst.py
=========================
Typst-based renderer for the rebuild path.

Takes a ``TailoredCV``-shaped dict (see ``agents/schemas/tailored_cv.json``)
and produces a polished, monochrome, ATS-safe PDF via the Typst typesetter.
Auto-handles tenure calculation, 4-corner role blocks, embedded fonts, and
intelligent pagination — quality the WeasyPrint template path can't easily
match.

The renderer is fully pip-installable (no system binaries, no apt deps)
so the Streamlit Cloud deploy picks it up automatically once the
``requirements.txt`` entry is in place.

Design choices:
  * **Monochrome.** All ink is ``rgb(26, 26, 26)`` (near-black) and the
    footer/dates are ``rgb(102, 102, 102)`` (medium grey). No colour
    accents — reads as senior-presentation.
  * **classic theme.** Tightest, most ATS-tested layout. Avoids the
    sidebar themes that don't parse well in legacy applicant systems.
  * **Smart contact parsing.** ``contact_bits`` is a flat list of
    strings from the LLM; this module classifies each into email / phone
    / location / website / socials before passing to the renderer (which
    expects typed fields, not a generic list).

Public API:
  * :func:`is_available` -> bool
  * :func:`render_cv_to_pdf(structured, output_path) -> Optional[str]`
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from rendercv.schema.rendercv_model_builder import build_rendercv_dictionary_and_model  # type: ignore
    from rendercv.renderer.typst import generate_typst  # type: ignore
    from rendercv.renderer.pdf_png import generate_pdf  # type: ignore
    import ruamel.yaml  # type: ignore
    _TYPST_OK = True
except Exception as _import_err:  # pragma: no cover - import guard
    build_rendercv_dictionary_and_model = None  # type: ignore
    generate_typst = None  # type: ignore
    generate_pdf = None  # type: ignore
    _TYPST_OK = False


def is_available() -> bool:
    """True iff the Typst renderer + its bundled binary are importable."""
    return _TYPST_OK


# ─────────────────────────────────────────────────────────────
# contact_bits classifier
# ─────────────────────────────────────────────────────────────

_EMAIL_RX     = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RX     = re.compile(r"^[\+\(\)\d][\d\s\-\(\)]{6,}$")
_URL_RX       = re.compile(r"https?://\S+|www\.\S+|\S+\.(?:com|io|dev|org|net|co|me)\b")
_LINKEDIN_RX  = re.compile(r"linkedin\.com/in/([A-Za-z0-9._-]+)", re.IGNORECASE)
_GITHUB_RX    = re.compile(r"github\.com/([A-Za-z0-9._-]+)", re.IGNORECASE)


def _classify_contact_bits(bits: List[str]) -> Dict[str, Any]:
    """Split a flat contact-string list into typed fields.

    Returns a dict with optional ``email``, ``phone``, ``location``,
    ``website``, and ``social_networks`` keys. Anything that doesn't
    classify falls into ``location`` as a last resort (the most common
    "uncategorised" contact bit is a city / country name).
    """
    out: Dict[str, Any] = {}
    socials: List[Dict[str, str]] = []
    location_candidates: List[str] = []

    for raw in bits or []:
        s = (raw or "").strip()
        if not s:
            continue

        m = _EMAIL_RX.search(s)
        if m and "email" not in out:
            out["email"] = m.group(0)
            continue

        m = _LINKEDIN_RX.search(s)
        if m:
            socials.append({"network": "LinkedIn", "username": m.group(1)})
            continue

        m = _GITHUB_RX.search(s)
        if m:
            socials.append({"network": "GitHub", "username": m.group(1)})
            continue

        if _URL_RX.search(s):
            if "website" not in out:
                out["website"] = s
            continue

        if _PHONE_RX.match(s):
            # Phone numbers go into the typed `phone` field only when
            # they're in E.164 form (starts with `+`). Non-international
            # numbers can't be validated by the renderer so we'll fold
            # them into the location string later so the value still
            # surfaces on the CV instead of being dropped.
            digits = re.sub(r"[^\d+]", "", s)
            if digits.startswith("+") and "phone" not in out:
                out["phone"] = f"tel:{digits}"
            else:
                out.setdefault("_unstructured_phone", s)
            continue

        # Default bucket: anything left is probably a place.
        location_candidates.append(s)

    if location_candidates and "location" not in out:
        out["location"] = location_candidates[0]
    if socials:
        out["social_networks"] = socials
    return out


# ─────────────────────────────────────────────────────────────
# section adapter (TailoredCV → renderer input)
# ─────────────────────────────────────────────────────────────

# Headings we map to specific renderer section keys; everything else
# falls through with its heading preserved. Case-insensitive matching.
_SECTION_KEYS = {
    "summary":            "summary",
    "professional summary": "summary",
    "profile":            "summary",
    "about":              "summary",
    "experience":         "experience",
    "professional experience": "experience",
    "work experience":    "experience",
    "education":          "education",
    "academic":           "education",
    "skills":             "skills",
    "core competencies":  "skills",
    "technical skills":   "skills",
    "projects":           "projects",
    "personal projects":  "projects",
    "certifications":     "certifications",
    "awards":             "awards",
}


def _section_key(heading: str) -> str:
    """Slug a section heading to a key the renderer is happy with. Falls
    back to the heading verbatim when no canonical match exists."""
    h = (heading or "").strip().lower()
    return _SECTION_KEYS.get(h, heading or "Section")


def _role_to_normal_entry(role: Dict[str, Any]) -> Dict[str, Any]:
    """Map our generic ``role`` shape to a renderer ``NormalEntry``.

    Our role: ``{title, dates, sub, bullets}``. The renderer's NormalEntry
    handles all of those fields cleanly without forcing us to typed
    Experience/Education/etc. — sufficient for the polished output.
    """
    out: Dict[str, Any] = {"name": (role.get("title") or "").strip()}
    sub = (role.get("sub") or "").strip()
    if sub:
        # "Company, Location" → location field gets the second half if a
        # comma is present, otherwise drop into location whole.
        if "," in sub:
            company, _, location = sub.partition(",")
            out["name"] = f"{out['name']} ({company.strip()})" if out["name"] else company.strip()
            out["location"] = location.strip()
        else:
            out["location"] = sub
    dates = (role.get("dates") or "").strip()
    if dates:
        out["date"] = dates
    bullets = [b for b in (role.get("bullets") or []) if (b or "").strip()]
    if bullets:
        out["highlights"] = bullets
    return out


def _build_renderer_dict(structured: Dict[str, Any]) -> Dict[str, Any]:
    """Build the renderer-ready dict from our TailoredCV input."""
    name = (structured.get("candidate_name") or "Candidate").strip()
    contact_typed = _classify_contact_bits(structured.get("contact_bits") or [])

    cv_block: Dict[str, Any] = {"name": name}
    # Pop our internal marker for a non-E.164 phone. We'll fold it into
    # the location string a few lines down so the value still appears
    # on the CV without tripping the renderer's strict phone validator.
    unstructured_phone = contact_typed.pop("_unstructured_phone", None)
    cv_block.update(contact_typed)
    if unstructured_phone:
        existing_loc = cv_block.get("location", "")
        if existing_loc:
            cv_block["location"] = f"{existing_loc} - {unstructured_phone}"
        else:
            cv_block["location"] = unstructured_phone

    # Sections — order preserved from the LLM output.
    sections: Dict[str, Any] = {}
    for sec in (structured.get("sections") or []):
        heading = (sec.get("heading") or "").strip()
        key = _section_key(heading) if heading else "Section"
        entries: List[Any] = []

        # roles → NormalEntry list
        for role in (sec.get("roles") or []):
            ne = _role_to_normal_entry(role)
            if ne.get("name"):
                entries.append(ne)

        # paragraphs → bullet-entry strings; tolerant of "Label: details"
        # format which the renderer will display cleanly.
        for p in (sec.get("paragraphs") or []):
            if p and p.strip():
                entries.append(p.strip())

        if entries:
            sections[key] = entries

    # Top-level summary (when LLM emits a summary string at root, not inside
    # a section). The renderer accepts a list of strings for the summary
    # section, which renders as a body paragraph block.
    if structured.get("summary"):
        s = structured["summary"].strip()
        if s and "summary" not in sections:
            sections["summary"] = [s]

    cv_block["sections"] = sections

    # Monochrome design block, hardcoded. classic theme = tightest ATS layout.
    design_block = {
        "theme": "classic",
        "colors": {
            "body":          "rgb(26, 26, 26)",
            "name":          "rgb(26, 26, 26)",
            "headline":      "rgb(26, 26, 26)",
            "connections":   "rgb(26, 26, 26)",
            "section_titles":"rgb(26, 26, 26)",
            "links":         "rgb(26, 26, 26)",
            "footer":        "rgb(102, 102, 102)",
            "top_note":      "rgb(102, 102, 102)",
        },
    }

    locale_block = {"language": "english"}

    return {
        "cv":      cv_block,
        "design":  design_block,
        "locale":  locale_block,
    }


def _dict_to_yaml_string(d: Dict[str, Any]) -> str:
    """Stable YAML serialiser — the renderer loads its input as YAML."""
    yaml = ruamel.yaml.YAML(typ="rt")
    yaml.default_flow_style = False
    yaml.width = 4096
    buf = __import__("io").StringIO()
    yaml.dump(d, buf)
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────

def render_cv_to_pdf(
    structured:     Dict[str, Any],
    job_title:      str,
    company:        str,
    output_dir:     str,
) -> Optional[str]:
    """Render a TailoredCV dict to PDF via the Typst pipeline.

    Returns the output PDF path on success, or ``None`` if the renderer
    isn't available / the input is empty / rendering fails. On ``None``
    the caller should fall back to the WeasyPrint structured path (or
    further down the chain).
    """
    if not _TYPST_OK:
        return None
    if not isinstance(structured, dict) or not structured.get("candidate_name"):
        return None

    try:
        # Run the ATS normaliser first — the structured output usually
        # comes from tailor_cv_structured which is already clean, but
        # callers may pass raw LLM output without that step. Cheap
        # and idempotent.
        try:
            from agents.ats_normalize import normalise_structured
            structured = normalise_structured(structured)
        except Exception:
            pass

        renderer_dict = _build_renderer_dict(structured)
        if not renderer_dict["cv"].get("sections"):
            return None

        yaml_str = _dict_to_yaml_string(renderer_dict)

        os.makedirs(output_dir, exist_ok=True)
        safe_co    = company.replace(" ", "_").replace("/", "-")
        safe_title = job_title.replace(" ", "_").replace("/", "-")
        out_pdf    = os.path.join(output_dir, f"CV_{safe_co}_{safe_title}.pdf")

        # The renderer's API expects a file path so it can resolve
        # relative paths inside the YAML (photos, fonts). Write the
        # YAML to a temp file in the output dir so the renderer
        # creates its scratch files next to ours, then move/rename.
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False,
            dir=output_dir, encoding="utf-8",
        ) as tf:
            tf.write(yaml_str)
            tmp_yaml_path = Path(tf.name)

        try:
            _, model = build_rendercv_dictionary_and_model(
                yaml_str, input_file_path=tmp_yaml_path,
            )
            # Force its working directory + output path to OUR output_dir
            # so the artefacts (typ + pdf) land where we expect.
            typst_path = generate_typst(model)
            pdf_path   = generate_pdf(model, typst_path)

            # The renderer wrote to its own naming scheme; rename to ours.
            if pdf_path and Path(pdf_path).exists():
                Path(pdf_path).rename(out_pdf)
                return out_pdf
            return None
        finally:
            try:
                tmp_yaml_path.unlink(missing_ok=True)
            except Exception:
                pass

    except Exception as e:
        # ASCII-safe error log: the Windows console doesn't always
        # tolerate non-ASCII glyphs (cp1252 default), and a UnicodeError
        # inside the failure handler would hide the real cause.
        print(f"   [!] Typst rebuild render failed: {type(e).__name__}: {e}")
        return None


__all__ = ["is_available", "render_cv_to_pdf"]
