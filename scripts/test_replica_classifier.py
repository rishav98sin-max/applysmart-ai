"""
Regression test for the replica/rebuild router in
`agents.pdf_editor.detect_replica_compatibility`.

The router decides whether the in-place "replica" editor (which preserves
the candidate's original PDF layout) can safely edit a CV, or whether we
must fall back to the rebuild path (which generates a brand-new PDF from
a template and visibly does not match the original).

Two real-world failures motivated the fixture set:

  • Cormac Holleran's CV (Run 25) — a clean single-column layout with a
    date-only left sidebar. The replica path COULD edit it cleanly, but
    the old detector falsely flagged it as multi-column (page 2's
    education table tripped a 15% bucket threshold on Streamlit Cloud's
    PyMuPDF version) → rebuild path took over → catastrophic output.

  • Five Canva-style designer CVs — collected as a deliberate sample
    of templates with photos, colour-block sidebars, two-column body,
    decorative banners. The old detector caught Kian Graham (image-ratio
    0.44) but missed the other four — they slipped to the replica path
    and the in-place editor mangled them.

Each fixture pins one fact: this CV must route to {replica | rebuild}.
Future detector tweaks must not silently re-break Cormac or let any
Canva template slip back through. Run this before merging any change to
`detect_replica_compatibility`.

Usage:
    python scripts/test_replica_classifier.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Tuple

# Repo root on sys.path so `agents.*` imports resolve when invoked from
# anywhere (CI, local, IDE).
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from agents.pdf_editor import detect_replica_compatibility  # noqa: E402

_DESIGN_DIR = _REPO / "CVs" / "Orignal Base CV" / "DesignerCVExamples"
_BASE_DIR   = _REPO / "CVs" / "Orignal Base CV"
_RUN25_DIR  = _REPO / "CVs" / "Run25"

# (label, expected_route, path). expected_route is "replica" when the
# in-place editor must handle it, "rebuild" when the fallback PDF
# generator must take over.
FIXTURES: List[Tuple[str, str, Path]] = [
    # Canva-style designer templates — must always route to rebuild.
    ("Daniel Gallego (Graphic Designer)", "rebuild",
     _DESIGN_DIR / "Blue and Yellow Modern Graphic Designer CV.pdf"),
    ("Kian Graham (Flight Attendant)",    "rebuild",
     _DESIGN_DIR / "Blue Light Blue Color Blocks Flight Attendant CV.pdf"),
    ("Silas Schuler (Systems Designer)",  "rebuild",
     _DESIGN_DIR / "Systems Design Resume in Bright Blue White Bold Accent Style.pdf"),
    ("Donna Stroupe (Sales Rep)",         "rebuild",
     _DESIGN_DIR / "White Simple Sales Representative CV Resume.pdf"),
    ("Rishav (Canva template)",           "rebuild",
     _DESIGN_DIR / "Rishav's Resume (1).pdf"),

    # Clean text-based CVs — must always use the replica path so the
    # candidate's original layout is preserved.
    ("Cormac Holleran (date-sidebar)",    "replica",
     _RUN25_DIR / "Cormac Holleran CV 2026.pdf"),
    ("Rishav PM CV (single column)",      "replica",
     _BASE_DIR / "RishavSingh_ProductManagerCV.pdf"),
]


def _route(path: Path) -> Tuple[str, dict]:
    r = detect_replica_compatibility(str(path))
    return ("replica" if r.get("compatible") else "rebuild"), r


def main() -> int:
    print(f"{'CV':38s}  {'expected':>9s}  {'got':>9s}  {'reason':>14s}  img  fills  verdict")
    print("-" * 100)

    failures = 0
    skipped  = 0
    for label, expected, path in FIXTURES:
        if not path.exists():
            # Fixture CVs live under `CVs/` which is gitignored (personal
            # data + public Canva templates). A fresh checkout won't have
            # them. Skip rather than fail so this script stays runnable
            # in CI; locally, present fixtures still gate the change.
            print(f"{label:38s}  {expected:>9s}  {'SKIP':>9s}  {'-':>14s}  -    -    SKIP (no file)")
            skipped += 1
            continue
        got, info = _route(path)
        verdict = "PASS" if got == expected else "FAIL"
        if got != expected:
            failures += 1
        print(
            f"{label:38s}  {expected:>9s}  {got:>9s}  "
            f"{info.get('reason',''): >14s}  "
            f"{info.get('image_ratio', 0):.3f}  "
            f"{info.get('big_fill_count', 0):>2d}    {verdict}"
        )

    print()
    ran = len(FIXTURES) - skipped
    if failures == 0:
        print(f"{ran} of {len(FIXTURES)} fixtures PASSED ({skipped} skipped — fixture file not present).")
        return 0
    print(f"{failures} of {ran} present fixtures FAILED ({skipped} skipped).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
