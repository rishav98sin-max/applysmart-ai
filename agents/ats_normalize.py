"""
agents/ats_normalize.py
=======================
ATS-safe Unicode normalisation for rendered CV / cover-letter text.

Applicant tracking systems and legacy CV parsers routinely choke on the
"smart" Unicode that web copy + LLM output produce by default — em-dashes,
curly quotes, ligatures, zero-width chars, non-breaking spaces. Strip them
to plain ASCII equivalents *at the render boundary* so the visible PDF
shows clean glyphs and any downstream text extraction reads them right.

Inspired by the same idea in santifer/career-ops' generate-pdf.mjs.

Also includes a deny-list for **Font Awesome class names** that have shown
up in LLM output verbatim (e.g. ``MOBILE-ALT`` in front of a phone number,
``Envelope`` before an email) — happens when the model has been trained
on HTML CV examples that used FA icons. These tokens render as garbage in
our pipeline so we filter them out of contact strings.

Public API
----------
- :func:`normalise_text(s)` -> str
- :func:`normalise_contact_bit(s)` -> Optional[str]   (None ⇒ drop the bit)
- :func:`normalise_structured(d)` -> dict             (deep clean of TailoredCV)
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Optional


# Map of "smart" / typographic Unicode → plain ASCII the PDF + ATS both like.
# Conservative on purpose — only fix glyphs that have caused real bugs.
_UNICODE_REPLACEMENTS: Dict[str, str] = {
    # Dashes
    "‐": "-",   # hyphen
    "‑": "-",   # non-breaking hyphen
    "‒": "-",   # figure dash
    "–": "-",   # en-dash
    "—": "-",   # em-dash
    "―": "-",   # horizontal bar
    # Quotes
    "‘": "'",   # left single
    "’": "'",   # right single / apostrophe
    "‚": "'",   # single low-9
    "‛": "'",
    "“": '"',   # left double
    "”": '"',   # right double
    "„": '"',   # double low-9
    "‟": '"',
    # Ellipsis
    "…": "...",
    # Bullets used as text (not as list markers — those are <li>)
    "•": "-",   # bullet
    "◦": "-",   # white bullet
    "·": "-",   # middle dot
    # Spaces & invisibles
    " ": " ",   # non-breaking space
    " ": " ",   # thin space
    " ": " ",   # hair space
    "​": "",    # zero-width space
    "‌": "",    # zero-width non-joiner
    "‍": "",    # zero-width joiner
    "﻿": "",    # BOM / zero-width no-break space
    # Common ligatures (some fonts don't have them, ATS parsers split mid-word)
    "ﬀ": "ff",
    "ﬁ": "fi",
    "ﬂ": "fl",
    "ﬃ": "ffi",
    "ﬄ": "ffl",
}


# Font Awesome class names that have leaked into LLM-generated contact lines.
# These are the FA "free" icon classes most commonly attached to phone/email/
# location/web/linkedin/github in HTML CV examples. We drop them silently;
# the actual contact value (the digits / address) stays intact.
_FA_LEAKED_TOKENS = {
    # Phone family
    "mobile-alt", "mobilealt", "mobile", "phone", "phone-alt",
    "phone-square", "phone-volume",
    # Email family
    "envelope", "envelope-open", "at",
    # Location family
    "map-marker", "map-marker-alt", "marker", "location-dot",
    "location-pin", "location-arrow",
    # Web / link family
    "globe", "link", "external-link", "external-link-alt", "house",
    # Socials
    "linkedin", "linkedin-in", "github", "github-square",
    "twitter", "x-twitter", "facebook", "instagram",
}


def normalise_text(s: str) -> str:
    """Strip smart Unicode and collapse runs of whitespace.

    Safe to call on any rendered string: bullet text, summary, headings,
    contact strings. Keeps newlines (callers that want flat strings can
    join on space themselves).
    """
    if not s:
        return ""
    # NFC first so combining sequences normalise before we map.
    out = unicodedata.normalize("NFC", s)
    for src, dst in _UNICODE_REPLACEMENTS.items():
        if src in out:
            out = out.replace(src, dst)
    # Collapse internal whitespace runs (spaces + tabs only, preserve \n).
    out = re.sub(r"[ \t]+", " ", out)
    # Trim trailing whitespace per line.
    out = "\n".join(line.rstrip() for line in out.split("\n"))
    return out.strip()


def normalise_contact_bit(s: str) -> Optional[str]:
    """Clean a single contact-line segment.

    Returns the cleaned string, or ``None`` if the segment is *only* a
    leaked Font Awesome class name (in which case the caller should drop
    the bit entirely rather than render the literal word).

    Examples
    --------
    >>> normalise_contact_bit("MOBILE-ALT 089 453 2181")
    '089 453 2181'
    >>> normalise_contact_bit("Envelope cormac@example.com")
    'cormac@example.com'
    >>> normalise_contact_bit("envelope")
    None
    >>> normalise_contact_bit("Dublin, Ireland")
    'Dublin, Ireland'
    """
    cleaned = normalise_text(s)
    if not cleaned:
        return None
    # Split on whitespace, drop leading tokens that look like FA class names.
    tokens = cleaned.split()
    while tokens:
        head = tokens[0].lower().strip(".,:;-_")
        if head in _FA_LEAKED_TOKENS:
            tokens.pop(0)
            continue
        break
    if not tokens:
        return None  # bit was nothing but an icon class — drop it
    return " ".join(tokens)


def normalise_structured(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Deep-clean a TailoredCV-shaped dict before it hits the renderer.

    - Runs :func:`normalise_text` on every string field.
    - Filters ``contact_bits`` via :func:`normalise_contact_bit` (drops
      any bit that was pure icon class).
    - Drops empty strings + empty arrays in place — keeps the renderer
      from emitting empty ``<li>`` / blank sections.

    Idempotent: calling twice produces the same output as calling once.
    Safe on partial / missing keys (returns a normalised copy, never
    raises).
    """
    if not isinstance(doc, dict):
        return doc

    out: Dict[str, Any] = {}

    if "candidate_name" in doc:
        out["candidate_name"] = normalise_text(str(doc["candidate_name"] or ""))

    bits_in = doc.get("contact_bits") or []
    bits_out: List[str] = []
    for b in bits_in:
        cleaned = normalise_contact_bit(str(b or ""))
        if cleaned:
            bits_out.append(cleaned)
    if bits_out:
        out["contact_bits"] = bits_out

    if "summary" in doc:
        s = normalise_text(str(doc["summary"] or ""))
        if s:
            out["summary"] = s

    sections_out: List[Dict[str, Any]] = []
    for section in doc.get("sections") or []:
        if not isinstance(section, dict):
            continue
        s_out: Dict[str, Any] = {}
        heading = normalise_text(str(section.get("heading", "")))
        if not heading:
            continue
        s_out["heading"] = heading

        roles_out: List[Dict[str, Any]] = []
        for role in section.get("roles") or []:
            if not isinstance(role, dict):
                continue
            r_out: Dict[str, Any] = {}
            title = normalise_text(str(role.get("title", "")))
            if not title:
                continue
            r_out["title"] = title
            for k in ("dates", "sub"):
                v = normalise_text(str(role.get(k, "")))
                if v:
                    r_out[k] = v
            bullets = [
                normalise_text(str(b or ""))
                for b in (role.get("bullets") or [])
            ]
            bullets = [b for b in bullets if b]
            if bullets:
                r_out["bullets"] = bullets
            roles_out.append(r_out)
        if roles_out:
            s_out["roles"] = roles_out

        paragraphs_out = [
            normalise_text(str(p or "")) for p in (section.get("paragraphs") or [])
        ]
        paragraphs_out = [p for p in paragraphs_out if p]
        if paragraphs_out:
            s_out["paragraphs"] = paragraphs_out

        # Drop a section that ended up with no content at all (heading + nothing).
        if "roles" in s_out or "paragraphs" in s_out:
            sections_out.append(s_out)

    out["sections"] = sections_out
    return out


__all__ = [
    "normalise_text",
    "normalise_contact_bit",
    "normalise_structured",
]
