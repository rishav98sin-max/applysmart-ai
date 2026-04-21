"""Smoke test for YOE extraction + early-exit matrix. No LLM cost."""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.job_matcher import (
    _extract_jd_yoe_requirement,
    _CAND_YOE_BY_LEVEL,
    _parse_candidate_level,
    _YOE_TOLERANCE_YEARS,
    _LABEL_BY_LEVEL,
)

print("=== YOE extraction from JDs ===")
jds = [
    ("We require 5+ years of experience in product management", 5),
    ("Minimum 8 years of hands-on development experience",       8),
    ("At least 3 years experience with SQL required",            3),
    ("Looking for a candidate with 3-7 years of relevant experience", 3),
    ("Ideal candidate has 10 or more years in the industry",    10),
    ("Fresh graduates welcome to apply",                         0),
    ("Entry level role, no prior experience needed",             0),
    ("Must have 2 to 4 years of experience",                     2),
]
for jd, expected in jds:
    yoe = _extract_jd_yoe_requirement(jd)
    status = "PASS" if yoe == expected else "FAIL"
    print(f"  [{status}] YOE={yoe} (expected {expected}) | {jd[:70]}")

print()
print("=== Early-exit decision matrix ===")
cases = [
    # With tolerance=4, the cutoff is cand_max + 4. Cases at the boundary
    # KEEP (auto-skip only triggers when JD clearly exceeds the candidate
    # range by more than the tolerance). Downstream the level-gap penalty
    # still penalises large title-vs-candidate mismatches.
    ("Fresher (0-1 yrs)",           5,  "KEEP"),   # 1 + 4 = 5, 5 > 5 is False
    ("Fresher (0-1 yrs)",           8,  "SKIP"),   # 8 > 5
    ("Entry / Associate (1-3 yrs)", 5,  "KEEP"),
    ("Entry / Associate (1-3 yrs)", 7,  "KEEP"),   # 3 + 4 = 7, boundary
    ("Entry / Associate (1-3 yrs)", 10, "SKIP"),   # 10 > 7
    ("Mid-level (3-6 yrs)",         5,  "KEEP"),
    ("Mid-level (3-6 yrs)",         10, "KEEP"),   # 6 + 4 = 10, boundary
    ("Mid-level (3-6 yrs)",         12, "SKIP"),   # 12 > 10
    ("Mid-level (3-6 yrs)",         15, "SKIP"),
    ("Senior (6-10 yrs)",           8,  "KEEP"),
    ("Senior (6-10 yrs)",           14, "KEEP"),   # 10 + 4 = 14, boundary
    ("Senior (6-10 yrs)",           18, "SKIP"),   # 18 > 14
]
for lbl, jd_min, expected in cases:
    cand_lvl = _parse_candidate_level(lbl)
    _, cand_max = _CAND_YOE_BY_LEVEL.get(cand_lvl, (0, 0))
    would_skip = jd_min > cand_max + _YOE_TOLERANCE_YEARS
    actual = "SKIP" if would_skip else "KEEP"
    status = "PASS" if actual == expected else "FAIL"
    print(
        f"  [{status}] {lbl:30} JD={jd_min:2}y  "
        f"(cand_max={cand_max}, tolerance={_YOE_TOLERANCE_YEARS}) "
        f"-> {actual}  (expected {expected})"
    )
