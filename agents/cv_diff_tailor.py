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
from agents.pdf_editor import build_outline_cached

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
    # 99% format mode: surface a HARD character budget for the summary too.
    # The summary bbox in the original PDF was sized for `orig_chars`. Allow
    # +10% (matches the bullet budget policy). Anything longer is rejected
    # by `_apply_summary_edit` (1.4× word-ratio guard) or by PyMuPDF's
    # textbox fit check at base font size.
    orig_summary_chars = len(cur_summary)
    summary_budget = max(400, int(orig_summary_chars * 1.10)) if orig_summary_chars else 600
    parts.append(
        f"CURRENT SUMMARY ({cur_word_count} words, {orig_summary_chars} chars, "
        f"max={summary_budget} chars — HARD limit; longer rewrites REVERT to original):"
    )
    parts.append(cur_summary or "(none)")
    parts.append("")
    parts.append("ROLES (0-indexed bullets — index 'i' is how the editor locates each bullet):")
    parts.append(
        "Each bullet shows [keep ≈N words]. THE RULE: a tailored bullet has "
        "the SAME word count as the original — match the N shown, within a "
        "word or two, NEITHER longer NOR shorter. The editor drops each "
        "rewrite into the exact slot the original occupies with no page "
        "reflow: a same-length rewrite fits; a longer one overflows the slot "
        "and a shorter one leaves a visible gap — BOTH are REJECTED. Tailor "
        "by RE-WORDING the bullet's content end to end — re-word ALL of it, "
        "keep every fact; never append a new clause and never drop content. "
        "Re-aim the SAME amount of content at THIS job."
    )
    for r in outline.get("roles", []):
        parts.append(f'Role "{r["header"]}":')
        for i, b in enumerate(r["bullets"]):
            # Bullets from build_outline are dicts {"text": str, "length": int};
            # tolerate legacy str entries too.
            btext = b["text"] if isinstance(b, dict) else str(b)
            # May 2026 (per-bullet in-place renderer): the target length is
            # the ORIGINAL bullet's length. The editor inserts each rewrite
            # into the original bullet's slot with no page reflow, so a
            # same-length rewrite occupies the same line count and the
            # format is preserved. We show the original length as the
            # target the LLM should hit (±10%).
            orig_len = len(btext.strip())
            orig_words = len(btext.split())
            parts.append(f"  [{i}] [keep ≈{orig_words} words] {btext}")
        parts.append("")
    skills = outline.get("skills") or []
    if skills:
        parts.append("SKILLS (do NOT reorder):")
        parts.append(", ".join(skills))
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────
# Prompt template (unchanged)
# ─────────────────────────────────────────────────────────────

_PROMPT_TEMPLATE = """╔══════════════════════════════════════════════════════════════════════╗
║  MANDATORY: produce genuine bullet rewrites — but tailoring          ║
║  RE-FRAMES, it never REMOVES. Rewrite every targeted bullet you can   ║
║  improve WITHOUT dropping a fact; keep the rest unchanged. Returning  ║
║  text=null for EVERY bullet = failed tailoring. A rewrite that drops  ║
║  a concrete fact = also failed (reverts to original).                ║
║                                                                       ║
║  THE LENGTH RULE: each rewrite must be ≈ the SAME LENGTH as its       ║
║  original (within ±10% of the [target≈N chars] shown). The output    ║
║  PDF places each rewrite in the original bullet's exact slot with    ║
║  no reflow — a same-length rewrite fits perfectly; a longer one is    ║
║  reverted (lost tailoring). Preserve every number + proper noun.      ║
╚══════════════════════════════════════════════════════════════════════╝

You are an EXECUTOR. A senior career strategist has already analysed
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
     • action=rewrite_verb_led → the strategy gives a "LEAD WITH:"
       directive naming a fact ALREADY IN that bullet. Produce a GENUINE
       rewrite: REBUILD the sentence from scratch. OPEN it with a STRONG
       PAST-TENSE ACTION VERB that mirrors the JD's language, and pull
       the LEAD-WITH fact into the OPENING CLAUSE (early — within the
       first ~8 words — but NOT necessarily the literal first word).
       Example pattern: lead-with fact "40% efficiency gain" →
       "Delivered 40% efficiency gain and 2x capacity by identifying a
       critical bottleneck…" — verb first, fact early, natural sentence.
       The wording changes; the FACTS do not. Keep every number, proper
       noun and claim exactly as the original; build only from words in
       that bullet or elsewhere in the CV; never use a JD term from the
       FORBIDDEN list.
       ⚠️  TWO WAYS TO FAIL — avoid BOTH:
         (1) BROKEN GRAMMAR — shoving the fact to the literal front and
             leaving the original verb stranded ("Supervisor + workers
             pattern… scoped the architecture, so…"). Word salad. The
             guard discards it. NEVER make a bare noun phrase the first
             word — open with a verb.
         (2) NEAR-COPY — coming back with only the verb swapped
             ("Built…"→"Implemented…") or a clause reordered. That is a
             COSMETIC non-rewrite, not tailoring. A real rewrite re-aims
             the bullet's LANGUAGE at this JD.
       ↳ REFRAME (THE KEY MOVE): if the bullet's action carries a
         "REFRAME — weave in the JD term '<X>'" line, your rewrite MUST
         surface that exact JD term — name what the bullet describes
         using the JD's word. The strategist verified '<X>' is CV-proven
         (the mechanism is real; only the label changes), so it is NOT
         fabrication and NOT on the FORBIDDEN list. A rewrite of a
         REFRAME bullet that does not contain its JD term is a FAILED
         rewrite — it is the cosmetic reorder we are trying to kill.
         You may also use '<X>' as a short "<X>: …" label opening the
         bullet. Example: a bullet about "a second LLM that grades
         output for fabrication", REFRAME term "LLM-as-judge eval" →
         "Built an LLM-as-judge eval that grades cover letters for
         fabrication and retries low scorers…".
         INTEGRATE IT NATURALLY — rephrase the sentence so the term
         reads as clean English a person would write. Do NOT bolt it on
         ("Owned 0-to-1 product ownership" is redundant — write "Owned
         the product 0-to-1"). Do NOT jam it mid-sentence after a colon
         ("…shipped VoC Insight Hub with eval practice: a live web app"
         is clumsy). If the term cannot be woven in cleanly at the
         bullet's fixed length, surface the bullet's own fact well and
         skip the term — a clean sentence beats a jammed keyword.
       Every rewrite is ONE clean grammatical sentence, verb-first.
     • action=promote → keep the original text (text=null) but place
       this bullet earlier in the role's array.
     • action=deprioritise → keep verbatim (text=null), place last.
4. For bullets NOT in the strategy: keep verbatim (text=null) in their
   original order. Do NOT improvise extra rewrites.
5. CROSS-CHECK every rewrite — the summary AND every single bullet —
   against the FORBIDDEN WORDS block above. NOT ONE of those words may
   appear anywhere in your output. They are JD vocabulary this candidate
   never used; if a sentence seems to need one, the underlying claim is
   not in the CV — drop the claim and reframe using what the CV supports.
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

{jd_only_terms_block}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

CV CONTENT (structured):
{outline}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{strategy_block}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

RULES (strict):

1. summary:
   WHO YOU ARE WRITING FOR:
   A recruiter on a 6-second scan. They have a stack of CVs. Your
   summary either earns them another 30 seconds with this candidate or
   sends the CV to the no-pile. You are NOT writing marketing copy.
   You are writing a human pitch in 2-4 crisp lines: "I am X, I have
   done Y, that's why I fit THIS role." Write like a person describing
   themselves to another person in plain English.

   THE 4-PART STRUCTURE (write in this order; each part is required):

     PART 1 — IDENTITY (one phrase, ~10-15 words):
       Role-family + years of experience + the candidate's domain.
       Pull role/YoE/domain VERBATIM from the CV. Use the
       `title_to_lead_with` from the STRATEGY block above if present.
       Example: "Marketing and Communications specialist with 4+ years
       across global tech brands including Lenovo and SAP Labs India."

       ⭐ JD-THESIS OVERRIDE: if the STRATEGY block contains a "JD THESIS"
       line, do NOT open with the generic "<title> with <N> years"
       formula. Instead, OPEN by echoing the JD's thesis in the
       candidate's own true terms — name the identity and the focus this
       JD is really hiring for — then fold role-family / YoE / domain
       into the next sentence. This makes the summary's first line speak
       the JD's language. Use ONLY CV-true facts; invent nothing.

     PART 2 — JD-ALIGNED PROOF (two phrases, ~25-40 words):
       Two themes the JD prioritises THAT ARE ALREADY IN THE CV.
       Lead with the highest-priority match. Pull these themes from
       the STRATEGY's `must_include_phrases` list — those phrases are
       pre-verified to exist in this candidate's CV. Use the
       candidate's OWN words; do not invent new framing.

     PART 3 — SPECIFIC PROOF (one phrase, ~15-25 words):
       One concrete artefact, project, platform, or tool the candidate
       built/used that maps directly to a JD requirement. Name it
       VERBATIM from the CV (e.g. "VoC Insight Hub", "Adobe Experience
       Manager", "Sprinklr", "Lenovo Tech World 2025").

     PART 4 — CREDENTIAL ANCHOR (one phrase, ~8-15 words) —
       ⚠️  STRICTLY CONDITIONAL, READ CAREFULLY:

       Look at the CURRENT SUMMARY shown above. Does its LAST sentence
       already state a degree + institution (e.g. "MSc Management (2.1),
       Trinity College Dublin")?

         • YES → copy that exact degree + institution phrase into your
           rewrite, VERBATIM. Do not alter the degree name, grade, or
           university.
         • NO → the original summary has no education line. OMIT Part 4
           ENTIRELY. Your rewrite ends after Part 3. Three parts is a
           complete, correct summary.

       NEVER invent a degree, a university, or a city. Do NOT write
       "Bachelor's degree", do NOT guess a university from the
       candidate's location or employers. A fabricated credential is
       the worst possible error — it is an outright lie on the CV, the
       guard will catch it, and your ENTIRE summary rewrite will be
       discarded. When in doubt, omit Part 4.

   THE STRICT VOCABULARY RULE — CV-ONLY, WITH ONE EXCEPTION:
   Every noun-phrase / acronym / proper-noun / framework / methodology /
   metric you put in the summary MUST literally appear elsewhere in the
   CV outline shown above (in the existing summary, bullets, skills,
   role headers, or projects).

   THE ONE EXCEPTION — SAFE RELABELS: if the STRATEGY block lists
   "SAFE RELABELS", those JD terms are PRE-CLEARED. The strategist
   verified the CV genuinely shows that thing under a different word.
   You MAY use the JD term — it is honest tailoring, not fabrication,
   because the named CV evidence proves it. The mechanism is real; only
   the label changes. Nothing else outside the CV may be added.

   This includes generic-sounding business acronyms. If the CV does NOT
   contain "ROI", you cannot add "measuring ROI" to the summary even if
   the JD asks for it. If the CV does NOT contain "stakeholder management",
   you cannot say "managed stakeholders" in the summary. If the candidate
   has those skills, they will be in the CV. If they are not in the CV,
   adding them is FABRICATION — the guard will detect it and revert your
   entire rewrite to the original, wasting your output.

   The FORBIDDEN WORDS block near the top of this prompt names, in full,
   the JD words confirmed absent from THIS candidate's CV. Re-read that
   list before you write the summary — not one of those words belongs
   here. Build the summary only from vocabulary the CV already contains.

   ┌────────────────────────────────────────────────────────────────┐
   │ BEFORE YOU SUBMIT, RUN THIS SELF-CHECK FOR THE SUMMARY:        │
   │                                                                │
   │   1. List every noun-phrase, acronym, framework, tool, metric, │
   │      methodology, or domain word you have used.                │
   │   2. For each: is it literally in the CV outline above?        │
   │   3. If even ONE item is not in the CV, remove it OR rephrase  │
   │      with a CV-grounded alternative. If you can't, abandon the │
   │      rewrite and keep the original summary verbatim.           │
   └────────────────────────────────────────────────────────────────┘

   WHY THIS MATTERS: a CV without ROI in the original but with ROI in
   the rewrite isn't a "stronger" CV — it is a CV that lies. Recruiters
   probe inconsistencies during interview. A truthful, JD-aligned
   summary using ONLY CV-present vocabulary always wins over a
   buzzword-stuffed one that the candidate can't defend.

   ┌────────────────────────────────────────────────────────────────┐
   │ SUMMARY LENGTH — MATCH THE ORIGINAL (HARD CONSTRAINT)          │
   │                                                                │
   │   Original summary: {cur_word_count} words                     │
   │   Your rewrite MUST be between {cur_word_min} and {cur_word_max} words │
   │   — i.e. essentially THE SAME LENGTH as the original.          │
   │                                                                │
   │   This is deliberate: a tailored summary is the SAME size as   │
   │   the original, just RETARGETED. You are not summarising or    │
   │   trimming — you are re-aiming the same amount of content at   │
   │   THIS job. If your draft comes out short, you have dropped    │
   │   CV detail — add it back: name more JD-relevant tools,        │
   │   platforms, methods, outcomes that are already in the CV      │
   │   until you reach the original word count.                     │
   │                                                                │
   │   Below the floor → blank gap in the PDF, reads under-baked,   │
   │   reverts to original. Above the ceiling → overflows the       │
   │   layout, reverts. Land INSIDE the band.                       │
   └────────────────────────────────────────────────────────────────┘

   MUST-PRESERVE CREDENTIALS (HARD — drop one and your rewrite is
   reverted):
     • Degree grade/classification (e.g. "(2.1)", "First Class",
       "Distinction", "GPA 3.8", "Magna Cum Laude").
     • University and company/employer names exactly as written.
     • Years of experience claim (e.g. "4+ years", "5 years").
     • Numeric outcomes already in the original summary (percentages,
       scale, headcount, revenue).
     • Job-title language identifying the candidate's specialism.

   TONE: confident, specific. BANNED words (mark of buzzword filler):
   "dynamic", "passionate", "results-driven", "synergy", "innovative",
   "thought leader", "rockstar", "ninja", "guru".

   ┌────────────────────────────────────────────────────────────────┐
   │ WORKED EXAMPLE — using PLACEHOLDERS, not real names            │
   │ (DO NOT copy these placeholder strings into your output —      │
   │  they are illustrative only)                                   │
   ├────────────────────────────────────────────────────────────────┤
   │ Pretend the CV outline contains:                               │
   │   role-family: "<ROLE-FAMILY>" (e.g. Senior Account Executive) │
   │   YoE: "<N>+ years"                                            │
   │   employers: <EMPLOYER-A>, <EMPLOYER-B>                        │
   │   tools/platforms: <TOOL-A>, <TOOL-B>                          │
   │   project: <FLAGSHIP-PROJECT-NAME>                             │
   │   degree: <DEGREE-NAME>, <UNIVERSITY-NAME>                     │
   │                                                                │
   │ Pretend the JD priorities are:                                 │
   │   <THEME-A>, <THEME-B>, <THEME-C>                              │
   │                                                                │
   │ Pretend <THEME-A> and <THEME-B> ARE in the CV (vocabulary      │
   │ palette confirms it).                                          │
   │                                                                │
   │ Then a strong rewrite would look like (template, NOT verbatim):│
   │ "<ROLE-FAMILY> with <N>+ years delivering <THEME-A> and        │
   │ <THEME-B> for <EMPLOYER-A> and <EMPLOYER-B>. Built <CV-PROOF   │
   │ phrase that the CV literally says> measured by <CV-METRIC      │
   │ that the CV literally says>. Led <FLAGSHIP-PROJECT-NAME>       │
   │ delivering <CV-OUTCOME>. <DEGREE-NAME>, <UNIVERSITY-NAME>."    │
   │                                                                │
   │ Why this template works:                                       │
   │   ✓ Part 1: identity = role-family + YoE + employer domain     │
   │   ✓ Part 2: TWO JD-aligned themes BOTH already in CV vocab     │
   │   ✓ Part 3: concrete proof (project name + metric, both        │
   │     verbatim from CV)                                          │
   │   ✓ Part 4: credential anchor (degree + university, verbatim)  │
   │   ✓ ZERO invented terms — every <PLACEHOLDER> resolves to a    │
   │     literal CV string, never a JD-only term                    │
   │                                                                │
   │ FILL THESE PLACEHOLDERS FROM THE ACTUAL CV ABOVE. Do not       │
   │ copy the placeholder strings into your output verbatim — and   │
   │ do not invent values when the CV doesn't supply them. If the   │
   │ CV doesn't list a degree, OMIT Part 4 entirely.                │
   └────────────────────────────────────────────────────────────────┘

2. bullets:
   For each role, return a list of bullet objects.

   REWRITING IS THE PRIMARY JOB OF THIS STEP — the user came to this tool
   explicitly asking for a per-JD tailored CV. A CV with 0-2 rewritten bullets
   across all roles is a FAILED tailoring — do not ship it.

   WHAT A GENUINE TAILORED REWRITE IS:
   You re-aim the SAME FACTS at THIS job. Every number, proper noun and
   factual claim stays exactly as in the original — but you pick a strong
   verb that mirrors the JD, move the most JD-relevant fact or outcome to
   the front, and re-word the framing so a 6-second scan lands on the
   match. Re-wording is the job; the facts are fixed points you write
   around. Moving a comma or swapping one article is NOT a rewrite.

     BEFORE: "Orchestrated always-on, multi-platform campaigns with
              video-first digital branding execution, delivering high
              reach and efficiency"
     AFTER:  "Delivered high reach and efficiency through always-on,
              multi-platform campaigns and video-first digital branding
              execution"
     WHY:    the outcome ("high reach and efficiency") moves to the
              front, the verb is sharpened and the framing re-aimed,
              yet every fact is the original's. THAT is a rewrite.

   The facts are LOCKED; the language around them is yours to re-aim.

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

   ╔══════════════════════════════════════════════════════════════════╗
   ║  THE PRESERVATION RULE — the single most important rule here.      ║
   ║  Tailoring RE-FRAMES a bullet. It NEVER REMOVES from it.           ║
   ║  Your rewrite must contain EVERY concrete fact the original had:   ║
   ║  every number, every acronym (PRD, MVP, RICE, JTBD, SLA…), every   ║
   ║  proper noun (project/client/tool names), every named method      ║
   ║  ("sprint-over-sprint", "JTBD-driven discovery"). You may ADD      ║
   ║  JD framing; you may NOT drop, omit, or generalise away a          ║
   ║  specific. "Prioritised MVP feature set" must NOT become "Made     ║
   ║  prioritisation decisions" — that DROPS "MVP feature set".         ║
   ║  A rewrite that drops a concrete term is REJECTED by the guard     ║
   ║  and reverts to the original. If you cannot re-aim a bullet at     ║
   ║  the JD without dropping a fact, KEEP IT UNCHANGED — that is the   ║
   ║  correct answer, never a failure.                                  ║
   ╚══════════════════════════════════════════════════════════════════╝

   You MUST NOT:
   - DROP / OMIT any bullet. Every original bullet MUST appear in your output exactly once.
     If a bullet is not JD-relevant, keep it verbatim (text=null) rather than removing it.
   - DROP a concrete fact WITHIN a bullet — a number, acronym, proper noun, named tool,
     named method, or specific scope the original bullet contained. Re-frame around it,
     never delete it. This is the #1 cause of a bad tailored CV.
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
   - SAFE RELABELS: if the STRATEGY block lists SAFE RELABELS, you MAY use
     those JD terms inside a bullet where they fit naturally — they are
     CV-proven, pre-cleared vocabulary. Surfacing a relabel into the
     relevant HOT-ZONE bullet is exactly the tailoring this JD rewards.
   - THEMED LABEL (HOT-ZONE bullets only): for a bullet inside a hot-zone
     project, you MAY open the rewrite with a short "Label: " prefix that
     names the JD requirement the bullet proves — e.g. "Evals: …",
     "Guardrails: …", "Observability: …". Use it ONLY when the label is a
     real JD requirement AND the bullet genuinely demonstrates it. The
     label counts toward the length budget — tighten the body to keep the
     rewrite the SAME length as the original. Never add themed labels to
     ordinary (non-hot-zone) roles.
   - LENGTH — MATCH THE ORIGINAL, BOTH DIRECTIONS:
     A tailored bullet RE-WORDS the original's content end to end — it does
     not add to it and does not trim it. So its length equals the
     original's. Each bullet shows its word count as [keep ≈N words]; your
     rewrite has N words, give or take two, NEITHER longer NOR shorter.
     Two ways to get REJECTED — avoid BOTH:
       - TOO LONG: you appended a clause, a second outcome, or extra JD
         detail not in the original bullet. That is padding; it overflows
         the bullet's fixed slot.
       - TOO SHORT: you compressed or dropped part of the original's
         content. The rewrite under-fills the slot and leaves a gap.
     A long bullet's rewrite is long — re-word the WHOLE thing, every clause
     of it. A short bullet's rewrite is short. Re-word every part of the
     original in the JD's language, keeping every number and proper noun,
     and the length takes care of itself.
     If you cannot re-aim a bullet at the JD at its original length, keep
     it unchanged (text=null) rather than padding or trimming it.

2a. EVERY STRATEGY-TARGETED BULLET MUST GENUINELY CHANGE.
    A `text` that is identical to the original — or differs only in
    whitespace, case, punctuation or articles — is NOT a rewrite. A
    post-processor normalises those away and demotes them to `text=null`,
    so a near-copy counts as ZERO tailoring.
    Every bullet the STRATEGY targets MUST come back genuinely re-worded:
    a lead verb that mirrors the JD, the JD-relevant fact or outcome
    surfaced near the front, and the connective / framing language
    re-aimed. Keep every number and proper noun verbatim while you do
    this — that fixes the FACTS, not the wording.
    `text=null` (keep verbatim) is the correct answer ONLY for bullets
    the strategy did NOT target. For a TARGETED bullet, returning the
    original is a failed instruction — re-word it until it is a genuine
    rewrite.

2b. STYLE BAN — ZERO em-dashes (—) and ZERO en-dashes (–) in any rewrite
    (summary OR bullets). They are the #1 stylistic tell of LLM-written
    prose. Use commas, semicolons, colons, parentheses, or full stops
    instead. Hyphens (-) inside compound words (data-driven, end-to-end,
    cross-functional) are fine; standalone dashes between clauses are not.
    A post-processor strips any dashes you emit, but a sentence built
    around a dash will read awkwardly after stripping — write without
    them from the start.

   ┌────────────────────────────────────────────────────────────────┐
   │ TAILORING MOVES — worked micro-examples                         │
   │ (illustrative only; <ALL-CAPS> are placeholders — never copy    │
   │  these strings, and never copy facts from this box into a CV)   │
   ├────────────────────────────────────────────────────────────────┤
   │ VERB-LEAD (the default bullet move):                            │
   │   Strategy says LEAD WITH "40% efficiency gain". Original:       │
   │   "Identified a bottleneck… and delivered 40% efficiency gain."  │
   │   GOOD: "Delivered 40% efficiency gain and 2x capacity by        │
   │   identifying a critical bottleneck…" → verb FIRST, fact in the  │
   │   opening clause.                                                │
   │   BAD:  "40% efficiency gain identified the bottleneck…" → bare  │
   │   noun first, verb stranded. Word salad. Rejected.               │
   │                                                                 │
   │ RELABEL (only if the strategy's SAFE RELABELS cleared it):      │
   │   Original bullet: "Set up a process that checks each output    │
   │   before release."  JD term: "guardrail".                       │
   │   Rewrite: "Built a guardrail that checks each output before    │
   │   release."  → same fact, the JD's word, same length.           │
   │                                                                 │
   │ THESIS-MIRROR (summary opener):                                 │
   │   JD thesis: "turn prototypes into production".                 │
   │   Generic opener "<ROLE> with <N> years…" becomes               │
   │   "<ROLE> who turns prototypes into production systems, with    │
   │   <N> years…"  — ONLY because the CV genuinely shows shipped    │
   │   production work.                                              │
   │                                                                 │
   │ THEMED LABEL (hot-zone bullet only):                            │
   │   Original: "Ran weekly checks on model output quality."        │
   │   Rewrite: "Evals: ran weekly quality checks on model output."  │
   │   → label names the JD requirement; body tightened to hold      │
   │   the same length.                                              │
   │                                                                 │
   │ LEAVE-ALONE: a bullet already leading with the JD's priority,   │
   │   or from a role the JD does not care about → text=null.        │
   └────────────────────────────────────────────────────────────────┘

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

def _call_llm(prompt: str, max_tokens: int = 3000) -> str:
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
    # Item 15 (variance): temperature lowered 0.2 → 0.1. Tailoring is a
    # constrained re-framing task, not open creative writing — lower temp
    # cuts run-to-run wobble (Run 21/22 swung 3-9 shipped bullets on the
    # same input) without hurting rewrite quality.
    deepseek_result = chat_deepseek(
        prompt, max_tokens=max_tokens, temperature=0.1, json_mode=True
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
    # Run 19 audit fix #39: track the second LLM call. Previously only
    # the first track_llm_call fired, undercounting budget by 1 per
    # fallback. BudgetExceeded then triggered later than actual cost.
    track_llm_call(agent="cv_diff_tailor")
    return chat_quality(prompt, max_tokens=max_tokens, temperature=0.1)


# ─────────────────────────────────────────────────────────────
# Sanitise diff (unchanged)
# ─────────────────────────────────────────────────────────────

# Numeric-token guard regex.
#
# May 2026 (Run 8 fix): the previous pattern `\d[\d.,]*\s*%?|\d+K\+?|...`
# also matched bare integers ("15", "9", "2024"). Combined with the
# verbatim-preservation rule in `_rewrite_is_safe`, this caused legitimate
# rewrites to be reverted whenever the LLM compressed descriptive context
# like "takes ~15 minutes of CV tweaking" → "manual job-application work".
# Run 8 lost bullet 0 on 2/3 CVs to exactly this case.
#
# New pattern enforces ONLY tokens carrying an explicit outcome / scale /
# magnitude marker:
#
#   * percentages           5%, 15%, 30.5%
#   * currency              $50K, $5M, $300
#   * unit-suffixed scale   600K, 5M, 1B, 150K+ (also "K+", "M+")
#   * count-with-plus       3+, 150+ (3+ years, 150+ tickets)
#
# Bare integers are NOT enforced. The LLM may freely drop or rephrase
# "9 guardrails", "~15 minutes", "2024" — these are descriptive, not
# outcome metrics. Outcome metrics carry suffixes by convention.
_NUMBER_RX = re.compile(
    r"\d[\d.,]*%"            # percentages
    r"|\$\d[\d.,]*[KMB]?"    # currency (optionally suffixed)
    r"|\d+[KMB]\+?"          # scale: 600K, 5M, 1B, 150K+
    r"|\d{1,4}\+",           # count-with-plus: 3+, 150+
    re.I,
)

# Run-17 audit fix #28: split the credential check from the general
# number-token check. The bullet-level guard (_rewrite_is_safe) still
# enforces every _NUMBER_RX token — those are outcome metrics tied to a
# specific bullet's claim. The SUMMARY-level credential check should only
# enforce tokens that are genuinely credentials (percentages, currency)
# and not generic scale tokens like "200+" or "600K". A summary that
# honestly compresses "Led 30% revenue growth across 200+ events and a
# $5M portfolio" to "Led 30% revenue growth on a $5M portfolio for
# enterprise clients" loses "200+" — that's scale, not a credential. The
# new regex keeps % and $ enforcement; the bullet check still preserves
# all numbers verbatim because that's where outcomes live.
_CREDENTIAL_NUMBER_RX = re.compile(
    r"\d[\d.,]*%"            # percentages (outcome credentials)
    r"|\$\d[\d.,]*[KMB]?",   # currency (financial credentials)
    re.I,
)


# Em-dash / en-dash normaliser for LLM-produced rewrites.
#
# May 2026: Run 8 evidence — DeepSeek and Llama both ignore the prompt-
# level "no em-dash" rule about 80% of the time. Em-dashes (—) and
# en-dashes (–) are the #1 stylistic tell of LLM-written prose; ATS
# tools and human readers both pattern-match on them. We strip them
# deterministically from every accepted rewrite (summary + bullets) so
# the shipped CV reads like a human wrote it.
#
# Replacement: dash → ", " (comma + space). Preserves sentence rhythm
# while erasing the dash signature. Hyphens (-) inside compound words
# like "data-driven" are untouched — only U+2014 (em-dash) and U+2013
# (en-dash) are matched.
#
# IMPORTANT: dashes BETWEEN two digits (numeric ranges like "5–7%",
# "2–3 minutes", "0–100 score") are preserved. Those are meaningful
# range punctuation, not the LLM-tell variety. The negative-lookbehind
# / negative-lookahead anchors handle the discrimination.
_DASH_RX = re.compile(r"(?<!\d)\s*[\u2014\u2013]\s*(?!\d)")


def _strip_em_en_dashes_text(s: str) -> str:
    """
    Replace every em-dash / en-dash in `s` with ", " and tidy any
    artefacts (",, ", ", ."). Returns the cleaned string. Empty input
    is returned unchanged.
    """
    if not s:
        return s
    t = _DASH_RX.sub(", ", s)
    t = re.sub(r",\s*,", ",", t)
    t = re.sub(r",\s*([.!?])", r"\1", t)
    return t


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
        return {"grades": [], "yoe": [], "numbers": [], "awards": []}
    grades: List[str] = []
    for rx in _GRADE_PATTERNS:
        for m in rx.finditer(summary):
            grades.append(m.group(0).strip().lower())
    yoe = [m.group(0).strip().lower() for m in _YOE_RX.finditer(summary)]
    # Run-17 audit fix #28: use the credential-class regex (% + currency only)
    # for summary preservation. Scale tokens like "200+" or "600K" can be
    # legitimately compressed when reframing the summary for a JD.
    numbers = [m.group(0).strip().lower() for m in _CREDENTIAL_NUMBER_RX.finditer(summary)]
    # Awards / recognition (Run 23 fix): the Archer summary silently
    # dropped "Accenture Kudos and Spotlight Awards" — an achievement
    # signal recruiters value. Capture distinctive capitalised words
    # within 4 tokens before "Award"/"Awards".
    awards: List[str] = []
    if "award" in summary.lower():
        _w = summary.split()
        for i, tok in enumerate(_w):
            if re.sub(r"[^a-z]", "", tok.lower()) in ("award", "awards"):
                for j in range(max(0, i - 4), i):
                    core = re.sub(r"[^A-Za-z]", "", _w[j])
                    if (len(core) >= 3 and core[0].isupper()
                            and core.lower() not in ("and", "the", "for", "with")):
                        awards.append(core.lower())
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
        "awards":  _dedupe(awards),
    }


def _summary_absorbed_bullet(summary: str, outline: Dict[str, Any]) -> bool:
    """
    True when an expanded summary has SWALLOWED a CV bullet — i.e. the LLM
    padded the summary to hit a word-count target by appending bullet text
    instead of genuinely expanding the prose. Observed in Run 21: the
    summary expand-retry produced "…Trinity College Dublin. Drove 5% user
    growth and 15% retention improvement…" — the trailing sentence was an
    IBM bullet glued on.

    EVERY sentence is checked (Run 22: the absorbed bullet sat mid-summary,
    sentence 4 of 6 — the old tail-only window missed it). A genuine
    summary sentence paraphrases; it does not >=75%-overlap a bullet
    verbatim, so this threshold does not false-positive on real prose.
    """
    if not summary:
        return False
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", summary) if s.strip()]

    def _toks(t: str) -> set:
        return set(re.sub(r"[^a-z0-9 ]+", " ", (t or "").lower()).split())

    for role in (outline.get("roles") or []):
        for b in (role.get("bullets") or []):
            bt = (b.get("text") if isinstance(b, dict) else str(b)) or ""
            bw = _toks(bt)
            if len(bw) < 6:
                continue
            for s in sents:
                sw = _toks(s)
                if len(sw) < 6:
                    continue
                if len(sw & bw) / len(sw) >= 0.75:
                    return True
    return False


def _summary_dropped_project(
    orig_summary: str, new_summary: str, outline: Dict[str, Any]
) -> Optional[str]:
    """
    Returns a named project that the ORIGINAL summary mentioned but the
    tailored summary dropped — or None. Run 22: the Archer summary
    silently dropped "ApplySmart AI", the candidate's flagship shipped
    project. A shipped project is a proof point; the re-aim may
    de-emphasise it but must not delete the name.
    """
    if not orig_summary or not new_summary:
        return None
    o_l, n_l = orig_summary.lower(), new_summary.lower()
    for role in (outline.get("roles") or []):
        if "project" not in str(role.get("section", "")).lower():
            continue
        header = (role.get("header") or "").strip()
        # project name = text before "|" or "Tech Stack" or a double-space
        name = re.split(r"\s*\|\s*|\s{2,}|\bTech Stack\b", header)[0].strip()
        if len(name) < 4:
            continue
        if name.lower() in o_l and name.lower() not in n_l:
            return name
    return None


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
    for kind in ("grades", "yoe", "numbers", "awards"):
        # May 2026 fix: case-insensitive check so "600K+" matches "600k+"
        gone = [tok for tok in orig.get(kind, []) if tok.lower() not in new_text_lower]
        if gone:
            missing[kind] = gone
    return missing or None


def _is_english_text(text: str, min_ascii_ratio: float = 0.85) -> bool:
    """
    Heuristic check to determine if text is predominantly English using
    ASCII character ratio. Non-English languages (e.g., Chinese, Arabic,
    Cyrillic scripts) have low ASCII ratios. Returns True if the text
    appears to be English, False otherwise.
    
    Args:
        text: The text to check.
        min_ascii_ratio: Minimum ratio of ASCII characters to consider text English.
                         Default 0.85 means at least 85% of characters must be ASCII.
    
    Returns:
        True if text appears to be English, False if likely non-English.
    """
    if not text:
        return True  # Empty text is considered "safe"
    
    # Count ASCII characters (ordinal < 128)
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    ascii_ratio = ascii_chars / max(1, len(text))
    
    return ascii_ratio >= min_ascii_ratio


def _compress_long_jd(job_description: str, max_words: int = 800) -> str:
    """
    Compress a very long job description by extracting key sections.
    When the JD exceeds max_words, this function extracts the most
    relevant sections (requirements, responsibilities, qualifications)
    and drops boilerplate (company descriptions, equal opportunity
    statements, etc.) to reduce token usage while preserving signal.

    Uses a simple heuristic: looks for common section headers and
    extracts content from them, up to the word limit.
    """
    if not job_description:
        return job_description
    
    jd_lower = job_description.lower()
    word_count = len(job_description.split())
    
    # If JD is already under the limit, return as-is
    if word_count <= max_words:
        return job_description
    
    # Common section headers in JDs (case-insensitive)
    section_patterns = [
        r"requirements?:?",
        r"qualifications?:?",
        r"responsibilities?:?",
        r"what you'll do",
        r"what we're looking for",
        r"you will:",
        r"you should:",
        r"key responsibilities",
        r"required skills",
        r"preferred skills",
        r"about the role",
        r"role overview",
        r"your role",
    ]
    
    # Try to extract key sections
    sections: List[str] = []
    
    for pattern in section_patterns:
        # Look for the section header
        match = re.search(pattern, jd_lower, re.IGNORECASE)
        if match:
            start = match.start()
            # Find the end of this section (next major header or end of text)
            remaining = jd_lower[start + len(match.group(0)):]
            next_section_match = re.search(
                r"\n\s*(?:requirements?:?|qualifications?:?|responsibilities?:?|benefits?:?|about us:|company:|what we offer)",
                remaining,
                re.IGNORECASE
            )
            if next_section_match:
                end = start + len(match.group(0)) + next_section_match.start()
                section_text = job_description[start:end].strip()
            else:
                # Take the rest of the text from this section
                section_text = job_description[start:].strip()
            
            if section_text and len(section_text.split()) > 10:
                sections.append(section_text)
    
    # If we found sections, concatenate them up to the word limit
    if sections:
        compressed = "\n\n".join(sections)
        # Still respect the word limit
        words = compressed.split()
        if len(words) > max_words:
            compressed = " ".join(words[:max_words]) + "..."
        return compressed
    
    # Fallback: if no sections found, just truncate to max_words
    words = job_description.split()
    return " ".join(words[:max_words]) + "..."

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


def _cv_vocabulary(outline: Dict[str, Any], cv_full_text: str = "") -> set:
    """
    Build a lowercased token+bigram vocabulary from everything in the CV
    (outline summary/bullets/skills/headers AND raw PDF text). Used by
    `_foreign_capitalized_terms` to decide whether a summary rewrite
    introduced terms not present in the CV.

    Run-17 audit fix #9: previously this returned a 1-element set
    containing the full concatenated text. Callers then did substring
    matching against that single blob — every "vocabulary check" was a
    plain `in` on the entire CV string, with no word boundaries. A
    coincidental letter sequence (e.g. "Stamp 1G" inside an unrelated
    bullet) could whitelist itself for the summary. Now we return an
    actual set of tokens + bigrams, and callers do real word-level
    membership checks.
    """
    parts: List[str] = []
    parts.append(outline.get("summary") or "")
    for r in outline.get("roles", []) or []:
        parts.append(r.get("header") or "")
        for b in r.get("bullets") or []:
            if isinstance(b, dict):
                parts.append(b.get("text") or "")
            elif isinstance(b, str):
                parts.append(b)
    skills = outline.get("skills")
    if isinstance(skills, list):
        parts.extend(s for s in skills if isinstance(s, str))
    elif isinstance(skills, str):
        parts.append(skills)
    if cv_full_text:
        parts.append(cv_full_text)

    text = " ".join(parts).lower()
    # Tokenise on non-alphanumeric (keep word boundaries). Drop short
    # tokens that match too freely.
    tokens = {w for w in re.split(r"[^a-z0-9+]+", text) if len(w) >= 2}
    # Also emit bigrams so phrases like "machine learning", "data science"
    # match cleanly even if individual words appear elsewhere.
    words = [w for w in re.split(r"[^a-z0-9+]+", text) if w]
    bigrams = {f"{words[i]} {words[i+1]}" for i in range(len(words) - 1)}

    # Keep the full-text blob too so callers that want substring fallback
    # can grab it via `next(iter(...))` — but the standard membership
    # check should use the token/bigram sets.
    return tokens | bigrams | {text}


# ─────────────────────────────────────────────────────────────
# JD-only vocabulary — PROACTIVE fabrication prevention (May 2026).
#
# The model is tempted to inject JD keywords ("influencer", "ROI",
# "content partnerships") that read on-target for the job but appear
# NOWHERE in the candidate's CV. Writing them is fabrication.
#
# We do NOT catch this after the fact and revert. Reverting every bullet
# that touched a JD term would collapse the tailoring rate — many
# rewrites lost, an un-tailored CV shipped. Instead we TRAIN the model
# up front: compute, deterministically, the JD words absent from the CV
# and hand the model that explicit "forbidden words" list in the prompt
# so it never writes them in the first place.
# ─────────────────────────────────────────────────────────────

# Pure verbs / adjectives / JD-meta words / stopwords. Using one of these
# is a writing choice, not a claim of experience — so they must never be
# flagged as fabrication even when absent from the CV. Skill / tool /
# domain NOUNS are deliberately NOT listed here: adding a noun the CV
# lacks IS fabrication and must stay flaggable.
_JD_LEAK_NONSKILL: frozenset = frozenset("""
the a an and or nor but yet so for of in on at to from with by as is are was
were be been being have has had do does did will would shall should can could
may might must this that these those it its we you your they them their our he
she his her not no all any each both few more most much many other such same
own very just only also than too then else when where why how who whom which
what while about into out up down over under again further once here there etc
via per eg ie new
description responsibilities responsibility qualification qualifications
requirement requirements department location salary benefits opportunity
opportunities environment culture candidate candidates applicant applicants
position positions vacancy role roles job jobs month months week weeks day
days year years time office remote hybrid company companies team teams ideal
preferred required responsible looking seeking join hire hiring apply applying
offer offers overview summary
drive drives drove driven driving deliver delivers delivered delivering lead
leads led leading manage manages managed managing build builds built building
create creates created creating develop develops developed developing execute
executes executed executing own owns owned owning run runs ran running support
supports supported supporting ensure ensures ensured ensuring provide provides
provided providing work works worked working help helps helped helping enable
enables enabled identify identifies identified analyse analyses analysed analyze
analyzes analyzed grow grows grew growing scale scales scaled scaling improve
improves improved improving launch launches launched launching optimise
optimises optimised optimize optimizes optimized plan plans planned planning
report reports reported reporting define defines defined defining maintain
maintains maintained handle handles handled make makes made meet meets meeting
collaborate collaborates collaborated collaborating coordinate coordinates
coordinated communicate communicates communicated assist assists assisted use
uses used using bring brings want wants need needs include includes included
including across within strong excellent proven dynamic passionate skilled
experienced senior junior relevant various multiple several great good better
best key core overall effective effectively highly well able ability
measure measures measured measuring creation bringing brought set sets setting
nurture nurtures nurtured nurturing develop suggest suggests suggested
""".split())


def _safe_relabels(strategy: Optional[Dict[str, Any]]) -> list:
    """The strategist's `safe_relabels` — JD terms it judged to be the
    industry-standard label for a skill the CV genuinely demonstrates under
    a DIFFERENT word. Returns [(jd_term, cv_evidence), ...] for well-formed
    entries. These terms are exempt from the forbidden-vocabulary guards:
    using one re-labels real CV experience, it does not fabricate."""
    out: list = []
    for entry in (strategy or {}).get("safe_relabels") or []:
        if isinstance(entry, dict):
            term = (entry.get("jd_term") or "").strip()
            ev   = (entry.get("cv_evidence") or "").strip()
        elif isinstance(entry, str):
            term, ev = entry.strip(), ""
        else:
            continue
        if term and len(term) >= 3:
            out.append((term, ev))
    return out


def _safe_relabel_wordset(relabels: list) -> set:
    """Content words (>=3 chars, lowercased) of every safe-relabel term."""
    ws: set = set()
    for term, _ in relabels:
        for w in re.findall(r"[A-Za-z][A-Za-z0-9+&-]{2,}", term.lower()):
            ws.add(w)
    return ws


def _all_words_safe(term: str, safe_ws: set) -> bool:
    """True when EVERY content word of `term` is a safe-relabel word — the
    term is fully covered by the strategist's whitelisted relabels."""
    words = re.findall(r"[A-Za-z][A-Za-z0-9+&-]{2,}", (term or "").lower())
    return bool(words) and all(w in safe_ws for w in words)


def _format_permitted_terms_block(relabels: list) -> str:
    """Render the 'permitted JD terms' block — strategist-whitelisted
    relabels the tailor MAY use. Empty string when there are none."""
    if not relabels:
        return ""
    rows = []
    for term, ev in relabels:
        rows.append(f"    {term}" + (f'  —  CV proof: "{ev}"' if ev else ""))
    body = "\n".join(rows)
    return (
        "\n\n"
        "╔══════════════════════════════════════════════════════════════════════╗\n"
        "║  PERMITTED JD TERMS — standard labels for THIS candidate's real work  ║\n"
        "╚══════════════════════════════════════════════════════════════════════╝\n"
        "\n"
        "The strategist verified each term below is the industry-standard label\n"
        "for a skill this candidate genuinely demonstrates in the CV, under a\n"
        "different word. You MAY use these terms to re-aim a bullet or the\n"
        "summary at the JD's language — this re-labels real experience, it does\n"
        "NOT fabricate, and it overrides the CV-only vocabulary rule for THESE\n"
        "terms only:\n"
        "\n"
        f"{body}\n"
        "\n"
        "Use a permitted term ONLY to relabel the matching CV experience above.\n"
        "It licenses no OTHER JD vocabulary — the FORBIDDEN list still stands.\n"
    )


def _jd_only_terms(
    job_description: str,
    outline: Dict[str, Any],
    cv_full_text: str = "",
    max_terms: int = 28,
) -> List[str]:
    """Deterministically list JD content words that appear NOWHERE in the CV.

    These are the words the model is most tempted to inject — JD keywords
    that sound on-target but name skills / tools / domains the candidate
    never claimed. The result drives a proactive "do not use these" block
    in the tailor prompt; it is NOT a revert guard. Ranking puts the most-
    repeated JD terms first (repetition == the JD's emphasis == the
    strongest pull on the model).
    """
    if not job_description or not job_description.strip():
        return []
    cv_vocab = _cv_vocabulary(outline, cv_full_text)

    freq: Dict[str, int] = {}
    for raw in re.findall(r"[A-Za-z][A-Za-z0-9+&-]{2,}", job_description):
        w = raw.lower().strip("-&+")
        if len(w) < 3 or w.isdigit() or w in _JD_LEAK_NONSKILL:
            continue
        freq[w] = freq.get(w, 0) + 1

    def _in_cv(w: str) -> bool:
        # token-set membership, tolerant of simple singular/plural drift
        if w in cv_vocab:
            return True
        sing = w[:-1] if w.endswith("s") else w
        return sing in cv_vocab or (w + "s") in cv_vocab

    jd_only = {w: c for w, c in freq.items() if not _in_cv(w)}
    if not jd_only:
        return []

    ranked = sorted(jd_only.items(), key=lambda kv: (-kv[1], kv[0]))
    strong = [w for w, c in ranked if c >= 2]
    # A tight, high-signal list trains far better than a long noisy one.
    # Prefer repeated terms; fall back to freq>=1 only for short JDs.
    terms = strong if len(strong) >= 6 else [w for w, _ in ranked]
    return terms[:max_terms]


def _merge_forbidden_terms(
    jd_only: List[str], strategy_dni: Optional[List[str]]
) -> List[str]:
    """Case-insensitive union of the deterministic JD-only list and the
    strategist's do_not_inject list. The strategist sometimes names multi-
    word phrases the token scan misses; the token scan catches generic JD
    words the strategist overlooks. Showing both gives the model the most
    complete picture."""
    out: List[str] = []
    seen: set = set()
    for term in list(jd_only) + list(strategy_dni or []):
        t = (term or "").strip()
        key = t.lower()
        if not t or len(t) < 3 or key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def _format_jd_only_terms_block(terms: List[str]) -> str:
    """Render the proactive 'forbidden words' block for the tailor prompt."""
    if not terms:
        return (
            "JD-ONLY VOCABULARY CHECK\n"
            "Every salient word in the job description above also appears "
            "somewhere in this candidate's CV — you have the full vocabulary "
            "palette to tailor with. Even so: never introduce a noun, tool, "
            "metric, framework, or domain term that is not already in the CV."
        )
    shown = ", ".join(terms)
    return (
        "╔══════════════════════════════════════════════════════════════════════╗\n"
        "║  FORBIDDEN WORDS — IN THE JOB DESCRIPTION, NOT IN THIS CANDIDATE'S CV  ║\n"
        "╚══════════════════════════════════════════════════════════════════════╝\n"
        "\n"
        "The words below were found in the job description above but appear\n"
        "NOWHERE in this candidate's CV — not in the summary, bullets, skills,\n"
        "projects, or role headers. They are the JOB's vocabulary, never the\n"
        "candidate's:\n"
        "\n"
        f"    {shown}\n"
        "\n"
        "DO NOT WRITE ANY OF THESE WORDS — not in the summary, not in a single\n"
        "bullet. Each one names a skill, tool, metric, or domain the candidate\n"
        "never claimed; writing it invents experience the candidate does not\n"
        "have. That is fabrication — the most serious error possible here.\n"
        "\n"
        "This OVERRIDES the STRATEGY block. If the strategy plan names one of\n"
        "these words in a target verb or keyword, the strategy is wrong on\n"
        "that point — do NOT use the word; re-angle the bullet with the\n"
        "candidate's real CV vocabulary instead.\n"
        "\n"
        "WRITE IT RIGHT THE FIRST TIME (there is no second pass — your first\n"
        "draft is what ships):\n"
        "  - Tailor by REORDERING and REFRAMING what the CV already says. Lead\n"
        "    with the candidate's real experience that is closest to the JD.\n"
        "  - When the JD stresses a theme, find the nearest thing the candidate\n"
        "    HAS actually done — already written in the CV — and put that\n"
        "    forward, phrased in the candidate's own existing words.\n"
        "  - If the candidate genuinely lacks a JD skill, stay silent about it.\n"
        "    An honest CV that omits a skill always beats a CV that claims one\n"
        "    the candidate cannot defend in an interview.\n"
        "  - A rewrite that NEEDS a forbidden word to make sense is the wrong\n"
        "    rewrite: pick a different angle the CV genuinely supports, or keep\n"
        "    that bullet unchanged (text=null). An honest untailored bullet\n"
        "    beats a tailored lie every time."
    )


def _scrub_strategy_jd_leak(strategy: Dict[str, Any], jd_only: List[str]) -> None:
    """Remove JD-only terms (words in the JD but absent from the CV) from
    the strategist's bullet targets, IN PLACE, before the strategy reaches
    the tailor.

    The strategist occasionally names a JD keyword the CV lacks
    ("influencer", "ROI") in a target_verb_phrase / target_keywords entry.
    Rendered into the BINDING strategy block, that instructs the tailor to
    fabricate. This pre-emptively cleans the PLAN — it is NOT a post-hoc
    revert of a finished rewrite; the bullet is still rewritten, just from
    a clean target.
    """
    bad = [t.lower() for t in (jd_only or []) if t and len(t) >= 3]
    if not bad:
        return
    try:
        rxs = [re.compile(rf"\b{re.escape(t)}\b", re.IGNORECASE) for t in bad]
    except re.error:
        return

    def _has_leak(s: str) -> bool:
        return any(rx.search(s or "") for rx in rxs)

    def _scrub_phrase(s: str) -> str:
        out = s or ""
        for rx in rxs:
            out = rx.sub(" ", out)
        out = re.sub(r"\s{2,}", " ", out).strip(" ,;-&/")
        # trim a dangling connective left behind by the removal
        out = re.sub(
            r"\s+(and|the|a|an|of|for|with|to|in|on)\s*$", "", out,
            flags=re.IGNORECASE,
        ).strip(" ,;-&/")
        return out

    bs = strategy.get("bullet_strategy")
    if not isinstance(bs, dict):
        return
    n_scrubbed = 0
    for actions in bs.values():
        for a in actions or []:
            if not isinstance(a, dict):
                continue
            lead = a.get("lead_with")
            if isinstance(lead, str) and _has_leak(lead):
                cleaned = _scrub_phrase(lead)
                # Too little left → blank it; the tailor then reorders
                # using the bullet's most JD-relevant fact on its own.
                a["lead_with"] = (
                    cleaned if len(cleaned.split()) >= 2 else ""
                )
                n_scrubbed += 1
    if n_scrubbed:
        print(
            f"   cv_diff_tailor: scrubbed JD-only term(s) from "
            f"{n_scrubbed} strategist bullet target(s) before tailoring."
        )


def _check_professional_identity_fabrication(orig_summary: str, new_summary: str, outline: Dict[str, Any]) -> Optional[str]:
    """
    Check if the summary introduces a professional identity not supported by the CV.
    
    Allows: "passionate about transitioning to X", "interested in exploring X"
    Allows: Highlighting experience that exists in bullets (even if framed differently)
    Allows: Role transitions within the same career family (e.g., Account Manager → Social Media Account Manager)
    Disallows: Adding completely NEW skills that don't exist at all in the CV
    
    Returns error message if fabrication detected, None otherwise.
    """
    if not orig_summary or not new_summary:
        return None
    
    # Extract bullet text from outline to check for related experience
    bullet_texts = []
    role_headers = []
    for role in outline.get("roles", []):
        header = (role.get("header") or role.get("title") or "").lower()
        if header:
            role_headers.append(header)
        for bullet in role.get("bullets", []):
            if isinstance(bullet, dict):
                bullet_texts.append(bullet.get("text", "").lower())
            elif isinstance(bullet, str):
                bullet_texts.append(bullet.lower())
    
    all_bullets_text = " ".join(bullet_texts)
    all_roles_text = " ".join(role_headers)
    
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
    
    # P1-5 (May 2026): Role-family awareness. Define role families to allow
    # transitions within the same career family. For example, Account Manager,
    # Social Media Account Manager, Senior Account Executive, and Account
    # Executive are all in the same "Account Management" family. Transitions
    # within a family should be allowed even if the exact title isn't in the CV.
    role_families = {
        "account_management": [
            "account manager", "account executive", "senior account executive",
            "social media account manager", "social media account executive",
            "client manager", "client executive", "customer success manager",
            "relationship manager", "key account manager"
        ],
        "marketing": [
            "marketing manager", "marketing executive", "digital marketing",
            "social media manager", "content marketing", "brand marketing",
            "marketing and communications", "communications manager",
            "marketing specialist", "marketing coordinator"
        ],
        "sales": [
            "sales manager", "sales executive", "business development",
            "sales representative", "account manager", "sales director",
            "enterprise sales", "sales engineer"
        ],
        "product": [
            "product manager", "product owner", "senior product manager",
            "technical product manager", "product lead", "associate product manager"
        ],
        "engineering": [
            "software engineer", "senior software engineer", "full stack engineer",
            "backend engineer", "frontend engineer", "devops engineer",
            "software developer", "principal engineer", "staff engineer"
        ],
        "data": [
            "data scientist", "data analyst", "data engineer",
            "machine learning engineer", "analytics engineer", "business analyst"
        ],
        "design": [
            "product designer", "ux designer", "ui designer", "design lead",
            "visual designer", "interaction designer", "service designer"
        ],
        "operations": [
            "operations manager", "operations analyst", "devops engineer",
            "site reliability engineer", "it operations", "business operations"
        ],
        "finance": [
            "financial analyst", "finance manager", "controller",
            "accountant", "fp&a analyst", "treasury analyst"
        ],
        "hr": [
            "hr manager", "recruiter", "talent acquisition", "people operations",
            "hr business partner", "recruiting coordinator"
        ],
        "consulting": [
            "consultant", "management consultant", "strategy consultant",
            "advisor", "principal consultant", "senior consultant"
        ]
    }
    
    # Check if any role in the CV belongs to a family
    cv_families = set()
    for family_name, titles in role_families.items():
        if any(title in all_roles_text for title in titles):
            cv_families.add(family_name)
    
    # Check if the new summary mentions a role title that belongs to any of the CV's families
    for family_name, titles in role_families.items():
        if family_name in cv_families:
            # If the CV has roles in this family, allow any title from this family in the summary
            if any(title in new_lower for title in titles):
                # This is a valid role-family transition - allow it
                return None
    
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


# Generic role / profession / seniority words. A capitalised phrase whose
# only CV-absent words are these is a job-title descriptor, NOT a fabricated
# proper noun (an invented university / employer / place). Such phrases are
# exempt from the foreign-term check below; role / domain identity is
# guarded separately by _check_professional_identity_fabrication.
_ROLE_TITLE_WORDS = {
    "manager", "builder", "engineer", "developer", "analyst", "designer",
    "specialist", "architect", "consultant", "strategist", "scientist",
    "researcher", "leader", "owner", "officer", "director", "associate",
    "principal", "coordinator", "administrator", "advisor", "adviser",
    "marketer", "expert", "professional", "practitioner", "generalist",
    "executive", "founder", "partner", "programmer", "technologist",
    "planner", "writer", "editor", "producer", "operator", "technician",
    "representative", "facilitator", "evangelist", "ambassador", "mentor",
    "coach", "lead", "head", "chief", "senior", "junior", "staff",
    "global", "regional", "intern", "trainee",
}


def _foreign_capitalized_terms(summary: str, cv_text_set: set,
                               exempt_words: set = frozenset()) -> List[str]:
    """
    Return a list of capitalized/acronym phrases that appear in `summary`
    but whose component words do NOT appear in any CV text. Stopwords are
    ignored.

    May 2026 (strict, hallucination-fix): a capitalised phrase is foreign
    if ANY of its substantive content words (≥4 chars, not a stopword) is
    entirely absent from the CV. The previous ">half missing AND has an
    acronym" rule let fabricated proper nouns slip through — e.g. the
    LLM invented "University of Mumbai" for a candidate whose CV says
    "St. Xavier's University, Kolkata": "University" IS in the CV so only
    1 of 2 words was missing (not >half), and there was no acronym, so
    the guard passed it. A fabricated institution / degree / place ALWAYS
    contains at least one CV-absent content word, so "any missing content
    word" catches it. Legitimate re-phrasings ("Integrated Marketing
    Communications" from "integrated marketing") still pass because every
    content word IS somewhere in the CV.

    Generic role / profession words (manager, builder, engineer, ...) are
    NOT treated as foreign: a job-title phrase is a common-noun role
    descriptor, never a fabricated proper noun. So "Product Builder" on a
    CV full of "Built ..." is fine. Role / domain identity changes are
    caught separately by _check_professional_identity_fabrication.
    """
    if not summary:
        return []
    # Run-17 audit fix #9: cv_text_set is now a real set (tokens + bigrams
    # + full text). Pick the longest element (= the full text blob) for
    # substring fallback; build cv_tokens directly from the set members
    # that look like words, which is a more accurate vocabulary check
    # than the previous "split the full text on \W+" approach.
    if not cv_text_set:
        return []
    cv_text = max(cv_text_set, key=len, default="")
    if not cv_text:
        return []
    foreign: List[str] = []
    seen: set = set()
    # Use the pre-built tokens from cv_text_set when available; fall back
    # to splitting the full text for legacy callers.
    cv_tokens = {t for t in cv_text_set if t and " " not in t and len(t) < 40}
    if not cv_tokens:
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
        # Strict rule (May 2026): a SUBSTANTIVE content word (≥4 chars,
        # not a generic stopword) that is entirely absent from the CV
        # makes the whole phrase foreign. This catches every fabricated
        # proper noun — invented universities, cities, degrees, employers,
        # certifications — because such an invention always introduces at
        # least one CV-absent content word. Short connectors ("of", "and",
        # "the") and digits are ignored.
        _PHRASE_STOPWORDS = {
            "of", "and", "the", "for", "with", "to", "in", "on", "at",
            "an", "a", "or", "by",
        }
        substantive_missing = [
            w for w in words
            if w not in cv_tokens
            and w not in cv_text
            and len(w) >= 4
            and w not in _PHRASE_STOPWORDS
            and w not in _ROLE_TITLE_WORDS
            and w not in exempt_words
            and not w.isdigit()
        ]
        if substantive_missing:
            foreign.append(term)
    return foreign

_MIN_BULLETS_PER_ROLE = 2
_REWRITE_LEN_MIN_RATIO = 0.45  # rewrite must be at least 45% of original length
# May 2026 fix (Claude spec): lowered from 0.50 to 0.45 to reduce false reverts
# on legitimate paraphrases that compress slightly.
#
# 99% format mode (final architecture, May 2026):
#   tightened the upper bound from 1.50 → 1.15.
#
# We now run PyMuPDF in-place edit with NO font shrinkage (the shrinkage
# ladder in pdf_editor._insert_fitted was removed). Any rewrite longer than
# the original's natural bbox is HARD-rejected by the editor — there is no
# silent "shrink to fit". So this ratio guard's job is to catch over-budget
# rewrites BEFORE we send them to the editor, where they would just bounce
# back and force a revert.
#
# Empirical mapping (PyMuPDF textbox fit at base_size):
#   1.00–1.10× : fits cleanly                    (target band)
#   1.10–1.15× : usually fits (word-length dependent)
#   1.15–1.20× : fits ~50% of the time (line-wrap luck)
#   1.20×+     : reliably overflows              (REJECT)
#
# May 2026 (per-bullet in-place renderer): the editor inserts each
# rewrite into the ORIGINAL bullet's slot with no page reflow. A rewrite
# longer than the original needs more lines than the slot has. The
# editor's line-gap ladder absorbs up to ~12% extra height, so a hard
# guard at 1.08 leaves the LLM a small margin while keeping virtually
# every accepted rewrite fitting its slot. The prompt budget shows 1.0×
# (original length) as the target; this guard is the safety net.
_REWRITE_LEN_MAX_RATIO = 1.08
# Small absolute grace on the ceiling (chars). A rewrite a few characters
# over the ratio ceiling (observed: 147 vs a 146 ceiling — a 1-char miss)
# almost always still word-wraps into the same slot. The apply-time slot
# check in pdf_editor does the EXACT word-wrap and reverts cleanly if a
# rewrite genuinely overflows — so this grace only lets true near-misses
# past the crude char-count pre-filter; it never ships an overflow.
_REWRITE_LEN_CEIL_GRACE = 4


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
    """Backwards-compat shim — delegates list ops to thread-local storage.

    Run-17 audit fix #16: the proxy supports list-like reads and method
    calls (.clear(), .append() via __getattr__) but ASSIGNMENT to the
    module name `_LAST_BULLET_REVERTS = [...]` would silently rebind the
    name and break thread-safety for every subsequent caller. We can't
    block module-level rebinding without metaclass tricks, but we CAN
    forbid attribute writes on the proxy itself to make accidental
    "self.foo = bar" style misuse fail loudly instead of silently
    corrupting the shared cache.
    """
    def __getattr__(self, name):
        return getattr(_get_bullet_reverts(), name)
    def __setattr__(self, name, value):
        raise AttributeError(
            f"_BulletRevertsProxy is read-only at the attribute level. "
            f"Call _LAST_BULLET_REVERTS.append(...) or .clear() instead "
            f"of assigning to '{name}'."
        )
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
    # Run 18 audit (Perplexity-flagged): "collaborating with stakeholders"
    # appeared in a solo-project bullet for ApplySmart AI on the Nike CV.
    # The existing list had "stakeholder alignment / aligned stakeholders /
    # managing stakeholders" but missed the "collaborating with" wording.
    "collaborating with stakeholders",
    "collaborated with stakeholders",
    "collaborating with the team",
    "collaborated with the team",
    "collaborating with engineering",
    "collaborated with engineering",
    "collaborating with cross-functional",
    "collaborated with cross-functional",
    "worked with engineering",
    "working with engineering",
    "worked with stakeholders",
    "working with stakeholders",
    "coordinating with",
    "coordinated with",
    "led a team",
    "managed a team",
    "leading a team",
    "managing a team",
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

    # Run-17 audit fix #21: don't revert when the original ALREADY hints at
    # collaboration via a synonym. A bullet "Built CLI shared with infra
    # colleagues" legitimately rewriting to "Built CLI adopted across
    # engineering teams" is honest paraphrase, not fabrication. Require
    # BOTH a banned phrase AND the absence of any collaborator noun in the
    # original before reverting.
    _COLLAB_HINTS = (
        "team", "colleague", "partner", "collaborat", "shared",
        "stakeholder", "engineer", "designer", "manager", "client",
        "with the", "with our", "with my", "with a ", "alongside",
        "joint", "co-built", "co-developed", "contributor",
    )
    if any(h in orig_l for h in _COLLAB_HINTS):
        # Original signals collaboration; rewriting with a banned phrase
        # is reframing, not fabrication. Skip the check.
        return None

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
#
# May 2026 fix (Claude spec): strategist sometimes mis-classifies CV terms
# as JD-only. Before rejecting, check if the term actually exists anywhere
# in the full CV text. If it does, accept it as a false positive.
# ─────────────────────────────────────────────────────────────
def _check_do_not_inject(
    rewrite:       str,
    original:      str,
    do_not_inject: List[str],
    cv_full_text:  str = "",
) -> Optional[str]:
    """Returns the offending term, or None when the rewrite is clean."""
    if not rewrite or not do_not_inject:
        return None
    # Run-17 audit fix #10: do NOT early-exit when cv_full_text is empty.
    # The previous behaviour silently disabled the guard whenever the PDF
    # parser returned "" (every DOCX upload), letting do_not_inject terms
    # leak in. The fallback below still uses original-bullet comparison
    # so something is enforced even without cv_full_text.
    new_lc  = (rewrite or "").lower()
    orig_lc = (original or "").lower()
    cv_lc   = (cv_full_text or "").lower()

    for raw_term in do_not_inject:
        term = (raw_term or "").strip().lower()
        if not term or len(term) < 3:
            continue
        # Run-17 audit fix #20: word-boundary match. The old substring
        # comparison made "API" match "rapid", "scraping", "snapshot",
        # silently reverting bullets that happened to contain those
        # letter sequences. Use a real word-boundary regex.
        try:
            rx = re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)
        except re.error:
            # Bad term from strategist — skip rather than crash.
            continue
        if rx.search(new_lc) and not rx.search(orig_lc):
            # If the term IS anywhere in the full CV text, the strategist
            # mis-classified it as JD-only. Accept the rewrite.
            if cv_lc and rx.search(cv_lc):
                continue
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


def _strip_banned_suffix_from_bullet(
    rewrite: str,
    suffix:  str,
) -> str:
    """
    Run 18 audit fix: instead of reverting the whole bullet when a banned
    suffix is detected, strip just the offending suffix and any leading
    connective ("and", "while", ", and", " — "). The rewritten content
    before the suffix is usually fine; only the cliché tail is bad.

    Returns the cleaned rewrite. If the strip would empty the bullet,
    returns the original rewrite unchanged so the caller can fall back
    to revert.
    """
    if not rewrite or not suffix:
        return rewrite

    rx = re.compile(
        # match optional leading connector before the suffix
        rf"(?:\s*[,;:]?\s*(?:and|while|by|through|whilst)\s+)?{re.escape(suffix)}",
        re.IGNORECASE,
    )
    stripped = rx.sub("", rewrite).strip()
    # Tidy trailing punctuation artefacts (", ." → ".", " ." → ".")
    stripped = re.sub(r"\s+([.,;:!?])", r"\1", stripped)
    stripped = re.sub(r"[,;:]\s*$", ".", stripped)
    # Ensure terminal punctuation
    if stripped and stripped[-1] not in ".!?":
        stripped = stripped + "."

    # If strip collapsed the bullet to almost nothing, give up — caller
    # will revert. Threshold: must be at least 25 chars or ≥30% of original
    # rewrite length, whichever is smaller.
    min_keep = min(25, int(len(rewrite) * 0.30))
    if len(stripped) < min_keep:
        return rewrite

    return stripped


def _check_content_preserved(original: str, rewrite: str) -> Optional[str]:
    """
    Preservation-first guard: a tailored bullet must RE-FRAME the original,
    never DROP its concrete facts. Returns the first concrete term from the
    original that is missing from the rewrite, or None when all are kept.

    Catches the Run-22 failure mode where the reframe dropped the
    candidate's own specifics to make room for a JD keyword:
      "Prioritised MVP feature set …"  → lost "MVP"
      "… sprint-over-sprint …"         → lost the named method
      "Authored initial PRDs …"        → lost "PRDs"

    Three concrete-term classes (plain numbers are checked separately in
    _rewrite_is_safe):
      (a) acronyms      — >=3 uppercase letters, optional trailing 's'
      (b) proper nouns  — mid-sentence capitalised words (projects,
                          clients, employers, tools, platforms)
      (c) distinctive hyphenated compounds — >=2 hyphens
    Reverting to the clean original on a drop is the safe outcome.
    """
    orig = original or ""
    new_l = (rewrite or "").lower()
    if not orig or not new_l:
        return None

    required: List[str] = []
    # (a) acronyms
    for m in re.finditer(r"\b([A-Z]{3,})s?\b", orig):
        required.append(m.group(1).lower())
    # (b) capitalised proper nouns (skip sentence-initial capitals)
    words = orig.split()
    for i, w in enumerate(words):
        core = re.sub(r"[^A-Za-z]", "", w)
        if len(core) < 3 or not core[0].isupper():
            continue
        if i == 0 or words[i - 1].rstrip().endswith((".", "!", "?", ":")):
            continue
        if core.isupper():               # already captured as an acronym
            continue
        required.append(core.lower())
    # (c) distinctive hyphenated compounds (>=2 hyphens, e.g. sprint-over-sprint)
    for m in re.finditer(r"\b\w+(?:-\w+){2,}\b", orig):
        for part in m.group(0).lower().split("-"):
            if len(part) >= 3:
                required.append(part)

    seen: set = set()
    for tok in required:
        if tok in seen:
            continue
        seen.add(tok)
        # stem-tolerant: accept singular/plural variants
        if tok in new_l or tok.rstrip("s") in new_l:
            continue
        return tok
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

    # May 2026 (same-length tailoring): the tailored bullet must occupy
    # roughly the SAME footprint as the original so the per-bullet
    # in-place editor can drop it into the original slot without reflow.
    # Band = 78%–108% of the original length.
    #   • Floor 0.78 — a rewrite much shorter than the original leaves a
    #     visible whitespace gap in its slot. A small absolute floor (45
    #     chars) keeps very short bullets rewritable.
    #   • Ceiling 1.08 (_REWRITE_LEN_MAX_RATIO) — longer than this risks
    #     an extra wrapped line that overflows the slot. A small absolute
    #     ceiling (95 chars) gives very short bullets room to JD-align.
    lo = max(45, round(orig_len * 0.78))
    # Ceiling carries a small absolute grace — a few chars over the ratio
    # still wraps into the same slot, and the apply-time slot check is the
    # real overflow gate. The floor stays strict (a short rewrite leaves a
    # visible gap, which the apply step cannot fix).
    hi = max(95, round(orig_len * _REWRITE_LEN_MAX_RATIO)) + _REWRITE_LEN_CEIL_GRACE
    # Guard against a degenerate band on tiny originals.
    if lo > hi:
        lo = min(lo, hi)

    if not (lo <= len(new) <= hi):
        return False, f"length {len(new)} outside {lo}-{hi}"
    orig_nums = {m.group(0).strip().lower() for m in _NUMBER_RX.finditer(orig)}
    new_text_l = new.lower()
    for tok in orig_nums:
        if not tok:
            continue
        # Direct verbatim presence (covers percentages, currency, K/M/B scale).
        if tok in new_text_l:
            continue
        # Run 18 audit fix: count-with-plus tokens like "200+" / "3+" are
        # SCALE markers (descriptive context), not credentials. Allow them
        # to be dropped entirely when the rewrite reframes the bullet — the
        # core claim survives and "Led 3+ initiatives" → "Led multiple
        # initiatives" is honest compression, not fabrication. Recruiters
        # don't reject a CV for losing "3+" so neither should our guard.
        # Credentials (% and $) are still strictly preserved below.
        m = re.fullmatch(r"(\d+)\+", tok)
        if m:
            # Audit fix #3 (kept): also accept natural English paraphrases
            # — "over 200", "more than 200", etc. — so if the LLM does
            # preserve the floor in prose form, we don't penalise it.
            n = m.group(1)
            paraphrases = (
                f"over {n}",
                f"more than {n}",
                f"{n} or more",
                f"at least {n}",
                f"upwards of {n}",
            )
            if any(p in new_text_l for p in paraphrases):
                continue
            # Drop-without-paraphrase is OK for count-with-plus tokens.
            # The bullet's core verb + outcome claim is what matters.
            continue
        # Run 18 audit fix: bare scale tokens without "+" (e.g. "600K",
        # "5M") are credential-class when paired with currency ($5M) but
        # descriptive when standalone (600K users). Currency-attached
        # scale is already matched by the first orig_nums check above —
        # if we're here with a bare K/M/B token, accept dropping it.
        if re.fullmatch(r"\d+[kmb]", tok, re.IGNORECASE):
            continue
        return False, f"number token {tok!r} missing"

    # Content-preservation guard (Run 22 fix) — a rewrite must RE-FRAME,
    # never DROP a concrete fact. Reverting to the clean original beats
    # shipping a bullet that lost the candidate's own specifics.
    _dropped = _check_content_preserved(orig, new)
    if _dropped:
        return False, f"dropped concrete term '{_dropped}'"

    # Stranded-verb / fragment-shuffle detector (Run 21 fix). A "rewrite"
    # that shoves a noun fragment to the front and leaves the original's
    # opening verb stranded mid-sentence — original "Scoped the
    # architecture as a supervisor + workers pattern…" mangled into
    # "Supervisor + workers pattern… scoped the architecture, so…" — is
    # broken grammar, not a rewrite. _rewrite_is_safe otherwise passes it
    # (length + numbers are fine). The tell: the original's leading verb
    # reappears in the rewrite immediately after a content word with no
    # conjunction / preposition before it.
    _ow = orig.split()
    if _ow:
        _w0 = re.sub(r"[^a-z]", "", _ow[0].lower())
        if len(_w0) >= 3:
            _rw = new.split()
            _conn = {
                "and", "or", "nor", "but", "then", "while", "whilst",
                "by", "to", "for", "with", "of", "after", "before",
                "through", "as", "into", "that", "which", "who",
            }
            for j in range(1, len(_rw)):
                if re.sub(r"[^a-z]", "", _rw[j].lower()) != _w0:
                    continue
                _prev = _rw[j - 1]
                _prev_clean = re.sub(r"[^a-z]", "", _prev.lower())
                if (_prev_clean not in _conn
                        and not _prev.rstrip().endswith((",", ";", ":"))):
                    return False, "stranded verb (broken grammar)"
                break

    return True, ""


def _normalise_bullet_list(
    order_raw:     Any,
    n_bullets:     int,
    orig_texts:    List[Any],
    section:       str = "experience",
    do_not_inject: Optional[List[str]] = None,
    cv_text:       str = "",
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
                # Strip em-dashes / en-dashes BEFORE the safety guards so
                # the length / number-token checks see the same string we
                # will eventually ship. Otherwise the guard runs on the
                # dash-containing version and the post-strip version
                # could end up shorter than the 50% length floor.
                t_clean = _strip_em_en_dashes_text(t.strip())
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
            # Identical-rewrite suppression (May 2026 / Run 12 fix).
            # Observed failure mode: the LLM submits `text` byte-identical
            # (or trivially whitespace/case-different) to the source bullet
            # and we count it as a rewrite in telemetry, inflating
            # n_rewrites while producing zero textual change in the PDF.
            # Treat it as "keep original" (text=None) so the metric
            # reflects reality and retries trigger when needed.
            def _norm_for_eq(s: str) -> str:
                # Near-identical detection (May 2026): a "rewrite" that only
                # adds a period, drops a comma, swaps a dash for a comma, or
                # drops an article ("the PR team" -> "PR team") is NOT a
                # rewrite \u2014 it is a cosmetic non-change. Counting it as a
                # rewrite re-renders a perfectly good bullet for zero gain
                # and inflates telemetry. Normalise away case, ALL
                # punctuation, and articles so these register as identical
                # and are demoted to text=None (the bullet is kept pristine).
                s_l = re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())
                return " ".join(
                    t for t in s_l.split() if t not in ("a", "an", "the")
                )
            if _norm_for_eq(text) == _norm_for_eq(orig_text):
                # Run-17 audit fix #7: log identical-rewrite suppressions
                # to the revert tracker so we can distinguish "LLM said the
                # rewrite was identical to original" from "rewrite genuinely
                # passed all guards". Both end up as text=None in the diff
                # but mean very different things for telemetry.
                _LAST_BULLET_REVERTS.append({
                    "bullet_index": idx,
                    "reason": "identical_rewrite",
                    "rewrite_preview": (text or "")[:120],
                })
                text = None
                normalised.append({"i": idx, "text": text})
                seen.add(idx)
                continue
            ok, reason = _rewrite_is_safe(orig_text, text, original_length=orig_len)
            if not ok:
                print(
                    f"   ⚠️  rewrite rejected (bullet {idx}, {reason}): "
                    f"{text[:80]!r} — reverting to original"
                )
                _LAST_BULLET_REVERTS.append({
                    "bullet_index": idx,
                    "reason": reason,
                    "rewrite_preview": text,
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
                        dni = _check_do_not_inject(text, orig_text, do_not_inject or [], cv_text)
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
                            # Run 18 audit: strip the suffix instead of
                            # reverting the whole bullet. The rewrite body is
                            # usually fine; only the cliché tail is bad.
                            bs = _check_banned_suffix_in_bullet(text, orig_text)
                            if bs:
                                stripped = _strip_banned_suffix_from_bullet(text, bs)
                                if stripped != text and stripped.strip():
                                    # Re-validate the stripped rewrite
                                    # against length + number guards.
                                    ok2, reason2 = _rewrite_is_safe(
                                        orig_text, stripped, original_length=orig_len
                                    )
                                    if ok2:
                                        print(
                                            f"   🧹 stripped banned suffix "
                                            f"{bs!r} from bullet {idx} "
                                            f"(kept {len(stripped)}/{len(text)} chars)"
                                        )
                                        text = stripped
                                    else:
                                        print(
                                            f"   ⚠️  rewrite rejected (bullet {idx}, "
                                            f"banned filler suffix {bs!r}, strip "
                                            f"failed length check): {text[:80]!r} "
                                            f"— reverting"
                                        )
                                        _LAST_BULLET_REVERTS.append({
                                            "bullet_index": idx,
                                            "reason": f"banned_suffix:{bs}",
                                            "rewrite_preview": text[:120],
                                        })
                                        text = None
                                else:
                                    print(
                                        f"   ⚠️  rewrite rejected (bullet {idx}, "
                                        f"banned filler suffix {bs!r}, strip "
                                        f"collapsed bullet): {text[:80]!r} — reverting"
                                    )
                                    _LAST_BULLET_REVERTS.append({
                                        "bullet_index": idx,
                                        "reason": f"banned_suffix:{bs}",
                                        "rewrite_preview": text[:120],
                                    })
                                    text = None
                            else:
                                # P1-2 (May 2026): Wrong-language detection for bullets.
                                # If the LLM produces a bullet rewrite in a non-English
                                # language (e.g., Chinese, Arabic, Cyrillic scripts),
                                # reject it and revert to the original. The ASCII ratio
                                # heuristic catches languages with non-Latin character sets.
                                if not _is_english_text(text, min_ascii_ratio=0.85):
                                    ascii_chars = sum(1 for c in text if ord(c) < 128)
                                    ascii_ratio = ascii_chars / max(1, len(text))
                                    print(
                                        f"   ⚠️  rewrite rejected (bullet {idx}, "
                                        f"wrong language ASCII ratio {ascii_ratio:.2f}): "
                                        f"{text[:80]!r} — reverting to original"
                                    )
                                    _LAST_BULLET_REVERTS.append({
                                        "bullet_index": idx,
                                        "reason": "wrong_language",
                                        "ascii_ratio": round(ascii_ratio, 3),
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


# ─────────────────────────────────────────────────────────────
# Token-overlap key matching (May 2026 / audit fix #2).
#
# The previous matching strategy (`startswith` / `in`) failed when the
# strategist re-framed a role label (e.g. "ApplySmart AI | AI-Powered
# Product" vs the real header "ApplySmart AI | Agentic AI Product …").
# All three prefix/contains checks miss because the divergent middle
# word ("AI-Powered" ↔ "Agentic AI") breaks substring continuity.
#
# Run 14 evidence: 2 of 4 role buckets ("ApplySmart AI | …",
# "Client: Elevance …") were emitted by the tailor LLM but failed to
# match any real role header → every bullet rewrite for those roles
# was silently dropped → bullets_rewritten=2/15 instead of ~8/15.
#
# This helper falls back to a *content-word overlap* score after the
# fast prefix paths fail. It treats role headers as bags of words
# (lowercased, stop-words removed, dates/punctuation stripped) and
# requires ≥60% of the LLM's content words to appear in some real
# header. The highest-scoring header wins; ties are broken by length
# (prefer shorter / more specific match).
# ─────────────────────────────────────────────────────────────
_KEY_MATCH_THRESHOLD = 0.60   # ≥60% of LLM key's content tokens must appear

# Words that carry no identifying signal — strip before scoring.
_KEY_STOPWORDS: frozenset = frozenset({
    "a", "an", "the", "and", "or", "of", "at", "in", "on", "for", "to",
    "with", "by", "as", "is", "are", "was", "were", "be", "been",
    "client", "role", "project", "company", "position", "title",
    "experience", "work", "team",
    # months — purely temporal, no role identity
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep",
    "oct", "nov", "dec",
    "january", "february", "march", "april", "june", "july",
    "august", "september", "october", "november", "december",
    "present", "current",
})


def _key_tokens(text: str) -> set:
    """Lowercase, split on non-alphanum, drop stopwords + pure-digit tokens."""
    if not text:
        return set()
    # Split on anything that is not alphanumeric.
    raw = re.split(r"[^a-z0-9]+", text.lower())
    return {
        w for w in raw
        if w
        and w not in _KEY_STOPWORDS
        and not w.isdigit()           # drop years / dates
        and len(w) >= 2               # drop single-char noise
    }


def _best_overlap_match(
    rk_l: str,
    real_roles: Dict[str, Dict[str, Any]],
) -> Optional[str]:
    """
    Find the real role header whose content tokens best cover the LLM key's
    content tokens. Returns the header (original case) if overlap ≥
    `_KEY_MATCH_THRESHOLD`, otherwise None.

    Used only after the cheap prefix/contains checks have failed.
    """
    rk_tokens = _key_tokens(rk_l)
    if not rk_tokens:
        return None
    best_score = 0.0
    best_header: Optional[str] = None
    best_len = 10**9
    for h_l, r in real_roles.items():
        h_tokens = _key_tokens(h_l)
        if not h_tokens:
            continue
        overlap = len(rk_tokens & h_tokens) / len(rk_tokens)
        if overlap < _KEY_MATCH_THRESHOLD:
            continue
        # Highest overlap wins; tie-break by shorter header (more specific).
        if overlap > best_score or (overlap == best_score and len(h_l) < best_len):
            best_score = overlap
            best_header = r["header"]
            best_len = len(h_l)
    return best_header


def _sanitise_diff(
    raw:           Dict[str, Any],
    outline:       Dict[str, Any],
    do_not_inject: Optional[List[str]] = None,
    cv_text:       str = "",
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
        # Strip em/en-dashes here so the credential + length guards
        # downstream evaluate the dash-free version we will actually
        # ship to the PDF editor.
        out["summary"] = _strip_em_en_dashes_text(s.strip())

    # Bullets
    real_roles = {r["header"].strip().lower(): r for r in outline.get("roles", [])}
    bullets_raw = raw.get("bullets") or {}

    # Run 19 audit fix #37: apply the strategist's section-key normaliser
    # to the tailor's bullets dict too. Previously the strategist normalised
    # its bullet_strategy keys (strips trailing metadata like "Tech Stack:
    # Python", drops META_KEYS like "ROLES:"/"PROJECTS:") but the tailor's
    # _sanitise_diff matched against raw role headers — if the LLM echoed
    # back a META key or a noisy header, the bullet entries were silently
    # dropped via the fuzzy-match fallback. Run 19 logs showed `unmatched=`
    # warnings that correspond exactly to these silent body losses.
    if isinstance(bullets_raw, dict) and bullets_raw:
        try:
            from agents.tailor_strategist import _normalise_section_keys
            bullets_raw = _normalise_section_keys(bullets_raw)
        except Exception:
            # If the import path drifts, fall back to raw keys — the
            # fuzzy match below still catches most cases.
            pass

    unmatched_keys: List[str] = []
    if isinstance(bullets_raw, dict):
        for rk, order in bullets_raw.items():
            if not isinstance(order, list):
                continue
            rk_l = str(rk).strip().lower()
            match_key = None
            if rk_l in real_roles:
                match_key = real_roles[rk_l]["header"]
            else:
                # Cheap prefix/contains pass first.
                for h_l, r in real_roles.items():
                    if h_l.startswith(rk_l) or rk_l.startswith(h_l) or rk_l in h_l:
                        match_key = r["header"]
                        break
                # Audit fix #2: fall back to content-word overlap when the
                # cheap pass misses. This catches re-framed labels like
                # "AI-Powered Product" vs "Agentic AI Product" where both
                # share ≥60% of identifying words ("ApplySmart", "AI",
                # "Product") despite the diverging middle word.
                if not match_key:
                    match_key = _best_overlap_match(rk_l, real_roles)
            if not match_key:
                unmatched_keys.append(str(rk))
                continue
            role = next(r for r in outline["roles"] if r["header"] == match_key)
            orig_texts = role.get("bullets") or []
            n = len(orig_texts)
            section = (role.get("section") or "experience").strip().lower()
            normalised = _normalise_bullet_list(
                order, n, orig_texts,
                section=section,
                do_not_inject=do_not_inject,
                cv_text=cv_text,
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

    # Observability (May 2026): surface section-key mismatches. If the
    # strategist/tailor produced bullet keys that none of the real role
    # headers matched (fuzzy or exact), log them with the list of real
    # headers so the regression is visible and actionable instead of
    # silently producing a zero-rewrite CV.
    if unmatched_keys:
        real_hdrs = [r["header"] for r in outline.get("roles", [])]
        print(
            f"   ⚠️  tailor: {len(unmatched_keys)} bullet section key(s) "
            f"did not match any role header. Unmatched={unmatched_keys!r} "
            f"Real headers={real_hdrs!r}"
        )
        # Run 19 audit fix #44: surface unmatched_keys to the supervisor
        # via _debug so it can be used as a retry signal. Previously this
        # was only printed — the supervisor had no way to know that the
        # tailor silently dropped half the LLM's intended rewrites.
        out.setdefault("_debug", {})["unmatched_keys"] = list(unmatched_keys)

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
    # Audit fix #4 (May 2026): re-assert the length cap on retry.
    # The reviewer's feedback often pushes the LLM toward verbose
    # restructurings ("Translated product strategy into epics, features,
    # user stories, leading 3+ concurrent…") that overshoot the 130%
    # hard cap. Without an explicit reminder on retry the post-processor
    # silently reverts the new bullets and we ship the original — the
    # reviewer then scores low again, retries run out, low score accepted.
    parts.append("")
    parts.append(
        "LENGTH CAP REMINDER (same as first pass — applies to this retry "
        "too): every rewritten bullet must be 50–150% of the original "
        "bullet's character count. If the reviewer's feedback requires a "
        "longer phrasing, COMPRESS by cutting adjectives or splitting one "
        "bullet's claim across a semicolon rather than exceeding 150%. "
        "The post-processor will silently revert any bullet outside the "
        "50–150% band, which is the exact failure mode that triggered "
        "this retry."
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
    cv_full_text:    str = "",
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
      cv_full_text  : raw CV text (any format). When the caller already has
                      extracted text (e.g. from a .docx upload), pass it
                      here. Falls back to PDF parsing for legacy PDF paths.
                      Required for `_check_do_not_inject` to run on DOCX
                      uploads where `parse_cv` would return empty.
    """
    if outline is None:
        outline = build_outline_cached(cv_pdf_path)

    # May 2026 fix (Claude spec): extract full CV text for do_not_inject guard
    # to reduce false positives when strategist mis-classifies CV terms.
    # Run 17 follow-up: if the caller provided cv_full_text, use it. The PDF
    # parser is PDF-only and silently returns empty on .docx uploads — that
    # would silently disable the do_not_inject guard.
    if cv_full_text:
        cv_text = cv_full_text
    else:
        from agents.cv_parser import parse_cv
        try:
            cv_text = parse_cv(cv_pdf_path) or ""
        except Exception:
            cv_text = ""

    # Reset the per-call bullet-revert tracker so counts reflect THIS job.
    _LAST_BULLET_REVERTS.clear()

    orig_summary = (outline.get("summary") or "").strip()
    orig_words   = len(orig_summary.split()) if orig_summary else 0

    # ── P0-2: Zero-bullets early-exit ───────────────────────────────────
    # If the CV has no bullet content at all (paragraph-style CVs, rebuild
    # outputs that lost role boundaries, or parser-incompatible layouts
    # that survived the replica check), running the LLM tailor wastes
    # ~25-30K tokens producing rewrites the editor can't apply. Return a
    # no-op diff so the supervisor can route to rebuild without spending.
    _total_bullets = sum(
        len(r.get("bullets") or []) for r in (outline.get("roles") or [])
    )
    if _total_bullets == 0:
        print(
            "   ⏭️  cv_diff_tailor: outline has 0 bullets across all roles "
            "— skipping LLM tailor (would produce no editable changes). "
            "Caller should route to rebuild path."
        )
        return {
            "summary":      orig_summary,    # preserve verbatim if any
            "bullets":      {},
            "skills_order": [],
            "_debug": {
                "early_exit":          "zero_bullets",
                "summary_reverts":     [],
                "bullet_reverts":      [],
                "bullet_reverts_count": 0,
                "all_reverted":        True,
            },
        }

    # ── JD-only forbidden list + strategy scrub (May 2026) ──────────
    # Compute the deterministic "JD words absent from the CV" list, then
    # pre-emptively scrub any such term the strategist named in a bullet
    # target — so the BINDING strategy never instructs the tailor to
    # inject a word the CV lacks. This cleans the PLAN up front; it is
    # not a post-hoc revert of a finished rewrite.
    try:
        _jd_only_for_scrub = _jd_only_terms(job_description, outline, cv_text)
    except Exception:
        _jd_only_for_scrub = []
    if strategy and _jd_only_for_scrub:
        try:
            _scrub_strategy_jd_leak(strategy, _jd_only_for_scrub)
        except Exception as e:
            print(f"   ⚠️  cv_diff_tailor: strategy JD-leak scrub failed ({e})")

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

    # Pre-compute concrete summary word-count bands (95%-115% of the
    # original) so the LLM sees absolute integers rather than having to
    # multiply percentages itself. LLMs reliably ignore "95%-115%" but
    # respect "between 76 and 92 words" (Run 12 evidence: DeepSeek shipped
    # 56 words against a 80-word original despite the percentage rule).
    _orig_word_count = len(orig_summary.split()) if orig_summary else 0
    # P0-1 (May 2026): Stub-summary handling. When the original is empty
    # or very short (< 20 words) the 95%-115% band is meaninglessly tight
    # (e.g., 11-13 words for a 12-word original) and the LLM either
    # produces an unbounded fabrication or we revert pointlessly. Use an
    # absolute floor/ceiling:
    #   • orig=0  : ban summary changes (LLM must return empty string).
    #   • orig<20 : allow expansion up to 60 words but no further.
    #   • orig>=20: standard 95%-115% band.
    _SUMMARY_STUB_THRESHOLD = 20
    _SUMMARY_STUB_CAP       = 60
    if _orig_word_count == 0:
        _orig_word_min, _orig_word_max = 0, 0
    elif _orig_word_count < _SUMMARY_STUB_THRESHOLD:
        _orig_word_min = max(1, _orig_word_count - 2)        # allow tighten
        _orig_word_max = min(_SUMMARY_STUB_CAP,
                             max(_orig_word_count + 10, int(_orig_word_count * 1.15)))
    else:
        # May 2026 (user-driven): the tailored summary should be NEARLY
        # THE SAME LENGTH as the original — same word count, retargeted
        # content. A summary that shrinks to 70% of the original leaves
        # a visible gap in the PDF and reads as under-baked. Tighten the
        # band to 97%-108% so the rewrite stays close to the original
        # footprint; the LLM fills any slack with JD-aligned CV detail
        # rather than ending short.
        _orig_word_min = max(1, int(round(_orig_word_count * 0.97)))
        _orig_word_max = int(round(_orig_word_count * 1.08))

    # ── Proactive fabrication prevention (May 2026) ──────────────────
    # Deterministically list the JD words that appear NOWHERE in the CV
    # and hand the model that explicit "forbidden words" list inside the
    # prompt. This TRAINS the model never to inject JD vocabulary in the
    # first place — it is NOT a revert guard. Reverting every rewrite
    # that touched a JD term would collapse the tailoring rate to zero;
    # teaching the model up front keeps tailoring high AND fabrication
    # at zero. The strategist's do_not_inject list is merged in for the
    # display so the model sees the most complete picture.
    strategy_dni: List[str] = list((strategy or {}).get("do_not_inject") or [])
    # B1: strategist-whitelisted relabels — JD terms that ARE the standard
    # label for CV-proven experience under a different word. These are
    # exempt from the forbidden-vocabulary guards (do-not-inject + the
    # FORBIDDEN block + the foreign-capitalised summary guard).
    _relabels = _safe_relabels(strategy)
    _safe_ws  = _safe_relabel_wordset(_relabels)
    strategy_dni = [t for t in strategy_dni if not _all_words_safe(t, _safe_ws)]
    try:
        _jd_only_list      = _jd_only_terms(job_description, outline, cv_text)
        _forbidden_display = _merge_forbidden_terms(_jd_only_list, strategy_dni)
        _forbidden_display = [t for t in _forbidden_display
                              if not _all_words_safe(t, _safe_ws)]
        _jd_only_block     = (_format_jd_only_terms_block(_forbidden_display)
                              + _format_permitted_terms_block(_relabels))
        if _forbidden_display:
            print(
                f"   cv_diff_tailor: {len(_forbidden_display)} JD-only "
                f"term(s) surfaced as FORBIDDEN in the tailor prompt "
                f"(proactive no-fabrication training, no revert)."
            )
        if _relabels:
            print(
                f"   cv_diff_tailor: {len(_relabels)} strategist relabel(s) "
                f"PERMITTED (CV-proven JD vocabulary)."
            )
    except Exception as e:
        print(f"   cv_diff_tailor: JD-only term scan failed ({e}) — continuing")
        _jd_only_block = _format_jd_only_terms_block([])

    def _render_prompt(extra: str = "") -> str:
        # P1-4 (May 2026): Long-JD compression. When the job description is
        # very long (e.g., > 800 words), it consumes excessive tokens in the
        # prompt and may cause context overflow. Compress the JD by extracting
        # key sections (requirements, responsibilities, qualifications) while
        # dropping boilerplate (company descriptions, equal opportunity statements).
        # This reduces token usage by 30-50% while preserving the signal the
        # tailor needs to produce a useful diff.
        _JD_MAX_WORD_THRESHOLD = 800
        jd_for_processing = job_description or ""
        jd_word_count = len(jd_for_processing.split())
        if jd_word_count > _JD_MAX_WORD_THRESHOLD:
            jd_compressed = _compress_long_jd(jd_for_processing, max_words=_JD_MAX_WORD_THRESHOLD)
            compressed_word_count = len(jd_compressed.split())
            print(
                f"   ↘️  cv_diff_tailor: JD compressed from {jd_word_count} → "
                f"{compressed_word_count} words to reduce token usage."
            )
            jd_for_processing = jd_compressed

        jd_block = wrap_untrusted_block(
            jd_for_processing.strip() or "(no description provided)",
            label="JOB_DESCRIPTION",
        )
        p = _PROMPT_TEMPLATE.format(
            safety_preamble       = untrusted_block_preamble(["JOB_DESCRIPTION"]),
            job_title             = job_title or "(unspecified)",
            company               = company   or "(unspecified)",
            job_description_block = jd_block,
            jd_only_terms_block   = _jd_only_block,
            outline               = _format_outline_for_prompt(outline),
            strategy_block        = strategy_block_str or "(no strategy provided — use the legacy fallback floors in the RULES section below)",
            cur_word_count        = _orig_word_count,
            cur_word_min          = _orig_word_min,
            cur_word_max          = _orig_word_max,
        )
        p += "\n\n" + _build_feedback_addendum(feedback, previous_diff)
        if extra:
            p += "\n\n" + extra
        return p

    raw_text = _call_llm(_render_prompt())
    raw_json = _extract_json(raw_text)
    diff     = _sanitise_diff(raw_json, outline, do_not_inject=strategy_dni, cv_text=cv_text)
    # Snapshot the FIRST-PASS bullet reverts. The summary expand/tighten
    # retries below also call _sanitise_diff, which appends to the shared
    # _LAST_BULLET_REVERTS tracker — without this snapshot the bullet-retry
    # blocks downstream would see identical_rewrite reverts double-counted
    # across every summary-retry pass (observed: "30 unchanged" on a
    # 16-bullet CV). The bullet-retry blocks restore this snapshot first.
    _first_pass_bullet_reverts = list(_LAST_BULLET_REVERTS)

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

    # P0-1 (May 2026): no-original-summary fabrication block. When the CV
    # has NO original summary text (orig_summary=""), any non-empty
    # rewrite from the LLM is by definition invented from JD + bullets —
    # there is no original for the credential / identity guards to
    # protect. Most CVs without a summary are deliberate (the candidate
    # chose not to include one); injecting an LLM-fabricated paragraph
    # would surprise the user and degrade trust. Drop the rewrite.
    if new_sum and not orig_summary:
        print(
            f"   ⚠️  cv_diff_tailor: LLM produced a summary ({len(new_sum.split())} words) "
            f"but the CV has no original summary — dropping rewrite to avoid "
            f"fabricating a section the user chose not to include."
        )
        diff["_debug"]["summary_reverts"].append({
            "reason":     "no_original_summary",
            "new_words":  len(new_sum.split()),
        })
        diff["summary"] = ""
        new_sum = ""

    # P0-1 (May 2026): stub-summary upper-bound enforcement. When the
    # original was 1-19 words, the LLM may still expand far beyond the
    # 60-word cap despite the prompt directive (small originals attract
    # over-explanation). Hard-cap the rewrite at _SUMMARY_STUB_CAP words;
    # if it overflows, revert to the original verbatim rather than ship
    # an outsized summary that overflows the layout box.
    if new_sum and orig_summary and 0 < _orig_word_count < _SUMMARY_STUB_THRESHOLD:
        new_words_now = len(new_sum.split())
        if new_words_now > _SUMMARY_STUB_CAP:
            print(
                f"   ⚠️  cv_diff_tailor: stub-summary rewrite overflowed cap "
                f"({new_words_now} > {_SUMMARY_STUB_CAP} words) — reverting "
                f"to original to preserve layout."
            )
            diff["_debug"]["summary_reverts"].append({
                "reason":     "stub_summary_overflow",
                "new_words":  new_words_now,
                "cap":        _SUMMARY_STUB_CAP,
                "orig_words": _orig_word_count,
            })
            diff["summary"] = orig_summary
            new_sum = orig_summary

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
            cv_vocab = _cv_vocabulary(outline, cv_full_text=cv_text)
            foreign = _foreign_capitalized_terms(new_sum, cv_vocab, _safe_ws)
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
                    # Item 13: one-shot retry to RESTORE the dropped
                    # credential before discarding the whole tailored
                    # summary. A revert loses all the JD re-aiming for the
                    # sake of one missing token — try to fix the token.
                    print(
                        f"   ↻  summary dropped credential(s) ({flat}) — "
                        f"retrying once to restore them."
                    )
                    _cred_fix = (
                        f"YOUR PREVIOUS SUMMARY DROPPED a credential the "
                        f"recruiter scans for: {flat}. Here is your "
                        f"summary:\n\n\"{new_sum}\"\n\nReturn the summary "
                        f"again with the dropped credential(s) restored "
                        f"VERBATIM as written in the original CV. Every "
                        f"degree grade, years-of-experience figure, "
                        f"employer and university name from the original "
                        f"summary MUST be present. Change nothing else and "
                        f"do not alter the length materially."
                    )
                    sum_cf = (_sanitise_diff(
                        _extract_json(_call_llm(_render_prompt(extra=_cred_fix))),
                        outline, do_not_inject=strategy_dni, cv_text=cv_text,
                    ).get("summary") or "").strip()
                    if (sum_cf
                            and not _check_credentials_preserved(orig_summary, sum_cf)
                            and not _summary_absorbed_bullet(sum_cf, outline)):
                        print("   ✓  credential retry restored the dropped credential(s).")
                        diff["summary"] = sum_cf
                        new_sum = sum_cf
                    else:
                        print(
                            "   ↺  credential retry did not restore them — "
                            "reverting to original summary."
                        )
                        diff["_debug"]["summary_reverts"].append({
                            "reason":  "credentials_dropped",
                            "missing": missing_creds,
                        })
                        diff["summary"] = orig_summary
                        new_sum = orig_summary
                else:
                    # P1-2 (May 2026): Wrong-language detection. If the LLM
                    # produces a summary in a non-English language (e.g., Chinese,
                    # Arabic, Cyrillic scripts), reject it and revert to the
                    # original. The ASCII ratio heuristic catches languages with
                    # non-Latin character sets. We use 0.85 as the threshold
                    # (same as cv_validator.py) to allow for common punctuation
                    # and accented characters in English text (é, ü, etc.).
                    if not _is_english_text(new_sum, min_ascii_ratio=0.85):
                        ascii_chars = sum(1 for c in new_sum if ord(c) < 128)
                        ascii_ratio = ascii_chars / max(1, len(new_sum))
                        print(
                            f"   ⚠️  summary appears to be in a non-English language "
                            f"(ASCII ratio {ascii_ratio:.2f}) — reverting to original "
                            f"summary to prevent language drift."
                        )
                        diff["_debug"]["summary_reverts"].append({
                            "reason":       "wrong_language",
                            "ascii_ratio":  round(ascii_ratio, 3),
                        })
                        diff["summary"] = orig_summary

    # Absorbed-bullet + project-preservation guard (Run 22 fix). Runs on
    # whatever summary survived the checks above.
    _final_sum = (diff.get("summary") or "").strip()
    if _final_sum and orig_summary and _final_sum != orig_summary:
        if _summary_absorbed_bullet(_final_sum, outline):
            print(
                "   ⚠️  summary absorbed a CV bullet (padded to length with "
                "bullet text) — reverting to original summary."
            )
            diff["_debug"]["summary_reverts"].append({"reason": "absorbed_bullet"})
            diff["summary"] = orig_summary
        else:
            _dropped_proj = _summary_dropped_project(orig_summary, _final_sum, outline)
            if _dropped_proj:
                print(
                    f"   ⚠️  summary dropped the named project "
                    f"'{_dropped_proj}' — reverting to original summary."
                )
                diff["_debug"]["summary_reverts"].append({
                    "reason": "project_dropped", "project": _dropped_proj,
                })
                diff["summary"] = orig_summary

    # ── Length-enforcement retry ─────────────────────────────────────
    # Triggers when the new summary is below 85% of the original word count.
    # The 85% floor catches real truncations (90→60 words) but allows modest
    # compression (90→80) when the LLM tightens prose. Previously set to
    # 95%, which reverted too many legitimate rewrites. If the retry is
    # still short, fall back to the ORIGINAL summary (no shortening).
    new_words = len((diff.get("summary") or "").split())
    # May 2026 (user-driven, same-length tailoring): the tailored summary
    # should be NEARLY THE SAME LENGTH as the original — same footprint,
    # retargeted content. The floor was 0.75, which let a 59-word rewrite
    # of an 80-word original ship (visibly short, leaves a PDF gap). Raise
    # to 0.90: anything shorter triggers the expand-retry, and the retry
    # prompt explicitly tells the LLM to add CV-grounded JD-relevant
    # detail until it reaches the original length.
    _SUMMARY_MIN_RATIO = 0.90        # expand-retry target — aim for >=90%
    _SUMMARY_MAX_RATIO = 1.12
    _SUMMARY_MIN_WORD_GRACE = 3      # words below the ratio the retry tolerates
    # 1+2 hybrid (user-driven): push HARD with up to _SUMMARY_RETRY_LIMIT
    # expand-retries; but if the best attempt still lands short while
    # at/above the HARD floor, ship it — a tailored, slightly-short summary
    # beats an untailored original. Only a genuinely stunted summary,
    # below the hard floor, reverts.
    _SUMMARY_RETRY_LIMIT = 2
    _SUMMARY_HARD_FLOOR_RATIO = 0.80
    # H3: suppress the length-retry on reviewer-driven retries. The reviewer
    # already triggered a re-tailor; cascading another inner retry on top
    # multiplies token use without adding signal (the LLM saw the directive
    # in the reviewer feedback already). Only run length-retry on first pass.
    on_retry_pass = bool(feedback or previous_diff)
    # Word count the expand-retry AIMS for, and the HARD floor below which
    # even a tailored summary is reverted as genuinely stunted.
    _target_words = max(
        1, int(orig_words * _SUMMARY_MIN_RATIO) - _SUMMARY_MIN_WORD_GRACE,
    )
    _hard_floor = max(1, int(orig_words * _SUMMARY_HARD_FLOOR_RATIO))
    if (not on_retry_pass) and orig_words >= 20 and new_words and new_words < _target_words:
        high = int(orig_words * _SUMMARY_MAX_RATIO)
        # Best (longest) credential-preserving attempt so far, seeded with
        # the first pass; the retry loop tries to beat it.
        best_sum = (diff.get("summary") or "").strip()
        best_n   = len(best_sum.split())
        for attempt in range(1, _SUMMARY_RETRY_LIMIT + 1):
            print(
                f"   ↻  summary too short ({best_n}/{orig_words} words, "
                f"need ≥{_target_words}) — expand-retry "
                f"{attempt}/{_SUMMARY_RETRY_LIMIT} (target {_target_words}-{high})."
            )
            # Show the LLM its OWN previous summary and tell it to expand
            # that exact text — DeepSeek reads "rewrite to fit a range" as
            # "be more concise" and shrinks further.
            words_short = max(1, _target_words - best_n)
            enforce = (
                f"YOUR PREVIOUS SUMMARY WAS TOO SHORT — it had {best_n} "
                f"words but should end up between {_target_words} and "
                f"{high} (the original is {orig_words}). Here is your "
                f"previous summary:\n\n\"{best_sum}\"\n\nDo NOT rewrite it "
                f"from scratch. KEEP these exact sentences and EXPAND them "
                f"by adding about {words_short}-{words_short + 8} more "
                f"words of CV-grounded detail — specific outcomes, "
                f"metrics, platform names, methodologies or technologies "
                f"that ALREADY appear in the candidate's CV. Invent "
                f"nothing. Do not exceed {high} words.\n"
                f"⚠️  NEVER reach the word count by appending or pasting in "
                f"a CV bullet sentence — the summary is flowing prose, not "
                f"a bullet list. Enrich the EXISTING sentences. If you "
                f"genuinely cannot reach {_target_words} words by honest "
                f"enrichment, a slightly shorter CLEAN summary is correct "
                f"— a padded one is not."
            )
            diff_r = _sanitise_diff(
                _extract_json(_call_llm(_render_prompt(extra=enforce))),
                outline, do_not_inject=strategy_dni, cv_text=cv_text,
            )
            sum_r = (diff_r.get("summary") or "").strip()
            n_r   = len(sum_r.split())
            # Keep an attempt only if it is LONGER and still preserves the
            # original's credential tokens (grade / YoE / numeric outcomes).
            # Bug B: reject an expansion that hit the word count by
            # absorbing a bullet rather than genuinely expanding the prose.
            if (sum_r and n_r > best_n
                    and not _check_credentials_preserved(orig_summary, sum_r)
                    and not _summary_absorbed_bullet(sum_r, outline)):
                best_sum, best_n = sum_r, n_r
            elif sum_r and _summary_absorbed_bullet(sum_r, outline):
                print(
                    "   ⚠️  summary expand-retry padded with a bullet — "
                    "rejected; keeping the shorter clean summary."
                )
            if best_n >= _target_words:
                break

        if best_n >= _hard_floor:
            # 1+2 hybrid: ship the best attempt — it is tailored and at or
            # above the hard floor, even if a few words under target.
            diff["summary"] = best_sum
            new_words = best_n
            if best_n >= _target_words:
                print(f"   ↻  summary expand-retry hit target ({best_n} words).")
            else:
                print(
                    f"   ↻  summary {best_n}/{orig_words} words — under "
                    f"target {_target_words} but ≥ hard floor {_hard_floor}: "
                    f"shipping the tailored summary."
                )
                diff["_debug"]["summary_reverts"].append({
                    "reason":         "length_below_target_accepted",
                    "new_words":      best_n,
                    "original_words": orig_words,
                    "target":         _target_words,
                })
        elif orig_summary:
            # Even the best attempt is below the hard floor — genuinely
            # stunted. Revert rather than ship a clearly short summary.
            print(
                f"   ↺  summary still too short after {_SUMMARY_RETRY_LIMIT} "
                f"expand-retries (best {best_n}/{orig_words}, hard floor "
                f"{_hard_floor}) — reverting to original summary."
            )
            diff["_debug"]["summary_reverts"].append({
                "reason":         "length_floor",
                "new_words":      best_n,
                "original_words": orig_words,
                "hard_floor":     _hard_floor,
            })
            diff["summary"] = orig_summary
            new_words = orig_words

    # ── Summary upper-bound enforcement (May 2026, user-driven) ──────
    # A tailored summary must occupy the SAME footprint as the original:
    # same font, same size, same LINE SPACING. The renderer keeps spacing
    # identical only when the rewrite genuinely fits the original slot. A
    # summary longer than the original forces the editor to compress the
    # line-gap to cram it in — the "stuck together" look. The retry above
    # handles undershoot; this handles overshoot. One retry with a
    # tighten-to-fit directive, then keep the shorter attempt. Never
    # revert to the un-tailored original — that discards the tailoring.
    _summary_hi_hard = int(round(orig_words * _SUMMARY_MAX_RATIO))
    if (not on_retry_pass) and orig_words >= 20 and new_words > _summary_hi_hard:
        _first_sum  = (diff.get("summary") or "").strip()
        _first_wcnt = new_words
        print(
            f"   ↻  summary too long ({new_words}/{orig_words} words, "
            f"ceiling {_summary_hi_hard}) — retrying with a tighten-to-fit "
            f"directive (target {_orig_word_min}-{_orig_word_max})."
        )
        enforce_long = (
            f"YOUR PREVIOUS SUMMARY WAS TOO LONG ({new_words} words; it MUST "
            f"land between {_orig_word_min} and {_orig_word_max} words — the "
            f"original is {orig_words}). A summary longer than the original "
            f"does not fit the CV's summary slot: the layout is forced to "
            f"squeeze the lines together, which looks wrong. Do NOT drop any "
            f"fact, number, employer, tool, platform, or credential. TIGHTEN "
            f"the existing prose instead — merge clauses, cut filler words "
            f"and redundant qualifiers, choose shorter phrasings. Same "
            f"content, same facts, fewer words. Count the words in your "
            f"final summary before submitting."
        )
        raw_text3 = _call_llm(_render_prompt(extra=enforce_long))
        raw_json3 = _extract_json(raw_text3)
        diff3     = _sanitise_diff(raw_json3, outline, do_not_inject=strategy_dni, cv_text=cv_text)
        new_sum3  = (diff3.get("summary") or "").strip()
        _retry_wcnt = len(new_sum3.split()) if new_sum3 else 0
        # Adopt the retry only if it is non-empty, genuinely shorter, and
        # still clean — a length fix that drops a credential, introduces a
        # CV-foreign term, or alters the professional identity is not a fix.
        _adopt_long = False
        if new_sum3 and 0 < _retry_wcnt < _first_wcnt:
            _cv_vocab_b = _cv_vocabulary(outline, cv_full_text=cv_text)
            _bad = (
                _check_credentials_preserved(orig_summary, new_sum3)
                or _foreign_capitalized_terms(new_sum3, _cv_vocab_b, _safe_ws)
                or _check_professional_identity_fabrication(
                    orig_summary, new_sum3, outline
                )
            )
            _adopt_long = not _bad
        if _adopt_long:
            diff["summary"] = new_sum3
            new_words = _retry_wcnt
            diff["_debug"].setdefault("summary_length_retries", []).append(
                {"reason": "too_long", "before": _first_wcnt, "after": _retry_wcnt}
            )
            print(
                f"   ✓  summary tighten retry: {_first_wcnt} → {_retry_wcnt} "
                f"words (ceiling {_summary_hi_hard})."
            )
        else:
            diff["summary"] = _first_sum
            new_words = _first_wcnt
            print(
                f"   ↺  summary tighten retry did not improve "
                f"({_first_wcnt} → {_retry_wcnt} words) — keeping the first "
                f"attempt; editor will compress line spacing to fit."
            )

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
                "YOUR PREVIOUS RESPONSE REWROTE 0 BULLETS. THIS IS A FAILURE.\n\n"
                "You MUST produce text rewrites on this attempt. Do NOT return "
                "text=null or omit the 'text' key for all bullets — that is the "
                "exact behaviour that just got rejected.\n\n"
                "ACTION REQUIRED: pick the role whose bullets best match the "
                "job description and rewrite AT LEAST 3 of its bullets, "
                "leading each rewrite with a strong JD-relevant verb "
                "(e.g. 'Delivered', 'Drove', 'Managed', 'Built', 'Led'). "
                "Preserve every number (5%, 600K+, 30%) and proper noun "
                "verbatim. Keep rewrites within 50–150% of the original "
                "bullet's character count.\n\n"
                "If you cannot improve a bullet, write the ORIGINAL TEXT "
                "verbatim as the 'text' value — but do NOT leave every bullet "
                "as text=null. The tailoring MUST include at least 3 non-null "
                "text values in the bullets section."
            )
            raw_text_rr = _call_llm(_render_prompt(extra=enforce_rewrites))
            raw_json_rr = _extract_json(raw_text_rr)
            diff_rr     = _sanitise_diff(raw_json_rr, outline, do_not_inject=strategy_dni, cv_text=cv_text)
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

    # Restore the first-pass bullet reverts — the summary retries above
    # polluted the shared tracker (Bug D). The bullet-retry blocks below
    # must reason about the FIRST PASS's bullet outcomes only.
    _LAST_BULLET_REVERTS.clear()
    _LAST_BULLET_REVERTS.extend(_first_pass_bullet_reverts)

    # ── Identical-rewrite retry (May 2026) ───────────────────────────
    # The strategist flagged a bullet for a verb-led rewrite, but the
    # tailor returned text that normalises to the original (cosmetic
    # near-copy → demoted to "identical_rewrite"). That bullet was
    # PLANNED to change and didn't. Do ONE retry showing the LLM each
    # unchanged bullet and demanding a genuine restructure — or an
    # honest omission if the bullet truly cannot be improved. First pass
    # only; reviewer-driven retries carry their own directive.
    if not (feedback or previous_diff):
        _identical_reverts = [
            r for r in list(_LAST_BULLET_REVERTS)
            if isinstance(r, dict)
            and str(r.get("reason", "")) == "identical_rewrite"
        ]
        if len(_identical_reverts) >= 1:
            print(
                f"   ↻  cv_diff_tailor: {len(_identical_reverts)} planned "
                f"bullet(s) returned UNCHANGED — retrying with a "
                f"restructure-or-omit directive."
            )
            _unchanged_lines = []
            for r in _identical_reverts:
                prev = (r.get("rewrite_preview") or "").strip()
                if not prev:
                    continue
                _unchanged_lines.append(
                    f'  - bullet {r.get("bullet_index")}: "{prev}"'
                )
            enforce_id = (
                f"{len(_identical_reverts)} bullet(s) the action plan "
                f"flagged came back UNCHANGED:\n\n"
                + "\n".join(_unchanged_lines)
                + "\n\nFor EACH one, apply this test honestly:\n"
                "• Can you genuinely improve it — surface a buried "
                "JD-relevant fact, lead with a stronger verb — WITHOUT "
                "dropping any fact and WITHOUT making it vaguer? If yes, "
                "rewrite it that way.\n"
                "• If NOT — if the only way to make it 'look different' "
                "would drop a specific (a metric, a named tool, 'MVP', "
                "'PRD', a project name) or weaken it — then KEEP IT "
                "EXACTLY as the original (omit it from the bullets JSON, "
                "or text=null). Keeping a strong bullet unchanged is the "
                "CORRECT answer, not a failure.\n"
                "PRESERVATION RULE: a rewrite RE-FRAMES; it never REMOVES "
                "a concrete fact the original had. A forced change that "
                "drops a specific is worse than no change. Do NOT return "
                "a cosmetic near-copy either. Return the full bullets JSON."
            )
            raw_text_id = _call_llm(_render_prompt(extra=enforce_id))
            raw_json_id = _extract_json(raw_text_id)
            _LAST_BULLET_REVERTS.clear()
            diff_id = _sanitise_diff(
                raw_json_id, outline,
                do_not_inject=strategy_dni, cv_text=cv_text,
            )
            n_rewrites_id, n_dropped_id = _count_diff_edits(diff_id)
            _n_rewrites_before_id = n_rewrites
            if n_rewrites_id > n_rewrites:
                id_sum = (diff_id.get("summary") or "").strip()
                id_sum_words = len(id_sum.split()) if id_sum else 0
                if (id_sum_words == 0
                        or (new_words > 0 and id_sum_words < new_words)) \
                        and diff.get("summary"):
                    diff_id["summary"] = diff["summary"]
                    id_sum_words = new_words
                diff       = diff_id
                n_rewrites = n_rewrites_id
                n_dropped  = n_dropped_id
                new_words  = id_sum_words or new_words
                print(
                    f"   ✓  identical-rewrite retry: {_n_rewrites_before_id} "
                    f"→ {n_rewrites_id} bullet rewrites."
                )

    # ── Length-fix retry (May 2026 — same-length tailoring) ───────────
    # If the first pass had bullet rewrites rejected on LENGTH, do ONE
    # retry. A "length N outside lo-hi" revert can mean too LONG (N > hi)
    # OR too SHORT (N < lo). Run 21 showed every length revert was too
    # SHORT — and the old compress-only retry pushed them shorter still,
    # a doom loop. We now split the two directions: compress the long
    # ones, EXPAND the short ones. First pass only.
    if not (feedback or previous_diff):
        _too_long, _too_short = [], []
        for r in list(_LAST_BULLET_REVERTS):
            if not (isinstance(r, dict)
                    and str(r.get("reason", "")).startswith("length ")):
                continue
            m = re.search(r"length (\d+) outside (\d+)-(\d+)",
                          str(r.get("reason", "")))
            draft = (r.get("rewrite_preview") or "").strip()
            if not m or not draft:
                continue
            n, lo, hi = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if n > hi:
                _too_long.append((draft, n, hi))
            elif n < lo:
                _too_short.append((draft, n, lo))
        _length_reverts = _too_long + _too_short
        if len(_length_reverts) >= 1:
            print(
                f"   ↻  cv_diff_tailor: {len(_length_reverts)} bullet "
                f"rewrite(s) rejected on length "
                f"({len(_too_long)} too long, {len(_too_short)} too short) "
                f"— retrying with a fit-to-slot directive (own drafts shown)."
            )
            _parts = []
            if _too_long:
                _ll = "\n".join(
                    f'  - TOO LONG by {n - hi} chars (limit {hi}, draft {n}):'
                    f'\n    YOUR DRAFT: "{d}"'
                    for d, n, hi in _too_long
                )
                _parts.append(
                    "These rewrites are TOO LONG for the bullet's fixed PDF "
                    "slot. SHORTEN each to AT OR BELOW its limit — cut "
                    "adjectives, hedges and redundant words; keep every "
                    "number and proper noun verbatim:\n\n" + _ll
                )
            if _too_short:
                _ls = "\n".join(
                    f'  - TOO SHORT by {lo - n} chars (floor {lo}, draft {n}):'
                    f'\n    YOUR DRAFT: "{d}"'
                    for d, n, lo in _too_short
                )
                _parts.append(
                    "These rewrites are TOO SHORT — you compressed them "
                    "below the bullet's slot, so they revert. EXPAND each to "
                    "AT OR ABOVE its floor by restoring CV-grounded detail "
                    "already true of that bullet (a metric, named tool, "
                    "scope, or outcome the original bullet contained). Do "
                    "NOT pad with filler and invent nothing:\n\n" + _ls
                )
            enforce_len = (
                f"{len(_length_reverts)} of your bullet rewrites were "
                f"REJECTED on length. Fix each YOUR DRAFT below — do NOT "
                f"re-tailor from scratch.\n\n"
                + "\n\n".join(_parts)
                + "\n\nReturn the full bullets JSON again with these fixed. "
                "A rewrite inside its slot SHIPS; one outside is DISCARDED."
            )
            raw_text_lr = _call_llm(_render_prompt(extra=enforce_len))
            raw_json_lr = _extract_json(raw_text_lr)
            _LAST_BULLET_REVERTS.clear()
            diff_lr = _sanitise_diff(
                raw_json_lr, outline,
                do_not_inject=strategy_dni, cv_text=cv_text,
            )
            n_rewrites_lr, n_dropped_lr = _count_diff_edits(diff_lr)
            # Adopt the retry only if it landed MORE rewrites than the
            # first pass. Otherwise keep the first pass (never regress).
            _n_rewrites_before = n_rewrites
            if n_rewrites_lr > n_rewrites:
                lr_sum = (diff_lr.get("summary") or "").strip()
                lr_sum_words = len(lr_sum.split()) if lr_sum else 0
                # Keep whichever summary is closer to the original length.
                if (lr_sum_words == 0
                        or (new_words > 0 and lr_sum_words < new_words)) \
                        and diff.get("summary"):
                    diff_lr["summary"] = diff["summary"]
                    lr_sum_words = new_words
                diff       = diff_lr
                n_rewrites = n_rewrites_lr
                n_dropped  = n_dropped_lr
                new_words  = lr_sum_words or new_words
                print(
                    f"   ✓  length-fix retry: {_n_rewrites_before} "
                    f"→ {n_rewrites_lr} bullet rewrites."
                )

    tag = " (retry)" if feedback or previous_diff else ""
    # Apr 28 follow-up: include LLM source so we can see at a glance whether
    # this kept diff came from Gemini or Groq fallback. Late import to avoid
    # an unconditional dependency on llm_client at module load time.
    try:
        from agents.llm_client import last_llm_source as _lls
        src = _lls()
    except Exception:
        src = "unknown"
    _sr = diff.get("_debug", {}).get("summary_reverts", [])
    _sr_note = f" | summary_reverts={len(_sr)}:{_sr[0].get('reason','?')}" if _sr else ""
    print(
        f"   ✂️  cv_diff_tailor{tag} [via {src}]: "
        f"summary={new_words}/{orig_words}w | "
        f"roles_edited={len(diff['bullets'])} | "
        f"bullets_rewritten={n_rewrites} | "
        f"bullets_dropped={n_dropped} | "
        f"skills_reordered={'yes' if diff['skills_order'] else 'no'}{_sr_note}"
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

    # Run-17 audit fix #3 (extended by Run 19 audit fix #33): flag the
    # case where the SUMMARY survived but the body is unchanged.
    #
    # Two failure modes feed into body_reverted now:
    #   (a) guards reverted every bullet rewrite (_LAST_BULLET_REVERTS > 0
    #       and n_rewrites == 0) — this was the original check.
    #   (b) LLM "silent skip": the model produced text=null for every
    #       bullet (or no entries at all in the diff). No guard fires
    #       because there's nothing to evaluate, so _LAST_BULLET_REVERTS
    #       stays empty AND n_rewrites stays 0. The original check missed
    #       this — supervisor saw "tailor produced nothing" but body_reverted
    #       was False, no retry triggered.
    # The extended check fires when the CV had enough bullets to tailor
    # (>=5 total) but no rewrites landed AND no skills change — that's
    # body-untouched regardless of guard activity.
    n_bullets_total_in_outline = sum(
        len(r.get("bullets") or []) for r in outline.get("roles", [])
    )
    bullets_attempted_and_all_reverted = (
        len(_LAST_BULLET_REVERTS) > 0
        and n_rewrites == 0
    )
    body_silently_untouched = (
        n_bullets_total_in_outline >= 5
        and n_rewrites == 0
        and not has_skills_change
    )

    diff["_debug"]["all_reverted"] = (
        not has_summary_change
        and not has_bullet_change
        and not has_skills_change
        and (summary_reverted or bullets_all_reverted)
    )
    # body_reverted now catches BOTH the guard-reverted case (a) AND the
    # LLM-silent-skip case (b). Either way the body is untailored.
    diff["_debug"]["body_reverted"] = (
        (bullets_attempted_and_all_reverted or body_silently_untouched)
        and not has_skills_change
    )
    # Also surface the silent-skip flag separately so the supervisor can
    # distinguish "guards over-fired" (recoverable with looser guards)
    # from "LLM gave up" (needs a stricter retry prompt).
    diff["_debug"]["body_silently_untouched"] = body_silently_untouched

    if diff["_debug"]["all_reverted"]:
        print(
            "   🛡️  cv_diff_tailor: ALL changes reverted by fabrication "
            "guards — diff is effectively a no-op. Caller should retry with "
            "stricter prompting or skip the replica path."
        )
    elif diff["_debug"]["body_reverted"]:
        if body_silently_untouched:
            print(
                f"   🛡️  cv_diff_tailor: LLM silent-skip — n_rewrites=0 "
                f"across {n_bullets_total_in_outline} available bullets and "
                f"no guards fired. Summary may have changed but the body is "
                f"verbatim original. Caller MUST retry with explicit "
                f"bullet-rewrite directive."
            )
        else:
            print(
                "   🛡️  cv_diff_tailor: every bullet rewrite reverted; summary "
                "kept. The body of the CV is unchanged — caller should retry "
                "with sharper JD-aligned bullet prompts."
            )
    return diff