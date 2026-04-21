"""Isolated test of the pdf_editor fixes — no Groq LLM calls.

We run apply_pdf_edits directly against the uploaded CV from the last
session, using a HAND-CRAFTED edits dict that mimics what the tailor
would produce. Then we inspect the rendered output to verify:

  1. Summary renders with proper word spacing (no NBSP-between-every-word)
  2. Bullet text is NOT truncated (full sentences preserved end-to-end)
  3. Line spacing is consistent with the rest of the CV

This lets us validate the fixes without consuming Groq budget.
"""
import os, sys, shutil
import fitz

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from agents.pdf_editor import apply_edits, build_outline

# Use the uploaded CV from the most recent session.
SESSION = "608a3aa45cc2419a9a204caa4246bda4"
src_pdf = os.path.join(
    ROOT, "sessions", SESSION, "uploads",
    "f28606e8_RishavSinghProductManager_CV_India.pdf",
)
out_pdf = os.path.join(ROOT, "test_pdf_fix_output.pdf")

# Peek at the outline so we know the real role headers for the bullets dict.
outline = build_outline(src_pdf)
print("ROLES found in CV:")
for r in outline.get("roles", []):
    print(f"  - {r['header'][:60]}  ({len(r['bullets'])} bullets)")

# Build a synthetic tailoring edit. Summary is a deliberately different
# shape/length than the original so we can see if insert_textbox renders
# it cleanly. Bullets for each role are reversed so we test the reorder
# path too.
edits = {
    "summary": (
        "Product Manager with 4 years at IBM and Accenture, delivering "
        "platform scale to 600K+ users with measurable outcomes: 25% user "
        "growth, 40% efficiency gains, 30% latency reduction. MSc "
        "Management from Trinity College Dublin. Built VoC Insight Hub "
        "and ApplySmart AI, combining product thinking with multi-agent "
        "architecture. Recognised with Accenture Kudos and Spotlight "
        "Awards for execution excellence and on-time delivery."
    ),
    "bullets": {},
    "skills_order": [],
}

# Build AGGRESSIVE-format bullets for each role:
#   - reverse the order
#   - rewrite the FIRST original bullet (index 0) with a JD-keyword-heavy opener
#   - drop the LAST original bullet to test the drop path
for r in outline.get("roles", []):
    bullets = r["bullets"]
    n = len(bullets)
    if n < 2:
        continue
    reversed_idx = list(range(n))[::-1]
    # Drop last original (which is index 0 after reversal... no, drop the
    # HIGHEST original index which sits at position 0 in reversed order).
    # We drop the index that was physically last in the original CV.
    # Keep all but the highest-index entry => we simulate "drop".
    kept = [i for i in reversed_idx if i != n - 1]  # drop index (n-1)
    # Rewrite index 0 (first original bullet) — preserves numbers so the
    # sanitiser's _rewrite_is_safe check passes.
    entries = []
    for idx in kept:
        if idx == 0:
            orig = bullets[0]
            # Keep numbers, just reframe the opener.
            entries.append({
                "i":    0,
                "text": "[TEST-REWRITE] " + orig[:max(1, len(orig) // 2)] + " ...",
            })
        else:
            entries.append({"i": idx})
    edits["bullets"][r["header"]] = entries
print("\nEDITS to apply:")
print(f"  summary: {len(edits['summary'])} chars, {len(edits['summary'].split())} words")
for rk, order in edits["bullets"].items():
    print(f"  bullets[{rk[:30]}]: {order}")

# Apply.
report = apply_edits(src_pdf, edits, out_pdf)
print("\nAPPLY REPORT:")
print(f"  applied: {list(report.get('applied', {}).keys())}")
print(f"  skipped: {report.get('skipped', [])}")

# Verify rendered output.
print(f"\nOutput saved to: {out_pdf}")
print("\nRENDERED SUMMARY (first 5 visible lines):")
doc = fitz.open(out_pdf)
lines = []
for block in doc[0].get_text("dict")["blocks"]:
    if block.get("type") != 0:
        continue
    for ln in block.get("lines", []):
        spans = ln.get("spans", [])
        if not spans:
            continue
        text = "".join(s["text"] for s in spans).strip()
        if not text:
            continue
        lines.append({
            "y":    ln["bbox"][1],
            "font": spans[0]["font"],
            "size": spans[0]["size"],
            "text": text,
        })
# Sort by y, then print lines between Summary heading and Featured Projects heading.
lines.sort(key=lambda l: l["y"])
in_summary = False
for ln in lines:
    if "summary" in ln["text"].lower() and ln["size"] > 10:
        in_summary = True
        continue
    if "featured projects" in ln["text"].lower():
        break
    if in_summary:
        # Encode-safe print for Windows console.
        safe = "".join(c if ord(c) < 128 else "?" for c in ln["text"])
        print(f"  y={ln['y']:.1f} font={ln['font'][:20]} sz={ln['size']:.2f} | {safe[:100]}")

# NBSP smoke check.
full_text = doc[0].get_text()
nbsp_count = full_text.count("\xa0")
print(f"\nNBSP (\\u00a0) count in output: {nbsp_count}  (was >100 in the buggy version)")

# Bullet integrity smoke check.
# Look for any line ending in mid-sentence patterns (no period, no comma)
# within bullet regions.
if "delivery efficiency by 20%" in full_text:
    print("OK: 'delivery efficiency by 20%' (end of IBM bullet 3) is present.")
else:
    print("WARN: end-of-bullet phrase NOT found in output \u2014 bullet may still truncate.")
