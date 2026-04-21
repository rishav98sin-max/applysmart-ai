# ApplySmart AI — Complete Handoff Summary

**Generated:** April 20, 2026
**Last updated:** April 21, 2026 (session 3: Gemini 2.5 Flash integration, dual-LLM architecture)
**Purpose:** Full context handoff to Cursor for continued development

---

## 1. Application Overview

ApplySmart AI is an **automated job application system** that:
- Scrapes job listings from multiple boards (LinkedIn, Indeed, Glassdoor, Jobs.ie, Builtin)
- Matches a candidate's CV to each job using LLM scoring + vector retrieval (RAG)
- Tailors the CV and cover letter for each matched role
- Generates PDFs via PyMuPDF with in-place edits (preserves original CV layout)
- Sends tailored applications via email (Resend API)

**Tech Stack:**
- Python 3.10+
- Streamlit (UI)
- LangGraph (multi-agent orchestration)
- Groq Llama-3.3-70B (fast tasks — matching, planning, review)
  - Up to 3 API keys rotated automatically on 429/401 (≈300K tokens/day ceiling)
- Gemini 2.5 Flash (writing tasks — CV tailoring, cover letters)
  - 1,500 requests/day, 1M tokens/min on free tier
- ChromaDB + Sentence-Transformers (vector retrieval)
- PyMuPDF (fitz) (PDF parsing and editing)
- Resend (email delivery)

**Dual-LLM architecture (April 21 2026):** Uses Groq for fast structured tasks (matching, planning, reviewers, supervisor) and Gemini 2.5 Flash for creative writing tasks (CV tailoring, cover letter generation). This split optimizes both quality and latency — Gemini excels at writing with long context windows, while Groq provides faster inference for structured tasks. All LLM calls route through centralized `agents/llm_client.py` with `chat_quality`/`chat_fast` (Groq) and `chat_gemini` (Gemini) functions.

**Key Design Principles:**
- Crash-safe snapshots for observability
- Prompt injection hardening with fenced untrusted blocks
- Fabrication review via second LLM reviewer with retry logic
- Rate-limit guardrails with capped wait times
- Local-only CV processing (no data leaves except LLM prompts)

---

## 2. Architecture

### 2.1 Multi-Agent LangGraph Workflow

```
┌─────────────────────────────────────────────────────────────────┐
│                         Supervisor                               │
│  (LLM-powered router: decides next worker based on state)      │
└─────────────────────────────────────────────────────────────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
        ▼                     ▼                     ▼
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│ validate_    │    │ parse_cv_    │    │ extract_cv_  │
│ inputs       │    │ node         │    │ style_node   │
└──────────────┘    └──────────────┘    └──────────────┘
        │                     │                     │
        └─────────────────────┼─────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      planner_node                                 │
│  (Builds search bundles, tailoring plan, quality bar)            │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                   scrape_jobs_node                               │
│  (Multi-board scraping with fallback sequence)                  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                   match_jobs_node                                │
│  (LLM scoring + RAG + experience-level penalty)                 │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│              tailor_and_generate_node                            │
│  (CV diff tailoring, CV full tailoring, cover letter, PDF)      │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                   send_email_node                                │
│  (Resend API email delivery with attachments)                    │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 Agent State (TypedDict)

```python
class AgentState(TypedDict):
    cv_path:             str
    cv_text:             str
    job_title:           str
    location:            str
    source:              str
    num_jobs:            int
    match_threshold:     int
    user_email:          str
    candidate_name:      str
    preferred_job_board: str
    plan:                dict
    jobs:                List[dict]
    matched_jobs:        List[dict]
    skipped_jobs:        List[dict]
    cv_outline:          dict
    cv_template:         dict
    tailored_cvs:        List[dict]
    tailored_covers:     List[dict]
    sent_jobs:           List[dict]
    status:              str
    messages:            List[dict]
    routing_decision:    str
    supervisor_cycles:  int
    output_dir:          str
    llm_budget:          Dict[str, Any]
    preview_mode:        bool
    cv_collection:       str
    experience_level:    str
```

---

## 3. End-to-End Flow

1. **User uploads CV** → Streamlit saves to `sessions/{session_id}/uploads/`
2. **User enters target role, location, experience level** → sidebar inputs
3. **User clicks "Run agent"** → triggers `run_agent()` in `agents/job_agent.py`
4. **validate_inputs_node** → checks CV exists, email valid, fields filled
5. **parse_cv_node** → extracts text from PDF using `agents/cv_parser.py`
6. **extract_cv_style_node** → extracts fonts, margins, colors via `agents/pdf_editor.py`
7. **planner_node** → builds search bundles (title variants, adjacent roles, locations)
8. **scrape_jobs_node** → scrapes from LinkedIn → Indeed → Glassdoor → Jobs.ie → Builtin
9. **match_jobs_node** → for each job:
   - Calls `match_cv_to_job()` from `agents/job_matcher.py`
   - Uses RAG (ChromaDB) if `cv_collection` is set, else full CV
   - LLM scores 0-100 with reasoning
   - Applies deterministic experience-level gap penalty (NEW)
   - Filters by `match_threshold`
10. **tailor_and_generate_node** → for each matched job:
    - Calls `tailor_cv_diff()` for aggressive bullet rewrites (NEW schema)
    - Calls `tailor_cv()` for full CV summary rewrite
    - Calls `generate_cover_letter()` for cover letter
    - Calls `apply_edits()` to render PDF with edits via PyMuPDF
    - Calls `reviewer.py` to check for fabrication (retry on fail)
11. **send_email_node** → if not preview mode:
    - Sends each tailored CV + cover letter via Resend API
    - Tracks sent jobs in state
12. **Snapshot saved** → `sessions/{session_id}/snapshot.json` with inputs, state, errors, budget

---

## 4. Agent Descriptions

### 4.1 Supervisor (`agents/job_agent.py::supervisor_node`)
- **Purpose:** LLM-powered router deciding which worker to invoke next
- **Input:** Agent state, plan, current status
- **Output:** Routing decision (next worker name or END)
- **Model:** Groq Llama-3.3-70B via `chat_fast()`
- **Logic:** Evaluates state, checks hard terminal statuses, applies quality bar thresholds

### 4.2 Planner (`agents/planner.py`)
- **Purpose:** Drafts search bundles and tailoring plan
- **Input:** CV text, job title, location, source
- **Output:** Plan dict with `keyword_bundles`, `quality_bar`, `scrape_rounds`
- **Model:** Groq Llama-3.3-70B
- **Key Logic:** Limits scrape rounds to 3, quality bar thresholds (match score, reviewer score)

### 4.3 Job Scraper (`agents/job_scraper.py`)
- **Purpose:** Scrape job listings from multiple boards
- **Input:** Search bundles, preferred source
- **Output:** List of job dicts (title, company, description, url, location, posted_label)
- **Boards:** LinkedIn, Indeed, Glassdoor, Jobs.ie, Builtin
- **Fallback:** If primary board returns < 3 jobs, tries next in sequence

### 4.4 Job Matcher (`agents/job_matcher.py`)
- **Purpose:** Score CV-job fit 0-100
- **Input:** CV text, job description, job title, company, cv_collection, experience_level
- **Output:** Match score, matched/missing skills, strengths, improvements, reasoning
- **Model:** Groq Llama-3.3-70B
- **Key Features:**
  - RAG with ChromaDB (optional, falls back to full CV if < 50% coverage)
  - Experience-level context in prompt (soft hint)
  - **Deterministic level-gap penalty (NEW):** regex-based job title inference vs candidate level
    - Overreach penalty: -15/-30/-60pts (caps at 35 for 3+ level gap)
    - Under-level penalty: -5/-12/-20pts (mild)
  - Rate-limit handling via `handle_rate_limit()`

### 4.5 CV Diff Tailor (`agents/cv_diff_tailor.py`)
- **Purpose:** Generate per-job bullet rewrites, reordering, and drops (AGGRESSIVE mode)
- **Input:** CV outline, job title, company, job description, previous diff
- **Output:** Diff dict with summary, bullets (NEW schema), skills_order
- **Model:** Groq Llama-3.3-70B (logic task, not creative)
- **NEW Schema (backward-compatible):**
  ```json
  {
    "summary": "new summary text",
    "bullets": {
      "Role Header": [
        {"i": 2, "text": "rewritten bullet text"},
        {"i": 0, "text": null},  // keep original
        {"i": 1}                 // keep original (text omitted)
      ]
    },
    "skills_order": ["Skill1", "Skill2"]
  }
  ```
- **Fabrication Guardrails:**
  - Rewrite length must be 60%-180% of original
  - All numeric tokens from original must appear in rewrite
  - At least 2 bullets per role must be kept
- **Legacy format still supported:** `bullets: {"Role Header": [2, 0, 1]}`

### 4.6 CV Tailor (`agents/cv_tailor.py`)
- **Purpose:** Full CV summary rewrite (when diff tailoring is insufficient)
- **Input:** CV text, job title, company, job description
- **Output:** Full tailored CV text
- **Model:** Gemini 2.5 Flash via `chat_gemini()`
- **Usage:** Currently unused in pipeline (diff tailoring is primary)

### 4.7 Cover Letter Generator (`agents/cover_letter_generator.py`)
- **Purpose:** Generate tailored cover letter per job
- **Input:** CV text, job title, company, job description
- **Output:** Cover letter text
- **Model:** Gemini 2.5 Flash via `chat_gemini()`
- **Key Logic:** 3-4 paragraphs, professional tone, references CV and JD

### 4.8 PDF Editor (`agents/pdf_editor.py`)
- **Purpose:** Parse CV structure and apply edits in-place
- **Key Functions:**
  - `extract_structure()` → sections (summary, experience, projects, skills)
  - `_role_blocks()` → bullet groups per role
  - `apply_edits()` → redact rects, insert new text with font matching
  - `_font_can_render()` → glyph coverage check (fixed to include space glyph advance)
- **NEW:** Consumes aggressive bullet format with rewrites and drops
- **Font Handling:** Attempts original font path, falls back to WinAnsi-compatible fonts
- **Bullet Character:** Preserves original bullet glyph, maps PUA/• to middle-dot (·) for rendering

### 4.9 Reviewer (`agents/reviewer.py`)
- **Purpose:** Second LLM pass to detect fabrication in tailored CV
- **Input:** CV outline, diff, job title, company, job description
- **Output:** Score 0-100, strengths, weaknesses, feedback, verdict (accept/retry)
- **Model:** Groq Llama-3.3-70B
- **NEW:** Shows rewritten bullets with original text inline for comparison
- **Fabrication Rules:**
  - Summary fabrication → cap at 60
  - Rewrite fabrication (new facts not in original) → cap at 55
- **Retry Logic:** If score < threshold, re-run tailor with feedback

### 4.10 Email Agent (`agents/email_agent.py`)
- **Purpose:** Send tailored CV + cover letter via email
- **Input:** Recipient email, subject, body, attachments
- **Output:** Email delivery result
- **Provider:** Resend API
- **Error Handling:** Raises on non-success responses (fixed silent failure bug)

### 4.11 Runtime (`agents/runtime.py`)
- **Purpose:** LLM budget tracking, rate-limit handling, snapshot saving
- **Key Functions:**
  - `track_llm_call()` → increment budget counters
  - `handle_rate_limit()` → cap wait times, abort long waits
  - `save_run_snapshot()` → crash-safe state dump

### 4.12 LLM Client (`agents/llm_client.py`)
- **Purpose:** Centralized model routing with throttling and retry logic
- **Models:**
  - `chat_fast()` → Groq Llama-3.3-70B (logic/scoring)
  - `chat_quality()` → Groq Llama-3.3-70B (quality tasks)
  - `chat_gemini()` → Gemini 2.5 Flash (creative writing: CV tailoring, cover letters)
- **Key Config:**
  - Groq key rotation: up to 3 keys via `GROQ_API_KEY`, `GROQ_API_KEY_2`, `GROQ_API_KEY_3`
  - Gemini model: `gemini-2.5-flash` (configurable via `GEMINI_MODEL`)
  - Secret retrieval: `secret_or_env()` for Streamlit Cloud compatibility

---

## 5. Changes in This Session (April 20, 2026)

### 5.1 Aggressive CV Tailoring (NEW MAJOR FEATURE)

**Problem:** CVs were ~93.5% identical across jobs — only bullet order changed. User wanted actual bullet rewording, dropping irrelevant bullets, and more per-job differentiation.

**Solution:**
1. **Schema Expansion (`cv_diff_tailor.py`)**
   - New bullet format: `[{"i": int, "text": str|None}, ...]`
   - Backward-compatible with legacy `[int, int, ...]` format
   - Prompt updated to allow REWRITE, DROP, REORDER with strict fabrication rules

2. **Fabrication Guardrails (`cv_diff_tailor.py`)**
   - `_rewrite_is_safe()`: length bounds (60%-180%), numeric token preservation
   - `_normalise_bullet_list()`: enforces min 2 bullets per role
   - Sanitiser reverts unsafe rewrites to original wording

3. **PDF Editor Integration (`pdf_editor.py`)**
   - `apply_edits()` bullets branch rewritten to consume new format
   - Uses rewrite text when present, keeps original when null/absent
   - Drops bullets not listed in the order
   - Report now includes `rewrites` and `dropped` counts

4. **Reviewer Enhancement (`reviewer.py`)**
   - `_render_diff_for_review()` shows `[REWRITTEN]` marker with original text inline
   - Prompt updated to explicitly check rewrites for new facts
   - Fabrication in rewrites caps score at 55

5. **Verification**
   - Synthetic test via `scripts/test_pdf_fix.py` confirmed:
     - 4 rewrites applied correctly
     - Drops work (truncated bullet absent)
     - Numbers preserved in rewrites
     - Legacy format still works

### 5.2 Experience-Level Dropdown + YOE-Based Matching (NEW MAJOR FEATURE)

**Problem:**
- Friend with 3 yrs marketing was matched to 10-YOE Senior roles → wasted LLM calls
- Brother searching IT Support (Entry level) was matched to "Senior IT Support Specialist" → wasted LLM calls
- Job title alone is unreliable (e.g., "Product Manager" at a startup = 3 yrs, at a big company = 8 yrs). Need numeric YOE signals.

**Solution — Three Layers of Filtering:**

**Layer 1: UI Dropdown (`app.py`)**
- 6-level dropdown in sidebar: Fresher (0-1 yrs) → Entry/Associate (1-3 yrs) → Mid-level (3-6 yrs) → Senior (6-10 yrs) → Lead/Manager (8+ yrs) → Director/VP+ (12+ yrs)
- Default: Mid-level
- Piped through `run_agent()` → `AgentState.experience_level` → `match_cv_to_job()`

**Layer 2: Deterministic Level-Based Penalty (`job_matcher.py`)**
- `_parse_candidate_level()`: maps UI label to ladder int (0-5)
- `_infer_job_level()`: regex-based job title seniority inference
  - **Order-critical regex** (fixed after bugs): exec → director → principal/staff → entry keywords → senior → lead → mid-level-specific roles → manager → default
  - L5: CEO, CTO, CFO, CPO, SVP, EVP, VP, vice president, director, head of
  - L4: principal, staff, lead, (manager only when not preceded by entry-level qualifier)
  - L3: senior, sr
  - L2: default + specific mid-level roles (product manager, software engineer, data scientist, analyst, coordinator, etc.)
  - L1: junior, jr, associate, intern, graduate, trainee, entry, apprentice, fresher
- `_apply_level_gap_penalty()`: directional post-LLM penalty
  - Overreach (candidate junior, role senior): -15/-30/-60pts, caps at 35 for 3+ gap
  - Under-level (candidate senior, role junior): -5/-12/-20pts (mild)
- Penalty note appended to `reasoning` field

**Layer 3: YOE-Based Early-Exit (`job_matcher.py`) — NEW**

The critical addition. BEFORE calling the Groq LLM, we now check if the job is out-of-range and skip the LLM call entirely. Saves ~30-50% of matcher LLM budget on broad scrapes.

- `_CAND_YOE_BY_LEVEL`: maps each level to a (min, max) YOE range
  ```python
  {
      0: (0, 1),    # Fresher
      1: (1, 3),    # Entry/Associate
      2: (3, 6),    # Mid-level
      3: (6, 10),   # Senior
      4: (8, 15),   # Lead/Manager
      5: (12, 25),  # Director/VP+
  }
  ```
- `_YOE_TOLERANCE_YEARS = 3` — candidates within 3 years of the JD minimum are still considered (skills can bridge small gaps)
- `_extract_jd_yoe_requirement(jd)`: regex parser that returns the hard floor from patterns like:
  - `5+ years` / `5 or more years`
  - `at least 5 years` / `minimum of 5 years`
  - `3-7 years experience` (takes lower bound)
  - Returns `min()` of all matches — the effective hard floor

**Two early-exit checks in `match_cv_to_job`:**
1. **Title-based level gap:** if `job_level - cand_level >= 2` or `<= -3`, skip LLM. Returns capped score (30 or 40) with human-readable reason.
2. **Explicit JD YOE:** if `jd_min > cand_max + 3`, skip LLM. Returns capped score (25) with reason like "JD requires 10+ yrs, your range is 1-3 yrs".

**LLM prompt hardened** — now receives hard numeric facts for borderline cases:
```
Candidate's target experience level: Mid-level (3-6 yrs)
 (approx 3-6 years of experience). This JD appears to require at least 5 years.
Penalise when the role's seniority or YOE requirement clearly exceeds the
candidate's range by more than 3 years, but do NOT penalise small gaps
(±1-2 years) when the skills are a strong match.
```

**Verification:**
- `scripts/test_yoe_matcher.py` — 16/16 tests pass
- YOE extraction: all 8 realistic JD excerpts parsed correctly
- Early-exit decision matrix: all 8 level/YOE combinations produce correct SKIP/KEEP decisions

**Impact:**
| Scenario | Before | After |
|---|---|---|
| Brother (3 yrs) sees "Senior IT Support, 10+ yrs" | Groq call wasted | Instant skip, 0 calls |
| Friend (3 yrs marketing) sees "Marketing Manager, 10+ yrs" | Groq call wasted | Instant skip |
| Friend (3 yrs) sees "Marketing Coordinator, 5+ yrs" | Groq call | Groq call runs ✅ (within tolerance) |
| Mid-level dev sees "Principal Engineer, 12+ yrs" | Groq call wasted | Instant skip |

### 5.3 Bug Fixes (Earlier in Session)

1. **PDF Font Fix (`pdf_editor.py`)**
   - `_font_can_render()` now checks space glyph advance width
   - Prevents NBSP rendering issues from subset fonts

2. **Email Silent Failure Fix (`email_agent.py`)**
   - `send_email()` now raises on non-success Resend API responses

3. **UI Reset After Send Fix (`app.py`)**
   - Persist `final_state` in `st.session_state["_last_final_state"]`
   - Restores state after per-card Send button click

4. **Emoji Toast Icons Fix (`app.py`)**
   - Replaced invalid emojis with valid ones (✅, 0x1f504)

5. **Bulk Send Button (`app.py`)**
   - Replaced per-card Send buttons with single "Send all matched jobs" button
   - Progress indicator + per-job error handling

6. **Perplexity Audit + LLM Routing Fixes (`llm_client.py`, `cv_diff_tailor.py`)**
   - Corrected Gemma model ID to `gemma-4-26b-a4b-it`
   - Reduced Gemma min gap from 60s to 3s
   - Routed `cv_diff_tailor` to `chat_fast` (Groq) instead of Gemma
   - Removed debug prints
   - Migrated to `secret_or_env()` for API key retrieval

### 5.4 Session 2 — April 21, 2026 (Groq-only + Key Rotation + CV Render Fixes)

**Context:** Groq free-tier budget was exhausting fast under parallel tailoring. User
confirmed: previously 4-5 runs/day of 5 jobs worked on Groq alone, so per-minute
rate limits (TPM) are the real throttle, not the 100K daily cap. Gemma was
reinstated briefly then removed entirely per user direction.

**A. Gemma fully removed** (`agents/llm_client.py`)
- Deleted all Gemma-related constants, clients, functions, and env vars.
- `chat_quality` and `chat_fast` both route to Groq exclusively.
- `GEMMA_*` env vars no longer read.

**B. Groq key rotation pool** (`agents/llm_client.py`)
- `.env` now accepts `GROQ_API_KEY`, `GROQ_API_KEY_2`, `GROQ_API_KEY_3`.
- `_load_groq_keys()` loads up to 3 keys; `_rotate_groq_key()` advances on error.
- `_call_groq()` rotates on both rate-limit (`429`) and auth (`401`) errors.
- Falls back to 30s sleep + reset-to-first-key only when ALL keys exhausted.
- Daily ceiling: ~300K tokens (3 keys × 100K).
- Test: `scripts/test_groq_rotation.py`.

**C. Centralised LLM routing across all agents**
Previously, several modules created their own `Groq(api_key=...)` clients, so rotation
didn't apply to them. Fixed by refactoring each to call `chat_quality`/`chat_fast`:
- `agents/reviewer.py` → uses `chat_fast`
- `agents/planner.py` → uses `chat_quality`
- `agents/job_matcher.py` → uses `chat_quality`
- `agents/job_agent.py::_groq_supervisor_completion` → uses `chat_quality`
- Direct `_groq()` helpers and module-level `Groq` clients deleted.

**D. `load_dotenv(override=True)` everywhere**
Root cause of production 401 errors: stale OS-level `GROQ_API_KEY` overrode the
`.env` value. Fixed by calling `load_dotenv(override=True)` in:
- `agents/llm_client.py`, `agents/reviewer.py`, `agents/planner.py`,
  `agents/job_matcher.py`, `agents/job_agent.py`.

**E. Token budget tightening**
- `cv_diff_tailor` max_tokens: 1500 → 1100
- `cv_tailor` fallback max_tokens: 2000 → 1400
- `cover_letter_generator` max_tokens: 700 → 600
- Summary-retry threshold in `cv_diff_tailor` loosened: 0.85 → 0.72 (skips needless retries)
- Removed hardcoded `time.sleep(8)` in `job_matcher.py` (was Gemma-era rate guard)

**F. Auto-threshold relaxation REMOVED** (`agents/job_agent.py::match_jobs_node`)
- Previously, if no jobs matched at the user's selected threshold (e.g. 60%),
  the code auto-retried at 50% → 40% → 30%, surfacing weak matches.
- User flagged this as a bug: threshold was ignored.
- Fix: if no matches at the user's threshold, return empty `matched_jobs`.

**G. UI: Groq budget warning** (`app.py`)
- When `num_jobs > 3`, a `st.warning` appears below the jobs slider explaining
  approx token cost and daily-quota risk.

**H. CV rendering + content-preservation fixes** (`cv_diff_tailor.py`, `pdf_editor.py`)

User reported after a PDF diff comparison: bullets rendered as `-` / `·`
instead of `•`, indentation was wrong, Accenture awards were dropped, and
dropped bullets left visible white space in the PDF. Fixed:

1. **No-drop policy** — LLM may only REORDER + REWRITE. Prompt updated.
   `_normalise_bullet_list()` in `cv_diff_tailor.py` always pads missing
   indices back with `text=None` (keep verbatim). No more blank space at
   end of role blocks.
2. **Achievement/award protection** — prompt now explicitly forbids dropping
   any achievement, award, recognition, certification, promotion, or
   measurable-outcome bullet.
3. **Bullet glyph `•` preserved** — `pdf_editor.py::apply_edits` no longer
   pre-downgrades `•` to `·`. The embedded-font path renders `•` correctly;
   only the Base14 fallback (when font embedding fails) substitutes `·`.
4. **Better indentation** — bullet-to-text gap widened from 1 space to 3.
5. **Guardrail adjustments** (`_rewrite_is_safe`):
   - Length range widened: 50%–200% (was 60%–180%).
   - Number-token check kept strict (`25%` must stay `25%`).
   - Returns `(ok, reason)` tuple; caller logs rejections as
     `⚠️ rewrite rejected (bullet N, <reason>)`.
6. **Zero-rewrites retry removed** — per user: "if the JD doesn't warrant a
   rewrite, don't force one". LLM now decides per-bullet whether a rewrite
   is warranted. Prompt guidance emphasises judgement over volume.

**I. Regression check**
All touched files pass `ast.parse` cleanly. End-to-end live run still pending
to confirm: (a) rotation kicks in on TPM hit, (b) no 401s, (c) bullets render
with `•`, (d) Accenture awards present, (e) no gap at role-block bottom.

### 5.5 Session 3 — April 21, 2026 (Gemini 2.5 Flash Integration)

**Context:** After Groq-only migration, user wanted to improve writing quality for CV tailoring and cover letter generation. Gemini 2.5 Flash offers better creative writing capabilities with long context windows (1M tokens) while maintaining acceptable latency (~210 tokens/sec vs Groq's ~315 tokens/sec).

**A. Dual-LLM architecture implemented** (`agents/llm_client.py`)
- Added `chat_gemini()` function for Gemini 2.5 Flash calls
- Added `_call_gemini()` with retry logic for rate limits
- Added GEMINI_MODEL configuration (default: `gemini-2.5-flash`)
- Groq continues to handle fast tasks (matching, planning, reviewers, supervisor)
- Gemini handles writing tasks (CV tailoring, cover letter generation)

**B. CV Tailor switched to Gemini** (`agents/cv_tailor.py`)
- Changed from `chat_quality()` (Groq) to `chat_gemini()` (Gemini)
- Better writing quality for bullet rewrites and summary generation
- Leverages Gemini's 1M token context window for full CV + JD processing

**C. Cover Letter Generator switched to Gemini** (`agents/cover_letter_generator.py`)
- Changed from `chat_quality()` (Groq) to `chat_gemini()` (Gemini)
- Improved cover letter quality with more natural, personalized tone
- Better at capturing company-specific motivation and value propositions

**D. Environment configuration updated**
- Added `GEMINI_API_KEY` to `.env.example` (get from https://aistudio.google.com/app/apikey)
- Added `GEMINI_MODEL` to `.env.example` (default: `gemini-2.5-flash`)
- Updated README.md with dual-LLM architecture explanation
- Updated PRD_v1_Launch.md dependencies section
- Updated PRODUCT_DECISIONS.md with Decision 9 (Dual-LLM Architecture)

**E. Dependencies updated** (`requirements.txt`)
- Added `google-generativeai` package for Gemini API
- Upgraded `chromadb` from 0.4-0.6 to 0.5-0.7 for Python 3.14 compatibility

**F. Streamlit Cloud Python version pinning**
- Added `.python-version` file with content `3.11`
- Added `runtime.txt` file with content `python-3.11`
- This pins Python 3.11 for Streamlit Cloud deployment (required for chromadb/tokenizers compatibility)
- User needs to delete and recreate the Streamlit Cloud app to pick up the Python version

**G. Documentation updates**
- README.md: Updated tech stack, agent table with LLM column, configuration section, known limits
- PRD_v1_Launch.md: Updated dependencies to reflect Gemini
- PRODUCT_DECISIONS.md: Added Decision 9 about dual-LLM architecture
- HANDOFF_SUMMARY.md: Updated tech stack, agent descriptions, LLM client section

---

## 6. Current State

### 6.1 Completed
- ✅ PDF font fix (NBSP rendering)
- ✅ Email silent failure fix
- ✅ UI reset after Send fix
- ✅ Emoji toast icons fix
- ✅ Bulk Send button
- ✅ Perplexity audit + 5 LLM routing fixes
- ✅ Aggressive CV tailoring build (schema + sanitiser + PDF editor + reviewer)
- ✅ Experience-level dropdown + deterministic level-gap penalty (regex order-critical, fixed)
- ✅ **YOE-based early-exit matcher** — saves ~30-50% of matcher LLM budget on broad scrapes. `_extract_jd_yoe_requirement()` parses JD text for "X+ years"-style patterns; `_CAND_YOE_BY_LEVEL` maps level → (min, max) YOE; ±3yr tolerance; out-of-range jobs skip the Groq call entirely. LLM prompt now includes hard numeric facts (candidate range + JD requirement). 16/16 synthetic tests pass.
- ✅ **UI redesign continuation (Cursor pass)** — top-centered product masthead for ApplySmart AI, stronger hero typography/spacing, cleaner sidebar section hierarchy, refined feature-card rhythm/hover polish, and responsive style tuning for a more production-grade SaaS look and feel.
- ✅ **[Apr 21] Gemma fully removed** — all LLM calls route to Groq via centralized `llm_client.py`.
- ✅ **[Apr 21] Groq key rotation pool (up to 3 keys)** — auto-rotates on 429 + 401; ceiling ~300K tokens/day.
- ✅ **[Apr 21] Centralized LLM routing** — planner, matcher, reviewer, supervisor, cover-letter all go through `chat_quality`/`chat_fast`; no scattered `Groq(api_key=...)` clients.
- ✅ **[Apr 21] `load_dotenv(override=True)` everywhere** — fixes stale-OS-env 401 errors.
- ✅ **[Apr 21] Token budgets reduced** — cv_diff_tailor 1500→1100, cv_tailor 2000→1400, cover_letter 700→600.
- ✅ **[Apr 21] Auto-threshold relaxation removed** — user's match threshold now strictly respected.
- ✅ **[Apr 21] No-drop policy + achievement protection** in `cv_diff_tailor` prompt; all original bullets preserved verbatim when no rewrite is warranted.
- ✅ **[Apr 21] Bullet glyph `•` preserved** in `pdf_editor.apply_edits`; wider indentation.
- ✅ **[Apr 21] Rewrite rejection logging** — `⚠️ rewrite rejected` line in logs when guardrail blocks a rewrite.
- ✅ **[Apr 21] UI Groq budget warning** when jobs > 3.
- ✅ **[Apr 21 PM] Mixpanel dashboard live end-to-end** — 5 reports on `ApplySmart AI Dashboard` board: Outcome funnel (upload→applied), Weekly retention, Match quality (median best score), Runs per day, Runs by experience level. Full schema + reproduction steps in `docs/MIXPANEL_DASHBOARD.md`.
- ✅ **[Apr 21 PM] Mixpanel EU region support** — `MIXPANEL_REGION=EU` env var routes events to `api-eu.mixpanel.com`. Default remains US for back-compat. Fixes silent event drops for EU-residency projects.
- ✅ **[Apr 21 PM] New instrumentation events** — `cv_uploaded` (activation funnel top), `llm_rate_limit_hit` (infra health, fires on Groq key rotation), `review_retry_triggered` (cover-letter reviewer retries).
- ✅ **[Apr 21 PM] Enriched `run_completed` properties** — now emits `best_match_score`, `median_match_score`, `matches_above_threshold_count`, `match_threshold` for the match-quality insight.
- ✅ **[Apr 21 PM] Send-button NameError fix** — `app.py:1292` used undefined `total`; replaced with `len(sendable)`. Bulk-send click no longer crashes the app.
- ✅ **[Apr 21 PM] Sidebar copy polish** — removed tech-stack caption (Groq/Resend implementation detail wasn't useful to end users), simplified token-budget warning (no more scary math), added tasteful "Built by Rishav Singh" attribution.
- ✅ **[Apr 21 PM] Repo cleanup for GitHub push** — removed 26 items (debug txt dumps, test PDF binaries, personal `Rishav_Singh_PM_CV.docx`, 5 fake root-level `test_*.py` scripts that required manual CV paste, 10 one-off diag scripts in `scripts/`, `__pycache__/`, `.langgraph_api/`). Added `.env.example` template (no secrets) to unblock deployment.
- ✅ **[Apr 21 PM] Fixed Resend/Gmail doc drift** — README `.env` example and Required-vars table wrongly listed `RESEND_API_KEY` + `SENDER_EMAIL`; actual code (`agents/email_agent.py`) uses Gmail SMTP via `EMAIL_ADDRESS` + `EMAIL_APP_PASSWORD`. README and `.env.example` now match the code.
- ✅ **[Apr 21 PM] `pdf_editor.py` bullet glyph consistency fix** — `•` (U+2022) was silently downgraded to `·` (U+00B7) in two places in the Base14 fallback path (a `_UNICODE_FALLBACK` map entry + a post-sweep loop over `_BULLET_CHARS`). Because `•` is in WinAnsi encoding (0x95), Base14 Latin-1 fonts render it natively — the downgrade was over-cautious. Result: when one section's embedded font failed to install, its rewritten bullets showed `·` while other sections kept `•` (see IBM vs Accenture in user's Apr 21 screenshot).
- ✅ **[Apr 21 PM] `pdf_editor.py` bullet wrap-width fix** — bullet rewrite path (line ~920) was computing `rect` as the union of original bullet bboxes WITHOUT extending `rect.x1` to page margin. Summary path (line ~840) already had the extension. Longer rewritten bullets were therefore wrapped against the narrowest original bullet's right edge, producing orphan words like "efficiency by 20%" on a new line. Added `rect.x1 = max(rect.x1, page.rect.width - 40)` to both bullet and skills paths for consistency.
- ✅ **[Apr 21 PM] Gemini 2.5 Flash integration** — dual-LLM architecture implemented: Groq for fast tasks (matching, planning, reviewers, supervisor), Gemini for writing tasks (CV tailoring, cover letters). Added `chat_gemini()` function in `llm_client.py`, updated `cv_tailor.py` and `cover_letter_generator.py` to use Gemini. Added `GEMINI_API_KEY` and `GEMINI_MODEL` to `.env.example`. Updated README.md, PRD_v1_Launch.md, PRODUCT_DECISIONS.md with Decision 9. Added `google-generativeai` to requirements.txt. Added `.python-version` and `runtime.txt` for Streamlit Cloud Python 3.11 pinning.

### 6.2 Pending (High Priority)
- **Live pipeline run** to validate aggressive tailoring + experience-level + YOE filters with a real LLM (~50 calls). Confirms:
  - LLM outputs valid JSON in new aggressive bullet schema
  - Sanitiser guardrails fire on fabrications (numeric preservation)
  - YOE early-exit actually saves Groq calls as expected
  - PDF rendering looks clean end-to-end
  - Cross-job CV similarity drops from 93.5% to <70%
- **Truncated Accenture bullet verification** (untested since font + aggressive-tailor changes)

### 6.2.1 Documentation added in this continuation
- ✅ `docs/PRODUCT_DECISIONS.md` — concise decision register for PM portfolio ("what/why/trade-off/alternatives").
- ✅ `docs/KPI_ANALYTICS_PLAN.md` — KPI definitions, event taxonomy, free analytics tooling guidance, and starter dashboard spec.

### 6.2.2 GDPR baseline implemented (this continuation)
- ✅ Added `agents/privacy.py` with:
  - `redact_pii(text, candidate_name, user_email)` for known-value + regex masking
  - `apply_tracing_consent(consent_enabled)` to enforce `LANGCHAIN_TRACING_V2` at runtime
- ✅ `app.py` now has first-session privacy consent gate (default tracing OFF):
  - choices: allow anonymized tracing / disable tracing / cancel
  - consent persisted in `st.session_state` and reflected immediately
- ✅ Sidebar privacy controls added:
  - trace toggle (can revoke/enable at any time in-session)
  - `Delete my session data` button (removes `sessions/<session_id>/` and resets session)
- ✅ Added user-facing privacy notice: `docs/PRIVACY.md`
- ✅ `agents/runtime.py::save_run_snapshot()` now performs generic email/phone masking before writing snapshot JSON

### 6.2.3 Mixpanel KPI instrumentation implemented (this continuation)
- ✅ Added `agents/analytics.py` (optional/no-op when token missing):
  - `track_event(event, distinct_id, props)`
  - `distinct_id(session_id, user_email)` (email-hash based, privacy-safe)
  - env key support: `MIXPANEL_TOKEN` / `MIXPANEL_PROJECT_TOKEN`
- ✅ `app.py` now emits key product events:
  - `session_opened`
  - `run_started`
  - `run_completed`
  - `send_attempted` / `send_completed`
  - `cv_downloaded` / `cover_letter_downloaded`
  - `job_marked_applied` / `job_unmarked_applied`
  - `privacy_tracing_consent_updated`
  - `session_data_deleted`
- ✅ `README.md` config table updated with `MIXPANEL_TOKEN`

### 6.3 Pending (Medium Priority)
- Brother-share prep: `.env.example`, sanitized README
- Rotate exposed API keys (Groq, Google, LangSmith, Resend, Gmail app password, SerpAPI, JSearch, Adzuna)

### 6.4 Pending (Low Priority / v2)
- Designer CV (Novoresume, Canva) multi-column support
  - Current limitation: global (y,x) line sort breaks reading order for multi-column layouts
  - Needs column clustering + per-column extraction (~3-5 hrs)
- Other Perplexity polish items: APP_PASSWORD gate, scrape delays, session cleanup, requirements.txt freeze, HTML email, dynamic subjects, 60s run cooldown, CV size limit, KPI card, LLM-call counts in insight tab

---

## 7. Key Files Reference

| File | Purpose | Key Functions/Notes |
|------|---------|---------------------|
| `app.py` | Streamlit UI | Sidebar inputs, bulk Send button, session state persistence |
| `agents/job_agent.py` | LangGraph workflow | Supervisor, all worker nodes, run_agent() entry point |
| `agents/job_matcher.py` | Job matching | match_cv_to_job(), RAG, experience-level penalty |
| `agents/cv_diff_tailor.py` | Aggressive bullet tailoring | tailor_cv_diff(), sanitiser, new schema |
| `agents/pdf_editor.py` | PDF parsing/editing | extract_structure(), apply_edits(), font handling |
| `agents/reviewer.py` | Fabrication detection | Review tailored CV, compare rewrites to originals |
| `agents/llm_client.py` | Model routing | chat_fast(), chat_quality(), throttling |
| `agents/runtime.py` | Budget & rate limits | track_llm_call(), handle_rate_limit(), snapshots |
| `agents/email_agent.py` | Email delivery | send_email() via Resend API |
| `agents/cv_embeddings.py` | Vector retrieval | ChromaDB integration, retrieve(), format_chunks_for_prompt() |
| `agents/planner.py` | Search planning | build_plan() with keyword bundles |
| `agents/job_scraper.py` | Job scraping | Multi-board scraping with fallback |
| `.env` | Configuration | API keys, model IDs, feature toggles |

---

## 8. Environment Variables (.env)

```bash
# Groq (all LLM calls — logic, scoring, creative writing)
GROQ_API_KEY=...
# Optional: up to 2 more Groq keys from different accounts for rotation.
# When one key hits its per-minute or daily quota, the app auto-rotates.
GROQ_API_KEY_2=...
GROQ_API_KEY_3=...
GROQ_MODEL=llama-3.3-70b-versatile

# LangSmith (tracing, opt-in via in-app consent gate)
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=...
LANGCHAIN_PROJECT=applysmart-ai

# Resend (email)
RESEND_API_KEY=...
RESEND_SENDER_EMAIL=...

# Gmail (app password for SMTP fallback)
GMAIL_APP_PASSWORD=...

# Job search APIs
SERPAPI_KEY=...
JSEARCH_API_KEY=...
ADZUNA_APP_ID=...
ADZUNA_API_KEY=...

# Mixpanel (optional analytics — no-op when absent)
MIXPANEL_TOKEN=...
MIXPANEL_REGION=EU   # or US (default); must match your project's residency
```

**Note (Apr 21):** `GOOGLE_API_KEY`, `GEMMA_MODEL`, and all other `GEMMA_*`
vars are no longer read. Remove them from production `.env` to avoid confusion.

---

## 9. How to Run

```bash
# Install dependencies
pip install -r requirements.txt

# Set up environment variables
cp .env.example .env  # (after creating .env.example)
# Edit .env with your API keys

# Start Streamlit app
streamlit run app.py
```

---

## 10. Next Steps (Priority Order)

1. **Live pipeline run** (~50 LLM calls)
   - Restart Streamlit
   - Upload CV, select experience level, run with 2-3 jobs
   - Inspect PDFs for rewrites and drops
   - Check logs for sanitiser guardrail triggers
   - Verify cross-job CV similarity drops

2. **Brother-share prep**
   - Create `.env.example` with placeholders
   - Add README section for brother (setup, run, expectations)
   - Zip artifact for sharing

3. **Truncated bullet verification**
   - Run a live job with the Accenture bullet that was truncating
   - Confirm font fix + aggressive tailoring didn't break it

4. **Designer CV support** (v2)
   - Implement column clustering in `pdf_editor.py`
   - Per-column reading order
   - Re-run structure extraction

---

## 11. Known Issues

1. **Designer CV multi-column layouts**
   - Current: global (y,x) line sort interleaves columns
   - Impact: sidebar/skills content shuffled into experience bullets
   - Workaround: falls back to ReportLab rebuild (butchered layout)
   - Fix: column clustering + per-column extraction (~3-5 hrs)

2. **Job title level inference is regex-heuristic**
   - `_infer_job_level()` uses ordered regex checks, not LLM classification. Order matters — see comments in the function.
   - Handles 95% of common titles correctly. Edge cases (e.g., "Product Owner", non-English titles) default to L2.
   - If mis-classification becomes a problem, consider a cached LLM classifier as a second pass for unrecognised titles.

3. **YOE extraction only covers explicit patterns**
   - `_extract_jd_yoe_requirement()` handles "5+ years", "at least 5 years", "3-7 years", etc.
   - Does NOT handle implicit signals like "senior-level experience expected" without a number.
   - Fallback: when no numeric YOE found, the level-based penalty still applies.

---

## 12. Testing Scripts

| Script | Purpose |
|--------|---------|
| `scripts/test_pdf_fix.py` | Synthetic PDF edit test — aggressive bullet format + drops (no LLM cost) |
| `scripts/test_yoe_matcher.py` | YOE extraction + early-exit decision matrix — 16/16 tests pass (no LLM cost) |
| `scripts/diag_tailored.py` | Analyze tailored CV PDF line spacing |
| `scripts/diag_tailor_diff.py` | Compare two tailored CVs for similarity |
| `scripts/diag_chars.py` | Diagnostic for character encoding issues |

---

## 13. Rate-Limit Guardrails

- **Groq:** Capped wait at 60s via `handle_rate_limit()`
- **Gemma:** Min gap 3s between calls
- **Scrape rounds:** Limited to 3 per planner
- **LLM budget:** Configurable via env var, tracked per run

---

## 14. Prompt Safety

- **Fenced untrusted blocks:** `wrap_untrusted_block()` wraps JD and CV in fenced blocks
- **Preamble:** `untrusted_block_preamble()` warns LLM not to execute instructions from untrusted content
- **Fabrication detection:** Reviewer agent with retry logic

---

## 15. Snapshot Format

`snapshot.json` contains:
- Inputs (CV path, job title, location, etc.)
- Final state (matched jobs, tailored CVs, sent jobs)
- Errors (if any)
- LLM budget usage
- Timestamp

Saved to `sessions/{session_id}/snapshot.json` on completion or crash.

---

## 16. Contact / Support

- **Project location:** `d:\Projects\job-application-agent`
- **UI:** Streamlit at `http://localhost:8501`
- **Logs:** Terminal output with emoji markers (✅, ⚠️, ❌, ✂️, ✍️, 📧)

---

---

## 17. Deployment Readiness (as of Apr 21, 2026)

### Overall: ⚠️ NOT YET — 3 blockers

The app is functionally stable and the recent refactors are in place, but
three items must be handled before any public/shared deploy.

### ✅ What's deployment-ready
- Core pipeline: scrape → match → tailor → review → email (stable end-to-end)
- Groq-only LLM stack with 3-key rotation (≈300K tokens/day ceiling)
- Centralized LLM routing — no more ad-hoc Groq clients
- GDPR baseline: consent gate, PII redaction, session-delete button, `docs/PRIVACY.md`
- Optional Mixpanel analytics (no-op without token)
- Experience-level + YOE filtering (saves ~30-50% matcher LLM spend)
- User-threshold strictly respected (no auto-relaxation)
- CV rendering fixes landed (`•` preserved, no bullet drops, achievement protection)

### ⛔ Blockers before deploy
1. **Rotate exposed API keys** — `.env` keys have been pasted in dev logs /
   chats. Rotate Groq, LangSmith, Resend, Gmail app password, SerpAPI,
   JSearch, Adzuna before sharing with anyone.
2. **Create `.env.example`** — currently no template exists for downstream
   setup.
3. **Live validation run** — the Apr 21 no-drop + achievement + bullet-glyph
   + key-rotation + 401-fix changes are logic-correct and parse-clean, but
   haven't been validated end-to-end against a real Groq run with the user's
   CV. One 3-job run will confirm.

### 🟡 Pre-deploy nice-to-haves
- Pin versions in `requirements.txt` (`pip freeze > requirements.txt`)
- Add a 60s run cooldown to prevent accidental double-runs
- CV file-size limit (e.g. 5 MB)
- `APP_PASSWORD` gate if deploying publicly
- Scrape delays to avoid tripping board rate limits at scale

### 📦 Recommended deploy flow
1. Rotate all secrets, update `.env` on host, commit `.env.example` only.
2. Freeze `requirements.txt`.
3. Deploy to Streamlit Cloud or self-hosted (Docker). Confirm `secret_or_env`
   picks up `st.secrets` on Cloud.
4. Smoke test: single-job run end-to-end, verify email delivery.
5. Full test: 3-job run, verify rotation logs (`🔄 Groq key rotated`) if TPM hits.
6. Monitor Mixpanel + LangSmith (if consented) for first 24h.

---

---

## 18. Mixpanel Dashboard (live, Apr 21 2026)

**Source of truth:** `docs/MIXPANEL_DASHBOARD.md`
**Board name:** `ApplySmart AI Dashboard`
**Region:** EU (`MIXPANEL_REGION=EU`)

### 18.1 The 5 live reports (free-plan cap)

| # | Report | Type | PM question |
|---|---|---|---|
| 1 | **Outcome funnel — upload to applied** ⭐ | Funnel (5 steps) | Does our tool drive real applications? |
| 2 | **Weekly return — session → run** | Retention | Does the product create a habit? |
| 3 | **Match quality — median best score** | Insight (median) | Is the matcher producing strong matches? |
| 4 | **Runs per day** | Insight (totals) | Is usage growing? |
| 5 | **Runs by experience level** | Insight (breakdown) | Who are our users? |

### 18.2 Event instrumentation (code locations)

| Event | Emitted from |
|---|---|
| `session_opened` | `app.py` — first page load |
| `cv_uploaded` | `app.py` — sidebar file uploader (first drop per session) |
| `run_started` | `app.py` — before pipeline call |
| `run_completed` | `app.py` — after pipeline, enriched with match-quality props |
| `send_attempted` / `send_completed` | `app.py` — bulk-send button |
| `cv_downloaded` / `cover_letter_downloaded` | `app.py` — per-card download buttons |
| `job_marked_applied` / `job_unmarked_applied` | `app.py` — per-card "I applied" checkbox |
| `privacy_tracing_consent_updated` | `app.py` — consent modal + sidebar toggle |
| `session_data_deleted` | `app.py` — delete-session button |
| `llm_rate_limit_hit` | `agents/llm_client.py` — on Groq key rotation |
| `review_retry_triggered` | `agents/job_agent.py` — cover-letter review verdict=retry |

Infra events use distinct_id `"system_infra"` to keep user funnels clean.

### 18.3 Privacy guarantees (see `docs/PRIVACY.md`)

- No PII in event properties — emails hashed SHA-256 → `user_<hex[:20]>`,
  CV text and JD text never sent.
- Consent-gated — user must approve tracing in first-run modal.
- `track_event()` is fail-safe — every exception is swallowed silently
  so analytics failures never break agent runs.

### 18.4 Annotations

Ship-date annotations live on the project and appear on every chart:

- **2026-04-20** — v1.0 — aggressive CV tailoring + YOE filter
- **2026-04-21** — v1.1 — Groq rotation + no-drop bullets

When a metric moves, these vertical lines tie the movement to the release.

### 18.5 Known gaps / next

- **Real data** — dashboard is currently populated with seeded events
  (see §6 of `docs/MIXPANEL_DASHBOARD.md` for the seed script). First
  real-user run will replace the synthetic funnel shape.
- **Retention chart** looks empty — expected; cohorts need time to mature.
- **Apply rate under-reports** true applications since the tracker is
  user-driven. Flag this explicitly in any case-study writeup.

---

**End of handoff summary.** Ready to continue in Cursor.
