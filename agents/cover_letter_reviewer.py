"""
agents.cover_letter_reviewer
============================

Fact-grounding review for generated cover letters. Mirrors the pattern of
agents.reviewer (which scores tailored CVs): the reviewer reads the letter
alongside the CV + JD and returns a structured verdict.

The critical rubric item is NO FABRICATION — every claim of fact (years at
a company, technology used, measurable outcome, degree, award) must map
cleanly to something in the CV text. Tone / JD-alignment / length are
scored too, but fabrication is the dominant axis:

    score >= ACCEPT_THRESHOLD  →  verdict="accept"
    otherwise                  →  verdict="retry"
                                   (feedback field tells the generator what
                                    to change; one retry is allowed)

Never raises. On any LLM / parse failure it returns a fail-open verdict
(score=100, verdict=accept) so we don't cause an infinite retry loop if
the reviewer itself is broken.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict

from dotenv import load_dotenv
from agents.prompt_safety import wrap_untrusted_block, untrusted_block_preamble
from agents.runtime       import track_llm_call

load_dotenv()

# Accept threshold — below this, the tailor node will retry once.
ACCEPT_THRESHOLD = int(os.getenv("COVER_REVIEWER_ACCEPT_THRESHOLD", "70"))


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _extract_json(text: str) -> Dict[str, Any]:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    return {}


# ─────────────────────────────────────────────────────────────
# Prompt (unchanged)
# ─────────────────────────────────────────────────────────────

_PROMPT = """You are the COVER-LETTER REVIEWER of a job-application automation system.

Your job: score a generated cover letter against the candidate's CV and the
job description, then return 1-3 sentences of ACTIONABLE feedback the writer
can use to revise on the next attempt.

{safety_preamble}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TARGET ROLE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Title  : {job_title}
Company: {company}

{job_description_block}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CANDIDATE CV (ground truth — only facts in here are allowed)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{cv_block}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COVER LETTER (to be reviewed)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{cover_letter}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RUBRIC (score 0-100)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Score the letter on FOUR dimensions, then combine:

  1. GROUNDING (40%)  — every factual claim maps to the CV.
                        Any fabricated company, date, degree, metric,
                        technology, or award => hard cap at 55.
  2. SPECIFICITY (25%) — the letter cites concrete experience from the CV
                        (employer names, numbers, specific problems solved)
                        rather than generic praise.
  3. JD ALIGNMENT (20%) — the letter naturally mirrors 2-4 keywords from
                        the JD and addresses the role's core need.
  4. TONE / LENGTH (15%) — 340-400 words; intelligent and warm;
                        no buzzwords ("dynamic", "passionate", "fast-paced",
                        "results-driven"); no "I look forward to hearing").

SCORE BANDS
  90-100: grounded, specific, JD-aligned, confident prose; no fabrication.
  75-89 : solid but 1 minor grounding gap OR 1 weak/generic paragraph.
  60-74 : generic or off-theme; bullets from CV not used effectively.
  0-59  : fabrication present OR letter ignores the role / is off-length.

STRICT RULES
- Fabrication detection is non-negotiable. If ANY claim (e.g. "led team
  of 10" when CV says "team of 5", or "PhD" when CV shows MSc) is not
  supported by the CV, cap score at 55 AND list the fabricated claim
  verbatim under "weaknesses".
- Feedback must be ACTIONABLE — name the paragraph and what to change.
- Max 3 sentences of feedback.

OUTPUT (JSON only — no prose, no markdown fences):
{{
  "score":        <int 0-100>,
  "strengths":    ["...", "..."],
  "weaknesses":   ["...", "..."],
  "fabrications": ["<claim not supported by CV>", "..."],
  "feedback":     "...",
  "verdict":      "accept"   // if score >= {threshold}, else "retry"
}}

Return the review now:"""


# ─────────────────────────────────────────────────────────────
# LLM call — chat_fast (Groq) — reviewer is a logic/scoring task
# ─────────────────────────────────────────────────────────────

def _call_llm(prompt: str, max_tokens: int = 500) -> str:
    # C1: removed 3-retry exception loop. chat_fast handles Groq key rotation
    # internally; an exception here means the whole pool is exhausted and
    # retrying just burns more failed-call quota. Single attempt; on exception,
    # return "" so the fail-open path in review_cover_letter() handles it.
    from agents.llm_client import chat_fast
    track_llm_call(agent="cover_reviewer")
    try:
        return chat_fast(prompt, max_tokens=max_tokens, temperature=0.1)
    except Exception as e:
        print(f"   ❌ cover-reviewer error: {type(e).__name__}: {e}")
        return ""


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────

def review_cover_letter(
    cv_text:         str,
    cover_letter:    str,
    job_description: str,
    job_title:       str = "",
    company:         str = "",
) -> Dict[str, Any]:
    """
    Score a generated cover letter. Returns a validated review dict:

        {
          "score":        int 0..100,
          "strengths":    [str, ...],
          "weaknesses":   [str, ...],
          "fabrications": [str, ...],
          "feedback":     str,
          "verdict":      "accept" | "retry",
          "threshold":    int,
        }

    Never raises. On LLM/parse failure returns a fail-open accept verdict.
    """
    jd_block = wrap_untrusted_block(
        (job_description or "").strip() or "(no description)",
        label="JOB_DESCRIPTION",
    )
    cv_block = wrap_untrusted_block(
        (cv_text or "").strip() or "(empty CV)",
        label="CANDIDATE_CV",
    )

    prompt = _PROMPT.format(
        safety_preamble       = untrusted_block_preamble(
            ["JOB_DESCRIPTION", "CANDIDATE_CV"]
        ),
        job_title             = job_title or "(unspecified)",
        company               = company   or "(unspecified)",
        job_description_block = jd_block,
        cv_block              = cv_block,
        cover_letter          = (cover_letter or "").strip() or "(empty letter)",
        threshold             = ACCEPT_THRESHOLD,
    )

    text = _call_llm(prompt)
    raw  = _extract_json(text)
    if not raw:
        return _failopen("(cover-reviewer unavailable)")

    # Normalise + validate
    try:
        score = max(0, min(100, int(raw.get("score", 0))))
    except Exception:
        return _failopen("(cover-reviewer returned non-numeric score)")

    fabrications = [str(x) for x in (raw.get("fabrications") or []) if x]
    # Hard rule: fabrications present → cap score
    if fabrications and score > 55:
        score = 55

    verdict = "accept" if score >= ACCEPT_THRESHOLD else "retry"

    return {
        "score":        score,
        "strengths":    [str(x) for x in (raw.get("strengths")  or [])][:5],
        "weaknesses":   [str(x) for x in (raw.get("weaknesses") or [])][:5],
        "fabrications": fabrications[:5],
        "feedback":     str(raw.get("feedback") or "").strip(),
        "verdict":      verdict,
        "threshold":    ACCEPT_THRESHOLD,
    }


def _failopen(reason: str) -> Dict[str, Any]:
    """Return an accept verdict when the reviewer itself can't be trusted."""
    return {
        "score":        100,
        "strengths":    [],
        "weaknesses":   [],
        "fabrications": [],
        "feedback":     reason,
        "verdict":      "accept",
        "threshold":    ACCEPT_THRESHOLD,
    }