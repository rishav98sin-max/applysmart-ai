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
    """
    Compact outline rendering for the cv_diff_tailor prompt.

    Apr 30 trim: dropped the verbose ROLES preamble (the rules are already
    stated extensively in the prompt template that follows) and the
    `(section=...)` per-role tag (used by the reviewer's render path, not
    by the tailor). Net: ~250-400 chars saved per call without losing any
    bullet content.
    """
    parts: List[str] = []
    cur_summary   = (outline.get("summary") or "").strip()
    cur_word_count = len(cur_summary.split()) if cur_summary else 0
    parts.append(f"CURRENT SUMMARY ({cur_word_count} words):")
    parts.append(cur_summary or "(none)")
    parts.append("")
    parts.append("ROLES (0-indexed bullets — index 'i' is how the editor locates each bullet):")
    parts.append(
        "Each bullet has a [max=N chars] budget — your rewrite MUST stay "
        "at or below N. Rewrites longer than the budget WILL be rejected "
        "by the editor and the original bullet kept in place. Aim for "
        "roughly the same length as the original (the budget is +10% "
        "headroom, not a target)."
    )
    for r in outline.get("roles", []):
        parts.append(f'Role "{r["header"]}":')
        for i, b in enumerate(r["bullets"]):
            # Bullets from build_outline are dicts {"text": str, "length": int};
            # tolerate legacy str entries too.
            btext = b["text"] if isinstance(b, dict) else str(b)
            # Length budget = original length + 10%, floor 80 chars so very
            # short bullets aren't impossible to rewrite. Capped at 350 to
            # avoid runaway over-budget on dense paragraphs that already
            # use the full PDF rect.
            orig_len = len(btext.strip())
            budget = max(80, min(350, int(orig_len * 1.10)))
            parts.append(f"  [{i}] [max={budget} chars] {btext}")
        parts.append("")
    skills = outline.get("skills") or []
    if skills:
        parts.append("SKILLS (do NOT reorder):")
        parts.append(", ".join(skills))
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────
# Prompt template (unchanged)
# ─────────────────────────────────────────────────────────────

_PROMPT_TEMPLATE = """You are an EXECUTOR. A senior career strategist has already analysed
the CV and the JD and produced a binding STRATEGY (below). Your job is
to faithfully execute that strategy as a CV diff. You do NOT improvise
a different strategy.

You must output a JSON object (no prose, no markdown) that describes edits.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
YOUR THINKING PROCESS (critical — do this BEFORE writing JSON):

1. READ the STRATEGY block. Note the narrative angle, the per-bullet
   action plan, and the do_not_inject list.
2. For the SUMMARY: open with the title the strategy says to lead with;
   weave in the must_include_phrases (which are already in the CV);
   keep every credential (grade, YoE, employer, university) intact.
3. For each bullet listed in the STRATEGY:
     • action=rewrite_verb_led → open with the target_verb_phrase the
       strategy gave you. Move the strategy's target_keywords to the
       front of the sentence. Keep every number and proper noun from
       the original verbatim. Do NOT just append the keywords — reshape
       the bullet so JD vocabulary leads.
     • action=promote → keep the original text (text=null) but place
       this bullet earlier in the role's array.
     • action=deprioritise → keep verbatim (text=null), place last.
4. For bullets NOT in the strategy: keep verbatim (text=null) in their
   original order. Do NOT improvise extra rewrites.
5. CHECK every rewrite against the do_not_inject list. If a rewrite
   contains any of those terms, scrub them — those terms are not in
   the CV and a guard will REJECT the rewrite if they appear.
6. ONLY NOW, format your rewrite plan into the JSON schema at the bottom.

If you skip straight to JSON or invent rewrites the strategy did not
authorise, the deterministic guards will revert your output to the
original and the user gets an un-tailored CV.
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

{strategy_block}

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

   MUST-PRESERVE CREDENTIALS (applies to the SUMMARY specifically):
   The original summary contains specific credentials a recruiter scans for.
   Your rewrite MUST keep every one of these intact, verbatim:
     • Degree grades / classifications (e.g. "(2.1)", "First Class",
       "Distinction", "GPA 3.8", "Magna Cum Laude").
     • University and company names — exactly as written in the original.
     • Years of experience claims (e.g. "4+ years", "5 years' experience").
     • Numeric outcomes already quoted in the summary
       (percentages, scale, headcount, revenue).
     • Job-title language identifying the candidate's specialism
       (e.g. "Technical Product Specialist", "QA & Performance Testing
       specialist").
   If your rewrite would drop ANY of the above to make room for JD
   keywords, DO NOT submit the rewrite — keep the original summary
   verbatim instead. A summary without the candidate's grade or YoE
   loses the recruiter immediately; a slightly less JD-aligned summary
   is far better than a credential-stripped one.

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

   Rewrite plan source of truth:
     • If the STRATEGY block above provides a per-bullet action plan,
       follow it EXACTLY. Each bullet listed there is your only target.
       Do NOT add rewrites the strategy did not authorise.
     • If the STRATEGY block is empty (fallback mode), use the legacy
       floors below:
         - HIGHLY RELEVANT role: rewrite at least 50% of bullets.
         - PARTIALLY RELEVANT role: rewrite at least 30% of bullets.
         - TANGENTIAL role: rewrite at least 1 bullet.

   You may REORDER freely — place the most job-relevant bullets first
   (the strategy's promote / deprioritise actions tell you the order).

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

EXAMPLE OUTPUT FORMAT (the role headers and text below are placeholders
to illustrate shape only — the real output must use the EXACT role
headers from the CV outline above, and every rewrite must preserve the
numbers / proper nouns from that specific bullet's original):
{{
  "summary": "…",
  "bullets": {{
    "<Employer Name>, <Job Title>": [
      {{"i": 2, "text": "<rewrite starting with a JD verb, preserving every number and proper noun from the original bullet 2>"}},
      {{"i": 0, "text": "<rewrite leading with JD keywords, preserving every number and proper noun from the original bullet 0>"}},
      {{"i": 1}}
    ],
    "<Another Employer Name>, <Job Title>": [
      {{"i": 0}},
      {{"i": 3, "text": "<rewrite preserving the original's numbers and scope>"}},
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
    Provider chain (May 2026): DeepSeek V4-Flash → Groq.

    Gemini was removed from the chain because GEMINI_BYPASS=1 has been the
    default since Apr 30 — `chat_gemini()` was just calling Groq under the
    hood, meaning a DeepSeek-fail used to trigger TWO Groq calls (~7K
    wasted tokens + ~3s wasted latency per failed attempt). Now we go
    DeepSeek → Groq directly.

    Why DeepSeek first:
      Llama 3.3 70B leans on canned suffixes ("...by analyzing market
      conditions and prioritizing market segment opportunities") to fake
      JD alignment when it can't find specific edits. DeepSeek V4 doesn't
      do that — it either makes a substantive edit or leaves the bullet
      alone. That's the exact behaviour we want for "polish, don't gut".

    Backward compatibility:
      With DEEPSEEK_API_KEY unset, chat_deepseek() returns None and the
      call falls straight through to Groq. Free-tier deployments on
      Streamlit Cloud are unaffected.
    """
    from agents.runtime    import track_llm_call
    from agents.llm_client import chat_deepseek, chat_quality

    track_llm_call(agent="cv_diff_tailor")

    # 1) DeepSeek (only if DEEPSEEK_API_KEY is configured)
    deepseek_result = chat_deepseek(
        prompt, max_tokens=max_tokens, temperature=0.2, json_mode=True
    )
    if deepseek_result:
        parsed = _extract_json(deepseek_result)
        is_actionable = bool(parsed) and bool(
            parsed.get("summary")
            or parsed.get("bullets")
            or parsed.get("skills_order")
        )
        if is_actionable:
            return deepseek_result
        print(
            f"   ↪️  cv_diff_tailor: DeepSeek output unusable "
            f"(len={len(deepseek_result)}, parsed={bool(parsed)}, "
            f"actionable={is_actionable}) — falling back to Groq"
        )

    # 2) Groq (final fallback)
    return chat_quality(prompt, max_tokens=max_tokens, temperature=0.2)


# ─────────────────────────────────────────────────────────────
# Sanitise diff (unchanged)
# ─────────────────────────────────────────────────────────────

_NUMBER_RX = re.compile(r"\d[\d.,]*\s*%?|\d+K\+?|\d+M\+?|\d+\+", re.I)


# ─────────────────────────────────────────────────────────────
# Credential-preservation guard (Apr 30)
# ─────────────────────────────────────────────────────────────
# Detects whether the rewritten summary dropped specific credentials that
# were present in the original. Drives a hard revert (keep original) when
# the rewrite would lose one of these recruiter-scan signals.
#
# Conservative by design: only flags tokens the regex is confident about
# (degree-grade patterns, X+ years claims). Numeric values are checked
# via _NUMBER_RX (already in use). Free-form names (universities, employers)
# are NOT auto-checked here — those are guarded by the existing
# "fabrication" + "foreign-term" logic.

# Degree grades / classifications. Examples that should match:
#   "(2.1)", "(2:1)", "First Class", "First-Class", "Distinction",
#   "GPA 3.8", "Magna Cum Laude", "Summa Cum Laude", "Cum Laude", "Honours".
_GRADE_PATTERNS = [
    re.compile(r"\(\s*[1-4][\.:][12]\s*\)"),                     # (2.1) / (2:1)
    re.compile(r"\bfirst[\s\-]class\b", re.I),
    re.compile(r"\bdistinction\b", re.I),
    re.compile(r"\b(?:summa|magna)\s+cum\s+laude\b", re.I),
    re.compile(r"\bcum\s+laude\b", re.I),
    re.compile(r"\bhonou?rs\b", re.I),
    re.compile(r"\bgpa\s*[:=]?\s*\d(?:\.\d+)?\b", re.I),
    re.compile(r"\b\d(?:\.\d+)?\s*/\s*\d(?:\.\d+)?\s*gpa\b", re.I),
]

# Years-of-experience patterns. Examples:
#   "4+ years", "5 years experience", "3 years' experience"
_YOE_RX = re.compile(r"\b\d+\+?\s*years?(?:['’]?\s+experience)?\b", re.I)


def _extract_credentials(summary: str) -> Dict[str, List[str]]:
    """
    Pull out the credential tokens present in `summary`. Used to compare
    original-vs-rewrite and revert the rewrite when something was dropped.
    """
    if not summary:
        return {"grades": [], "yoe": [], "numbers": []}
    grades: List[str] = []
    for rx in _GRADE_PATTERNS:
        for m in rx.finditer(summary):
            grades.append(m.group(0).strip().lower())
    yoe = [m.group(0).strip().lower() for m in _YOE_RX.finditer(summary)]
    numbers = [m.group(0).strip().lower() for m in _NUMBER_RX.finditer(summary)]
    # De-duplicate while preserving order.
    def _dedupe(items: List[str]) -> List[str]:
        seen: set = set()
        out: List[str] = []
        for it in items:
            if it not in seen:
                seen.add(it)
                out.append(it)
        return out
    return {
        "grades":  _dedupe(grades),
        "yoe":     _dedupe(yoe),
        "numbers": _dedupe(numbers),
    }


def _check_credentials_preserved(
    orig_summary: str,
    new_summary:  str,
) -> Optional[Dict[str, List[str]]]:
    """
    Returns None when no credentials are missing from the rewrite. Returns a
    dict of {kind: [missing_tokens]} when at least one credential was dropped.
    The caller should revert to the original summary on a non-None return.
    """
    if not orig_summary or not new_summary:
        return None
    orig = _extract_credentials(orig_summary)
    new  = _extract_credentials(new_summary.lower())
    # `new` is built from the lowercased rewrite for membership checks.
    new_text_lower = new_summary.lower()
    missing: Dict[str, List[str]] = {}
    for kind in ("grades", "yoe", "numbers"):
        gone = [tok for tok in orig[kind] if tok not in new_text_lower]
        if gone:
            missing[kind] = gone
    return missing or None

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
# May 2026 fix #2b: tightened upper bound from 2.0 → 1.20 to prevent forced
# font shrinkage in the PDF editor.
#
# Background: when a rewritten bullet is longer than the original PDF rect
# can fit at the original font size, `pdf_editor._insert_fitted` drops the
# font size 0.5pt at a time until it fits. Different bullets shrink different
# amounts (0pt, 0.5pt, 1pt, 1.5pt) → visibly inconsistent bullet sizes in the
# rendered CV. Shrinkage > 1pt is noticeable to a recruiter eye-balling the
# page; even 0.5pt creates uneven line gaps that read as "low quality".
#
# A rewrite at 1.20× the original length is the empirical threshold above
# which rect overflow becomes likely (single-line bullets wrap to two lines,
# multi-line bullets push past the rect's y1). Capping here means: any
# rewrite >20% longer than original gets reverted to original by
# `_rewrite_is_safe`, so we never even attempt to render a shrunk version.
#
# This pairs with the per-bullet character budget in `_format_outline_for_prompt`:
# the prompt asks for ≤+10% headroom, the sanitiser allows up to +20% (small
# tolerance for LLM rounding), and >+20% is rejected.
#
# Net behaviour: bullets ship at ORIGINAL font size or are kept untouched.
# Zero font shrinkage → consistent spacing → "polished" visual output.
_REWRITE_LEN_MAX_RATIO = 1.20


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


# ─────────────────────────────────────────────────────────────
# May 1: deterministic Personal-Projects fabrication guard.
#
# Background: yesterday I added an "PERSONAL-PROJECT FABRICATIONS = HARD
# CAP AT 50" rule to the reviewer prompt. Production showed the LLM
# ignoring it (Harvey Nash CV had `Partnered with engineering teams` on a
# solo project, reviewer scored 85/accept). LLM-judged enforcement of
# hard rules is unreliable; switching to a deterministic post-check.
#
# Trigger condition (all must hold):
#   - The role's section is "projects"
#   - The phrase appears in the REWRITE
#   - The phrase does NOT appear in the ORIGINAL bullet
#
# A solo personal-project bullet that introduces "team", "stakeholders",
# "the organisation", "platform teams", "partnered with engineering",
# "primary liaison", "managed escalations", etc. is FABRICATING
# collaborators that don't exist. Revert the rewrite to original.
#
# Phrases that CAN legitimately appear in a project bullet (and so are
# NOT banned) include "team" inside "team of three" patterns where the
# original already established it. The conditional check (original
# already has it) handles this correctly.
# ─────────────────────────────────────────────────────────────
_PROJECT_FABRICATION_PHRASES: tuple = (
    "partnered with engineering",
    "partnered with design",
    "partnered with product",
    "partnered with business",
    "partnered with the",
    "engineering teams",
    "business teams",
    "platform teams",
    "design teams",
    "product teams",
    "cross-functional teams",
    "cross-functional team",
    "cross functional teams",
    "across teams",
    "across the org",
    "across the organisation",
    "across the organization",
    "the organisation",
    "the organization",
    "company-wide",
    "company wide",
    "primary liaison",
    "managed escalations",
    "managed expectations of",
    "stakeholder alignment",
    "stakeholder management",
    "drove alignment across",
    "aligned stakeholders",
    "managing stakeholders",
)


def _check_solo_project_fabrication(
    original: str,
    rewrite:  str,
    section:  str,
) -> Optional[str]:
    """
    Returns the offending phrase if a solo-project rewrite introduces a
    team/stakeholder/organisation framing that the original lacks.
    Returns None when the rewrite is clean.

    Only fires when section == "projects" — the same phrases on an
    Experience role are legitimate (you DO partner with engineering teams
    at IBM). The original-bullet check ensures we don't revert if the
    candidate's own project description already mentions collaborators.
    """
    sec = (section or "").strip().lower()
    if sec != "projects":
        return None
    orig_l = (original or "").lower()
    new_l  = (rewrite or "").lower()
    for phrase in _PROJECT_FABRICATION_PHRASES:
        if phrase in new_l and phrase not in orig_l:
            return phrase
    return None


# ─────────────────────────────────────────────────────────────
# May 1: cross-bullet contamination guard.
#
# The LLM occasionally rewrites bullet A's content using facts (numbers,
# specific platforms) that live in bullet B's original. The number-token
# guard (_rewrite_is_safe) only checks "does this rewrite preserve the
# numbers in THIS bullet's original?" — it has no view of OTHER bullets
# in the same role, so contamination passes.
#
# Run-2 example (Harvey Nash IBM section):
#   Original bullet 0: "Drove 5% user growth and 15% retention... 5-7%"
#   Original bullet 1: "Authored... 600K+ user platform... 30% latency"
#   Rewrite of bullet 0: "...5% user growth and 15%... 5-7%, AND
#                         authored product artifacts for a 600K+ user
#                         platform, reducing system latency by 30%"
# The rewrite passes _rewrite_is_safe (5%, 15%, 5-7% all preserved) but
# duplicates bullet 1's facts. Recruiters see "30% latency reduction"
# twice in adjacent bullets → looks sloppy.
#
# Detection: numeric tokens that appear in the rewrite, are NOT in this
# bullet's original, but ARE in some OTHER bullet's original. Allowlist
# years (1900-2099) and small ints (1-9) to avoid false positives on
# generic counts.
# ─────────────────────────────────────────────────────────────
def _is_common_number_token(tok: str) -> bool:
    """
    Allowlist for tokens that don't constitute contamination evidence.
    Years (1900-2099), single digits, and bare 1-2 character integers
    are too common to attribute to a specific bullet.
    """
    t = tok.strip().rstrip("%+").rstrip("k").rstrip("K").rstrip("M")
    if not t:
        return True
    try:
        n = float(t.replace(",", ""))
        if 1900 <= n <= 2099:
            return True
        if n < 10 and "%" not in tok and "+" not in tok and "K" not in tok.upper() and "M" not in tok.upper():
            return True
    except ValueError:
        pass
    return False


def _check_cross_role_contamination(
    rewrite:        str,
    this_original:  str,
    all_originals:  List[str],
    this_index:     int,
) -> Optional[str]:
    """
    Returns the offending token if the rewrite contains a numeric token
    that doesn't appear in this bullet's original but appears in another
    bullet's original within the same role. Returns None when clean.
    """
    rewrite_l = (rewrite or "").lower()
    this_orig_l = (this_original or "").lower()
    rewrite_nums = {m.group(0).strip().lower() for m in _NUMBER_RX.finditer(rewrite_l)}
    this_orig_nums = {m.group(0).strip().lower() for m in _NUMBER_RX.finditer(this_orig_l)}
    new_nums = rewrite_nums - this_orig_nums
    if not new_nums:
        return None
    # Build set of numeric tokens present in OTHER bullets' originals.
    other_nums: set = set()
    for j, other in enumerate(all_originals):
        if j == this_index:
            continue
        other_text = other.get("text") if isinstance(other, dict) else (other or "")
        for m in _NUMBER_RX.finditer((other_text or "").lower()):
            other_nums.add(m.group(0).strip().lower())
    for tok in new_nums:
        if _is_common_number_token(tok):
            continue
        if tok in other_nums:
            return tok
    return None


# ─────────────────────────────────────────────────────────────
# May 1: do_not_inject guard.
#
# Sourced from the strategist's `do_not_inject` list (JD-only terms that
# do NOT appear in the CV). The strategist explicitly classifies these
# during gap analysis. If a bullet rewrite mentions any of them, the
# rewrite is fabricating a skill/tool/domain the candidate doesn't have.
#
# Match policy: case-insensitive whole-word substring. We test the
# rewrite against the term (e.g. "microservices", "UAT", "decisioning")
# and reject if the term appears in the rewrite but did NOT appear in
# the original bullet (a term legitimately in the original is not a
# new injection — it's pre-existing CV content).
# ─────────────────────────────────────────────────────────────
def _check_do_not_inject(
    rewrite:      str,
    original:     str,
    do_not_inject: List[str],
) -> Optional[str]:
    """Returns the offending term, or None when the rewrite is clean."""
    if not rewrite or not do_not_inject:
        return None
    new_l  = " " + (rewrite or "").lower() + " "
    orig_l = " " + (original or "").lower() + " "
    for raw_term in do_not_inject:
        term = (raw_term or "").strip().lower()
        if not term or len(term) < 3:
            continue
        # Whole-word-ish match: pad with spaces / punctuation boundaries.
        # The simple substring is acceptable here — these terms are
        # multi-character technical phrases, not common substrings.
        if term in new_l and term not in orig_l:
            return raw_term
    return None


# ─────────────────────────────────────────────────────────────
# May 1: banned filler-suffix guard for CV bullets.
#
# Run 3 showed the LLM repeatedly suffixing CV bullets with corporate-
# filler phrases ("ensuring transparency and managing risks", "and
# driving business value delivery", "while championing innovation and
# exploring emerging technologies"). These add zero information and
# are tells of templated writing. They appear in the JD vocabulary but
# tacking them onto bullets that don't have these as outcomes is
# meaningless padding.
#
# Rule: a bullet rewrite that ENDS with one of these phrases AND the
# original did not contain it gets reverted to the original. This
# stops the suffix-graft pattern at the source.
# ─────────────────────────────────────────────────────────────
_CV_BULLET_BANNED_SUFFIXES: tuple = (
    "ensuring transparency and managing risks",
    "ensuring transparency",
    "managing risks",
    "and driving business value delivery",
    "drive business value delivery",
    "driving business value",
    "drive business value",
    "while championing innovation",
    "championing innovation",
    "exploring emerging technologies and strategic opportunities",
    "and strategic opportunities",
    "with a strong analytical skillset",
    "and attention to detail",
    "driving ambiguity to outcomes",
    "drive ambiguity to outcomes",
    "driving business outcomes through data-driven insights",
    "and driving business outcomes",
    "drive business outcomes",
    "ensuring high-quality product delivery",
    "and improving delivery efficiency",
    "and informing product strategy",
)


def _check_banned_suffix_in_bullet(
    rewrite:  str,
    original: str,
) -> Optional[str]:
    """
    Returns the offending banned suffix if the rewrite contains corporate
    filler that the original did not. Returns None when the rewrite is
    clean. Match is case-insensitive substring (these are long phrases,
    so substring is reliable).
    """
    if not rewrite:
        return None
    new_l  = (rewrite or "").lower()
    orig_l = (original or "").lower()
    for phrase in _CV_BULLET_BANNED_SUFFIXES:
        if phrase in new_l and phrase not in orig_l:
            return phrase
    return None


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
    order_raw:     Any,
    n_bullets:     int,
    orig_texts:    List[Any],
    section:       str = "experience",
    do_not_inject: Optional[List[str]] = None,
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
            else:
                # May 1 deterministic guards (run only when length+number
                # checks already passed; revert wins over rewrite).
                pf = _check_solo_project_fabrication(orig_text, text, section)
                if pf:
                    print(
                        f"   ⚠️  rewrite rejected (bullet {idx}, "
                        f"solo-project fabrication '{pf}'): "
                        f"{text[:80]!r} — reverting to original"
                    )
                    _LAST_BULLET_REVERTS.append({
                        "bullet_index": idx,
                        "reason": f"solo_project_fabrication:{pf}",
                        "rewrite_preview": text[:120],
                    })
                    text = None
                else:
                    cc = _check_cross_role_contamination(
                        text, orig_text, orig_texts, idx,
                    )
                    if cc:
                        print(
                            f"   ⚠️  rewrite rejected (bullet {idx}, "
                            f"cross-bullet contamination token {cc!r}): "
                            f"{text[:80]!r} — reverting to original"
                        )
                        _LAST_BULLET_REVERTS.append({
                            "bullet_index": idx,
                            "reason": f"cross_contamination:{cc}",
                            "rewrite_preview": text[:120],
                        })
                        text = None
                    else:
                        # do_not_inject — strategist-classified JD-only terms.
                        dni = _check_do_not_inject(text, orig_text, do_not_inject or [])
                        if dni:
                            print(
                                f"   ⚠️  rewrite rejected (bullet {idx}, "
                                f"do_not_inject term {dni!r} — JD-only, "
                                f"not in CV): {text[:80]!r} — reverting"
                            )
                            _LAST_BULLET_REVERTS.append({
                                "bullet_index": idx,
                                "reason": f"do_not_inject:{dni}",
                                "rewrite_preview": text[:120],
                            })
                            text = None
                        else:
                            # Banned filler-suffix guard.
                            bs = _check_banned_suffix_in_bullet(text, orig_text)
                            if bs:
                                print(
                                    f"   ⚠️  rewrite rejected (bullet {idx}, "
                                    f"banned filler suffix {bs!r}): "
                                    f"{text[:80]!r} — reverting"
                                )
                                _LAST_BULLET_REVERTS.append({
                                    "bullet_index": idx,
                                    "reason": f"banned_suffix:{bs}",
                                    "rewrite_preview": text[:120],
                                })
                                text = None
        normalised.append({"i": idx, "text": text})
        seen.add(idx)

    # No-drop policy: every original bullet must appear in the output.
    # Append any missing indices verbatim (text=None) in original order.
    for i in range(n_bullets):
        if i not in seen:
            normalised.append({"i": i, "text": None})
            seen.add(i)

    return normalised


def _sanitise_diff(
    raw:           Dict[str, Any],
    outline:       Dict[str, Any],
    do_not_inject: Optional[List[str]] = None,
) -> Dict[str, Any]:
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
            section = (role.get("section") or "experience").strip().lower()
            normalised = _normalise_bullet_list(
                order, n, orig_texts,
                section=section,
                do_not_inject=do_not_inject,
            )
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

def _summarise_previous_diff(previous_diff: Dict[str, Any]) -> str:
    """
    Compact one-line-per-role summary of the previous diff, used in the
    retry addendum instead of the full pretty-printed JSON dump (~1,400
    chars). Lets the LLM see WHAT it changed last time without re-reading
    every rewritten string. Net: ~350 chars vs ~1,400 (~80% smaller).
    """
    if not previous_diff:
        return ""
    lines: List[str] = []
    sm = (previous_diff.get("summary") or "").strip()
    if sm:
        lines.append(f"prev_summary_len={len(sm)} chars")
    bullets = previous_diff.get("bullets") or {}
    if isinstance(bullets, dict):
        for role, entries in bullets.items():
            if not isinstance(entries, list):
                continue
            n_total    = len(entries)
            n_rewrites = sum(
                1 for e in entries
                if isinstance(e, dict) and e.get("text")
            )
            order = ",".join(
                str(e.get("i") if isinstance(e, dict) else e)
                for e in entries
            )
            lines.append(
                f"{role[:50]}: order=[{order}] rewritten={n_rewrites}/{n_total}"
            )
    return "\n".join(lines)


def _build_feedback_addendum(
    feedback:      str,
    previous_diff: Optional[Dict[str, Any]],
) -> str:
    """
    Retry-only addendum appended to the prompt when the reviewer rejected the
    previous attempt.

    Apr 30 trim: replaced the full `json.dumps(previous_diff, indent=2)[:1400]`
    dump with `_summarise_previous_diff()` — saves ~1,000 chars per retry
    call without losing what the LLM actually needs (it never reads the
    full diff JSON; it just needs to know what it tried last time).
    """
    if not feedback and not previous_diff:
        return ""
    parts: List[str] = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "REVIEWER FEEDBACK ON YOUR PREVIOUS ATTEMPT (incorporate this):",
    ]
    if previous_diff:
        summary_line = _summarise_previous_diff(previous_diff)
        if summary_line:
            parts.append("Previous attempt (compact):")
            parts.append(summary_line)
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
    strategy:        Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Produce a validated structured diff for the CV at `cv_pdf_path` targeting
    the given job. Safe to pass straight to agents.pdf_editor.apply_edits().

    Optional parameters used by the reviewer-driven retry loop:
      feedback      : reviewer's short actionable feedback string
      previous_diff : the diff the tailor produced on the previous attempt
      outline       : precomputed outline (to avoid re-parsing the PDF on retry)
      strategy      : optional strategist output (see agents.tailor_strategist).
                      When provided, the tailor executes its per-bullet action
                      plan instead of inventing one. When omitted, the tailor
                      falls back to the legacy "rewrite N% of relevant bullets"
                      heuristic.
    """
    if outline is None:
        outline = build_outline(cv_pdf_path)

    # Reset the per-call bullet-revert tracker so counts reflect THIS job.
    _LAST_BULLET_REVERTS.clear()

    orig_summary = (outline.get("summary") or "").strip()
    orig_words   = len(orig_summary.split()) if orig_summary else 0

    # Render the strategy block once (empty string when no strategy was
    # provided OR the strategist returned an empty payload). The tailor
    # prompt template requires a {strategy_block} substitution either way.
    try:
        from agents.tailor_strategist import render_strategy_for_tailor
        strategy_block_str = render_strategy_for_tailor(strategy or {})
    except Exception as e:
        print(f"   ⚠️  cv_diff_tailor: strategy render failed ({e}) — falling back to no-strategy mode")
        strategy_block_str = ""

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
            strategy_block        = strategy_block_str or "(no strategy provided — use the legacy fallback floors in the RULES section below)",
        )
        p += "\n\n" + _build_feedback_addendum(feedback, previous_diff)
        if extra:
            p += "\n\n" + extra
        return p

    # Surface the strategist's do_not_inject list so every bullet rewrite
    # gets checked against JD-only terms (microservices / cloud / UAT etc.
    # when those words don't appear in the CV).
    strategy_dni: List[str] = list((strategy or {}).get("do_not_inject") or [])

    raw_text = _call_llm(_render_prompt())
    raw_json = _extract_json(raw_text)
    diff     = _sanitise_diff(raw_json, outline, do_not_inject=strategy_dni)

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
            else:
                # Apr 30: credential-preservation guard. If the rewrite
                # dropped a degree grade ("(2.1)"), YoE claim ("4+ years"),
                # or numeric outcome present in the original summary,
                # revert. Recruiters scan for these signals in the 6-second
                # first pass — losing one is a CV-quality regression even
                # if the JD-alignment improved.
                missing_creds = _check_credentials_preserved(orig_summary, new_sum)
                if missing_creds:
                    flat = ", ".join(
                        f"{k}={v}" for k, v in missing_creds.items()
                    )
                    print(
                        f"   ⚠️  summary dropped credential tokens ({flat}) — "
                        f"reverting to original summary to preserve "
                        f"recruiter-scan signals."
                    )
                    diff["_debug"]["summary_reverts"].append({
                        "reason":  "credentials_dropped",
                        "missing": missing_creds,
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
        diff2     = _sanitise_diff(raw_json2, outline, do_not_inject=strategy_dni)
        new_sum2  = (diff2.get("summary") or "").strip()
        if new_sum2 and len(new_sum2.split()) >= int(orig_words * _SUMMARY_MIN_RATIO):
            # May 1 fix: re-run the credential-preservation guard on the
            # length-retry summary. Yesterday's run produced a length-retry
            # rewrite that dropped "(2.1)" because this branch never
            # consulted _check_credentials_preserved. We now treat a
            # credential-dropped retry the same as a too-short retry: fall
            # back to the original summary verbatim. Length floor is a
            # quality goal; credential preservation is a non-negotiable.
            missing_creds_retry = _check_credentials_preserved(orig_summary, new_sum2)
            if missing_creds_retry:
                flat = ", ".join(
                    f"{k}={v}" for k, v in missing_creds_retry.items()
                )
                print(
                    f"   ↺  length-retry rewrite dropped credential tokens "
                    f"({flat}) — reverting to original summary."
                )
                diff["_debug"]["summary_reverts"].append({
                    "reason":  "credentials_dropped_on_retry",
                    "missing": missing_creds_retry,
                })
                diff["summary"] = orig_summary
                new_words = orig_words
            else:
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
            diff_rr     = _sanitise_diff(raw_json_rr, outline, do_not_inject=strategy_dni)
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