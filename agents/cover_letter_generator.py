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
- Reference something real and specific about the company
- Show authentic passion for what this company is trying to do
- NEVER start with "I am writing to apply..." or "I am excited to apply..."
- NEVER use: "dynamic", "fast-paced", "passionate team player", "I believe I would be a great fit"

PARAGRAPH 2 — VALUE + CONTRIBUTION BLOCK 1 (3-4 sentences):
- Pick the single most relevant experience from the CV for THIS specific role
- Structure: Where they worked → What problem they solved → What they did → Measurable outcome
- Name the employer, the challenge, and the specific result with numbers
- End by explicitly connecting this to what the company needs right now

PARAGRAPH 3 — VALUE + CONTRIBUTION BLOCK 2 (3-4 sentences):
- Pick a DIFFERENT experience that addresses a different JD requirement
- Show the candidate's unique angle — what they bring that others likely don't
- Connect their background to a specific challenge or goal the company has

PARAGRAPH 4 — CLOSE (2-3 sentences):
- Express specific enthusiasm for what this role offers the candidate
- Confident, warm closing thought — NOT "I look forward to hearing from you"
- Do NOT add any closing line like "Yours sincerely" or the candidate's name

STRICT RULES:
- 340-400 words total
- Every fact must come ONLY from the CV — never invent anything
- Plain text only — no headers, bullets, asterisks, markdown
- Do NOT include "Dear Hiring Manager", "Warm Regards", or any salutation/sign-off

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

WRITE THE LETTER BODY NOW (4 paragraphs only, blank line between paragraphs):
"""


# ─────────────────────────────────────────────────────────────
# Helpers (all unchanged)
# ─────────────────────────────────────────────────────────────

def finalize_cover_letter(body_text: str, candidate_name: str) -> str:
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
    candidate_name:  str = "Rishav Singh",
    retries:         int = 3,
) -> str:
    from agents.runtime       import track_llm_call, handle_rate_limit
    from agents.prompt_safety import wrap_untrusted_block, untrusted_block_preamble
    from agents.llm_client    import chat_gemini

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
            raw = chat_gemini(prompt, max_tokens=budget, temperature=0.4)

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

            return finalize_cover_letter(raw, candidate_name)

        except Exception as e:
            err = str(e).lower()
            if any(x in err for x in ["rate", "429", "quota", "resource"]):
                handle_rate_limit(_parse_retry_seconds(str(e)), agent="cover_letter")
            else:
                print(f"   ❌ Cover letter error (attempt {attempt + 1}): {e}")
                if attempt < retries - 1:
                    time.sleep(4)

    # ── Placeholder fallback (unchanged) ──────────────────────
    print("   ⚠️  Cover letter failed after retries — using placeholder")
    placeholder_body = (
        f"I am writing to express my strong interest in the {job_title} role at {company}. "
        f"Having spent four years delivering complex technical projects at IBM and Accenture, "
        f"I understand the demands of product-driven environments.\n\n"
        f"My experience at IBM involved leading performance testing for banking clients — "
        f"reducing system response times by 40% through systematic bottleneck identification.\n\n"
        f"At Accenture, I led a team of five engineers delivering three major releases on "
        f"schedule for Elevance Health, navigating competing priorities across cross-functional "
        f"stakeholders.\n\n"
        f"I would welcome the opportunity to discuss how my background aligns with your needs."
    )
    return finalize_cover_letter(placeholder_body, candidate_name)


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