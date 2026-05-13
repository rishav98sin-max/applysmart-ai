"""
agents.cv_docx_pipeline
=======================

Routing & orchestration glue for the DOCX-based CV tailoring path
(May 2026 / DOCX path).

This module is the SINGLE entry point job_agent.py uses to decide:

    "For this user's CV, should we tailor via DOCX (in-place edit then
     render via LibreOffice headless) or via the original PDF replica /
     rebuild path?"

The routing rules (May 13 / step 7, updated per Claude spec):

  1.  User uploaded a `.docx` file directly:
        → DOCX path, full confidence (score = 100).

  2.  User uploaded a `.pdf` file AND env `DOCX_PATH_ENABLED=1`:
        → Convert PDF → DOCX via `pdf2docx`. Score the conversion.
          - Score ≥ DOCX_CONVERTIBILITY_THRESHOLD AND outline parses
            with ≥1 role: DOCX path.
          - Otherwise: fall back to existing PDF replica path.

  3.  Anything else (env disabled, no python-docx, conversion crash):
        → return None so caller routes to existing PDF replica path.

The router NEVER raises. All failures degrade gracefully to None.

This module deliberately does NOT run the LLM tailor / reviewer loop —
that stays in `job_agent.tailor_and_generate_node` so quality parity is
preserved between the two paths. We only:

  - decide if the DOCX path is viable
  - build the outline that the tailor will consume
  - hold the docx_path and any conversion artefacts
  - expose `apply_diff_and_render` for the apply-and-render step
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from agents.cv_docx_parser   import build_outline_from_docx
from agents.cv_docx_editor   import apply_diff_to_docx
from agents.cv_docx_to_pdf   import render_pdf_from_docx
from agents.cv_pdf_to_docx   import convert_pdf_to_docx, CONVERTIBILITY_THRESHOLD


# ─────────────────────────────────────────────────────────────
# Feature flag
# ─────────────────────────────────────────────────────────────
#
# The DOCX path is gated behind an env var until we've battle-tested it
# across a range of CV templates. Default OFF — production runs continue
# to use the existing PDF replica/rebuild path until we explicitly opt
# in. When the user uploads a `.docx` directly, the flag is IGNORED:
# no sensible alternative path exists for native DOCX input.

def _is_docx_path_enabled() -> bool:
    """True when env DOCX_PATH_ENABLED is a truthy value."""
    raw = os.getenv("DOCX_PATH_ENABLED", "0").strip().lower()
    return raw not in ("", "0", "false", "no", "off")


# ─────────────────────────────────────────────────────────────
# Route bundle
# ─────────────────────────────────────────────────────────────

@dataclass
class CVDocxRoute:
    """
    Bundle of state describing a viable DOCX-based tailoring run.

    Attributes:
        docx_path:         Filesystem path to the DOCX the editor will
                           open. For user-uploaded DOCX, this points at
                           the user's file directly. For PDF-converted
                           DOCX, this is the converted file in `workdir`.
        outline:           Result of `build_outline_from_docx(docx_path)`.
                           Same shape as `pdf_editor.build_outline`, so
                           `cv_diff_tailor` and `review_tailored_cv` can
                           consume it without modification.
        convertibility:    0–100 score. 100 for user-uploaded DOCX
                           (we know it's authoritative). For converted
                           PDF, the score from `cv_pdf_to_docx`.
        source_was_pdf:    True when the route was created by converting
                           an uploaded PDF. Useful for diagnostics and
                           for cleanup decisions (we own the converted
                           file; we don't own the user-uploaded one).
        workdir:           Directory holding intermediate artefacts
                           (converted DOCX, edited DOCX). Owned by the
                           caller (typically `out_dir` from the agent).
    """
    docx_path:      str
    outline:        Dict[str, Any]
    convertibility: int
    source_was_pdf: bool
    workdir:        str
    # Filled in by `apply_diff_and_render` if the caller asks; set so
    # downstream nodes can inspect intermediate artefacts for debugging.
    edited_docx_path: str = ""


# ─────────────────────────────────────────────────────────────
# Routing
# ─────────────────────────────────────────────────────────────

def try_route_docx(
    cv_path: str,
    workdir: str,
) -> Optional[CVDocxRoute]:
    """
    Decide if the DOCX path is viable for this CV. Returns a populated
    `CVDocxRoute` on success, or None when the caller should fall back
    to the existing PDF replica path.

    Routing logic:
      - `cv_path` ends in `.docx` (case-insensitive):
          parse outline; if it has ≥1 role, use the DOCX path.

      - `cv_path` ends in `.pdf` AND `DOCX_PATH_ENABLED` env is truthy:
          run `convert_pdf_to_docx`; if `acceptable` AND outline has
          ≥1 role, use the DOCX path; otherwise None.

      - Anything else: None.

    Never raises. On any failure (missing dep, malformed file, low
    convertibility) returns None so the caller falls back cleanly.
    """
    if not cv_path or not os.path.exists(cv_path):
        return None

    ext = os.path.splitext(cv_path)[1].lower()

    # Case 1: native DOCX upload — always honour, regardless of env flag.
    if ext == ".docx":
        outline = build_outline_from_docx(cv_path)
        if not outline.get("roles"):
            print(
                "   ℹ️  cv_docx_pipeline: DOCX outline has 0 roles — "
                "falling back to rebuild path. "
                "(Validator should already have caught this; safety net only.)"
            )
            return None
        print(
            f"   📄 cv_docx_pipeline: DOCX route activated "
            f"(native upload, {len(outline['roles'])} role(s))."
        )
        return CVDocxRoute(
            docx_path      = cv_path,
            outline        = outline,
            convertibility = 100,
            source_was_pdf = False,
            workdir        = workdir,
        )

    # Case 2: PDF upload — feature-flagged.
    if ext == ".pdf":
        if not _is_docx_path_enabled():
            return None

        # Place the converted DOCX inside the supplied workdir so the
        # caller decides cleanup. `pdf2docx` writes silently next to the
        # input by default; we override to avoid littering the user's
        # CV folder with our intermediate `.docx` files.
        try:
            os.makedirs(workdir, exist_ok=True)
        except OSError:
            pass
        base = os.path.splitext(os.path.basename(cv_path))[0]
        converted_docx = os.path.join(workdir, f"{base}__converted.docx")

        result = convert_pdf_to_docx(cv_path, converted_docx)
        if not result.get("ok") or not result.get("acceptable"):
            print(
                f"   ↩️  cv_docx_pipeline: PDF conversion not acceptable "
                f"(score={result.get('score', 0)}/100, "
                f"reason={(result.get('reason') or '').strip()[:120]!r}) — "
                f"using PDF replica path instead."
            )
            return None

        outline = build_outline_from_docx(converted_docx)
        if not outline.get("roles"):
            print(
                "   ↩️  cv_docx_pipeline: converted DOCX outline has 0 roles "
                "— using PDF replica path instead."
            )
            return None

        # May 2026 fix: check outline quality for pdf2docx corruption
        # (text duplication, merged bullet content into headers)
        from agents.cv_docx_parser import _outline_quality_ok
        if not _outline_quality_ok(outline):
            print(
                "   ↩️  cv_docx_pipeline: converted DOCX outline quality check failed "
                "(likely pdf2docx corruption) — using PDF replica path instead."
            )
            return None

        print(
            f"   📄 cv_docx_pipeline: DOCX route activated "
            f"(PDF→DOCX, score={result.get('score')}/100, "
            f"{len(outline['roles'])} role(s))."
        )
        return CVDocxRoute(
            docx_path      = converted_docx,
            outline        = outline,
            convertibility = int(result.get("score", 0)),
            source_was_pdf = True,
            workdir        = workdir,
        )

    # Case 3: unknown extension — let upstream reject.
    return None


# ─────────────────────────────────────────────────────────────
# Apply + render
# ─────────────────────────────────────────────────────────────

def apply_diff_and_render(
    route:        CVDocxRoute,
    diff:         Dict[str, Any],
    output_pdf:   str,
) -> Tuple[bool, str]:
    """
    Given a viable route and a sanitised diff, apply the diff to the
    DOCX and render the edited DOCX to PDF at `output_pdf`. Returns
    `(True, "")` on success or `(False, reason)` on failure.

    On render failure we still keep the edited DOCX around at
    `route.edited_docx_path` so the caller can fall back to the rebuild
    path with the tailored content (rather than losing the LLM work).

    The function is a thin orchestrator — all real logic lives in
    `cv_docx_editor.apply_diff_to_docx` and
    `cv_docx_to_pdf.render_pdf_from_docx`.
    """
    if not route or not route.docx_path:
        return False, "missing route"
    if not isinstance(diff, dict):
        return False, "diff is not a dict"

    base = os.path.splitext(os.path.basename(route.docx_path))[0]
    edited_docx = os.path.join(route.workdir, f"{base}__edited.docx")
    route.edited_docx_path = edited_docx

    ok, reason = apply_diff_to_docx(
        docx_path   = route.docx_path,
        diff        = diff,
        outline     = route.outline,
        output_path = edited_docx,
    )
    if not ok:
        return False, f"apply_diff_to_docx failed: {reason}"

    ok2, reason2 = render_pdf_from_docx(
        docx_path   = edited_docx,
        output_path = output_pdf,
    )
    if not ok2:
        return False, f"render_pdf_from_docx failed: {reason2}"

    return True, ""
