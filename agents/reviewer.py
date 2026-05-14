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
# Lowered from 72 → 65: the old 72 threshold caused unnecessary retries
# when the tailor made real rewrites against borderline-match jobs (60-70%
# match score). A job that matched at 60% can't be tailored to 72+ because
# the structural gap is real — the reviewer correctly identifies it. Retrying
# just burns tokens without improving the output.
ACCEPT_THRESHOLD = int(os.getenv("REVIEWER_ACCEPT_THRESHOLD", "65"))


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
        # Apr 30: surface section type ("projects" vs "experience" etc.) so
        # the reviewer prompt's Personal-Projects fabrication rule has the
        # context it needs to fire. Defaults to "experience" when missing
        # to preserve backwards compat with older outline producers.
        section = (role.get("section") or "experience").strip().lower()
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
        parts.append(f"ROLE: {header}  [section={section}]")
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

{do_not_inject_block}

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

- PERSONAL-PROJECT FABRICATIONS = HARD CAP AT 50.
  * Each ROLE block is tagged with [section=...]. Roles with section=projects
    are SOLO work by default — no team, no stakeholders, no organisation,
    no cross-functional partners — UNLESS the original bullet already
    states otherwise (e.g. "co-built with X", "team of three", "with
    engineers", "shared with colleagues").
  * If a [REWRITTEN] bullet under a section=projects role introduces ANY
    of the following framings that the original lacks, this is FABRICATION
    and you MUST cap the score at 50 and list the offending phrase as the
    #1 weakness — UNLESS the original (shown below the [REWRITTEN] marker)
    or any of its sibling bullets in the same project already signals
    collaboration via a synonym ("with X", "shared", "colleagues",
    "engineers", "designers", "managers", "partner", "joint", "co-built",
    "team of N", "contributors"). In that case the rewrite is paraphrase,
    not fabrication — DO NOT cap.
      - "cross-functional", "cross-team"
      - "stakeholders", "stakeholder alignment", "primary liaison"
      - "the organisation", "across the org", "company-wide"
      - "managed escalations", "managed expectations of …"
      - "platform teams", "engineering teams", "business teams"
      - "partnered with engineering / design / product / business"
  * Note: bare "team" / "teams" alone is NOT enough to cap — those words
    appear in too many legitimate phrasings. Only cap when one of the
    multi-word phrases above appears AND the original lacks any collaboration
    signal.
  * A solo personal-project bullet rewritten as "Acted as bridge between
    business stakeholders and deep technical teams" is FABRICATION even
    if it doesn't add a number — it invents collaborators. Flag it.

- CREDENTIAL PRESERVATION (summary).
  * If the original summary contains a degree grade ("(2.1)", "First
    Class", "Distinction", "Cum Laude"), a years-of-experience claim
    ("4+ years", "5 years' experience"), or a numeric outcome, the
    rewritten summary MUST preserve every one verbatim. If any are
    missing, list "credential lost: <token>" as a weakness and cap the
    score at 65.
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

- MANDATORY SCORING DEDUCTIONS (apply BEFORE finalising the score):
  These deductions catch the silent-failure modes where the rubric above
  would otherwise score a partially-tailored CV at 85+.
    * Tailored SUMMARY is byte-identical to original (no rewrite at all):
      DEDUCT 15 points. The summary is the most-read section; an
      untailored summary means the tailor effectively didn't run.
    * More than 3 bullets across the whole CV are NOT marked [REWRITTEN]
      when at least 8 bullets exist and the JD has specific responsibilities
      the bullets could have been re-framed around: DEDUCT 10 points.
      Pure reorder-without-rewrite is not real tailoring.
    * Any [REWRITTEN] bullet under section=projects contains relational
      fabrication ("collaborating with stakeholders", "partnered with
      engineering", "cross-functional team", "led a team", etc.) that
      the original bullet does NOT contain: DEDUCT 20 points and list
      the offending phrase as the #1 weakness.
  These deductions stack. Apply all that fire, then clamp to [0, 100].
  Floor for "accept" verdict remains the configured ACCEPT_THRESHOLD.

OUTPUT (JSON only, no prose, no markdown fences):
{{
  "score":      <int 0-100>,
  "strengths":  ["...", "..."],
  "weaknesses": ["...", "..."],
  "feedback":   "...",
  "verdict":    "accept"   // if score >= {threshold}, else "retry"
}}

Return the review now:"""


def _call_llm(prompt: str, max_tokens: int = 500) -> tuple:
    """
    Returns (text, error_kind):
      - ("<json or whatever>", None)         : LLM responded
      - ("", "transport")                    : exception raised (rate limit,
                                               network, key exhaustion)
      - ("", "empty")                        : LLM returned empty string

    Run-17 audit fix #5: the previous version conflated transport errors
    with empty responses, both returning "". The caller then fail-opened
    at score=100 ("reviewer unavailable") for BOTH cases, which silently
    auto-accepted every job after a Groq key exhaustion. Now the caller
    can fail-CLOSED on transport (force retry) while still fail-opening
    when the LLM genuinely returned nothing parseable.
    """
    track_llm_call(agent="reviewer")
    try:
        text = chat_fast(prompt, max_tokens=max_tokens, temperature=0.1)
        if text is None or text == "":
            return "", "empty"
        return text, None
    except Exception as e:
        print(f"   ❌ reviewer LLM error: {type(e).__name__}: {e}")
        return "", "transport"


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
    do_not_inject:    Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Score the tailored CV (original + diff applied) against the JD. Returns
    a validated review dict. Never raises: on LLM failure, returns
    `{score: 100, verdict: 'accept', feedback: '(reviewer unavailable)'}`
    so downstream retry logic doesn't loop.

    do_not_inject: list of JD terms the tailor was instructed NOT to add
    (because they don't appear in the CV — fabrication risk). When provided,
    the reviewer is told not to penalise their absence. Without this, the
    reviewer sees "field sales", "GMV", "Dineout platform" missing from the
    tailored CV and scores it 60, triggering a retry that can't possibly fix
    the gap because those terms were correctly excluded.
    """
    from agents.prompt_safety import wrap_untrusted_block, untrusted_block_preamble
    tailored_cv = _render_diff_for_review(outline, diff)
    jd_block = wrap_untrusted_block(
        (job_description or "").strip() or "(no description)",
        label="JOB_DESCRIPTION",
    )

    # Build the do_not_inject advisory block for the reviewer prompt.
    # This prevents the reviewer from penalising correctly-excluded terms.
    do_not_inject_block = ""
    if do_not_inject:
        terms = ", ".join(f'"{t}"' for t in do_not_inject[:20])
        do_not_inject_block = (
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "TERMS CORRECTLY EXCLUDED FROM THE CV\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "The tailor was explicitly instructed NOT to add the following\n"
            "JD-specific terms because they do NOT appear in the candidate's\n"
            "original CV. Their absence is intentional (fabrication prevention),\n"
            "NOT a tailoring failure. Do NOT penalise or mention missing any of\n"
            "these terms in your score, weaknesses, or feedback:\n"
            f"  {terms}"
        )

    prompt = _PROMPT.format(
        safety_preamble       = untrusted_block_preamble(["JOB_DESCRIPTION"]),
        job_title             = job_title or "(unspecified)",
        company               = company   or "(unspecified)",
        job_description_block = jd_block,
        do_not_inject_block   = do_not_inject_block,
        tailored_cv           = tailored_cv,
        threshold             = ACCEPT_THRESHOLD,
    )

    text, err_kind = _call_llm(prompt)
    raw  = _extract_json(text) if text else {}
    if not raw:
        # Run-17 audit fix #5: distinguish three cases — transport error
        # (fail-CLOSED, force retry), empty LLM response (fail-CLOSED at
        # score=55, retry), and unparseable JSON (fail-CLOSED at score=50,
        # retry). The OLD code fail-OPENED at score=100 on transport
        # errors, which silently auto-accepted every subsequent job after
        # a Groq key exhaustion.
        if err_kind == "transport":
            try:
                from agents.analytics import track_event
                track_event("reviewer_failclosed_transport", "system_infra", {
                    "company": company, "title": job_title,
                })
            except Exception:
                pass
            print(
                "   ⚠️  reviewer transport error — failing closed "
                "(score=55, retry) to avoid silent auto-accept"
            )
            return {
                "score":      55,
                "strengths":  [],
                "weaknesses": ["reviewer transport error — unable to verify"],
                "feedback":   "Reviewer could not be reached. "
                              "Re-tailor with stricter adherence to the JD.",
                "verdict":    "retry",
            }
        if err_kind == "empty" or not text:
            # LLM returned nothing — fail-CLOSED at borderline score, retry
            # once. If we're already on the retry attempt, the upstream
            # MAX_TAILOR_RETRIES loop will accept whatever score lands.
            try:
                from agents.analytics import track_event
                track_event("reviewer_failclosed_empty", "system_infra", {
                    "company": company, "title": job_title,
                })
            except Exception:
                pass
            return {
                "score":      55,
                "strengths":  [],
                "weaknesses": ["reviewer returned empty response"],
                "feedback":   "Reviewer produced no review. "
                              "Re-tailor with sharper JD-aligned verbs.",
                "verdict":    "retry",
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
