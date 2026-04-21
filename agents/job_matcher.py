# agents/job_matcher.py

import os
import json
import re
import time
from agents.runtime import track_llm_call
from agents.llm_client import chat_quality

# ─────────────────────────────────────────────────────────────
# EXPERIENCE-LEVEL GAP PENALTY
# ─────────────────────────────────────────────────────────────
#
# The matcher prompt soft-hints about the candidate's level, but the LLM
# is inconsistent at enforcing it. We add a deterministic post-scoring
# penalty based on title regex → seniority inference vs the candidate's
# declared level.
#
# Ladder (must match app.py sidebar options):
#   0 - Fresher          (0-1 yrs, new grad)
#   1 - Entry/Associate  (1-3 yrs)
#   2 - Mid-level        (3-6 yrs)     ← default
#   3 - Senior           (6-10 yrs)
#   4 - Lead/Manager     (8+ yrs, people management)
#   5 - Director/VP+     (12+ yrs, exec)

_LEVEL_BY_LABEL = {
    "fresher":                       0,
    "entry / associate":             1,
    "entry/associate":               1,
    "mid-level":                     2,
    "mid level":                     2,
    "senior":                        3,
    "lead / manager":                4,
    "lead/manager":                  4,
    "director / vp+":                5,
    "director/vp+":                  5,
}

_LABEL_BY_LEVEL = {
    0: "Fresher",
    1: "Entry/Associate",
    2: "Mid-level",
    3: "Senior",
    4: "Lead/Manager",
    5: "Director/VP+",
}

# Candidate YOE range per level — used to compare against JD YOE requirements.
# The MAX value is what we use for the tolerance check: a candidate with
# max=6 yrs is still considered for a 10-yr-minimum JD (within +4 tolerance).
_CAND_YOE_BY_LEVEL = {
    0: (0, 1),    # Fresher           → tolerates JDs up to ~5 yrs
    1: (1, 3),    # Entry/Associate   → tolerates JDs up to ~7 yrs
    2: (3, 6),    # Mid-level         → tolerates JDs up to ~10 yrs
    3: (6, 10),   # Senior            → tolerates JDs up to ~14 yrs
    4: (8, 15),   # Lead/Manager      → tolerates JDs up to ~19 yrs
    5: (12, 25),  # Director/VP+      → no upper filter
}

# Tolerance: candidate is considered IF candidate_max + TOLERANCE >= jd_min.
# 4 years is intentionally forgiving — strong skill match can bridge a
# YOE gap, and mis-parsed JD YOE (e.g. '5-8 years' read as 8) shouldn't
# auto-skip a 3-yr candidate who's otherwise perfect for the role.
_YOE_TOLERANCE_YEARS = 4


def _extract_jd_yoe_requirement(jd_text: str) -> int:
    """
    Parse the JD for explicit YOE requirements and return the MINIMUM years
    required. Returns 0 when no requirement found.

    Recognises patterns like:
      - "5+ years"              / "5 or more years"
      - "at least 5 years"      / "minimum of 5 years"
      - "3-7 years experience"  (takes the lower bound)
      - "5 years experience required"

    Uses the LOWEST minimum found in the JD — that's the hard floor.
    For example "3-7 years" extracts 3 (not 7) because a candidate with
    3 YOE satisfies the range's lower bound.
    """
    if not jd_text:
        return 0
    t = re.sub(r"\s+", " ", jd_text.lower())

    candidates: list = []
    # "5+ years" / "5 or more years" (also matches plain "5 years")
    for m in re.finditer(r"\b(\d{1,2})\s*\+?\s*(?:or\s+more\s+)?years?\b", t):
        candidates.append(int(m.group(1)))
    # "at least 5 years" / "minimum of 5 years"
    for m in re.finditer(r"\b(?:at\s+least|minimum(?:\s+of)?)\s+(\d{1,2})\s*years?\b", t):
        candidates.append(int(m.group(1)))
    # "3-7 years" / "3 to 7 years" → lower bound is the effective minimum
    for m in re.finditer(r"\b(\d{1,2})\s*(?:-|to)\s*(\d{1,2})\s*years?\b", t):
        candidates.append(int(m.group(1)))

    if not candidates:
        return 0
    reasonable = [c for c in candidates if 0 < c <= 25]
    if not reasonable:
        return 0
    # Lowest = the effective hard floor the candidate must clear.
    return min(reasonable)


def _parse_candidate_level(label: str) -> int:
    """
    Map the UI dropdown label (e.g. 'Mid-level (3-6 yrs)') to a ladder int.
    Returns 2 (Mid-level) if unrecognised.
    """
    if not label:
        return 2
    # Strip the parenthesised yrs hint and lowercase.
    base = re.sub(r"\s*\(.*?\)\s*", "", label).strip().lower()
    return _LEVEL_BY_LABEL.get(base, 2)


def _infer_job_level(title: str) -> int:
    """
    Heuristic: regex-infer the seniority level from the job title alone.
    Returns 2 (Mid-level) when nothing matches.
    """
    t = (title or "").lower()
    # Order matters: more specific patterns first.
    if re.search(r"\b(chief|c[eo]o|cto|cfo|cpo|svp|evp|vp|vice\s+president)\b", t):
        return 5
    if re.search(r"\b(director|head\s+of)\b", t):
        return 5
    if re.search(r"\b(principal|staff)\b", t):
        return 4
    # Check for entry-level keywords BEFORE mid-level defaults.
    if re.search(r"\b(junior|jr\.?|associate|intern|graduate|trainee|entry|apprentice|fresher)\b", t):
        return 1
    # Check for seniority modifiers BEFORE mid-level defaults.
    if re.search(r"\b(senior|sr\.?|snr)\b", t):
        return 3
    # Check for lead BEFORE mid-level patterns (e.g., "Lead Product Manager" -> L4, not L2).
    if re.search(r"\blead\b", t):
        return 4
    # Common mid-level role titles that default to L2 unless modified above.
    # These are checked AFTER seniority/entry/lead checks so "Senior Product Manager"
    # gets L3, "Lead Product Manager" gets L4, and plain "Product Manager" gets L2.
    if re.search(r"\b(product\s+manager|software\s+engineer|data\s+scientist|data\s+analyst|backend\s+engineer|frontend\s+engineer|full\s+stack\s+engineer|devops\s+engineer|qa\s+engineer|business\s+analyst|project\s+manager|program\s+manager|operations\s+manager|marketing\s+manager|sales\s+manager|hr\s+manager|finance\s+manager|account\s+manager|customer\s+success\s+manager|consultant|specialist|analyst|coordinator)\b", t):
        return 2
    # Check for manager (only if no entry/senior/lead/mid-level keyword matched).
    if re.search(r"\b(manager|mgr)\b", t):
        return 4
    return 2


def _apply_level_gap_penalty(
    score:        int,
    job_title:    str,
    candidate:    int,
) -> tuple:
    """
    Apply a deterministic penalty based on the gap between the candidate's
    declared level and the job title's inferred level.

    Returns (adjusted_score, note). note is a short string for the caller
    to surface in the reasoning field (empty string when no adjustment).
    """
    job_lvl  = _infer_job_level(job_title)
    gap      = job_lvl - candidate          # positive = candidate overreaching
    if gap == 0:
        return score, ""

    if gap > 0:
        # Candidate is below the role. Strong penalty (demos a bad fit).
        # 1 level = -15, 2 levels = -30, 3+ levels = -60 and cap at 35.
        penalty = min(15 * gap, 60)
        adjusted = max(0, score - penalty)
        if gap >= 3:
            adjusted = min(adjusted, 35)
        note = (
            f"Seniority mismatch: job inferred as "
            f"{_LABEL_BY_LEVEL[job_lvl]}, candidate selected "
            f"{_LABEL_BY_LEVEL[candidate]} (-{penalty}pt)."
        )
        return adjusted, note

    # gap < 0  — candidate is above the role (under-leveling).
    # Mild penalty: 1 level -5, 2 levels -12, 3+ -20.
    absgap = -gap
    penalty = min(5 * absgap + max(0, absgap - 1) * 2, 20)
    adjusted = max(0, score - penalty)
    note = (
        f"Role is below candidate level "
        f"({_LABEL_BY_LEVEL[job_lvl]} vs {_LABEL_BY_LEVEL[candidate]}) "
        f"(-{penalty}pt)."
    )
    return adjusted, note


def _parse_retry_seconds(error_message: str) -> float:
    match = re.search(r"Please try again in (\d+)m([\d.]+)s", str(error_message))
    if match:
        return int(match.group(1)) * 60 + float(match.group(2))
    match = re.search(r"Please try again in ([\d.]+)s", str(error_message))
    if match:
        return float(match.group(1))
    return 60


def _extract_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return {}


def _call_with_retry(prompt: str, max_tokens: int = 400, retries: int = 3) -> str:
    from agents.runtime import track_llm_call
    for attempt in range(retries):
        try:
            track_llm_call(agent="matcher")
            return chat_quality(prompt, max_tokens=max_tokens, temperature=0.2)
        except Exception as e:
            print(f"   ❌ LLM error (attempt {attempt+1}): {type(e).__name__}: {e}")
            if attempt < retries - 1:
                time.sleep(5)
            else:
                raise

    return ""


def match_cv_to_job(
    cv_text:         str,
    job_description: str,
    job_title:       str = "",
    company:         str = "",
    cv_collection:   str = "",
    experience_level: str = "",  # ✅ NEW — experience level filter
) -> dict:

    _default_fail = {
        "match_score":    0,
        "matched_skills": [],
        "missing_skills": [],
        "strengths":      [],
        "improvements":   [],
        "reasoning":      "",
    }

    if not job_description or len(job_description.strip()) < 30:
        print(f"   ⚠️  Empty description for {job_title} at {company}")
        return {**_default_fail, "improvements": ["Job description was empty."]}

    if not cv_text or len(cv_text.strip()) < 50:
        print(f"   ⚠️  Empty CV text")
        return {**_default_fail, "improvements": ["CV text was empty."]}

    # ── EARLY-EXIT level gap check (saves an LLM call) ─────────────────
    # Two complementary checks BEFORE the LLM call:
    #   1. Title-based level gap:   skip when job_level - cand_level >= 2
    #   2. Explicit YOE in JD:      skip when jd_min > cand_max + TOLERANCE
    # Either condition short-circuits the LLM call and returns a capped
    # score so this job still shows up in the "skipped" panel with a
    # human-readable reason.
    cand_yoe_min, cand_yoe_max = 0, 0
    jd_min_yoe                 = 0
    if experience_level:
        cand_lvl = _parse_candidate_level(experience_level)
        job_lvl  = _infer_job_level(job_title)
        gap      = job_lvl - cand_lvl
        cand_yoe_min, cand_yoe_max = _CAND_YOE_BY_LEVEL.get(cand_lvl, (0, 0))
        jd_min_yoe                 = _extract_jd_yoe_requirement(job_description)

        # (1) Title-based level gap — auto-skip if too large.
        if gap >= 2 or gap <= -3:
            note = (
                f"Skipped LLM scoring: seniority gap too large "
                f"(job inferred as {_LABEL_BY_LEVEL[job_lvl]}, "
                f"candidate selected {_LABEL_BY_LEVEL[cand_lvl]}, "
                f"gap={gap:+d})."
            )
            capped = 30 if gap >= 2 else 40
            print(f"   ⏭️  {note} (capped at {capped}/100)")
            return {
                **_default_fail,
                "match_score": capped,
                "reasoning":   note,
                "improvements": [
                    "Adjust your experience level selection, "
                    "or search for roles closer to your level."
                ],
            }

        # (2) Explicit YOE requirement in JD — auto-skip if the JD asks for
        # clearly more years than the candidate has (±4 yr tolerance).
        # Example: candidate_max=3, jd_min=10 → 10 > 3+4 → SKIP
        # Example: candidate_max=3, jd_min=6  → 6 > 3+4 is False → KEEP
        if jd_min_yoe > 0 and jd_min_yoe > cand_yoe_max + _YOE_TOLERANCE_YEARS:
            note = (
                f"Skipped LLM scoring: JD requires {jd_min_yoe}+ yrs, "
                f"candidate range is {cand_yoe_min}-{cand_yoe_max} yrs "
                f"(tolerance {_YOE_TOLERANCE_YEARS}y)."
            )
            print(f"   ⏭️  {note} (capped at 25/100)")
            return {
                **_default_fail,
                "match_score": 25,
                "reasoning":   note,
                "improvements": [
                    f"JD requires {jd_min_yoe}+ years of experience; "
                    "consider searching for less senior roles."
                ],
            }

    # ── RAG retrieval with full-CV fallback ──────────────────────────────
    cv_for_prompt = cv_text  # default: always use full CV
    if cv_collection:
        try:
            from agents.cv_embeddings import retrieve, format_chunks_for_prompt
            # k=24 is a soft cap — retrieve() still caps at collection count
            # so a short CV indexed as ~8 chunks just returns all 8. Higher k
            # means we rarely need the sparse-fallback for normal-length CVs.
            chunks = retrieve(cv_collection, job_description, k=24)
            if chunks:
                candidate = format_chunks_for_prompt(chunks)
                # If retrieval covers <50% of CV chars, use full CV instead.
                # This is expected behaviour for short CVs with tiny chunks,
                # not a bug — downgraded from warning to info.
                if len(candidate) < len(cv_text) * 0.5:
                    print(f"   ℹ️  RAG coverage {len(candidate)}/{len(cv_text)} chars "
                          f"(<50%) — using full CV for stronger context")
                    cv_for_prompt = cv_text
                else:
                    cv_for_prompt = candidate
                    print(f"   🧠 matcher using {len(chunks)} retrieved chunks "
                          f"({len(cv_for_prompt)} chars vs full {len(cv_text)})")
            else:
                print(f"   ⚠️  RAG returned no chunks — using full CV")
                cv_for_prompt = cv_text
        except Exception as e:
            print(f"   ⚠️  vector retrieval failed ({type(e).__name__}: {e}) "
                  f"— using full CV")
            cv_for_prompt = cv_text

    from agents.prompt_safety import wrap_untrusted_block, untrusted_block_preamble
    jd_block  = wrap_untrusted_block(job_description, label="JOB_DESCRIPTION")
    cv_block  = wrap_untrusted_block(cv_for_prompt,   label="CANDIDATE_CV")
    preamble  = untrusted_block_preamble(["JOB_DESCRIPTION", "CANDIDATE_CV"])

    # ✅ Experience level + YOE context added to prompt. We feed the LLM
    # hard numeric facts (candidate range, JD requirement) instead of just
    # a soft level label, so borderline cases score more accurately.
    level_context = ""
    if experience_level:
        parts = [
            f"\nCandidate's target experience level: {experience_level}"
        ]
        if cand_yoe_max > 0:
            parts.append(
                f" (approx {cand_yoe_min}-{cand_yoe_max} years of experience)"
            )
        if jd_min_yoe > 0:
            parts.append(
                f". This JD appears to require at least {jd_min_yoe} years"
            )
        parts.append(
            ".\nPenalise when the role's seniority or YOE requirement "
            "clearly exceeds the candidate's range by more than 4 years, "
            "but do NOT penalise small gaps (±1-3 years) when the skills "
            "are a strong match.\n"
        )
        level_context = "".join(parts)

    prompt = f"""
You are an expert recruiter. Score how well this CV matches the job.

{preamble}
{level_context}
JOB TITLE: {job_title}
COMPANY: {company}

{jd_block}

{cv_block}

IMPORTANT — Skill classification rules:
- matched_skills: Skills that appear in BOTH the JD and the CV
- missing_skills: Skills that are EXPLICITLY required by the JD but NOT found in the CV
- Do NOT mark a skill as "missing" if it's in the CV but not in the JD — that's normal
- Do NOT mark a skill as "missing" if it's a general term not specifically required by the JD
- Only mark skills as "missing" when they are clearly required by the JD and completely absent from the CV

Respond ONLY with a JSON object (no markdown, no extra text):
{{
  "match_score":    <integer 0-100>,
  "matched_skills": ["skill1", "skill2"],
  "missing_skills": ["skill1", "skill2"],
  "strengths":      ["strength1", "strength2"],
  "improvements":   ["improvement1"],
  "reasoning":      "<1-2 sentence explanation>"
}}
""".strip()

    raw = _call_with_retry(prompt, max_tokens=400)

    if not raw:
        return {**_default_fail, "improvements": ["LLM call failed after retries."]}

    print(f"   📝 Raw response (200 chars): {raw[:200]}")

    result = _extract_json(raw)
    if not result or "match_score" not in result:
        print(f"   ⚠️  JSON parse failed for {job_title} at {company}")
        return {**_default_fail, "reasoning": raw[:300]}

    result["match_score"] = max(0, min(100, int(result["match_score"])))

    # ── Deterministic experience-level gap penalty ─────────────────────
    # The LLM soft-hints the level via `level_context`, but scoring is
    # inconsistent. We post-process with a regex-based seniority inference
    # so a Fresher applying for VP roles gets hard-capped regardless of
    # how keyword-similar the JD looks.
    if experience_level:
        cand_lvl = _parse_candidate_level(experience_level)
        orig_score = result["match_score"]
        adjusted, note = _apply_level_gap_penalty(
            orig_score, job_title, cand_lvl,
        )
        if note:
            result["match_score"] = adjusted
            existing = (result.get("reasoning") or "").strip()
            result["reasoning"] = f"{note} {existing}".strip() if existing else note
            print(
                f"   ⚖️  level-gap: {orig_score}→{adjusted}/100 ({note})"
            )

    print(f"   ✅ Match score: {result['match_score']}/100 — {job_title} at {company}")
    return result