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
import time
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from agents.pdf_editor import build_outline

load_dotenv()


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _parse_retry_seconds(err: str) -> float:
    m = re.search(r"Please try again in (\d+)m([\d.]+)s", str(err))
    if m:
        return int(m.group(1)) * 60 + float(m.group(2))
    m = re.search(r"Please try again in ([\d.]+)s", str(err))
    if m:
        return float(m.group(1))
    return 30.0


def _extract_json(text: str) -> dict:
    if not text:
        return {}
    for match in re.finditer(r"\{.*\}", text, re.DOTALL):
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            continue
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
            parts.append(f"  [{i}] {b}")
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
   The CURRENT SUMMARY is shown above with an exact word count. Your rewrite
   MUST be between 90% and 115% of that count — measure as you write. A short
   summary leaves an ugly white gap in the PDF because the layout rect is
   sized for the original.

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
   - Length: target 90-120% of the original bullet's length to avoid PDF overflow.

3. skills_order:
   If SKILLS above is a plain comma-separated list, return it reordered so
   job-relevant skills appear first (same items, no additions, no rewording).
   If SKILLS is marked "(categorised layout)", return an empty list [].

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

def _call_llm(prompt: str, max_tokens: int = 2000, retries: int = 3) -> str:
    from agents.runtime    import track_llm_call, handle_rate_limit
    from agents.llm_client import chat_gemini, chat_quality

    for attempt in range(retries):
        try:
            track_llm_call(agent="cv_diff_tailor")
            # Prefer Gemini; it handles long JSON + many roles/bullets better.
            result = chat_gemini(prompt, max_tokens=max_tokens, temperature=0.2)
            if result:
                return result
            # Explicit Groq fallback if Gemini returned empty (rare — chat_gemini
            # already falls back internally, but belt-and-braces for 0-rewrite
            # regression seen in the Cormac run).
            print(f"   ⚠️  cv_diff_tailor: Gemini empty, trying Groq (attempt {attempt + 1})")
            result = chat_quality(prompt, max_tokens=max_tokens, temperature=0.2)
            if result:
                return result
            print(f"   ⚠️  cv_diff_tailor empty response (attempt {attempt + 1})")
            time.sleep(4)

        except Exception as e:
            err = str(e).lower()
            if any(x in err for x in ["rate", "429", "quota", "resource"]):
                handle_rate_limit(_parse_retry_seconds(str(e)), agent="cv_diff_tailor")
            else:
                print(f"   ❌ cv_diff_tailor LLM error (attempt {attempt + 1}): {e}")
                if attempt < retries - 1:
                    time.sleep(3)

    return ""


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
            parts.append(b or "")
    skills = outline.get("skills")
    if isinstance(skills, list):
        parts.extend(s for s in skills if isinstance(s, str))
    elif isinstance(skills, str):
        parts.append(skills)
    text = " ".join(parts).lower()
    # Also strip common separators for robust membership checks.
    return {text}  # return as single-element set; callers use 'in' on the text


def _foreign_capitalized_terms(summary: str, cv_text_set: set) -> List[str]:
    """
    Return a list of capitalized/acronym phrases that appear in `summary`
    but NOT in any of the strings in `cv_text_set`. Stopwords are ignored.
    """
    if not summary:
        return []
    cv_text = next(iter(cv_text_set), "") if cv_text_set else ""
    if not cv_text:
        return []
    foreign: List[str] = []
    seen: set = set()
    for m in _CAPTERM_RX.finditer(summary):
        term = m.group(0).strip()
        if term in _CAPTERM_STOPWORDS:
            continue
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        if key not in cv_text:
            foreign.append(term)
    return foreign

_MIN_BULLETS_PER_ROLE = 2
_REWRITE_LEN_MIN_RATIO = 0.5   # rewrite must be at least 50% of original length
_REWRITE_LEN_MAX_RATIO = 2.0   # and at most 200% — longer usually means fabrication


def _rewrite_is_safe(original: str, rewrite: str) -> tuple:
    """
    Guardrail: reject rewrites that look like fabrications or truncations.
    - Length must be 50%-200% of the original.
    - Every number-like token in the ORIGINAL must appear verbatim in the
      rewrite. '25%' must stay '25%' (not '25 percent'), '600K' stays '600K'
      (not '60K'). This prevents both fabrication and semantic drift.

    Returns (ok, reason). reason is "" when ok.
    """
    orig = (original or "").strip()
    new  = (rewrite or "").strip()
    if not new:
        return False, "empty rewrite"
    lo, hi = len(orig) * _REWRITE_LEN_MIN_RATIO, len(orig) * _REWRITE_LEN_MAX_RATIO
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
    orig_texts:  List[str],
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
            orig = orig_texts[idx] if idx < len(orig_texts) else ""
            ok, reason = _rewrite_is_safe(orig, text)
            if not ok:
                print(
                    f"   ⚠️  rewrite rejected (bullet {idx}, {reason}): "
                    f"{text[:80]!r} — reverting to original"
                )
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

    # Skills
    skills_order = raw.get("skills_order") or []
    real_skills  = outline.get("skills") or []
    if isinstance(skills_order, list) and real_skills:
        real_lookup = {s.strip().lower(): s for s in real_skills}
        clean_sk: List[str] = []
        seen_sk:  set = set()
        for item in skills_order:
            key = str(item).strip().lower()
            if key in real_lookup and key not in seen_sk:
                clean_sk.append(real_lookup[key])
                seen_sk.add(key)
        for s in real_skills:
            if s.strip().lower() not in seen_sk:
                clean_sk.append(s)
        if clean_sk and clean_sk != real_skills:
            out["skills_order"] = clean_sk

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
    new_sum = (diff.get("summary") or "").strip()
    if new_sum and orig_summary:
        cv_vocab = _cv_vocabulary(outline)
        foreign  = _foreign_capitalized_terms(new_sum, cv_vocab)
        if foreign:
            print(
                f"   ⚠️  summary introduced CV-foreign terms {foreign!r} — "
                f"reverting to original summary to avoid fabrication."
            )
            diff["summary"] = orig_summary

    # ── Length-enforcement retry ─────────────────────────────────────
    new_words = len((diff.get("summary") or "").split())
    if orig_words >= 20 and new_words and new_words < int(orig_words * 0.72):
        low  = int(orig_words * 0.9)
        high = int(orig_words * 1.15)
        print(
            f"   ↻  summary too short ({new_words}/{orig_words} words) — "
            f"retrying with hard target {low}-{high}."
        )
        enforce = (
            f"YOUR PREVIOUS SUMMARY WAS TOO SHORT ({new_words} words). "
            f"The target is {low}-{high} words. Rewrite the summary to fall "
            f"strictly within that range. You may add more CV-grounded detail "
            f"(specific outcomes, years, platforms, methodologies) but DO NOT "
            f"invent anything that is not in the CV."
        )
        raw_text2 = _call_llm(_render_prompt(extra=enforce))
        raw_json2 = _extract_json(raw_text2)
        diff2     = _sanitise_diff(raw_json2, outline)
        new_sum2  = (diff2.get("summary") or "").strip()
        if new_sum2 and len(new_sum2.split()) > new_words:
            diff["summary"] = new_sum2
            new_words = len(new_sum2.split())

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
    if n_rewrites == 0 and not (feedback or previous_diff):
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
    print(
        f"   ✂️  cv_diff_tailor{tag}: "
        f"summary={new_words}/{orig_words}w | "
        f"roles_edited={len(diff['bullets'])} | "
        f"bullets_rewritten={n_rewrites} | "
        f"bullets_dropped={n_dropped} | "
        f"skills_reordered={'yes' if diff['skills_order'] else 'no'}"
    )
    return diff