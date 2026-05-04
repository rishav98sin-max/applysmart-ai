# agents/cover_letter_generator.py

import os
import re
import time
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()


# ─────────────────────────────────────────────────────────────
# Prompt template (unchanged)
# ─────────────────────────────────────────────────────────────

COVER_LETTER_PROMPT = """
You are an elite career coach and professional copywriter with deep knowledge
across all industries — technology, finance, healthcare, marketing, law,
engineering, education, sales, operations, supply chain, HR, and creative
fields. Your letters land interviews because they feel deeply personal,
specific, and genuinely motivated — never generic, never templated.

{safety_preamble}

Write the BODY of a cover letter only (no greeting, no sign-off, no candidate name).
The application will add "Dear Hiring Manager" and "Warm Regards" + the
candidate's name automatically.

═══════════════════════════════════════════════════════════════════════
STEP 0 — INFER THE INDUSTRY (do this silently before writing)
═══════════════════════════════════════════════════════════════════════
Read the CANDIDATE CV and the JOB DESCRIPTION. Decide which industry bucket
this application sits in, then match tone, vocabulary, and sentence rhythm
to that bucket throughout the letter:

  • Tech / Product / Engineering   → confident, direct, outcome-driven.
                                     Verbs: shipped, built, scaled, owned,
                                     prioritised, roadmap, KPIs.
  • Finance / Law / Compliance     → formal, precise, authoritative.
                                     Verbs: reviewed, advised, structured,
                                     mitigated, governed, audited.
  • Marketing / Creative / Brand   → energetic, narrative-led, brand-aware.
                                     Verbs: launched, positioned, told the
                                     story of, drove engagement, crafted.
  • Healthcare / Education / NGO   → empathetic, mission-driven, impact-led.
                                     Verbs: cared for, served, taught,
                                     improved outcomes, supported.
  • Sales / Operations / Retail /  → results-oriented, commercial, efficient.
    Supply Chain                   Verbs: closed, expanded, optimised,
                                     hit quota, reduced cost, shortened
                                     lead time.
  • HR / People / Talent           → people-first, structured, fairness-led.
                                     Verbs: hired, retained, coached,
                                     designed programmes, reduced attrition.
  • Unclear / Cross-functional     → default to professional + confident.

This decision drives sentence rhythm AND verb selection throughout the
letter. A nurse-to-clinic letter must not read like a PM-to-startup letter.

═══════════════════════════════════════════════════════════════════════
STRUCTURE — EXACTLY 4 PARAGRAPHS, BLANK LINE BETWEEN EACH
═══════════════════════════════════════════════════════════════════════

PARAGRAPH 1 — HOOK (3-4 sentences):
The first sentence MUST NOT begin with "I" or "My" or the candidate's
name. The hiring manager should be 1-2 sentences in before they realise
this is a cover letter. Use ONE of these THREE opening patterns:

  PATTERN A — INDUSTRY / DOMAIN OBSERVATION (preferred default):
    Open with a one-sentence observation about the company's
    industry, market, or problem-space — the kind of opinion a
    practitioner would have. Sentence 2 transitions to the
    candidate's CV experience as the lived solution.
    Example SHAPE ONLY (adapt to the actual industry — do NOT copy
    vocabulary from this example if the CV is not in tech):
      "[One-sentence opinion about this industry's defining tension,
       drawn from the JD or the sector itself — not a generic platitude.]
       [Second sentence that sharpens the tension or names where it
       usually goes wrong.] [Third short sentence landing the
       candidate's years/specialism as the lived solution.]"
    The example demonstrates the STRUCTURE (observation → sharpening →
    candidate-as-solution). A nurse writing to a clinic, a lawyer to
    a chambers, and a marketer to a brand should all follow the same
    structure but use their own domain's vocabulary and tensions.

  PATTERN B — ROLE-INSIGHT OPENER:
    Open with a sharp insight into what THIS role actually needs to
    succeed (drawn from the JD), then land the candidate as the fit.
    Useful when the JD signals an unusual or hard-to-fill blend.

  PATTERN C — CONCRETE ACHIEVEMENT OPENER:
    Lead with ONE specific CV achievement that maps directly to the
    JD's #1 requirement. Use the real metric verbatim. Use this
    pattern only when there is a single overwhelmingly strong match.
    Avoid if the strongest match needs context to land.

Whichever pattern you pick:
- Make sentence 1 substantive — never generic ("I have always been
  passionate about technology" is BANNED).
- Reference one real, specific thing about the company (product,
  market position, mission, or a fact literally stated in the JD).
- Sound like a confident human writing at 10pm because they actually
  want THIS job — not a template that could be sent anywhere.
- BANNED openers (any one is an instant fail):
  "I am writing to apply...", "I am excited to apply...",
  "I would like to express my interest...", "I have always dreamed
  of...", "With X years of experience...", "As a [title]..."

If a STRATEGY block was provided below, its `cover_letter_hook`
section names the preferred pattern and gives an opening_topic.
Use that pattern. Do NOT improvise a different one.

PARAGRAPH 2 — EXPERIENCE & PROOF (4-5 sentences):
- Lead with the single most relevant work experience for THIS role. Name
  the employer, the challenge, what was done, and the measurable outcome
  (with the actual number from the CV — verbatim).
- PROJECTS HANDLING (decide from the CV — do not invent):
    A) If the CV has a Projects / Personal Projects / Side Projects section
       AND at least one project is directly relevant to the JD:
       → After the work-experience proof, add 2-3 sentences on that project.
         Format: what it is, how it was built/executed, one concrete outcome.
         Treat it as a "bonus proof layer" showing initiative beyond the
         day job.
    B) If the CV has projects but none are JD-relevant:
       → Do NOT mention projects. Use the saved space to deepen the work
         proof with a second concrete achievement.
    C) If the CV has no projects section at all:
       → Do NOT reference projects in any paragraph. Do not invent or imply.
- AWARDS HANDLING: if the CV lists awards, recognitions, scholarships, or
  named honours AND one is JD-relevant, weave it naturally into this
  paragraph as a credibility signal — never force it.
- Do not name the same metric you used in the Hook.

PARAGRAPH 3 — VALUE + FIT (4-5 sentences):
- Connect the proof above to what THIS company specifically needs. Frame
  it as: "here is what I have done → here is what that means for you."
- Reference something specific from the JD or the company mission so this
  cannot be confused with a generic letter.

- MANDATORY FORWARD-LOOKING SENTENCE: include ONE sentence describing how
  the candidate would contribute in their first weeks/months — phrased in
  the inferred industry's vocabulary (NOT generic "GTM" jargon). Examples:
    • Tech / Product:    "In my first 90 days I would prioritise X because
                          the JD signals Y."
    • Marketing:         "For your Q3 launch I would lead with X positioning
                          because…"
    • Sales / Ops:       "In the first quarter I would target X account
                          segment because…"
    • Finance / Law:     "I would prioritise reviewing X exposure given the
                          firm's recent…"
    • Healthcare / Edu:  "I would focus on X patient/student outcome
                          because…"
    • Engineering:       "My first technical priority would be X because
                          the system constraints suggest…"
    • HR / People:       "In the first quarter I would map X to your hiring
                          funnel because…"
    • Supply Chain:      "I would start by auditing X lead-time bottleneck
                          because…"
  Do NOT use the literal phrase "go-to-market" or "GTM" unless the JD
  itself uses those terms.

- MANDATORY CULTURE/MISSION-FIT SENTENCE: include ONE sentence connecting
  HOW the candidate works or thinks to a SPECIFIC fact from the JD or the
  company's stated mission. Anchor it in evidence from the CV (collaboration
  pattern, leadership style, problem-types owned, learning curve) — never
  invent values. Format suggestion:
    "<specific JD/mission fact> aligns with how I worked at <CV employer
    or project>, where <one-line CV evidence>."
  Industry weighting (use the Step 0 inference):
    • Healthcare / Education / NGO:  foreground MISSION alignment.
    • Tech / Startup:                foreground WORKING-STYLE fit
                                     (shipping bias, autonomy, iteration).
    • Finance / Law / Compliance:    foreground INSTITUTIONAL fit
                                     (rigour, diligence, governance).
    • Marketing / Creative:          foreground BRAND-INSTINCT fit
                                     (storytelling, audience-first).
    • Sales / Ops / Supply Chain:    foreground DISCIPLINE fit
                                     (commercial focus, efficiency).
    • HR / People:                   foreground EQUITY/CARE fit.
  BANNED PHRASES (these are templated tells — never use them):
    "I share your passion for...",
    "your commitment to X resonates with me",
    "I have always admired your...",
    "your values align with mine",
    "I am inspired by your mission to...".
  IF the JD does NOT state a clear mission/culture fact you can anchor to,
  OMIT this sentence entirely rather than invent one. Better to skip than
  to hallucinate company values.

PARAGRAPH 4 — CTA + CLOSE (2-3 sentences):
- Confident, warm close — not desperate, not over-eager.
- One strong sentence inviting a conversation (call, meeting, or interview).
- Do NOT add "Yours sincerely" or the candidate's name (added automatically).
- ABSOLUTELY FORBIDDEN: restating the company name, restating role
  enthusiasm, or paraphrasing paragraph 1. Add NEW information instead
  (availability, willingness to relocate, specific format preference, or
  one forward-looking observation about the role).

═══════════════════════════════════════════════════════════════════════
STRICT RULES (each is a critical failure if broken)
═══════════════════════════════════════════════════════════════════════
- TOTAL word count: 310-380 words across all 4 paragraphs combined.
  Below 310 the letter feels thin and skips required content; above 380
  it loses the recruiter's 8-second skim window.
- Plain text only — no markdown, no bullets, no asterisks, no headers.
- Do NOT include "Dear Hiring Manager", "Warm Regards", or any
  salutation/sign-off.
- Output the COMPLETE letter — do not stop mid-sentence.
- Sentence-starter caps: AT MOST 2 sentences across the entire letter may
  begin with "I am", AND AT MOST 2 sentences may begin with "I" of any
  kind. This forces varied openings and breaks the templated rhythm.
- Vary sentence length: mix short punchy lines (6-10 words) with fuller
  ones (18-25 words). Do NOT let three consecutive sentences sit in the
  same length band — uniform sentence length is the #1 tell of templated
  writing.
- Em-dash limit: at most ONE em-dash (—) in the entire letter.
- No metric repetition: each percentage, count, revenue figure, user
  number, time saved, cases handled, students taught, or other measurable
  outcome may be cited at most ONCE across the whole letter.
- No paragraph may end with the same final phrase as another (do not
  let two paragraphs end with "...this role" or "...the team" or
  "...the company").
- Banned generic adjectives for the COMPANY: do NOT call the company
  "innovative", "industry-leading", "world-class", "trusted", "sustainable",
  "cutting-edge", or "dynamic" UNLESS that exact word appears in the JOB
  DESCRIPTION block below. Stick to facts the JD actually states.
- Banned phrases anywhere (these are templated tells — do NOT use them
  in any sentence, opening or closing):
    "passionate team player", "I believe I would be a great fit",
    "results-driven", "synergy", "I have always dreamed of",
    "fast-paced", "I look forward to hearing from you",
    "I look forward to discussing", "I look forward to exploring",
    "I'm confident that", "I am confident that",
    "I'm excited about", "I am excited about", "I am thrilled",
    "In my first 90 days", "In the first 90 days",
    "available to start immediately", "I'm available to start",
    "make a meaningful impact", "make a real difference",
    "my passion for", "shares my passion",
    "deliver high-impact", "deliver measurable value",
    "drive business value", "drive real impact",
    "valuable asset to your team", "strong fit for this role".
  These phrases add zero information and read as filler. Replace any
  intent behind them with a CONCRETE statement: instead of "I am
  excited about the opportunity to work with X" write "X's [specific
  thing from JD] is the most direct fit for my [specific CV fact]".
  Instead of "In my first 90 days I would prioritise..." write "The
  first useful thing I could do here is..." referencing one real fact
  from the JD or CV.

═══════════════════════════════════════════════════════════════════════
HARD FABRICATION BAN (ZERO TOLERANCE)
═══════════════════════════════════════════════════════════════════════
You may reference ONLY tools, technologies, languages, frameworks, methods,
certifications, projects, awards, and employers that are LITERALLY written
somewhere in the CANDIDATE CV block below. It is a critical failure to
mention anything the CV does not contain.

Numbers preservation: every metric you cite — percentages, counts, revenue,
users, time saved, cases handled, students taught, deals closed, response
times, retention rates, attrition rates, lead times, etc. — MUST appear
verbatim in the CV. Do NOT invent metrics. Do NOT round numbers to nicer
values. Do NOT translate units.

Examples of what you MUST NOT do (cross-industry):
  - JD mentions "Python, JMeter, Selenium, GitHub" but CV only lists
    "Java, LoadRunner, VuGen, JIRA" → FORBIDDEN to claim Python / JMeter /
    Selenium / GitHub experience.
  - JD says "Scrum, Kanban" + CV has no agile methodology → do NOT claim agile.
  - JD wants "AWS" + CV doesn't mention AWS/cloud → do NOT claim cloud.
  - JD wants "GCSE chemistry teaching experience" + CV only lists primary-
    school teaching → FORBIDDEN to claim secondary-school experience.
  - JD wants "M&A advisory" + CV only lists corporate audit → FORBIDDEN to
    claim transactional / deal-side experience.
  - JD wants "Salesforce admin certification" + CV doesn't list it → do
    NOT claim certification.

If a paragraph would require JD-only terms the CV doesn't support, choose
a DIFFERENT angle that IS grounded in the CV. The candidate's existing
strengths are always enough.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CANDIDATE : {candidate_name}
ROLE      : {job_title}
COMPANY   : {company}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

JOB DESCRIPTION:
{job_description}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

CANDIDATE CV:
{cv_text}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{strategy_block}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

WRITE THE COMPLETE LETTER BODY NOW (exactly 4 paragraphs, blank line
between paragraphs, 310-380 words total, no salutation, no sign-off,
no truncation):
"""


# ─────────────────────────────────────────────────────────────
# Helpers (all unchanged)
# ─────────────────────────────────────────────────────────────

def finalize_cover_letter(body_text: str, candidate_name: str) -> str:
    """
    Wrap the LLM's body text with a salutation and sign-off so the renderer
    receives the full cover-letter contract (sal / body / signoff / name).

    P7-followup (Apr 28): salutation reverted to "Dear Hiring Manager"
    per user preference (the brief P9 switch to "Dear Manager" was too
    casual). The PDF renderers append the trailing comma on each side of
    the salutation/signoff during layout.
    """
    body = (body_text or "").strip()
    body = _strip_accidental_salutation_signoff(body, candidate_name)
    return (
        f"Dear Hiring Manager\n\n"
        f"{body}\n\n"
        f"Warm Regards\n\n"
        f"{candidate_name.strip()}"
    )


def _strip_accidental_salutation_signoff(body: str, candidate_name: str) -> str:
    t = body.strip()
    t = re.sub(r"(?is)^\s*(dear|to)\s+[^,\n]+[,]?\s*\n+", "", t, count=1)
    t = re.sub(
        r"(?is)\n+\s*(yours\s+sincerely|yours\s+faithfully|best\s+regards|kind\s+regards|"
        r"sincerely|warm\s+regards)\b[^\n]*\n+.*$",
        "",
        t,
    )
    cn = (candidate_name or "").strip()
    if cn and t.rstrip().endswith(cn):
        t = t.rstrip()[: -len(cn)].rstrip()
    return t.strip()


# ─────────────────────────────────────────────────────────────
# Post-generation fabrication guard
# ─────────────────────────────────────────────────────────────

_CAPTERM_RX = re.compile(
    r"\b(?:[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+"   # CamelCase  (LoadRunner, JMeter)
    r"|[A-Z]{2,}(?:\+\+|#)?"                     # Acronyms   (AWS, C++, C#)
    r"|[A-Z][A-Za-z]*(?:\.[A-Za-z]+)+)"          # Dotted     (Node.js, Vue.js)
)

# Generic capitalized words that appear in every CV/cover letter and must not
# be flagged as fabrications (company-agnostic; tool-agnostic).
_CL_STOPWORDS = {
    "I", "The", "A", "An", "And", "Or", "But", "My", "We", "Our", "You", "Your",
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December",
    "Dear", "Hiring", "Manager", "Regards", "Sincerely",
    "MSc", "BSc", "MBA", "PhD", "BA", "MA",
}


# ─────────────────────────────────────────────────────────────
# May 1: deterministic banned-phrase post-validator.
#
# Background: yesterday's run (LangFuseLogsRun2) showed all 3 cover
# letters using "I'm confident", "I'm excited", "In my first 90 days",
# "I look forward to discussing" — every one of these is in the prompt's
# banned list, but the LLM ignored them. The prompt's "do not say X"
# instructions are ~70% effective; deterministic post-checks plug the
# remaining 30%.
#
# Rule: if the produced letter contains any of the templated tells
# below (case-insensitive substring match), trigger one retry with a
# directive listing the SPECIFIC phrases the LLM used. After the retry,
# ship whatever we have (don't loop indefinitely — total cover-letter
# token budget is bounded).
#
# NOTE: "willing to relocate" is INTENTIONALLY OMITTED per user's
# instruction (May 1) — they may legitimately want to express relocation
# willingness when applying to non-Ireland roles. Only the corporate-
# filler tells get blocked.
# ─────────────────────────────────────────────────────────────
_BANNED_COVER_LETTER_PHRASES: tuple = (
    "passionate team player",
    "i believe i would be a great fit",
    "results-driven",
    "synergy",
    "i have always dreamed",
    "fast-paced",
    "i look forward to hearing from you",
    "i look forward to discussing",
    "i look forward to exploring",
    "i'm confident that",
    "i am confident that",
    "i'm excited about",
    "i am excited about",
    "i am thrilled",
    "in my first 90 days",
    "in the first 90 days",
    "available to start immediately",
    "i'm available to start",
    "make a meaningful impact",
    "make a real difference",
    "my passion for",
    "shares my passion",
    "deliver high-impact",
    "deliver measurable value",
    "drive business value",
    "drive real impact",
    "valuable asset to your team",
    "strong fit for this role",
)


def _banned_phrases_in_letter(body: str) -> list:
    """
    Returns the list of templated tells found in the cover letter body
    (case-insensitive). Empty list = clean letter.
    """
    if not body:
        return []
    body_l = body.lower()
    return [p for p in _BANNED_COVER_LETTER_PHRASES if p in body_l]


# ─────────────────────────────────────────────────────────────
# May 1: deterministic banned-phrase stripper.
#
# Run 3 + Harvey-Nash test showed the LLM swapping one banned phrase
# for another on every retry: attempt 1 had "I'm confident", attempt 2
# fixed that but added "In my first 90 days", attempt 3 fixed that but
# brought back "fast-paced" and "I'm excited about". Three retries, no
# convergence, +3,500 wasted tokens per letter.
#
# Better fix: strip these phrases deterministically AFTER the LLM is
# done. They're all corporate filler — when removed, the surrounding
# sentence either stands on its own ("I can make a meaningful
# contribution" instead of "I'm confident that I can make a meaningful
# contribution") or the entire sentence is filler-only and can be
# dropped. The replacements below are tuned so the resulting prose
# stays readable; sentences that would degenerate to nonsense get
# dropped wholesale by `_drop_orphan_sentences`.
#
# Rule: this strip runs AFTER the foreign-terms guard and AFTER the
# LLM retry loop. It's the last stop before shipping the letter.
# ─────────────────────────────────────────────────────────────

# (lowercase pattern, replacement). Pattern matches case-insensitively.
# Replacements are spliced verbatim — preserve their casing.
_BANNED_PHRASE_REPLACEMENTS: tuple = (
    # "I'm confident that I can…" → "I can…"
    (r"\bi['\u2019]?m\s+confident\s+that\s+", ""),
    (r"\bi\s+am\s+confident\s+that\s+", ""),
    # "I'm excited about the prospect of Ving X" — the tail is a gerund
    # phrase ("Ving X"), so replace with "I am drawn to" + gerund to keep
    # grammar intact without the awkward "contribute by contributing".
    (r"\bi['\u2019]?m\s+excited\s+about\s+the\s+prospect\s+of\s+",
     "I am drawn to the chance of "),
    (r"\bi\s+am\s+excited\s+about\s+the\s+prospect\s+of\s+",
     "I am drawn to the chance of "),
    # "I'm excited about the opportunity to V X" — the tail is an
    # infinitive ("to V X"), so replace with "I would welcome the chance to"
    # which slots in ahead of "to V" naturally.
    (r"\bi['\u2019]?m\s+excited\s+about\s+the\s+opportunity\s+to\s+",
     "I would welcome the chance to "),
    (r"\bi\s+am\s+excited\s+about\s+the\s+opportunity\s+to\s+",
     "I would welcome the chance to "),
    # Generic fallback "I'm excited about X" / "I am excited about X"
    # — drop the hedge, keep the substance. Safe when the preceding
    # "the prospect of / the opportunity to" variants didn't match.
    (r"\bi['\u2019]?m\s+excited\s+about\s+", ""),
    (r"\bi\s+am\s+excited\s+about\s+", ""),
    (r"\bi\s+am\s+thrilled\s+(?:to|about|by)\s+", ""),
    # "fast-paced," / "fast-paced " — drop the adjective, keep the noun.
    (r"\bfast[-\s]paced[,]?\s+", ""),
    # "make a meaningful impact / contribution / difference" — drop, the
    # surrounding sentence usually stands on its own.
    (r"\bmake\s+a\s+meaningful\s+(impact|contribution|difference)\b", r"contribute"),
    (r"\bmake\s+a\s+real\s+difference\b", "contribute"),
    # "my passion for X" → "my interest in X" — keep the preposition
    # chain readable (dropping to empty leaves awkward "aligns with
    # building scalable AI…" constructions).
    (r"\bshares?\s+my\s+passion\s+for\s+", "values "),
    (r"\bmy\s+passion\s+for\s+", "my interest in "),
    # "In my first 90 days" → "In my first weeks" (less templated)
    (r"\bin\s+(?:my|the)\s+first\s+90\s+days\b", "In my first weeks"),
    # Closing-paragraph filler.
    (r"\bi\s+look\s+forward\s+to\s+(?:hearing\s+from\s+you|discussing|exploring)\b",
     "I would welcome a conversation"),
    # Generic "available to start immediately" — drop (the sender's
    # availability is signalled by the application itself).
    (r"\bavailable\s+to\s+start\s+immediately\b", "available immediately"),
    (r"\bi['\u2019]?m\s+available\s+to\s+start\b", "I am available"),
    # "deliver high-impact" / "deliver measurable value" — drop hedge.
    (r"\bdeliver\s+high[-\s]impact\s+", "deliver "),
    (r"\bdeliver\s+measurable\s+value\b", "deliver outcomes"),
    # "drive business value" / "drive real impact" — drop hedge.
    (r"\bdrive\s+business\s+value\b", "drive outcomes"),
    (r"\bdrive\s+real\s+impact\b", "drive outcomes"),
    # "valuable asset to your team" — generic filler, drop.
    (r"\bvaluable\s+asset\s+to\s+your\s+team\b", "useful contributor"),
    # "make(s) me a strong fit for this role" — replace the full predicate
    # so we don't leave an orphan "make me." fragment behind.
    (r"\bmake(?:s)?\s+(?:me|him|her)\s+(?:a\s+)?strong\s+fit\s+for\s+this\s+role\b",
     "match this role well"),
    # Bare "strong fit for this role" — drop the tail; nearby grammar
    # usually carries it (". A strong fit..." → ".").
    (r"[,]?\s*(?:a\s+)?strong\s+fit\s+for\s+this\s+role\b", ""),
    # "I believe I would be a great fit" — drop entirely.
    (r"\bi\s+believe\s+i\s+would\s+be\s+(?:a\s+)?great\s+fit\b", ""),
    # "passionate team player" — drop.
    (r"\bpassionate\s+team\s+player\b", "team contributor"),
    # "results-driven" — drop hyphenated adjective.
    (r"\bresults[-\s]driven[,]?\s+", ""),
    # "synergy" — drop (no good fix; the surrounding sentence usually
    # works without it).
    (r"\bsynergy\b", "alignment"),
    # "I have always dreamed of" — drop the whole hedge.
    (r"\bi\s+have\s+always\s+dreamed\s+of\s+", ""),
)


def _strip_banned_phrases(body: str) -> tuple:
    """
    Deterministic post-pass. Returns (cleaned_body, list_of_phrases_stripped).
    Empty list = no changes made.
    """
    if not body:
        return body, []

    out = body
    stripped: List[str] = []
    for pattern_str, replacement in _BANNED_PHRASE_REPLACEMENTS:
        rx = re.compile(pattern_str, re.IGNORECASE)
        new_out, n = rx.subn(replacement, out)
        if n > 0:
            stripped.append(pattern_str)
            out = new_out

    # Cleanup: collapse double spaces and orphan punctuation introduced
    # by the deletions.
    out = re.sub(r"  +", " ", out)
    out = re.sub(r"\s+([,.;:!?])", r"\1", out)
    out = re.sub(r"\(\s+", "(", out)
    out = re.sub(r"\s+\)", ")", out)
    # Capitalise the first letter of each sentence (some replacements
    # leave a lowercase word at the start of a sentence).
    def _cap(m):
        return m.group(1) + m.group(2).upper()
    out = re.sub(r"(^|[.!?]\s+)([a-z])", _cap, out)

    return out.strip(), stripped


# ─────────────────────────────────────────────────────────────
# May 1: render strategist output for the cover letter prompt.
#
# The cover letter consumes a tight subset of the strategist's JSON:
#   • narrative_angle            — must reinforce in the letter's framing
#   • cover_letter_hook.pattern  — one of industry_observation /
#                                   role_insight / concrete_achievement
#   • cover_letter_hook.opening_topic — first-sentence framing (no
#                                   candidate self-reference)
#   • do_not_inject              — JD-only terms the CV doesn't have
#                                   (microservices, cloud, etc.); the
#                                   letter MUST NOT mention these.
#
# The render is plain text injected before the JD/CV blocks so the
# directives sit close to the structural rules (paragraph layout,
# banned phrases). Returns "" when there's no strategy so legacy
# callers see the unchanged prompt.
# ─────────────────────────────────────────────────────────────

def _render_strategy_for_cover_letter(strategy: Dict[str, Any]) -> str:
    if not strategy or strategy.get("_source") == "empty":
        return ""

    lines: List[str] = []
    lines.append("═══════════════════════════════════════════════════════════════════════")
    lines.append("STRATEGY (BINDING — the CV tailoring used this same strategy):")
    lines.append("═══════════════════════════════════════════════════════════════════════")

    angle = (strategy.get("narrative_angle") or "").strip()
    if angle:
        lines.append(f"\nNARRATIVE ANGLE: {angle}")
        lines.append(
            "The cover letter must reinforce this same angle — same story,"
            " told in prose. The CV bullets and the letter must agree."
        )

    hook = strategy.get("cover_letter_hook") or {}
    pattern = (hook.get("pattern") or "").strip().lower()
    topic   = (hook.get("opening_topic") or "").strip()
    if pattern:
        pattern_label = {
            "industry_observation":  "PATTERN A — INDUSTRY OBSERVATION",
            "role_insight":          "PATTERN B — ROLE INSIGHT",
            "concrete_achievement":  "PATTERN C — CONCRETE ACHIEVEMENT",
        }.get(pattern, f"PATTERN: {pattern}")
        lines.append(f"\nHOOK PATTERN: {pattern_label}")
        if topic:
            lines.append(f'OPENING TOPIC for sentence 1: "{topic}"')
        lines.append(
            "Use this exact pattern. Do NOT improvise a different opening."
        )

    dni = strategy.get("do_not_inject") or []
    if dni:
        lines.append("\nDO NOT MENTION (JD terms not in the CV — fabrication):")
        lines.append(f"  {dni}")
        lines.append(
            "The letter is rejected if any of these terms appear in it."
        )

    return "\n".join(lines)


def _foreign_terms_in_letter(body: str, cv_text: str, company: str, job_title: str) -> list:
    """
    Find capitalized/acronym tokens in `body` that are NOT present in the CV
    text, the company name, or the job title. These usually indicate tools
    or frameworks the LLM invented from the JD. Returns a deduped list.
    """
    if not body:
        return []
    haystack = " ".join([cv_text or "", company or "", job_title or ""]).lower()
    foreign: list = []
    seen: set = set()
    for m in _CAPTERM_RX.finditer(body):
        term = m.group(0).strip()
        if term in _CL_STOPWORDS:
            continue
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        if key not in haystack:
            foreign.append(term)
    return foreign


_TRUNCATION_TRAILERS = (
    " and", " or", " but", " with", " for", " to", " of", " in", " by",
    " that", " which", " while", " when", " where", " as", " a", " an", " the",
    " from", " on", " into",
)


def _cover_letter_is_complete(body: str) -> bool:
    """
    Return False when the body looks truncated — too short, ends mid-word,
    or trails off on a connective word. These letters would otherwise ship
    as a 2-line stub like the AIB case in the April 22 run.
    """
    if not body:
        return False
    stripped = body.strip()
    if len(stripped) < 500:
        return False  # <~80 words — always too short for a 340-400 word letter

    word_count = len(stripped.split())
    if word_count < 140:
        return False

    # Last non-whitespace char should be sentence-ending punctuation.
    if stripped[-1] not in ".!?\"'":
        return False

    # Reject endings that trail off on connectives (e.g. "... customers and").
    lower = stripped.lower()
    for trailer in _TRUNCATION_TRAILERS:
        if lower.endswith(trailer) or lower.endswith(trailer + "."):
            return False

    return True


def _parse_retry_seconds(error_message: str) -> float:
    match = re.search(r"Please try again in (\d+)m([\d.]+)s", str(error_message))
    if match:
        return int(match.group(1)) * 60 + float(match.group(2))
    match = re.search(r"Please try again in ([\d.]+)s", str(error_message))
    if match:
        return float(match.group(1))
    return 60


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────

def generate_cover_letter(
    cv_text:         str,
    job_description: str,
    job_title:       str = "",
    company:         str = "",
    candidate_name:  str = "",
    retries:         int = 3,
    strategy:        Optional[Dict[str, Any]] = None,
) -> str:
    """
    `strategy` is the optional output of agents.tailor_strategist (same dict
    shape used by cv_diff_tailor). When provided, the narrative_angle and
    cover_letter_hook fields are rendered into the prompt as binding
    guidance — the letter must use the strategy's hook pattern and reinforce
    the same angle as the tailored CV. When omitted, the letter falls back
    to deciding its own opening and angle (legacy behaviour).
    """
    from agents.runtime       import track_llm_call, handle_rate_limit, BudgetExceeded
    from agents.prompt_safety import wrap_untrusted_block, untrusted_block_preamble
    from agents.llm_client    import chat_deepseek, chat_gemini, chat_quality, last_llm_source

    jd_wrapped = wrap_untrusted_block(job_description, label="JOB_DESCRIPTION")
    cv_wrapped = wrap_untrusted_block(cv_text,         label="CANDIDATE_CV")
    preamble   = untrusted_block_preamble(["JOB_DESCRIPTION", "CANDIDATE_CV"])

    # Render the strategist's narrative_angle + hook directives into a
    # short directive block. Empty string when no strategy was supplied
    # so legacy behaviour is preserved.
    strategy_block = _render_strategy_for_cover_letter(strategy or {})

    prompt = COVER_LETTER_PROMPT.format(
        cv_text         = cv_wrapped,
        job_description = jd_wrapped,
        job_title       = job_title,
        company         = company,
        candidate_name  = candidate_name,
        safety_preamble = preamble,
        strategy_block  = strategy_block,
    )

    token_budgets = [900, 1200, 1500]

    for attempt in range(retries):
        try:
            track_llm_call(agent="cover_letter")

            budget = token_budgets[min(attempt, len(token_budgets) - 1)]

            # Provider chain (May 2026): DeepSeek → Gemini → Groq.
            # On attempt 0 we try DeepSeek first (better instruction-following,
            # less boilerplate); on falls-through or non-completion we drop
            # through to the existing Gemini→Groq fallback. Subsequent retries
            # skip DeepSeek and Gemini entirely (we already know they're not
            # producing valid output for this artifact this run, and retries
            # benefit most from Groq's larger token budget headroom).
            if attempt == 0:
                raw = chat_deepseek(
                    prompt, max_tokens=budget, temperature=0.4
                )
                if not raw or not _cover_letter_is_complete(raw):
                    if raw:
                        print(
                            f"   ↪️  Cover letter: DeepSeek incomplete on attempt 1 "
                            f"(len={len(raw)}) — trying Gemini"
                        )
                    raw = chat_gemini(prompt, max_tokens=budget, temperature=0.4)
                    if not raw or not _cover_letter_is_complete(raw):
                        fail_len = len(raw or "")
                        print(
                            f"   ↪️  Cover letter: Gemini truncated/empty on attempt 1 "
                            f"(len={fail_len}) — instant Groq fallback"
                        )
                        raw = chat_quality(prompt, max_tokens=budget, temperature=0.4)
            else:
                # Retries always go to Groq (DeepSeek/Gemini quota is precious
                # + we already know they're failing for this artifact this run).
                raw = chat_quality(prompt, max_tokens=budget, temperature=0.4)

            if not raw:
                print(f"   ⚠️  Cover letter empty on attempt {attempt + 1} — retrying...")
                time.sleep(4)
                continue

            if not _cover_letter_is_complete(raw):
                print(
                    f"   ⚠️  Cover letter looks truncated on attempt {attempt + 1} "
                    f"(len={len(raw)}, ends={raw[-40:]!r}) — retrying with larger budget"
                )
                time.sleep(2)
                continue

            # ── Fabrication guard ─────────────────────────────
            # Flag capitalized/acronym tokens not present in the CV. The
            # threshold (>=2) tolerates a single legitimate proper noun
            # (e.g. the company's product line) while still catching
            # JD-keyword invention like "Python, JMeter, Selenium".
            foreign = _foreign_terms_in_letter(raw, cv_text, company, job_title)
            if len(foreign) >= 2:
                print(
                    f"   ⚠️  Cover letter introduced CV-foreign terms {foreign!r} "
                    f"on attempt {attempt + 1} — retrying with stricter prompt"
                )
                if attempt < retries - 1:
                    # Tighten prompt on next pass.
                    prompt = (
                        prompt
                        + f"\n\nRETRY NOTE: Your previous draft mentioned "
                          f"{', '.join(foreign)} which do NOT appear in the CV. "
                          f"Rewrite using ONLY terms literally present in the CV."
                    )
                    time.sleep(2)
                    continue
                # Last attempt still fabricated — fall through to placeholder.
                break

            # ── Deterministic banned-phrase strip ─────────────
            # May 1: replaced the previous "retry loop on banned phrases"
            # with a deterministic post-pass. The retry loop was burning
            # ~3,500 tokens per letter swapping one templated tell for
            # another (Run 3 + Harvey-Nash test both showed this). The
            # strip removes the hedges/filler in-place and leaves the
            # surrounding sentence intact — same readable letter, no
            # extra LLM calls.
            cleaned_raw, stripped_phrases = _strip_banned_phrases(raw)
            if stripped_phrases:
                print(
                    f"   🧹 Cover letter: stripped {len(stripped_phrases)} "
                    f"banned filler pattern(s) deterministically "
                    f"(no extra LLM call)."
                )
                raw = cleaned_raw

            # Re-check after the strip — rare, but a phrase from the
            # banned list might not be covered by the replacement table.
            # Log and ship; the letter is still better than the LLM's
            # raw output.
            residual_banned = _banned_phrases_in_letter(raw)
            if residual_banned:
                print(
                    f"   ⚠️  Cover letter has residual banned phrases "
                    f"{residual_banned[:3]!r} not in the replacement table — "
                    f"shipping anyway (these are minor)."
                )

            # Apr 28 follow-up: log which LLM produced the kept output.
            # Helps diagnose "is Gemini ever actually used?" without combing
            # through all the call-attempt logs.
            print(
                f"   ✅ Cover letter via {last_llm_source()} "
                f"(attempt {attempt + 1}, {len(raw.split())} words)"
            )
            return finalize_cover_letter(raw, candidate_name)

        except Exception as e:
            # M4: BudgetExceeded must propagate. handle_rate_limit() raises it
            # when Groq says "wait > MAX_RATE_LIMIT_WAIT" — the whole point is
            # to abort the run cleanly instead of hanging 10+ minutes. The old
            # broad-except silently swallowed it and continued the retry loop,
            # defeating the safety mechanism entirely.
            if isinstance(e, BudgetExceeded):
                raise
            err = str(e).lower()
            if any(x in err for x in ["rate", "429", "quota", "resource"]):
                # handle_rate_limit may itself raise BudgetExceeded — let it.
                handle_rate_limit(_parse_retry_seconds(str(e)), agent="cover_letter")
            else:
                print(f"   ❌ Cover letter error (attempt {attempt + 1}): {e}")
                if attempt < retries - 1:
                    time.sleep(4)

    # ── Placeholder fallback ─────────────────────────────────
    # MUST be CV-agnostic. We never know which user the LLM call failed for,
    # so the placeholder cannot reference any specific employer, school, or
    # achievement — doing so would fabricate a CV for whoever runs this app.
    # Better to ship a short, neutral letter and surface the failure than
    # to ship a confident-sounding letter that's factually wrong about the user.
    print("   ⚠️  Cover letter failed after retries — using neutral placeholder")
    role_phrase   = f"the {job_title} role" if job_title else "this role"
    company_at    = f" at {company}" if company else ""
    placeholder_body = (
        f"I am writing to express my strong interest in {role_phrase}{company_at}. "
        f"After reviewing the job description, I believe my background aligns well "
        f"with what you are looking for, and I would welcome the chance to discuss "
        f"how my experience can contribute to your team.\n\n"
        f"Please find my CV attached, which outlines the experience and skills most "
        f"relevant to this position. I would be glad to walk through any specific "
        f"areas in more detail at your convenience.\n\n"
        f"Thank you for considering my application. I look forward to the opportunity "
        f"to discuss how I can support your team's goals."
    )
    return finalize_cover_letter(placeholder_body, candidate_name or "")


# ─────────────────────────────────────────────────────────────
# Test
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_cv = """
    Rishav Singh | Dublin, Ireland | rishav98sin@gmail.com

    SUMMARY
    QA and Performance Testing specialist with 4+ years experience at IBM and Accenture.
    MSc Management from Trinity College Dublin. Transitioning to Product Management.

    EXPERIENCE
    IBM - Test Specialist (2023-2024)
    - Reduced system response time by 40% through performance bottleneck identification

    Accenture - Consultant (2020-2023)
    - Led team of 5 engineers, delivered 3 major releases on time
    """

    test_jd = "Product Manager - Stripe, Dublin. API products, financial services, Agile."

    print("Writing cover letter...\n")
    letter = generate_cover_letter(
        cv_text         = test_cv,
        job_description = test_jd,
        job_title       = "Product Manager",
        company         = "Stripe",
        candidate_name  = "Rishav Singh",
    )
    print(letter)