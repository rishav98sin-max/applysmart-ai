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
You are an elite cover letter writer. Your letters land interviews at top companies
because they feel deeply personal, specific and genuinely motivated — never generic.

{safety_preamble}

Write the BODY of a cover letter only (no greeting, no sign-off, no candidate name).
The application will add "Dear Hiring Manager" and "Warm Regards" + the candidate's name automatically.

Use exactly 4 paragraphs, separated by a single blank line between paragraphs.

PARAGRAPH 1 — HOOK + MOTIVATION (3-4 sentences):
- Open with a specific, genuine reason WHY this candidate wants THIS role at THIS company
- Reference something real and specific about the company (its product, mission, market position)
- Show authentic passion for what this company is trying to do
- NEVER start with "I am writing to apply..." or "I am excited to apply..." or "I would like to express my interest..."
- NEVER use: "dynamic", "fast-paced", "passionate team player", "I believe I would be a great fit", "results-driven"

PARAGRAPH 2 — VALUE + CONTRIBUTION BLOCK 1 (3-4 sentences):
- Pick the single most relevant experience from the CV for THIS role
- Structure: Where they worked → What problem they solved → What they did → MEASURABLE OUTCOME
- Name the employer, the challenge, and the specific result with the actual number from the CV
- End by explicitly connecting this to what the company needs right now

PARAGRAPH 3 — VALUE + CONTRIBUTION BLOCK 2 (3-4 sentences):
- Pick a DIFFERENT achievement that addresses a different JD requirement
- This achievement may come from a second employer OR from a named CV project
  (e.g. "ApplySmart AI", "VoC Insight Hub") if a project demonstrates a
  JD-relevant capability the candidate's work history doesn't already cover
- Show the candidate's unique angle — what they bring that others likely don't
- Include ONE concrete GTM-style sentence about how this experience would
  translate to the new role: e.g. "In my first 90 days I would prioritise X
  because the JD signals Y" or "This is directly applicable to <specific
  challenge from JD>". This is MANDATORY — the letter must show product
  thinking, not just past achievements.

PARAGRAPH 4 — CLOSE + CALL TO ACTION (2-3 sentences):
- Clear ask (meeting, introductory call, or interview)
- Confident, warm closing — NOT "I look forward to hearing from you"
- Do NOT add "Yours sincerely" or the candidate's name (those are added automatically)
- ABSOLUTELY FORBIDDEN: restating the company name, restating role enthusiasm,
  or paraphrasing paragraph 1. The closing paragraph must add NEW information
  (availability, willingness to relocate, specific format preference, or a
  one-line forward-looking note) — never recap.

═══════════════════════════════════════════════════════════════════════
STRICT RULES (each is a critical failure if broken)
═══════════════════════════════════════════════════════════════════════
- TOTAL word count: 280-380 words across all 4 paragraphs combined.
- Plain text only — no markdown, no bullets, no asterisks, no headers.
- Do NOT include "Dear Hiring Manager", "Warm Regards", or any salutation/sign-off.
- Output the COMPLETE letter — do not stop mid-sentence.
- Sentence-starter cap: AT MOST 2 sentences across the entire letter may
  begin with "I am". This forces varied openings and prevents the repetitive
  "I am excited / I am confident / I am impressed" pile-up.
- Ban generic company praise: do NOT call the company "innovative",
  "industry-leading", "world-class", "trusted", "sustainable", or any
  similar adjective UNLESS that exact word appears in the JOB DESCRIPTION
  block below. Stick to facts the JD actually states.
- Each paragraph must end with a different sentence pattern — do not let
  consecutive paragraphs end with "...this role" or "...the team".

═══════════════════════════════════════════════════════════════════════
HARD FABRICATION BAN (ZERO TOLERANCE)
═══════════════════════════════════════════════════════════════════════
You may reference ONLY tools, technologies, languages, frameworks, methods,
certifications, projects, and employers that are LITERALLY written somewhere
in the CANDIDATE CV block below. It is a critical failure to mention anything
the CV does not contain.

Numbers preservation: every percentage, count, multiplier, or measure that
you cite as a candidate achievement MUST appear verbatim in the CV. Do NOT
invent metrics. Do NOT round numbers to nicer values.

Examples of what you MUST NOT do:
  - JD mentions "Python, JMeter, Selenium, GitHub" but CV only lists
    "Java, LoadRunner, VuGen, JIRA" → FORBIDDEN to claim Python / JMeter /
    Selenium / GitHub experience.
  - JD says "Scrum, Kanban" + CV has no agile methodology → do NOT claim agile.
  - JD wants "AWS" + CV doesn't mention AWS/cloud → do NOT claim cloud.

If a paragraph would require JD-only terms, choose a DIFFERENT angle that IS
grounded in the CV. The candidate's existing strengths are always enough.

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
between paragraphs, 280-380 words total, no salutation, no sign-off,
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