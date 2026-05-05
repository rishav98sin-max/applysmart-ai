"""Verify Fix C1: synthetic diff with identical-string 'rewrites' should
be converted to text=None (keep original) and not counted as rewrites."""
import sys
sys.path.insert(0, r"d:\Projects\job-application-agent")
from agents.cv_diff_tailor import _normalise_bullet_list

orig = [
    {"text": "Defined and executed integrated digital strategy for key accounts.", "length": 68},
    {"text": "Managed full-cycle Media Releases.", "length": 34},
    {"text": "Collaborated across functions and maintained stakeholder relationships.", "length": 72},
]

# Synthetic diff: 1 genuine rewrite + 2 identical-string "rewrites".
diff_raw = [
    {"i": 0, "text": "Managed key accounts including Lenovo and Amazon, driving integrated digital strategy."},  # real change
    {"i": 1, "text": "Managed full-cycle Media Releases."},                                                       # IDENTICAL
    {"i": 2, "text": "  collaborated ACROSS functions and maintained stakeholder relationships.  "},            # identical after normalise
]

result = _normalise_bullet_list(
    diff_raw,
    n_bullets=3,
    orig_texts=orig,
    section="experience",
    do_not_inject=[],
)

print("=== NORMALISED OUTPUT ===")
for r in result:
    status = "REWRITE" if r.get("text") else "keep original"
    print(f"  [#{r['i']}] {status}: {(r.get('text') or '')[:80]}")

n_real = sum(1 for r in result if r.get("text"))
n_total = len(result)
print(f"\nReal rewrites: {n_real} / {n_total} entries")
expected = 1
print(f"Expected: {expected} real rewrite (indexes 1 and 2 should be suppressed as identical)")
print("PASS" if n_real == expected else f"FAIL — got {n_real}, expected {expected}")
