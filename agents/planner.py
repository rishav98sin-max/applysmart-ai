# agents/planner.py
#
# Planner agent. Runs ONCE at the start of every pipeline, after the CV has
# been parsed. Produces a structured `plan` that downstream workers and the
# supervisor consult to make decisions.
#
# A plan is an LLM-drafted, JSON-validated object of the form:
#
#   {
#     "keyword_bundles": [
#       {"title": "Product Manager", "keywords": "Product Manager",
#        "location": "Dublin, Ireland", "reason": "primary user request"},
#       {"title": "Product Owner",   "keywords": "Product Owner",
#        "location": "Dublin, Ireland", "reason": "adjacent title, same skills"},
#       ...
#     ],
#     "quality_bar": {
#       "min_matches": 3,
#       "min_score":   60,
#       "max_scrape_rounds": 3
#     },
#     "emphasis_skills": ["SQL", "stakeholder management", "PRDs"],
#     "tone_hints":      "confident, specific, reference measurable outcomes",
#     "reasoning":       "one short paragraph about the plan"
#   }
#
# The planner never makes decisions by itself — it only proposes. The
# supervisor is the component that chooses what to execute next.

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Optional

from agents.runtime import track_llm_call
from agents.llm_client import chat_quality


def _extract_json(text: str) -> dict:
    if not text:
        return {}
    for match in re.finditer(r"\{.*\}", text, re.DOTALL):
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            continue
    return {}


_PROMPT = """You are the PLANNER agent of a job-application automation system.

The user has uploaded their CV and asked to find matching roles. Your job
is to draft a compact SEARCH + TAILORING PLAN that downstream agents (a
scraper, a matcher, a CV tailor, a cover-letter writer, an email sender)
and a supervisor will use.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
USER INPUTS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Desired job title : {job_title}
Desired location  : {location}
Target jobs       : {num_jobs}
User-set threshold: {match_threshold} (0-100)
Preferred board   : {source}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CV (truncated)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{cv_preview}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Produce 4-7 keyword bundles (fewer is fine if the CV is narrow, but err
   on the side of MORE — each bundle is a cheap scrape round that may
   surface matches the others miss):
   - Bundle 1: exact user-requested title + exact location.
   - Bundle 2: close synonym / variant title (e.g. 'Product Manager' →
     'Technical Product Manager', 'Digital Product Manager').
   - Bundle 3: adjacent title the CV also qualifies for
     (e.g. 'Product Manager' → 'Product Owner', 'Associate PM'),
     optionally with a broadened location (country-level if the user gave a city).
   - Do NOT invent unrelated titles. Stay within the CV's professional domain.
2. quality_bar:
   - min_matches    : how many >=min_score results are "enough" (2-5)
   - min_score      : honour the user's threshold unless clearly unrealistic
     (never drop below 50 without cause).
   - max_scrape_rounds: hard cap on scraping loops (<= len(keyword_bundles), max 5).
     Default to len(keyword_bundles) so the supervisor can cycle through all
     bundles if early rounds don't meet min_matches.
3. emphasis_skills: 3-7 skills/themes the CV already demonstrates that should
   be surfaced first when tailoring. Only pick what is TRULY in the CV.
4. tone_hints: 1 sentence on tone for the cover letter / summary rewrite.
5. Output ONLY a single JSON object (no prose, no markdown fences).

EXAMPLE OUTPUT:
{{
  "keyword_bundles": [
    {{"title": "Product Manager",             "keywords": "Product Manager",
      "location": "Dublin, Ireland",          "reason": "primary user request"}},
    {{"title": "Technical Product Manager",   "keywords": "Technical Product Manager",
      "location": "Dublin, Ireland",          "reason": "technical-depth variant of PM"}},
    {{"title": "Product Owner",               "keywords": "Product Owner",
      "location": "Dublin, Ireland",          "reason": "adjacent title, same skills"}},
    {{"title": "Associate Product Manager",   "keywords": "Associate Product Manager",
      "location": "Ireland",                  "reason": "broadened level + wider location"}},
    {{"title": "Product Analyst",             "keywords": "Product Analyst",
      "location": "Dublin, Ireland",          "reason": "data-leaning variant, entry into PM"}}
  ],
  "quality_bar": {{"min_matches": 3, "min_score": 60, "max_scrape_rounds": 2}},
  "emphasis_skills": ["PRDs", "stakeholder management", "SQL", "agentic AI"],
  "tone_hints": "confident and specific; quote measurable outcomes",
  "reasoning": "Primary role is PM. CV shows strong API/product and analytics experience so Product Owner is a safe adjacent search. Ireland broadens if Dublin is thin."
}}

Return the plan JSON now:"""


def _call_llm(prompt: str, max_tokens: int = 900) -> str:
    # C1: removed 3-retry exception loop. chat_quality already rotates Groq
    # keys; an exception means the pool is exhausted, retrying just burns
    # more failed-call quota. On exception, return "" so plan_search() falls
    # back to the deterministic _fallback_plan() instead of hanging.
    from agents.runtime import track_llm_call
    track_llm_call(agent="planner")
    try:
        return chat_quality(prompt, max_tokens=max_tokens, temperature=0.3)
    except Exception as e:
        print(f"   ❌ planner LLM error: {type(e).__name__}: {e}")
        return ""


def _sanitise_plan(raw: Dict[str, Any], user_inputs: Dict[str, Any]) -> Dict[str, Any]:
    """Validate + clamp the LLM's plan against reasonable bounds."""
    # ── keyword_bundles ──────────────────────────────────
    bundles_raw = raw.get("keyword_bundles")
    bundles: List[Dict[str, str]] = []
    if isinstance(bundles_raw, list):
        for b in bundles_raw:
            if not isinstance(b, dict):
                continue
            title = str(b.get("title") or "").strip()
            kw    = str(b.get("keywords") or title).strip()
            loc   = str(b.get("location") or user_inputs.get("location", "")).strip()
            reason = str(b.get("reason") or "").strip()
            if title and kw:
                bundles.append({
                    "title": title, "keywords": kw,
                    "location": loc, "reason": reason,
                })
    # Guarantee at least the user's primary bundle exists.
    if not bundles or bundles[0]["title"].lower() != user_inputs["job_title"].lower():
        bundles.insert(0, {
            "title":    user_inputs["job_title"],
            "keywords": user_inputs["job_title"],
            "location": user_inputs.get("location", ""),
            "reason":   "user-requested primary search",
        })
    bundles = bundles[:8]  # cap — enough for adaptive broadening, bounded to control cost

    # ── quality_bar ──────────────────────────────────────
    qb_raw = raw.get("quality_bar") or {}
    user_thr = int(user_inputs.get("match_threshold", 60))
    min_matches = int(qb_raw.get("min_matches", 3)) if isinstance(qb_raw, dict) else 3
    min_score   = int(qb_raw.get("min_score",   user_thr)) if isinstance(qb_raw, dict) else user_thr
    # Default to len(bundles) so the supervisor can cycle through every bundle
    # if early rounds don't meet min_matches. Capped at 5 to bound cost on a
    # huge plan, but no longer hard-capped at 2 (which made bundles 3+ unreachable).
    default_rounds = min(5, len(bundles)) if bundles else 1
    max_rounds  = int(qb_raw.get("max_scrape_rounds", default_rounds)) if isinstance(qb_raw, dict) else default_rounds

    min_matches = max(1, min(min_matches, 10))
    min_score   = max(40, min(min_score, 95))
    max_rounds  = max(1, min(max_rounds, len(bundles), 5))

    # ── emphasis_skills ──────────────────────────────────
    skills = raw.get("emphasis_skills")
    clean_skills: List[str] = []
    if isinstance(skills, list):
        for s in skills:
            t = str(s).strip()
            if t and t not in clean_skills:
                clean_skills.append(t)
        clean_skills = clean_skills[:8]

    # ── tone_hints / reasoning ───────────────────────────
    tone = str(raw.get("tone_hints") or "").strip()[:300]
    reasoning = str(raw.get("reasoning") or "").strip()[:600]

    return {
        "keyword_bundles": bundles,
        "quality_bar":     {
            "min_matches":       min_matches,
            "min_score":         min_score,
            "max_scrape_rounds": max_rounds,
        },
        "emphasis_skills": clean_skills,
        "tone_hints":      tone,
        "reasoning":       reasoning,
    }


def _fallback_plan(user_inputs: Dict[str, Any]) -> Dict[str, Any]:
    """Used when the LLM call fails — deterministic, safe default."""
    title = user_inputs["job_title"]
    loc   = user_inputs.get("location", "")
    bundles = [
        {"title": title, "keywords": title, "location": loc,
         "reason": "user-requested primary search"},
    ]
    short = title.split()[0] if title.split() else title
    if short and short.lower() != title.lower():
        bundles.append({
            "title": short, "keywords": short, "location": loc,
            "reason": "broadened single-word title fallback",
        })
    return {
        "keyword_bundles": bundles,
        "quality_bar": {
            "min_matches":       max(1, min(3, int(user_inputs.get("num_jobs", 5)))),
            "min_score":         int(user_inputs.get("match_threshold", 60)),
            "max_scrape_rounds": min(2, len(bundles)),
        },
        "emphasis_skills": [],
        "tone_hints":      "",
        "reasoning":       "LLM planner unavailable — using deterministic fallback plan.",
    }


def build_plan(cv_text: str, user_inputs: Dict[str, Any]) -> Dict[str, Any]:
    """
    Produce a sanitised plan from CV text + user inputs. Never raises:
    falls back to a deterministic plan if the LLM fails.
    """
    cv_preview = (cv_text or "").strip()
    if len(cv_preview) > 3500:
        cv_preview = cv_preview[:3500] + "\n... (truncated)"

    prompt = _PROMPT.format(
        job_title       = user_inputs.get("job_title", ""),
        location        = user_inputs.get("location", ""),
        num_jobs        = user_inputs.get("num_jobs", 5),
        match_threshold = user_inputs.get("match_threshold", 60),
        source          = user_inputs.get("source", "LinkedIn"),
        cv_preview      = cv_preview or "(empty CV)",
    )

    raw = _extract_json(_call_llm(prompt))
    if not raw:
        print("   ⚠️  planner: LLM produced no JSON — using fallback plan.")
        return _fallback_plan(user_inputs)
    plan = _sanitise_plan(raw, user_inputs)

    # Debug log: show planner's decisions.
    print(
        f"   🗺  planner: {len(plan['keyword_bundles'])} bundles, "
        f"min_matches={plan['quality_bar']['min_matches']}, "
        f"min_score={plan['quality_bar']['min_score']}, "
        f"max_rounds={plan['quality_bar']['max_scrape_rounds']}"
    )
    for i, b in enumerate(plan["keyword_bundles"]):
        print(f"        bundle[{i}]: {b['title']!r} @ {b['location']!r}  — {b['reason']}")
    return plan
