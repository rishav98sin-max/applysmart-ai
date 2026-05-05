"""Verify Fix B: apply Run 12's diff (with summary reverted to original) and
confirm the output PDF still has summary text, not an empty block."""
import json, sys, os, fitz
sys.path.insert(0, r"d:\Projects\job-application-agent")
from agents.pdf_editor import apply_edits, build_outline

CV_IN  = r"d:\Projects\job-application-agent\CVs\Orignal Base CV\Shrestha Ghosh_CV.pdf"
CV_OUT = r"d:\Projects\job-application-agent\diagnostics\_fix_b_output.pdf"

# Load Run 12's kept tailor diff.
LANGFUSE = r"d:\Projects\job-application-agent\diagnostics\LangFuseLogsRun12.jsonl"
rows = [json.loads(l) for l in open(LANGFUSE, encoding="utf-8")]
tailor_out = json.loads([r for r in rows if r.get("name") == "cv_diff_tailor"][-1]["output"])

# Simulate the credential-guard revert path: force summary to original.
outline = build_outline(CV_IN)
orig_summary = outline["summary"]
print(f"Original summary length: {len(orig_summary)} chars")

# Build the diff as the tailor would pass to apply_edits after guards fire.
diff = {
    "summary":      orig_summary,           # reverted by identity guard
    "bullets":      tailor_out.get("bullets") or {},
    "skills_order": [],
}
print(f"Diff summary length: {len(diff['summary'])} chars (should equal orig)")

report = apply_edits(CV_IN, diff, CV_OUT)
print()
print("=== REPORT ===")
print(json.dumps(report, indent=2, default=str)[:1500])
print()

# Read back the output PDF and confirm summary text survived.
doc = fitz.open(CV_OUT)
txt = "\n".join(p.get_text("text") for p in doc)
doc.close()

idx = txt.find("PROFESSIONAL SUMMARY")
block = txt[idx:idx+800] if idx >= 0 else "<not found>"
print("=== OUTPUT SUMMARY BLOCK ===")
print(block[:800])
print()

summary_body = txt[idx+len("PROFESSIONAL SUMMARY"):txt.find("WORK EXPERIENCE", idx)] if idx >= 0 else ""
words = len(summary_body.split())
print(f"Summary body: {words} words (expected > 50)")
print("PASS" if words > 50 else "FAIL — summary was wiped")
