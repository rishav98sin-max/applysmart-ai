"""Focused geometry-bridge validation.

For each collapse CV:
  1. read_outline_llm -> confirm _geometry present and validate_geometry_blocks passes
  2. synthesize a small diff (summary + first bullet rewrite per role) from the
     recovered outline
  3. apply_edits(..., structure_override=geometry) -> confirm it APPLIES edits in
     place (would apply NOTHING via the heuristic, which finds 0 roles) and the
     output PDF is non-empty
"""
import os
os.environ["REPLICA_LLM_READER"] = "1"
os.environ["REPLICA_PARSE_GATE"] = "1"
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import sys
import fitz

from agents.cv_structure_reader import read_outline_llm, validate_geometry_blocks
from agents.pdf_editor import build_outline, apply_edits, extract_structure, _role_blocks

CVS = [
    r"sessions\0352ba20b0304f988f5f37b1b13ac56f\uploads\a53963d7_DebayudhRoy_Resume.pdf",
    r"sessions\35ae44d3ca73434d94f65b6bdd44c118\uploads\bc9ddf46_Saumyadeep_Bhattacharjee_CV.pdf",
    r"sessions\379c70ca9a3e4906a28f54aab32bf788\uploads\5d5c4d66_CV_de.pdf",
]

OUT = os.path.join("_audit_e2e", "bridge_out")
os.makedirs(OUT, exist_ok=True)


def heuristic_role_count(pdf):
    n = 0
    for sec in extract_structure(pdf):
        if sec["type"] in ("experience", "projects"):
            n += sum(1 for r in _role_blocks(sec) if r.get("bullet_groups"))
    return n


def pdf_text(pdf):
    d = fitz.open(pdf)
    try:
        return "\n".join(p.get_text() for p in d)
    finally:
        d.close()


for cv in CVS:
    name = os.path.basename(cv)
    print("=" * 70)
    print(name)
    if not os.path.exists(cv):
        print("  MISSING — skip")
        continue

    heur = heuristic_role_count(cv)
    print(f"  heuristic roles (bullets>0): {heur}")

    outline = read_outline_llm(cv, verbose=True)
    if not outline:
        print("  read_outline_llm -> None (reader declined)")
        continue

    geo = outline.get("_geometry")
    print(f"  outline roles: {len(outline.get('roles') or [])}  "
          f"_geometry present: {geo is not None}  "
          f"gate: {validate_geometry_blocks(geo)}")
    if not geo:
        print("  NO GEOMETRY — would route to rebuild")
        continue

    # Synthesize a realistic diff from the recovered outline: rewrite the
    # summary (prefix marker) + first bullet of each role (prefix marker).
    diff = {"bullets": {}}
    if outline.get("summary"):
        diff["summary"] = "Results-driven product leader. " + outline["summary"]
    for r in outline["roles"]:
        if not r.get("bullets"):
            continue
        b0 = r["bullets"][0]["text"]
        diff["bullets"][r["header"]] = [{"i": 0, "text": "Spearheaded " + b0}]

    out_pdf = os.path.join(OUT, name)

    # Control: apply WITHOUT override (heuristic) — expect ~0 bullet applies.
    rep_ctrl = apply_edits(cv, diff, out_pdf + ".ctrl.pdf", structure_override=None)
    ctrl_bullets = [k for k in rep_ctrl.get("applied", {}) if k != "summary"]

    # Bridge: apply WITH override — expect bullets to apply.
    rep = apply_edits(cv, diff, out_pdf, structure_override=geo)
    applied = rep.get("applied", {})
    bridge_bullets = [k for k in applied if k != "summary"]

    size = os.path.getsize(out_pdf) if os.path.exists(out_pdf) else 0
    # Did the rewrite text actually land in the rendered PDF?
    txt = pdf_text(out_pdf) if size else ""
    landed = ("Spearheaded" in txt) or ("Results-driven product leader" in txt)

    print(f"  CONTROL (no override) applied-bullet-roles: {len(ctrl_bullets)}")
    print(f"  BRIDGE  (override)    applied keys: {list(applied.keys())}")
    print(f"  output size: {size}B  rewrite-text-landed: {landed}")
    print(f"  skipped: {rep.get('skipped')}")
    verdict = "PASS" if (size > 0 and (bridge_bullets or 'summary' in applied) and landed) else "FAIL"
    print(f"  >>> {verdict}")

print("=" * 70)
print("done")
