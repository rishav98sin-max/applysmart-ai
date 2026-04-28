# agents/cv_diff_tailor.py
#
# Produces a STRUCTURED diff for in-place CV editing (see agents/pdf_editor.py).
# Output contract:
#   {
#     "summary":      "<rewritten 2-4 line summary>",
#     "bullets":      { "<role header>": [ordered_original_indices], ... },
#     "skills_order": ["Skill1", "Skill2", ...]    # optional, [] if N/A
#   }
#
# No prose, no free-form rewriting: every bullet is kept verbatim — the LLM
# only reorders them by original index. This is what makes in-place PDF
# editing safe and layout-preserving.

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from agents.pdf_editor import build_outline

load_dotenv()


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _extract_json(text: str) -> dict:
    """
    Robust JSON extraction. Handles:
      • Plain JSON output
      • Markdown-fenced output (```json ... ``` or just ``` ... ```)
      • JSON with leading/trailing chatter ("Here is the JSON: { ... }")
      • Multi-line JSON (DOTALL)

    Apr 28 follow-up: the previous regex `\\{.*\\}` with greedy match would
    fail silently on markdown-wrapped responses (Gemini commonly wraps JSON
    in ```json fences) AND on responses where the regex grabbed too much
    (first `{` to last `}`) including non-JSON chatter between two JSON
    blocks. This extractor tries strict-parse first (after fence removal),
    then falls back to balanced-brace scanning, and finally the legacy
    regex. On TOTAL failure it logs the first 400 chars of the raw response
    so we can diagnose what the model actually returned instead of treating
    the failure as a silent "empty diff" retry.
    """
    if not text:
        return {}

    # 1) Strip markdown fences (```json ... ``` or ``` ... ```) on a stripped
    #    copy, then try a strict json.loads. This handles ~80% of real cases.
    stripped = text.strip()
    fence_rx = re.compile(
        r"^```(?:json|JSON)?\s*\n?(.*?)\n?\s*```\s*$",
        re.DOTALL,
    )
    m = fence_rx.match(stripped)
    if m:
        stripped = m.group(1).strip()
    try:
        result = json.loads(stripped)
        if isinstance(result, dict):
            return result
    except (json.JSONDecodeError, TypeError):
        pass

    # 2) Balanced-brace scan: find the first '{' and walk forward tracking
    #    nesting depth + string state, stopping at the matching '}'. This
    #    correctly handles nested objects without the greedy-regex pitfall.
    start = stripped.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(stripped)):
            ch = stripped[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = stripped[start:i + 1]
                    try:
                        result = json.loads(candidate)
                        if isinstance(result, dict):
                            return result
                    except json.JSONDecodeError:
                        pass
                    break
        # Move past this '{' and try the next one
        start = stripped.find("{", start + 1)

    # 3) Legacy fallback: the original regex (non-greedy) catches some cases
    #    where the balanced scan above didn't (e.g. malformed strings).
    for match in re.finditer(r"\{.*?\}", text, re.DOTALL):
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            continue

    # 4) Total failure — log a sample so the next debug session has data.
    #    Empty-diff retries previously hid this completely.
    sample = (text[:400] + "...") if len(text) > 400 else text
    print(
        f"   ⚠️  cv_diff_tailor: JSON parse failed on raw response "
        f"({len(text)} chars). First 400: {sample!r}"
    )
    return {}


def _format_outline_for_prompt(outline: Dict[str, Any]) -> str:
    parts: List[str] = []
    cur_summary   = (outline.get("summary") or "").strip()
    cur_word_count = len(cur_summary.split()) if cur_summary else 0
    parts.append(f"CURRENT SUMMARY ({cur_word_count} words):")
    parts.append(cur_summary or "(none)")
    parts.append("")
    parts.append(
        "ROLES (each with 0-indexed bullets — you may REORDER, REWRITE, or DROP "
        "per the RULES below; the index 'i' is how the PDF editor locates "
        "the bullet on the page):"
    )
    for r in outline.get("roles", []):
        parts.append(f'Role "{r["header"]}" (section={r["section"]}):')
        for i, b in enumerate(r["bullets"]):
            # Bullets from build_outline are dicts {"text": str, "length": int};
            # tolerate legacy str entries too.
            btext = b["text"] if isinstance(b, dict) else str(b)
            parts.append(f"  [{i}] {btext}")
        parts.append("")
    skills = outline.get("skills") or []
    if skills:
        parts.append("SKILLS (comma-separated; you may reorder):")
        parts.append(", ".join(skills))
    else:
        parts.append("SKILLS: (categorised layout — do NOT reorder)")
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────
# Prompt template (unchanged)
# ─────────────────────────────────────────────────────────────

_PROMPT_TEMPLATE = """You are a CV editor preparing a tailored application for a SPECIFIC role.

You must output a JSON object (no prose, no markdown) that describes edits.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
YOUR THINKING PROCESS (critical — do this BEFORE writing JSON):

1. READ the job description. Identify 3-5 concrete skills, tools, or
   responsibilities the JD emphasises most.
2. SCAN the candidate's CV. For each role, mentally mark every bullet as
   HIGHLY / PARTIALLY / TANGENTIALLY relevant to those JD emphases.
3. DECIDE your rewrite plan: which bullets will you rewrite, and what
   JD-language angle will each take? You should be planning to rewrite
   ~50-70% of HIGHLY relevant roles' bullets, ~30% of PARTIALLY, and at
   least one from each TANGENTIAL role.
4. DRAFT each rewrite in your head, leading with a JD verb, keeping
   every number/proper-noun from the original intact.
5. ONLY NOW, format your rewrite plan into the JSON schema at the bottom.

If you skip straight to JSON without doing steps 1-4 mentally, you will
produce a diff with 0-2 rewrites and fail the user. The JSON schema is
the LAST thing you think about, not the first.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{safety_preamble}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TARGET ROLE: {job_title}
COMPANY    : {company}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{job_description_block}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

CV CONTENT (structured):
{outline}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

RULES (strict):

1. summary:
   WHAT THIS SECTION IS FOR:
   A recruiter spends ~6 seconds on a CV on the first pass. The summary
   is the user's story — who they are, what they have done, and why they
   fit THIS role. Write it as a crisp 2-4 line human pitch, not as
   marketing copy.

   WHAT YOU MUST DO:
   - Look at the JD. Identify the 2-3 skills or experiences already in
     the candidate's CV (summary, bullets, or skills section) that
     most directly match the JD's top requirements.
   - Foreground those CV items naturally in the rewrite so a recruiter
     scanning for 6 seconds immediately sees why this candidate fits
     THIS role. Example: if the CV lists skills A, B, C, D and the JD
     focuses on A, lead with A. If the next job focuses on D, lead the
     rewrite for that job with D instead.
   - Keep the rest of the candidate's story (employer names, years of
     experience, specialism, degree) intact around those highlights.
   - Never sound robotic. Write like a person describing themselves to
     another person in plain English.

   The CURRENT SUMMARY is shown above with an exact word count. Your rewrite
   MUST be between 95% and 115% of that count — measure as you write. A
   shorter summary leaves an ugly white gap in the PDF because the layout
   rect is sized for the original, and a shortened summary also loses
   impact. NEVER drop below 95% of the original length.

   HARD FABRICATION BAN (applies to the SUMMARY specifically):
   - Reference ONLY facts, skills, tools, frameworks, certifications,
     regulations, domains, and methodologies that are LITERALLY present
     elsewhere in the CV outline shown above.
   - You MUST NOT add JD-only skills to the summary. Example: if the JD
     mentions "US GAAP", "IFRS", "SOX", "Python", etc. but these words do
     NOT appear anywhere in the candidate's CV, you are FORBIDDEN from
     mentioning them — even as "experience in" or "familiar with".
   - If you cannot write a strong summary without the JD's buzzwords, keep
     the ORIGINAL summary verbatim (set summary to the exact original).
   - Re-ordering existing CV skills/phrases is encouraged. Inventing new
     ones is a critical failure.

   Tone: confident, specific, no buzzwords ("dynamic", "passionate",
   "results-driven", etc.).

2. bullets:
   For each role, return a list of bullet objects.

   REWRITING IS THE PRIMARY JOB OF THIS STEP — the user came to this tool
   explicitly asking for a per-JD tailored CV. A CV with 0-2 rewritten bullets
   across all roles is a FAILED tailoring — do not ship it.

   ╔════════════════════════════════════════════════════════════════════╗
   ║ NUMBER & PROPER-NOUN PRESERVATION — STRICTEST RULE IN THIS PROMPT  ║
   ╠════════════════════════════════════════════════════════════════════╣
   ║ Every numeric token (5%, 15%, 600K+, 3+, $2M, 18 months, 40%,      ║
   ║ x2, 150+, 25%, etc.) AND every proper noun (employer name, tech    ║
   ║ stack item, product name, certification) that appears in the       ║
   ║ ORIGINAL bullet MUST appear VERBATIM in your rewrite.              ║
   ║                                                                    ║
   ║ A post-processor scans every rewrite. If even ONE numeric token    ║
   ║ from the original is missing in your rewrite, the entire bullet    ║
   ║ rewrite is REJECTED and the original is shown verbatim — wasting  ║
   ║ your output and producing an un-tailored CV.                       ║
   ║                                                                    ║
   ║ BEFORE you write each rewrite, mentally do this:                   ║
   ║   1. List every number in the original bullet                      ║
   ║   2. List every proper noun (capitalised tech / company / product) ║
   ║   3. Draft the rewrite leading with JD verbs/keywords               ║
   ║   4. CHECK: are ALL items from steps 1-2 present in your draft?    ║
   ║      If no → revise until they all appear, or abandon the rewrite  ║
   ║      and return the original verbatim (text=null).                 ║
   ║                                                                    ║
   ║ EXAMPLES OF REWRITES THE GUARD WILL REJECT:                        ║
   ║                                                                    ║
   ║ Original:  "Drove 5% user growth and 15% retention improvement     ║
   ║            through data-driven recommendations"                     ║
   ║ BAD:      "Drove user growth and retention improvement through     ║
   ║            data-driven product recommendations"                     ║
   ║            ↑ MISSING: 5%, 15%  →  REJECTED                         ║
   ║                                                                    ║
   ║ Original:  "Authored artifacts for a 600K+ user platform,          ║
   ║            reducing system latency by 30%"                          ║
   ║ BAD:      "Authored artifacts for a large user platform,           ║
   ║            applying AI product fluency"                             ║
   ║            ↑ MISSING: 600K+, 30%  →  REJECTED                      ║
   ║                                                                    ║
   ║ Original:  "Led 3+ concurrent product initiatives managing         ║
   ║            stakeholder alignment"                                   ║
   ║ BAD:      "Led concurrent product initiatives, managing            ║
   ║            stakeholder alignment across teams"                      ║
   ║            ↑ MISSING: 3+  →  REJECTED                              ║
   ║                                                                    ║
   ║ EXAMPLES OF REWRITES THE GUARD WILL ACCEPT:                        ║
   ║                                                                    ║
   ║ Original:  "Drove 5% user growth and 15% retention improvement     ║
   ║            through data-driven recommendations"                     ║
   ║ GOOD:     "Owned roadmap that delivered 5% user growth and 15%     ║
   ║            retention lift via data-driven recommendations"          ║
   ║            ↑ 5% and 15% both verbatim → ACCEPTED                   ║
   ║                                                                    ║
   ║ Original:  "Authored artifacts for a 600K+ user platform,          ║
   ║            reducing system latency by 30%"                          ║
   ║ GOOD:     "Shipped product artifacts for the 600K+ user platform,  ║
   ║            cutting system latency by 30% via prioritised PRDs"     ║
   ║            ↑ 600K+ and 30% both verbatim → ACCEPTED                ║
   ╚════════════════════════════════════════════════════════════════════╝

   Mandatory rewrite targets (per role):
     • HIGHLY RELEVANT role (job title, domain, or core tech match): rewrite
       **at least 50% of bullets, ideally 60-70%**. Lead every rewrite with
       JD keywords and verbs.
     • PARTIALLY RELEVANT role (adjacent domain, transferable skills):
       rewrite **at least 30% of bullets** — focus on the bullets whose
       underlying skill matches the JD.
     • TANGENTIAL role (different domain/industry, but same seniority or
       soft skills): rewrite **at least 1 bullet** to reframe the
       transferable skill using JD language.

   You may REORDER freely — place the most job-relevant bullets first.
   You may REWRITE more than the minimums above; more is almost always
   better, provided the MUST NOT rules below are respected.

   You MUST NOT:
   - DROP / OMIT any bullet. Every original bullet MUST appear in your output exactly once.
     If a bullet is not JD-relevant, keep it verbatim (text=null) rather than removing it.
   - Remove or omit ANY achievement, award, recognition, certification, promotion, or
     measurable outcome bullet (e.g. "Awarded ...", "Recognised as ...", "Top performer",
     "Employee of the Month"). These are evidence and belong in every tailored CV.
   - Invent new claims, numbers, platforms, tools, or outcomes not in the original bullet.
   - Change numeric outcomes: "25% user growth" stays "25% user growth".
   - Change concrete nouns: platforms, company names, acronyms, product names.
   - Add certifications, degrees, or years of experience that aren't already there.
   - Change role headers or dates.

   Each bullet object has:
     "i":    required int — the ORIGINAL bullet index (0..N-1). This is how
             the PDF editor locates the bullet's position on the page.
     "text": optional string — the NEW wording. Omit or set to null to keep
             the original bullet text verbatim.

   Rewriting guidance:
   - Start with a strong verb that matches the JD's language (e.g. if JD says
     "shipped", "delivered", "owned", mirror that verb).
   - Move JD-relevant keywords to the front of the bullet.
   - Keep the factual core (numbers, tech, outcomes) identical.
   - Prefer concrete over abstract: "authored 12 PRDs" not "produced many documents".
   - Length: STRICT 90-120% of the original bullet's character length.
     Count characters before submitting. The post-processor REJECTS any
     rewrite outside 50%-200% of the original — but the visible-quality
     band is much tighter at 90-120%. Going above 130% means your rewrite
     either added filler words or invented a new fact (both are failures).
     EXAMPLE: original is 180 chars → your rewrite must be 162-216 chars.
     EXAMPLE: original is 95 chars → your rewrite must be 85-114 chars.
     If you cannot land within 120% while keeping every number and proper
     noun verbatim, return text=null (revert) instead of submitting an
     overlong rewrite that will be rejected anyway.

3. skills_order:
   Do NOT reorder, add to, or reword the skills list. Always return an
   empty list []. The candidate's skills section must stay exactly as
   in the original CV — highlighting of relevant skills is done inside
   the summary and bullets, not by reshuffling the skills block.

4. Output ONLY a JSON object with keys: summary, bullets, skills_order.

EXAMPLE OUTPUT FORMAT:
{{
  "summary": "…",
  "bullets": {{
    "IBM India Pvt. Ltd., Technical Product Specialist": [
      {{"i": 2, "text": "Led 3+ enterprise-scale product initiatives managing stakeholder alignment across engineering, design, and business teams to improve delivery efficiency by 20%"}},
      {{"i": 0, "text": "Drove 25% user growth and 15% retention improvement through data-driven product recommendations for a 600K+ user platform"}},
      {{"i": 1}}
    ],
    "Accenture Solutions Pvt. Ltd., Performance Engineering Analyst": [
      {{"i": 0}},
      {{"i": 3, "text": "Owned end-to-end delivery of 3+ applications from ideation through launch, resolving 150+ user-impacting issues within SLA"}},
      {{"i": 1}}
    ]
  }},
  "skills_order": []
}}

Return the JSON now:"""


# ─────────────────────────────────────────────────────────────
# LLM call — Gemini-primary, Groq-fallback.
# Aggressive bullet rewriting + long structured-JSON output benefit from
# Gemini 2.5 Flash's larger context and better instruction adherence on
# complex outlines (observed 0-rewrite failures on 9-role/28-bullet CVs
# when using Groq only). chat_gemini() auto-falls-back to Groq internally
# on any error or missing key, so this is strictly an upgrade.
# ─────────────────────────────────────────────────────────────

def _call_llm(prompt: str, max_tokens: int = 2000) -> str:
    """
    Strategy B (Apr 28 follow-up): single Gemini attempt with INSTANT Groq
    fallback on parse-failure / empty / unactionable output.

    Why no Gemini retries:
      Gemini Flash on free tier truncates JSON output mid-string ~80% of
      the time. The truncated text passes the "non-empty" check but fails
      json.loads. Retrying Gemini doesn't help — the truncation is a
      server-side behaviour we can't prompt-engineer around. Each retry
      just burns 5-12s of cooldown quota with no improvement.

      The durable pattern: give Gemini ONE shot. If the response is
      parseable AND contains actionable keys (summary/bullets/
      skills_order), keep it. Otherwise fall through to Groq immediately.

    Result: Gemini still gets to produce the diff when it works
    (~20-30% of attempts) — your stated preference of "Gemini for the
    first try" is honoured. When it fails, Groq picks up with a clean
    JSON output that drives the REPLICA path (preserving the user's
    original CV layout), instead of triggering rebuild.
    """
    from agents.runtime    import track_llm_call
    from agents.llm_client import chat_gemini, chat_quality

    track_llm_call(agent="cv_diff_tailor")

    # Try Gemini first.
    gemini_result = chat_gemini(prompt, max_tokens=max_tokens, temperature=0.2)

    if gemini_result:
        # Validate: must parse AND contain at least one actionable key.
        # An empty {"summary": "", "bullets": {}, "skills_order": []} is
        # equivalent to "no edits" and should trigger Groq fallback —
        # otherwise downstream logic ships an effective no-op as a tailor.
        parsed = _extract_json(gemini_result)
        is_actionable = bool(parsed) and bool(
            parsed.get("summary")
            or parsed.get("bullets")
            or parsed.get("skills_order")
        )
        if is_actionable:
            return gemini_result
        print(
            f"   ↪️  cv_diff_tailor: Gemini output unusable "
            f"(len={len(gemini_result)}, parsed={bool(parsed)}, "
            f"actionable={is_actionable}) — instant Groq fallback"
        )
    else:
        print("   ↪️  cv_diff_tailor: Gemini returned empty — instant Groq fallback")

    return chat_quality(prompt, max_tokens=max_tokens, temperature=0.2)


# ─────────────────────────────────────────────────────────────
# Sanitise diff (unchanged)
# ─────────────────────────────────────────────────────────────

_NUMBER_RX = re.compile(r"\d[\d.,]*\s*%?|\d+K\+?|\d+M\+?|\d+\+", re.I)

# Capitalized / acronym term pattern used by the summary-fabrication guard.
# Matches multi-word proper nouns ("US GAAP", "International Financial"),
# standalone acronyms (IFRS, SOX, GAAP), and CamelCase tokens (LangGraph).
_CAPTERM_RX = re.compile(
    r"\b(?:[A-Z]{2,}(?:[\/\-&][A-Z0-9]+)*|"                   # ACRONYM, SOX, AI/ML
    r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3}|"                      # Title Case 1-4 words
    r"[A-Z][a-z]+[A-Z][A-Za-z]+)\b"                            # CamelCase
)

# Ignore very common Title-Case words that appear in many CVs so they don't
# falsely trigger the fabrication guard (months, generic terms, etc.).
_CAPTERM_STOPWORDS = {
    "The", "A", "An", "And", "Or", "But", "Of", "In", "On", "At", "To", "For", "With",
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December",
    "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun",
    "I", "My", "Me", "We", "Our", "Us", "They", "You",
    "Responsible", "Experienced", "Skilled", "Proven", "Strong",
    "Professional", "Summary", "Experience", "Education", "Skills",
}


def _cv_vocabulary(outline: Dict[str, Any]) -> set:
    """
    Build a lowercased token-and-bigram vocabulary from everything in the
    outline (summary, bullets, skills, role headers). Used to decide whether
    a new summary introduced terms not present in the CV.
    """
    parts: List[str] = []
    parts.append(outline.get("summary") or "")
    for r in outline.get("roles", []) or []:
        parts.append(r.get("header") or "")
        for b in r.get("bullets") or []:
            # build_outline emits {"text": str, "length": int}; legacy str also tolerated.
            if isinstance(b, dict):
                parts.append(b.get("text") or "")
            elif isinstance(b, str):
                parts.append(b)
    skills = outline.get("skills")
    if isinstance(skills, list):
        parts.extend(s for s in skills if isinstance(s, str))
    elif isinstance(skills, str):
        parts.append(skills)
    text = " ".join(parts).lower()
    # Also strip common separators for robust membership checks.
    return {text}  # return as single-element set; callers use 'in' on the text


def _check_professional_identity_fabrication(orig_summary: str, new_summary: str, outline: Dict[str, Any]) -> Optional[str]:
    """
    Check if the summary introduces a professional identity not supported by the CV.
    
    Allows: "passionate about transitioning to X", "interested in exploring X"
    Allows: Highlighting experience that exists in bullets (even if framed differently)
    Disallows: Adding completely NEW skills that don't exist at all in the CV
    
    Returns error message if fabrication detected, None otherwise.
    """
    if not orig_summary or not new_summary:
        return None
    
    # Extract bullet text from outline to check for related experience
    bullet_texts = []
    for role in outline.get("roles", []):
        for bullet in role.get("bullets", []):
            if isinstance(bullet, dict):
                bullet_texts.append(bullet.get("text", "").lower())
            elif isinstance(bullet, str):
                bullet_texts.append(bullet.lower())
    
    all_bullets_text = " ".join(bullet_texts)
    
    # Extract professional identity phrases from both summaries
    orig_lower = orig_summary.lower()
    new_lower = new_summary.lower()
    
    # Allow transition language
    transition_phrases = [
        "passionate about", "interested in", "eager to", "looking to",
        "transitioning to", "pivot into", "exploring", "aspiring to"
    ]
    if any(phrase in new_lower for phrase in transition_phrases):
        return None  # Allow transition language
    
    # Check if new summary adds a domain not in original CV
    # Common professional domains to check
    domains = [
        "sales", "marketing", "engineering", "product", "design",
        "operations", "finance", "hr", "consulting", "data"
    ]
    
    for domain in domains:
        # Check if domain appears in new summary but not in original
        if domain in new_lower and domain not in orig_lower:
            # Check if related experience exists in bullets
            # If bullets have relevant experience, allow the summary to frame it
            # Example: bullets have "client relationships", "revenue" → allow "sales" in summary
            related_keywords = {
                "sales": ["client", "revenue", "quota", "account", "deal", "customer", "target", "growth"],
                "marketing": ["campaign", "brand", "content", "social", "engagement", "reach", "promotion"],
                "engineering": ["code", "develop", "build", "software", "technical", "system", "architecture"],
                "product": ["feature", "roadmap", "user", "launch", "iteration", "strategy"],
                "design": ["ui", "ux", "visual", "creative", "interface", "user experience"],
                "operations": ["process", "workflow", "efficiency", "optimize", "scale", "logistics"],
                "finance": ["budget", "financial", "reporting", "analysis", "forecast", "investment"],
                "hr": ["recruitment", "hiring", "talent", "people", "culture", "onboarding"],
                "consulting": ["advisory", "client", "strategy", "recommendation", "solution"],
                "data": ["analytics", "analysis", "insight", "metrics", "report", "database"]
            }
            
            if domain in related_keywords:
                keywords = related_keywords[domain]
                # Check if any related keywords exist in bullets
                has_related_exp = any(keyword in all_bullets_text for keyword in keywords)
                if has_related_exp:
                    # Allow the domain in summary since related experience exists in bullets
                    continue
                else:
                    # Block if no related experience exists in CV at all
                    return f"summary adds '{domain}' professional identity with no supporting experience in CV bullets"
            else:
                # Domain not in our keyword list, be conservative and block it
                return f"summary adds '{domain}' professional identity not present in original CV"
    
    return None


def _foreign_capitalized_terms(summary: str, cv_text_set: set) -> List[str]:
    """
    Return a list of capitalized/acronym phrases that appear in `summary`
    but whose component words do NOT appear in any CV text. Stopwords are
    ignored.

    Rationale: the previous implementation flagged a whole phrase as foreign
    if its verbatim lowercased form was absent from the CV. That over-fired
    on legitimate re-phrasings like "Integrated Marketing Communications"
    (when the CV has "integrated marketing") or "B2B Campaigns" (when the
    CV has "B2B" + "campaigns" separately). We now inspect each content
    word of the phrase: a phrase is only "foreign" if MORE THAN HALF of
    its content words are missing from the CV vocabulary AND it contains
    at least one acronym-ish token (≥2 uppercase letters). This still
    catches real fabrications ("US GAAP", "IFRS", "SOX") when the CV
    never mentions them, but lets through prose rewrites that re-compose
    CV facts.
    """
    if not summary:
        return []
    cv_text = next(iter(cv_text_set), "") if cv_text_set else ""
    if not cv_text:
        return []
    foreign: List[str] = []
    seen: set = set()
    # Split CV text into a token set for cheap word-level membership.
    cv_tokens = {w for w in re.split(r"\W+", cv_text) if w}
    word_rx = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-/&]*")
    for m in _CAPTERM_RX.finditer(summary):
        term = m.group(0).strip()
        if term in _CAPTERM_STOPWORDS:
            continue
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        # Short-circuit: whole phrase already in CV text → definitely safe.
        if key in cv_text:
            continue
        # Decompose into content words and check each against the CV token
        # set. Single-word matches beat the substring check.
        words = [w.lower() for w in word_rx.findall(term)]
        if not words:
            continue
        missing = [w for w in words if w not in cv_tokens and w not in cv_text]
        # A phrase is foreign only if the majority of its words are missing
        # AND it contains an all-caps acronym-like token. This keeps us from
        # flagging "Integrated Marketing Communications" while still
        # catching "US GAAP" / "IFRS" / "Agile Scrum" on a non-agile CV.
        has_acronym_like = any(
            len(w) >= 2 and w.isupper() and w.isalpha()
            for w in word_rx.findall(term)
        )
        if len(missing) > len(words) / 2 and has_acronym_like:
            foreign.append(term)
    return foreign

_MIN_BULLETS_PER_ROLE = 2
_REWRITE_LEN_MIN_RATIO = 0.5   # rewrite must be at least 50% of original length
_REWRITE_LEN_MAX_RATIO = 2.0   # and at most 200% — longer usually means fabrication


# H4: thread-local revert tracker. Reset at the start of every
# `tailor_cv_diff` call, populated by `_normalise_role_bullets` whenever
# `_rewrite_is_safe` rejects an LLM rewrite. Surfaced in the diff's
# `_debug` block so the UI and Mixpanel can count silent reverts.
#
# Thread-local because `TAILOR_JOB_CONCURRENCY=2` runs two tailor calls
# concurrently and a module-level list would cross-contaminate the
# observability data (thread A's reverts attributed to thread B's diff).
_THREAD_LOCAL = threading.local()


def _get_bullet_reverts() -> List[Dict[str, Any]]:
    """Lazily-initialised per-thread bullet-revert list."""
    if not hasattr(_THREAD_LOCAL, "bullet_reverts"):
        _THREAD_LOCAL.bullet_reverts = []
    return _THREAD_LOCAL.bullet_reverts


# Backwards-compat shim: keep `_LAST_BULLET_REVERTS` as a property-like
# accessor for any existing reads. Writes go through _get_bullet_reverts().
class _BulletRevertsProxy:
    """Backwards-compat shim — delegates list ops to thread-local storage."""
    def __getattr__(self, name):
        return getattr(_get_bullet_reverts(), name)
    def __iter__(self):
        return iter(_get_bullet_reverts())
    def __len__(self):
        return len(_get_bullet_reverts())
    def __getitem__(self, idx):
        return _get_bullet_reverts()[idx]

_LAST_BULLET_REVERTS = _BulletRevertsProxy()


def _rewrite_is_safe(original: str, rewrite: str, original_length: Optional[int] = None) -> tuple:
    """
    Guardrail: reject rewrites that look like fabrications or truncations.
    - Length must be 50%-200% of the original.
    - Every number-like token in the ORIGINAL must appear verbatim in the
      rewrite. '25%' must stay '25%' (not '25 percent'), '600K' stays '600K'
      (not '60K'). This prevents both fabrication and semantic drift.

    Args:
        original: Original bullet text (for number token extraction)
        rewrite: Proposed rewrite text
        original_length: Complete logical bullet length (if provided, used instead of len(original))

    Returns (ok, reason). reason is "" when ok.
    """
    orig = (original or "").strip()
    new  = (rewrite or "").strip()
    if not new:
        return False, "empty rewrite"
    # Use the provided original_length if available, otherwise fall back to len(orig)
    orig_len = original_length if original_length is not None else len(orig)
    lo, hi = orig_len * _REWRITE_LEN_MIN_RATIO, orig_len * _REWRITE_LEN_MAX_RATIO
    if not (lo <= len(new) <= hi):
        return False, f"length {len(new)} outside {int(lo)}-{int(hi)}"
    orig_nums = {m.group(0).strip().lower() for m in _NUMBER_RX.finditer(orig)}
    new_text_l = new.lower()
    for tok in orig_nums:
        if tok and tok not in new_text_l:
            return False, f"number token {tok!r} missing"
    return True, ""


def _normalise_bullet_list(
    order_raw:   Any,
    n_bullets:   int,
    orig_texts:  List[Any],
) -> List[Dict[str, Any]]:
    """
    Accept either:
      - Old format: [2, 0, 1]                     (reorder only)
      - New format: [{"i": 2, "text": "..."},
                     {"i": 0, "text": null},
                     {"i": 1}]                    (reorder + rewrite + drop)

    Normalise to a single internal form:
      [{"i": <int>, "text": <str|None>}, ...]
    where "text" is the new wording or None (meaning keep original).

    Guarantees:
      - Indices are unique and in range.
      - Rewrites that fail _rewrite_is_safe() are reverted to None.
      - At least _MIN_BULLETS_PER_ROLE entries are returned (missing
        originals are re-appended in original order if the LLM dropped
        too many).
      - If the final list equals the trivial order with no rewrites,
        caller should treat it as "no change".
    """
    if not isinstance(order_raw, list):
        return []

    seen: set = set()
    normalised: List[Dict[str, Any]] = []
    for item in order_raw:
        idx: Optional[int] = None
        text: Optional[str] = None
        if isinstance(item, dict):
            try:
                idx = int(item.get("i"))
            except (TypeError, ValueError):
                continue
            t = item.get("text")
            if isinstance(t, str):
                t_clean = t.strip()
                if t_clean:
                    text = t_clean
        else:
            try:
                idx = int(item)
            except (TypeError, ValueError):
                continue
        if idx is None or not (0 <= idx < n_bullets) or idx in seen:
            continue
        # Guardrail rewrites
        if text is not None:
            orig_bullet = orig_texts[idx] if idx < len(orig_texts) else ""
            if isinstance(orig_bullet, dict):
                orig_text = orig_bullet.get("text", "")
                orig_len = orig_bullet.get("length", len(orig_text))
            else:
                orig_text = orig_bullet
                orig_len = len(orig_bullet) if orig_bullet else 0
            ok, reason = _rewrite_is_safe(orig_text, text, original_length=orig_len)
            if not ok:
                print(
                    f"   ⚠️  rewrite rejected (bullet {idx}, {reason}): "
                    f"{text[:80]!r} — reverting to original"
                )
                _LAST_BULLET_REVERTS.append({
                    "bullet_index": idx,
                    "reason": reason,
                    "rewrite_preview": text[:120],
                })
                text = None   # fall back to original wording
        normalised.append({"i": idx, "text": text})
        seen.add(idx)

    # No-drop policy: every original bullet must appear in the output.
    # Append any missing indices verbatim (text=None) in original order.
    for i in range(n_bullets):
        if i not in seen:
            normalised.append({"i": i, "text": None})
            seen.add(i)

    return normalised


def _sanitise_diff(raw: Dict[str, Any], outline: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate and repair the LLM output against the real outline.
    - bullets dict keys matched tolerantly against real role headers.
    - Bullets accept old [2,0,1] or new [{"i":2,"text":"..."}] format;
      both normalise to [{"i": int, "text": str|None}, ...].
    - Unsafe rewrites (fabrication/truncation) fall back to original wording.
    - skills_order filtered to items present in outline['skills'].
    """
    out: Dict[str, Any] = {"summary": "", "bullets": {}, "skills_order": []}

    # Summary
    s = raw.get("summary")
    if isinstance(s, str):
        out["summary"] = s.strip()

    # Bullets
    real_roles = {r["header"].strip().lower(): r for r in outline.get("roles", [])}
    bullets_raw = raw.get("bullets") or {}
    if isinstance(bullets_raw, dict):
        for rk, order in bullets_raw.items():
            if not isinstance(order, list):
                continue
            rk_l = str(rk).strip().lower()
            match_key = None
            if rk_l in real_roles:
                match_key = real_roles[rk_l]["header"]
            else:
                for h_l, r in real_roles.items():
                    if h_l.startswith(rk_l) or rk_l.startswith(h_l) or rk_l in h_l:
                        match_key = r["header"]
                        break
            if not match_key:
                continue
            role = next(r for r in outline["roles"] if r["header"] == match_key)
            orig_texts = role.get("bullets") or []
            n = len(orig_texts)
            normalised = _normalise_bullet_list(order, n, orig_texts)
            if not normalised:
                continue
            # Suppress trivial "same order, no rewrites" outputs.
            is_noop = (
                len(normalised) == n
                and all(
                    nb["i"] == i and nb["text"] is None
                    for i, nb in enumerate(normalised)
                )
            )
            if not is_noop:
                out["bullets"][match_key] = normalised

    # Skills — policy: DO NOT reorder skills. The candidate's skills block
    # stays byte-identical to the original CV. Drop any LLM-returned order.
    out["skills_order"] = []

    return out


# ─────────────────────────────────────────────────────────────
# Feedback addendum (unchanged)
# ─────────────────────────────────────────────────────────────

def _build_feedback_addendum(
    feedback:      str,
    previous_diff: Optional[Dict[str, Any]],
) -> str:
    if not feedback and not previous_diff:
        return ""
    parts: List[str] = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "REVIEWER FEEDBACK ON YOUR PREVIOUS ATTEMPT (incorporate this):",
    ]
    if previous_diff:
        parts.append("Your previous diff was:")
        try:
            parts.append(json.dumps(previous_diff, indent=2)[:1400])
        except Exception:
            pass
    if feedback:
        parts.append("")
        parts.append("Reviewer said:")
        parts.append(feedback.strip())
    parts.append("")
    # Force actual behaviour change on retry — the previous attempt was
    # rejected by the reviewer, so returning the same (or another
    # bullets-unchanged) diff is a non-fix.
    parts.append(
        "Produce a NEW diff that directly addresses the feedback. On this "
        "retry you MUST rewrite additional bullets — the reviewer's "
        "critique cannot be satisfied by keeping bullets verbatim. If the "
        "feedback mentions 'JD verbs', 'keywords', or 'rewrite bullets', "
        "rewrite at least 50% of the bullets in each JD-relevant role, "
        "leading with JD language. The only hard constraints are (a) no "
        "fabrication — every fact must trace to the original CV — and "
        "(b) same indices space (use the 'i' field to reference original "
        "bullets). Returning the same bullets-untouched diff as last time "
        "will be rejected again."
    )
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────

def tailor_cv_diff(
    cv_pdf_path:     str,
    job_description: str,
    job_title:       str = "",
    company:         str = "",
    feedback:        str = "",
    previous_diff:   Optional[Dict[str, Any]] = None,
    outline:         Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Produce a validated structured diff for the CV at `cv_pdf_path` targeting
    the given job. Safe to pass straight to agents.pdf_editor.apply_edits().

    Optional parameters used by the reviewer-driven retry loop:
      feedback      : reviewer's short actionable feedback string
      previous_diff : the diff the tailor produced on the previous attempt
      outline       : precomputed outline (to avoid re-parsing the PDF on retry)
    """
    if outline is None:
        outline = build_outline(cv_pdf_path)

    # Reset the per-call bullet-revert tracker so counts reflect THIS job.
    _LAST_BULLET_REVERTS.clear()

    orig_summary = (outline.get("summary") or "").strip()
    orig_words   = len(orig_summary.split()) if orig_summary else 0

    from agents.prompt_safety import wrap_untrusted_block, untrusted_block_preamble

    def _render_prompt(extra: str = "") -> str:
        jd_block = wrap_untrusted_block(
            (job_description or "").strip() or "(no description provided)",
            label="JOB_DESCRIPTION",
        )
        p = _PROMPT_TEMPLATE.format(
            safety_preamble       = untrusted_block_preamble(["JOB_DESCRIPTION"]),
            job_title             = job_title or "(unspecified)",
            company               = company   or "(unspecified)",
            job_description_block = jd_block,
            outline               = _format_outline_for_prompt(outline),
        )
        p += "\n\n" + _build_feedback_addendum(feedback, previous_diff)
        if extra:
            p += "\n\n" + extra
        return p

    raw_text = _call_llm(_render_prompt())
    raw_json = _extract_json(raw_text)
    diff     = _sanitise_diff(raw_json, outline)

    # ── Summary fabrication guard ────────────────────────────────────
    # Reject summaries that introduce proper nouns / skill terms absent
    # from the original CV text. Common failure: LLM adds JD-only skills
    # like "US GAAP", "IFRS", "SOX" etc. to the summary.
    #
    # Observability: record the reason (and foreign terms) on the diff
    # itself under "_debug" so the pipeline can surface it in the UI /
    # Mixpanel instead of dying silently in stdout.
    diff.setdefault("_debug", {}).setdefault("summary_reverts", [])
    new_sum = (diff.get("summary") or "").strip()
    if new_sum and orig_summary:
        
        # Check for professional identity fabrication
        identity_error = _check_professional_identity_fabrication(orig_summary, new_sum, outline)
        if identity_error:
            print(
                f"   ⚠️  {identity_error} — reverting to original summary to avoid fabrication."
            )
            diff["_debug"]["summary_reverts"].append({
                "reason": "professional_identity_fabrication",
                "detail": identity_error,
            })
            diff["summary"] = orig_summary
        else:
            # Check for foreign capitalized terms
            cv_vocab = _cv_vocabulary(outline)
            foreign = _foreign_capitalized_terms(new_sum, cv_vocab)
            if foreign:
                print(
                    f"   ⚠️  summary introduced CV-foreign terms {foreign!r} — "
                    f"reverting to original summary to avoid fabrication."
                )
                diff["_debug"]["summary_reverts"].append({
                    "reason": "cv_foreign_terms",
                    "terms":  foreign[:8],
                })
                diff["summary"] = orig_summary

    # ── Length-enforcement retry ─────────────────────────────────────
    # Triggers when the new summary is below 85% of the original word count.
    # The 85% floor catches real truncations (90→60 words) but allows modest
    # compression (90→80) when the LLM tightens prose. Previously set to
    # 95%, which reverted too many legitimate rewrites. If the retry is
    # still short, fall back to the ORIGINAL summary (no shortening).
    new_words = len((diff.get("summary") or "").split())
    _SUMMARY_MIN_RATIO = 0.85
    _SUMMARY_MAX_RATIO = 1.15
    # H3: suppress the length-retry on reviewer-driven retries. The reviewer
    # already triggered a re-tailor; cascading another inner retry on top
    # multiplies token use without adding signal (the LLM saw the directive
    # in the reviewer feedback already). Only run length-retry on first pass.
    on_retry_pass = bool(feedback or previous_diff)
    if (not on_retry_pass) and orig_words >= 20 and new_words and new_words < int(orig_words * _SUMMARY_MIN_RATIO):
        low  = int(orig_words * _SUMMARY_MIN_RATIO)
        high = int(orig_words * _SUMMARY_MAX_RATIO)
        print(
            f"   ↻  summary too short ({new_words}/{orig_words} words, "
            f"need ≥{low}) — retrying with hard target {low}-{high}."
        )
        enforce = (
            f"YOUR PREVIOUS SUMMARY WAS TOO SHORT ({new_words} words; "
            f"original was {orig_words}). The target is {low}-{high} words "
            f"(95%-115% of original). Rewrite the summary to fall strictly "
            f"within that range. You MAY add more CV-grounded detail "
            f"(specific outcomes, years, platforms, methodologies) but you "
            f"MUST NOT invent anything that is not in the CV."
        )
        raw_text2 = _call_llm(_render_prompt(extra=enforce))
        raw_json2 = _extract_json(raw_text2)
        diff2     = _sanitise_diff(raw_json2, outline)
        new_sum2  = (diff2.get("summary") or "").strip()
        if new_sum2 and len(new_sum2.split()) >= int(orig_words * _SUMMARY_MIN_RATIO):
            diff["summary"] = new_sum2
            new_words = len(new_sum2.split())
        elif orig_summary:
            # Retry still short — revert to original rather than ship a
            # noticeably shortened summary.
            short_words = len(new_sum2.split()) if new_sum2 else 0
            print(
                f"   ↺  retry still short ({short_words} words) "
                f"— reverting to original summary verbatim."
            )
            diff["_debug"]["summary_reverts"].append({
                "reason": "length_floor",
                "new_words": short_words,
                "original_words": orig_words,
                "floor": int(orig_words * _SUMMARY_MIN_RATIO),
            })
            diff["summary"] = orig_summary
            new_words = orig_words

    # Count rewrites across all roles (bullets with non-null "text" field).
    def _count_diff_edits(d: Dict[str, Any]) -> tuple:
        nr = 0
        nd = 0
        for role_key, entries in d.get("bullets", {}).items():
            if not isinstance(entries, list):
                continue
            role_obj = next(
                (r for r in outline["roles"] if r["header"] == role_key),
                None,
            )
            orig_n = len(role_obj["bullets"]) if role_obj else 0
            nr += sum(1 for e in entries if isinstance(e, dict) and e.get("text"))
            nd += max(0, orig_n - len(entries))
        return nr, nd

    n_rewrites, n_dropped = _count_diff_edits(diff)

    # Zero-rewrite escape hatch — the LLM was instructed to rewrite at least
    # 30-50% of bullets per relevant role. If it returned 0 rewrites across
    # the whole CV, it defaulted to "safest path" and the tailoring is
    # shallow. Force exactly one retry with a loud directive. Suppressed
    # when we're already in a reviewer-driven retry (handled upstream).
    # B3: skip escape-hatch when the FIRST call returned a totally empty
    # response (no summary, no bullets, no skills_order). That's a sign the
    # LLM SAFETY-blocked, returned malformed JSON, or quota-exhausted
    # mid-call — a second call is unlikely to help and just burns a slot
    # under the 5 RPM Gemini ceiling. The supervisor-level B1 retry will
    # handle it with a stricter prompt instead.
    raw_was_empty = not (
        diff.get("summary") or diff.get("bullets") or diff.get("skills_order")
    )
    if n_rewrites == 0 and not (feedback or previous_diff) and not raw_was_empty:
        total_bullets = sum(len(r.get("bullets") or []) for r in outline.get("roles", []))
        if total_bullets >= 3:
            print(
                f"   ↻  cv_diff_tailor: 0 rewrites on first pass — "
                f"forcing bullet-rewrite retry (roles={len(outline.get('roles', []))}, "
                f"bullets={total_bullets})."
            )
            enforce_rewrites = (
                "YOUR PREVIOUS RESPONSE REWROTE 0 BULLETS. This violates the "
                "RULES — a CV with 0 bullet rewrites is a failed tailoring. "
                "Produce a new diff that rewrites AT LEAST 3 bullets across "
                "the most JD-relevant role(s), leading with JD verbs and "
                "keywords. Every fact must still trace to the original CV."
            )
            raw_text_rr = _call_llm(_render_prompt(extra=enforce_rewrites))
            raw_json_rr = _extract_json(raw_text_rr)
            diff_rr     = _sanitise_diff(raw_json_rr, outline)
            n_rewrites_rr, n_dropped_rr = _count_diff_edits(diff_rr)
            if n_rewrites_rr > 0:
                # Preserve the BETTER summary — we forced this retry to fix
                # bullets, not summary. If the retry produced a shorter or
                # empty summary, keep the original. We compare by word count
                # against the target band (90-115% of original). The closer
                # to the band, the better.
                prev_sum_words = new_words
                rr_sum         = (diff_rr.get("summary") or "").strip()
                rr_sum_words   = len(rr_sum.split()) if rr_sum else 0
                keep_prev_summary = (
                    rr_sum_words == 0
                    or (prev_sum_words > 0 and rr_sum_words < prev_sum_words)
                )
                if keep_prev_summary and diff.get("summary"):
                    diff_rr["summary"] = diff["summary"]
                    rr_sum_words = prev_sum_words

                diff        = diff_rr
                n_rewrites  = n_rewrites_rr
                n_dropped   = n_dropped_rr
                new_words   = rr_sum_words or new_words

    tag = " (retry)" if feedback or previous_diff else ""
    # Apr 28 follow-up: include LLM source so we can see at a glance whether
    # this kept diff came from Gemini or Groq fallback. Late import to avoid
    # an unconditional dependency on llm_client at module load time.
    try:
        from agents.llm_client import last_llm_source as _lls
        src = _lls()
    except Exception:
        src = "unknown"
    print(
        f"   ✂️  cv_diff_tailor{tag} [via {src}]: "
        f"summary={new_words}/{orig_words}w | "
        f"roles_edited={len(diff['bullets'])} | "
        f"bullets_rewritten={n_rewrites} | "
        f"bullets_dropped={n_dropped} | "
        f"skills_reordered={'yes' if diff['skills_order'] else 'no'}"
    )

    # Attach bullet-revert observability for the UI / Mixpanel. Keeps the
    # diff shape stable for pdf_editor (it ignores _debug) while giving
    # downstream reporting a count of bullets that were silently reverted
    # by _rewrite_is_safe.
    diff.setdefault("_debug", {})
    diff["_debug"]["bullet_reverts"] = list(_LAST_BULLET_REVERTS)
    diff["_debug"]["bullet_reverts_count"] = len(_LAST_BULLET_REVERTS)

    # C3: total-revert detection. When fabrication guards reverted EVERY
    # bullet rewrite AND the summary, the resulting "tailored" diff is
    # functionally identical to the original CV. Flag this so the supervisor
    # in job_agent._do_cv_tailor can force a retry with stricter prompting
    # rather than shipping an unchanged CV with a deceptive high reviewer
    # score (the reviewer would naturally accept the original CV).
    summary_reverted = bool(diff["_debug"].get("summary_reverts"))
    bullets_all_reverted = (
        n_rewrites == 0
        and len(_LAST_BULLET_REVERTS) > 0
    )
    has_summary_change = bool((diff.get("summary") or "").strip()) and not summary_reverted
    has_bullet_change  = n_rewrites > 0
    has_skills_change  = bool(diff.get("skills_order"))
    diff["_debug"]["all_reverted"] = (
        not has_summary_change
        and not has_bullet_change
        and not has_skills_change
        and (summary_reverted or bullets_all_reverted)
    )
    if diff["_debug"]["all_reverted"]:
        print(
            "   🛡️  cv_diff_tailor: ALL changes reverted by fabrication "
            "guards — diff is effectively a no-op. Caller should retry with "
            "stricter prompting or skip the replica path."
        )
    return diff