# agents/cover_letter_generator.py

import os
import re
import time
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
- Open with a bold, specific statement that immediately signals fit. Lead
  with ONE of three patterns:
    1. A sharp, concrete achievement from the CV with its real metric.
    2. A forward-looking value claim about what this role really needs.
    3. A direct insight into the role's core challenge that the JD signals.
- Reference something real and specific about the company (its product,
  market position, mission, or a fact stated in the JD itself).
- Make it sound like a confident human, not a template.
- BANNED openers: "I am writing to apply...", "I am excited to apply...",
  "I would like to express my interest...", "I have always dreamed of...".

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
) -> str:
    from agents.runtime       import track_llm_call, handle_rate_limit, BudgetExceeded
    from agents.prompt_safety import wrap_untrusted_block, untrusted_block_preamble
    from agents.llm_client    import chat_gemini, chat_quality, last_llm_source

    jd_wrapped = wrap_untrusted_block(job_description, label="JOB_DESCRIPTION")
    cv_wrapped = wrap_untrusted_block(cv_text,         label="CANDIDATE_CV")
    preamble   = untrusted_block_preamble(["JOB_DESCRIPTION", "CANDIDATE_CV"])

    prompt = COVER_LETTER_PROMPT.format(
        cv_text         = cv_wrapped,
        job_description = jd_wrapped,
        job_title       = job_title,
        company         = company,
        candidate_name  = candidate_name,
        safety_preamble = preamble,
    )

    token_budgets = [900, 1200, 1500]

    for attempt in range(retries):
        try:
            track_llm_call(agent="cover_letter")

            budget = token_budgets[min(attempt, len(token_budgets) - 1)]

            # Strategy B (Apr 28 follow-up): single Gemini attempt with
            # INSTANT Groq fallback on truncation/empty. Subsequent retries
            # use Groq directly — no point burning Gemini quota on a model
            # that has already demonstrated it's truncating prose mid-stream
            # for this run's free-tier window.
            if attempt == 0:
                raw = chat_gemini(prompt, max_tokens=budget, temperature=0.4)
                if not raw or not _cover_letter_is_complete(raw):
                    fail_len = len(raw or "")
                    print(
                        f"   ↪️  Cover letter: Gemini truncated/empty on attempt 1 "
                        f"(len={fail_len}) — instant Groq fallback"
                    )
                    raw = chat_quality(prompt, max_tokens=budget, temperature=0.4)
            else:
                # Retries always go to Groq (Gemini quota is precious + we
                # already know it's failing for this artifact this run).
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

            # ── Banned-phrase guard ───────────────────────────
            # May 1: deterministic check for templated tells the LLM
            # ignores in the prompt rules ("I'm confident", "I'm excited",
            # "In my first 90 days", "look forward to discussing", etc.).
            # See _BANNED_COVER_LETTER_PHRASES for the list. We only
            # trigger ONE retry on this — running multiple retries would
            # explode the cover-letter token cost without much extra
            # quality. If the retry still has filler, ship it (the
            # alternative is a placeholder, which is worse).
            banned = _banned_phrases_in_letter(raw)
            if banned and attempt < retries - 1:
                print(
                    f"   ⚠️  Cover letter used banned filler phrases "
                    f"{banned[:5]!r} on attempt {attempt + 1} — retrying "
                    f"with stricter directive"
                )
                prompt = (
                    prompt
                    + f"\n\nRETRY NOTE — DO NOT REPEAT THIS MISTAKE: "
                      f"Your previous draft contained the templated tells "
                      f"{', '.join(repr(p) for p in banned)}. Rewrite the "
                      f"letter without ANY of those phrases. Replace each "
                      f"with a CONCRETE statement grounded in either the "
                      f"CV or the JD. The reader can tell when a letter is "
                      f"templated — these are the exact tells that give it "
                      f"away. Returning a draft that still contains any "
                      f"of these phrases will be rejected."
                )
                time.sleep(2)
                continue
            elif banned:
                # Last attempt still has filler — log and ship rather than
                # discard the letter (placeholder fallback is worse).
                print(
                    f"   ⚠️  Cover letter shipping with banned filler "
                    f"{banned[:3]!r} (retries exhausted)"
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