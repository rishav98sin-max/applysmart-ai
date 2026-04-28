# agents/reviewer.py
#
# Reviewer agent. Given the ORIGINAL CV outline, the DIFF a tailor agent
# produced (summary rewrite + bullet reorder), and the target JD, it scores
# the tailored CV (0-100) and produces short, actionable feedback.
#
# Used inside `tailor_and_generate_node` to decide whether to accept the
# first tailor attempt or re-tailor with feedback.
#
# Output shape (validated):
#   {
#     "score":      int 0-100,          # "how well does this tailored CV match the JD?"
#     "strengths":  [str, ...],         # up to 3 things the tailor did well
#     "weaknesses": [str, ...],         # up to 3 concrete misses / gaps
#     "feedback":   str,                # 1-3 sentences, actionable, referring to CV facts
#     "verdict":    "accept" | "retry"  # convenience: reviewer's own recommendation
#   }
#
# The reviewer never invents facts. If the diff over-claims (e.g. summary
# mentions a skill not in the original CV), that IS a weakness.

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Optional

from agents.runtime import track_llm_call
from agents.llm_client import chat_fast

# When reviewer score < this, the tailor node will retry (once).
ACCEPT_THRESHOLD = int(os.getenv("REVIEWER_ACCEPT_THRESHOLD", "72"))


def _extract_json(text: str) -> dict:
    if not text:
        return {}
    for match in re.finditer(r"\{.*\}", text, re.DOTALL):
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            continue
    return {}


def _render_diff_for_review(
    outline: Dict[str, Any],
    diff: Dict[str, Any],
) -> str:
    """
    Produce a human-readable rendering of the TAILORED CV = (original outline
    with the diff applied). The reviewer reads this to judge JD-fit.
    """
    parts: List[str] = []

    # Summary (new or unchanged)
    new_summary = (diff.get("summary") or "").strip()
    old_summary = (outline.get("summary") or "").strip()
    parts.append("SUMMARY (tailored):")
    parts.append(new_summary or old_summary or "(none)")
    parts.append("")

    # Roles with possibly-reordered / rewritten / dropped bullets.
    # The bullet edits dict accepts two shapes:
    #   legacy: {"Role Header": [2, 0, 1]}
    #   new:    {"Role Header": [{"i": 2, "text": "new wording" | None}, ...]}
    bullets_diff = diff.get("bullets") or {}

    def _btext(b) -> str:
        # build_outline emits {"text": str, "length": int}; tolerate legacy str.
        if isinstance(b, dict):
            return str(b.get("text") or "")
        return str(b) if b is not None else ""

    for role in outline.get("roles", []):
        header = role["header"]
        originals = role.get("bullets", [])
        order = bullets_diff.get(header)
        rendered: List[str] = []
        if isinstance(order, list) and order:
            for item in order:
                if isinstance(item, dict):
                    try:
                        idx = int(item.get("i"))
                    except (TypeError, ValueError):
                        continue
                    if not (0 <= idx < len(originals)):
                        continue
                    orig_text = _btext(originals[idx])
                    new_text  = item.get("text")
                    if isinstance(new_text, str) and new_text.strip():
                        # Show BOTH so the reviewer can spot fabrication.
                        rendered.append(
                            f"  - [REWRITTEN] {new_text.strip()}\n"
                            f"    (original: {orig_text})"
                        )
                    else:
                        rendered.append(f"  - {orig_text}")
                else:
                    try:
                        idx = int(item)
                    except (TypeError, ValueError):
                        continue
                    if 0 <= idx < len(originals):
                        rendered.append(f"  - {_btext(originals[idx])}")
        else:
            # No edits for this role — show originals as-is.
            for b in originals:
                rendered.append(f"  - {_btext(b)}")
        parts.append(f"ROLE: {header}")
        parts.extend(rendered)
        parts.append("")

    # Skills
    skills_order = diff.get("skills_order") or []
    skills = skills_order or outline.get("skills") or []
    if skills:
        parts.append("SKILLS:")
        parts.append(", ".join(skills))

    return "\n".join(parts)


_PROMPT = """You are the REVIEWER agent of a job-application automation system.

Your job: score how well a TAILORED CV fits a specific JOB DESCRIPTION,
then give 1-3 sentences of ACTIONABLE feedback the tailor can use to revise.

{safety_preamble}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TARGET ROLE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Title  : {job_title}
Company: {company}

{job_description_block}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TAILORED CV (as the candidate would see it after the diff was applied)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{tailored_cv}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RUBRIC (score 0-100)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
90-100: Summary mirrors the JD's exact keywords; MULTIPLE bullets REWRITTEN
        to lead with JD verbs/keywords; no filler; no fabrication.
70-89 : Summary is good AND at least some bullets are [REWRITTEN] to mirror
        the JD, but 1-2 keywords missed or rewrites could be sharper.
50-69 : Summary was updated but bullets were barely touched (reorder only,
        no rewrites) OR rewrites are too generic to be role-specific.
0-49  : Summary is off-theme OR fabrications present OR top bullets ignore
        the JD entirely.

STRICT RULES
- NEVER reward fabrication.
  * If the SUMMARY mentions a skill or claim not evident in the CV bullets,
    cap the score at 60 and list it as a weakness.
  * If any bullet is marked [REWRITTEN], compare it to the "(original: ...)"
    line directly below. If the rewrite introduces NEW facts — numbers,
    platforms, tools, certifications, outcomes, team sizes, years — that do
    NOT appear in the original, flag it as a weakness and cap the score
    at 55. Reframing the same facts is fine; inventing new ones is not.
- PENALISE SUMMARY-ONLY TAILORING.
  * Count the [REWRITTEN] markers in the bullets. If ZERO bullets are
    rewritten across the whole CV AND the JD lists specific responsibilities
    the bullets could have been re-framed around, cap the score at 70 and
    list "bullets not rewritten" as the #1 weakness. A tailor that only
    touches the summary is not doing real per-job tailoring.
- Feedback must be ACTIONABLE: tell the tailor exactly what to change
  (e.g. "Rewrite Accenture bullet 2 to lead with 'Led cross-functional…'
  to match the JD's language", NOT "make it more tailored").
- Keep feedback short — 1 to 3 sentences max.

OUTPUT (JSON only, no prose, no markdown fences):
{{
  "score":      <int 0-100>,
  "strengths":  ["...", "..."],
  "weaknesses": ["...", "..."],
  "feedback":   "...",
  "verdict":    "accept"   // if score >= {threshold}, else "retry"
}}

Return the review now:"""


def _call_llm(prompt: str, max_tokens: int = 500) -> str:
    # C1: removed module-level 3-retry loop. chat_fast already rotates Groq
    # keys internally; retrying here just multiplies token waste on real
    # exhaustion (3 module retries × N keys × inner client retries = 9-30 calls
    # for a single logical operation, all hitting the same exhausted pool).
    # Exceptions propagate to the per-job try/except in job_agent.py where
    # they're handled gracefully as job-level failures.
    track_llm_call(agent="reviewer")
    try:
        return chat_fast(prompt, max_tokens=max_tokens, temperature=0.1)
    except Exception as e:
        print(f"   ❌ reviewer LLM error: {type(e).__name__}: {e}")
        return ""


def _sanitise(raw: Dict[str, Any]) -> Dict[str, Any]:
    try:
        score = int(raw.get("score", 0))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(100, score))

    def _as_list(x) -> List[str]:
        if not isinstance(x, list):
            return []
        out = []
        for item in x:
            s = str(item).strip()
            if s:
                out.append(s)
        return out[:5]

    strengths  = _as_list(raw.get("strengths"))
    weaknesses = _as_list(raw.get("weaknesses"))
    feedback   = str(raw.get("feedback") or "").strip()[:500]
    verdict    = str(raw.get("verdict") or "").strip().lower()
    if verdict not in ("accept", "retry"):
        verdict = "accept" if score >= ACCEPT_THRESHOLD else "retry"

    return {
        "score":      score,
        "strengths":  strengths,
        "weaknesses": weaknesses,
        "feedback":   feedback,
        "verdict":    verdict,
    }


def review_tailored_cv(
    outline:          Dict[str, Any],
    diff:             Dict[str, Any],
    job_description:  str,
    job_title:        str = "",
    company:          str = "",
) -> Dict[str, Any]:
    """
    Score the tailored CV (original + diff applied) against the JD. Returns
    a validated review dict. Never raises: on LLM failure, returns
    `{score: 100, verdict: 'accept', feedback: '(reviewer unavailable)'}`
    so downstream retry logic doesn't loop.
    """
    from agents.prompt_safety import wrap_untrusted_block, untrusted_block_preamble
    tailored_cv = _render_diff_for_review(outline, diff)
    jd_block = wrap_untrusted_block(
        (job_description or "").strip() or "(no description)",
        label="JOB_DESCRIPTION",
    )
    prompt = _PROMPT.format(
        safety_preamble      = untrusted_block_preamble(["JOB_DESCRIPTION"]),
        job_title            = job_title or "(unspecified)",
        company              = company   or "(unspecified)",
        job_description_block = jd_block,
        tailored_cv          = tailored_cv,
        threshold            = ACCEPT_THRESHOLD,
    )

    text = _call_llm(prompt)
    raw  = _extract_json(text)
    if not raw:
        # C2: distinguish "reviewer didn't run" from "reviewer ran but emitted
        # garbage". The latter is suspicious — malformed JSON often correlates
        # with borderline-fabricating tailor output (LLM gets confused mid-
        # rationale). Fail-OPEN only when the LLM produced no text at all
        # (transport / quota failure). Fail-CLOSED with a forced retry when
        # the LLM produced text we couldn't parse.
        if not text:
            # Genuine reviewer-unavailable case → don't block the pipeline.
            try:
                from agents.analytics import track_event
                track_event("reviewer_failopen_noresponse", "system_infra", {
                    "company": company, "title": job_title,
                })
            except Exception:
                pass
            return {
                "score":      100,
                "strengths":  [],
                "weaknesses": [],
                "feedback":   "(reviewer unavailable — accepted by default)",
                "verdict":    "accept",
            }
        # Got text but couldn't parse JSON → fail-closed at score=50, retry.
        try:
            from agents.analytics import track_event
            track_event("reviewer_failclosed_badjson", "system_infra", {
                "company": company, "title": job_title,
                "raw_preview": text[:200],
            })
        except Exception:
            pass
        print(
            "   ⚠️  reviewer returned unparseable JSON — failing closed "
            "(score=50, retry) instead of fail-open"
        )
        return {
            "score":      50,
            "strengths":  [],
            "weaknesses": ["reviewer JSON parse failed — output may be malformed"],
            "feedback":   "Reviewer could not parse tailored output. "
                          "Re-tailor with a stricter, more literal style.",
            "verdict":    "retry",
        }

    review = _sanitise(raw)
    print(
        f"   🕵️  reviewer: {review['score']}/100 "
        f"({review['verdict']}) — {review['feedback'][:120]}"
    )
    return review
