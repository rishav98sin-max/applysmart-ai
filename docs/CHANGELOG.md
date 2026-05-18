# ApplySmart AI — Changelog

> Version-by-version build log. **What** changed and **why** it changed.
> Customer hypothesis and product bets live in `PM_CASE_STUDY.md`;
> engineering handoff details live in `HANDOFF_SUMMARY.md`.
>
> Format: each version notes the bet it was testing, the change, and the
> evidence that drove it.

---

## v1.4.3 — Preservation-first tailoring + matcher fixes (18 May 2026)

**Bet under test:** Will a *preservation-first* discipline — a rewrite RE-FRAMES but never REMOVES — stop the pipeline from shipping bullets that are *worse* than the original?

### Shipped

**Tailoring quality (batches 11–15):**

- **Verb-led rewrites** — the strategist told the tailor to literally OPEN a bullet with a noun fact, forcing broken/passive grammar ("Supervisor + workers pattern… scoped the architecture"). Now: open with a strong verb, surface the fact early. A `_rewrite_is_safe` stranded-verb guard rejects the salad.
- **Reframe** — each bullet action carries a `jd_keyword`: the CV-proven JD term that bullet should weave in. Turns cosmetic reorders into JD-aligned rewrites ("Designed an **agentic** supervisor…").
- **Preservation guard** (`_check_content_preserved`) — the keystone. A rewrite must keep every concrete fact of the original: acronyms (PRD, MVP, RICE), proper nouns, named methods (sprint-over-sprint). Drop one → revert. Run 22 showed the reframe trading the candidate's specifics for keywords ("Prioritised MVP feature set" → "Made prioritisation decisions"); this stops it.
- **Summary guards** — absorbed-bullet check widened to all sentences; project-drop check (a tailored summary must keep the named projects the original had); a one-shot credential-restore retry before reverting.
- **Retries softened** — keeping a strong bullet unchanged is now an explicit *correct* answer, so the identical-rewrite retry no longer manufactures degradations.
- **`jd_keyword` quality guard** — blanks long role-phrase keywords that bolt onto a verb redundantly ("Owned 0-to-1 product ownership").
- **Variance** — tailor temperature 0.2 → 0.1 (constrained re-framing, not open writing).

**Matcher semantic matching:**

- `_drop_false_misses` — a "missing skill" the matcher LLM flagged is dropped when the CV actually contains its words within a tight window (Run 22: "Automation workflows" flagged missing while the CV lists "workflow automation"). Recovered skills move to `matched_skills`.
- Matcher prompt: word-order/variant equivalence + infer domain from named clients/employers (a CV naming a healthcare client demonstrates healthcare-domain experience).

### Why

Run 21/22 traces showed every iteration fixed one defect and surfaced another — because every guard checked "did you ADD something bad", none checked "did you DROP something good". The pipeline was generative-first; the discipline that made hand-tailoring safe — preserve every fact, re-frame only — was never coded. v1.4.3 installs it: tailoring is now *safe* (nothing ships worse than the original) and *meaningful* (JD vocabulary woven in, content intact).

---

## v1.4.2 — Tailoring Yield + In-Place Render Hardening (18 May 2026)

**Bet under test:** Will removing the strategist's self-imposed volume cap and fixing the broken `lead_with` instruction raise the number of genuinely-tailored bullets per CV — instead of the pipeline shipping 2–3 cosmetic rewrites and reverting the rest?

### Shipped

**Tailoring yield** (`cv_diff_tailor.py`, `tailor_strategist.py`):

- **Strategist volume uncap** — removed the "2–5 per role, rarely all" rationing language and raised the hard per-role cap 6 → 12. The strategist now walks every role/project (whole roles were being skipped) and lists every bullet that genuinely needs a rewrite. The adaptive token budget scales with the new cap.
- **Deterministic `lead_with` guard** — the strategist's #1 failure mode was pointing `lead_with` at the bullet's *existing opening* (or writing a long clause), telling the tailor to "lead with" words the bullet already opens with → near-copy → `identical_rewrite` revert. A post-process guard now blanks any echoing/over-long `lead_with` so the tailor picks the buried fact itself.
- **`identical_rewrite` retry** — when planned bullets come back as near-copies, one retry shows the LLM its own unchanged drafts and demands a genuine restructure-or-omit.
- **Over-length retry hybrid** — bullets rejected as too long are retried with their own draft + exact character limit shown back (mirrors the summary length retry).

**PDF in-place rendering** (`pdf_editor.py`): TextWriter-based block rendering with width-aware wrapping and block-justification detection; bundled font cloning + ATS text normalisation; table-border capture/redraw so edits don't drop cell borders; sidebar / 2-column bullet filtering.

### Why

On a representative 2-column CV (Shrestha → Airtel "Product Marketing Manager") the pipeline tailored only **3 of 27 bullets across 1 of 3 roles** — the strategist rationed itself to 6 actions and half of those returned as cosmetic near-copies. Root-cause trace: (1) the "2–5 per role" prompt language plus a hard 6-entry cap; (2) `lead_with` values that echoed each bullet's own opening, leaving the tailor nothing to do. After the fixes the same run tailored **11 bullets across all 3 roles**, 9 rendered into the PDF (2 cleanly reverted at apply-time for slot overflow).

---

## v1.4.1 — Tailor Prompt & Guard Fixes (13 May 2026)

**Bet under test:** Will fixing the per-bullet budget ceiling and `_check_do_not_inject` guard eliminate the zero-rewrite failure mode that was producing PDFs identical to the original?

### Shipped

- **Per-bullet character budget raised** (`cv_diff_tailor.py`): `int(orig_len * 1.10)` → `int(orig_len * 1.40)`. The old +10% ceiling made the LLM believe it had no room for meaningful rewrites, causing all `text: null` responses.
- **Mandatory rewrite banner** added to top of `_PROMPT_TEMPLATE`: a `╔══╗` block explicitly telling the model that returning `text=null` for every bullet is a failure, with a minimum of 3 rewrites required.
- **Zero-rewrite escape hatch strengthened**: the `enforce_rewrites` addendum now names the exact behaviour that was rejected and provides concrete instructions (pick a role, lead with strong JD verbs, fallback to verbatim original rather than null).
- **Retry feedback addendum corrected**: "50–130%" → "50–150%" to match `_REWRITE_LEN_MAX_RATIO = 1.50`. Models were over-compressing because they read the addendum cap, not the code.
- **`_check_do_not_inject` bugfix**: when `cv_full_text=""` (PDF parse failure), the old guard `if cv_l and term in cv_l` always evaluated `False`, blocking every rewrite. Added `if not cv_full_text: return None` early-exit so injection checks are skipped entirely when there is no CV text to compare against.

### Why

End-to-end runs were producing PDFs byte-identical to the input despite the tailor running successfully — the diff had 0 rewrites. Root cause trace: Groq (fallback LLM) produced `text: null` for all bullets → escape hatch retried → still 0 → `job_agent` retry with feedback → still 0 → accepted best_diff with trivial summary → `apply_pdf_edits` short-circuit → nothing applied → identical PDF.

---

## v1.4 — DOCX Path + LibreOffice Render (13 May 2026)

**Bet under test:** Will editing CVs as DOCX (flow-based, paragraph-level) eliminate the coordinate-drift and font-shrinkage problems that plagued the PDF in-place editor for complex layouts?

### Shipped

- **Native `.docx` upload support**: users can upload Word documents directly; no PDF conversion required.
- **PDF → DOCX conversion path** (`agents/cv_pdf_to_docx.py`): `pdf2docx`-based conversion with a 0–100 convertibility score. PDFs scoring ≥60 use the DOCX path; lower scores fall back to the existing PDF replica/rebuild path.
- **DOCX tailor editor** (`agents/cv_docx_editor.py`): applies the `cv_diff_tailor` JSON diff at paragraph/run level — preserves bullet glyphs, blanks continuation paragraphs, zero font shrinkage.
- **LibreOffice headless render** (`agents/cv_docx_to_pdf.py`): replaces the previous `mammoth → WeasyPrint` approach. LibreOffice preserves Word fonts, tables, and layout exactly. Requires `libreoffice` in `packages.txt` (Streamlit Cloud) or PATH (local).
- **Router** (`agents/cv_docx_pipeline.py`): `try_route_docx()` + `apply_diff_and_render()` public API. Gated by `DOCX_PATH_ENABLED=1` env var for PDF uploads; `.docx` uploads always use this path.
- **Feature flag**: `DOCX_PATH_ENABLED=1` (default on). Set `0` to force legacy PyMuPDF in-place path.

### Why

The PDF in-place editor (`pdf_editor.py`) worked well for standard single-column CVs but produced misaligned text and font-size drift for designer templates and multi-column layouts. Editing at the DOCX paragraph level bypasses coordinate calculations entirely, and LibreOffice renders the result faithfully. Tested on the project owner's base CV: convertibility score 90/100, 3 roles parsed correctly, 3 bullet rewrites verified end-to-end.

---

## v1.3 — CV Tailoring Pipeline Robustness (5 May 2026)

**Bet under test:** Will comprehensive guardrails and edge-case handling make the CV tailoring pipeline robust enough to handle diverse CV formats and job descriptions without manual intervention?

### Shipped

- **Comprehensive CV tailoring pipeline fixes (P0, P1, P2-1):**
  - **P0-1: Stub-summary handling** — Adjusts word count bands for very short summaries (<20 words), prevents fabrication when no original summary exists, and caps stub summaries at 60 words
  - **P0-2: Zero-bullets early-exit** — Returns early with no-op diff when CV has 0 bullets to save ~25K tokens
  - **P0-3: Job dedup audit** — Audited existing dedup logic (sufficient, no changes needed)
  - **P1-1: Cross-page summary/bullets** — Guards against in-place edits when content spans multiple pages
  - **P1-2: Wrong-language detection** — ASCII ratio heuristic to detect non-English LLM output in summaries and bullets
  - **P1-3: Short-JD skip strategist** — Skips strategist call when JD < 50 words to save tokens
  - **P1-4: Long-JD compression** — Compresses JDs > 800 words by extracting key sections (requirements, responsibilities, qualifications)
  - **P1-5: Identity guard role-family awareness** — Allows role transitions within same career family (e.g., Account Manager → Social Media Account Manager) with role-family mappings
  - **P1-6: Surface strategist-key mismatch** — Logs when bullet keys don't match role headers for better observability
  - **P2-1: Outline cache** — In-memory cache with file mtime/size validation to avoid re-parsing PDFs

- **Earlier bundled fixes:**
  - **Outline parsing improvements** — `_merge_fragmented_roles` to collapse fragmented roles from 2-column layouts
  - **Summary preservation** — `_summaries_equivalent` to detect when summaries are byte-identical
  - **Identical-rewrite suppression** — Demotes byte-identical rewrites to text=None to save tokens
  - **Prompt rule against no-op rewrites** — Added Rule 2a to prompt template
  - **Concrete word-count band** — Passes absolute integers (e.g., "76-92 words") instead of percentages

- **Strategist module enhancements:**
  - 4-stage tolerant JSON parser (BOM, fences, brace-balance walk, strict=False)
  - max_tokens bump 900 → 2500 to stop JSON truncation
  - Short-JD skip and long-JD compression integration

- **Quality fixes:**
  - Cover letter depth requirements (require BOTH projects when CV has 2+)
  - Em/en-dash stripping from LLM outputs (removes stylistic tell)
  - Bullet glyph selection (default to U+2022 with Base14 fallback)
  - Number guard tightened to enforce only magnitude-marked tokens
  - CV embeddings: coerce dict-shaped bullets to text
  - LangFuse telemetry improvements for DeepSeek
  - **DeepSeek V4-Flash integration** — `chat_deepseek()` added to `llm_client.py` as primary writing LLM for CV strategy, bullet tailoring, and cover letters. `DEEPSEEK_API_KEY` + `DEEPSEEK_MODEL` env vars. Falls back to Groq automatically on any failure. `LLM_PROVIDER=nvidia` alternative path via NVIDIA NIM free-tier.
  - **`GEMINI_BYPASS=True` default** — `chat_gemini()` routes to Groq instead of Gemini by default. Removes the Gemini dependency for users who haven't set `GEMINI_API_KEY`. Set `GEMINI_BYPASS=0` to re-enable Gemini.

### Why

Run 12 telemetry showed several failure modes in the CV tailoring pipeline: summaries being cut too aggressively, bullets being dropped, language drift, and strategist JSON truncation. The comprehensive P0/P1/P2-1 analysis identified 55 potential failure modes, and the prioritized fixes address the most critical ones that were causing real issues in production.

---

## v1.2 — Production Polish (22 Apr 2026)

**Bet under test:** Will rebuild-path output be ATS-safe and visually trustworthy enough that users don't reject it for designer / multi-column CVs?

### Shipped

- **WeasyPrint HTML/CSS rebuild path** replaces ReportLab as the default fallback for designer / multi-column CVs. Native deps (`libpango`, `libcairo`, `libgdk-pixbuf`) declared in `packages.txt`. ReportLab kept as last-resort safety net for hosts without WeasyPrint's native deps.
- **Canonical CV section order** enforced by both renderers: Header → Summary → Experience → Education → Achievements → Skills → Other. Skills now sits after Achievements (recruiter-skim-order).
- **Original page count preserved** — `extract_cv_style` now captures `doc.page_count` so a 2-page CV stays 2 pages on rebuild.
- **Summary fabrication guard** — max 5% length shortening; CV-foreign proper nouns revert to original.
- **Cover letter fabrication guard** (prompt + post-gen) catches JD-only tool/framework names; retries with tightened prompt before falling back to placeholder.
- **Cover letter placeholder rewrite** — old placeholder hardcoded Rishav's employment history; new placeholder is CV-agnostic (uses only `job_title` and `company` from inputs).
- **Gemini 3-key rotation pool** matching Groq; cross-provider fallback only when both pools exhausted.
- **Deployment-wide daily usage counter** backed by file cache so all users and tabs see the same runs-left value; daily reset auto-detected from Groq response headers.
- **Mixpanel `distinct_id`** persisted via `?aid=<uuid>` query param so the funnel survives page refreshes.
- **Reviewer prompt fix** — `_render_diff_for_review` now extracts `.text` from bullet dicts instead of dumping Python `repr`, so the reviewer scores against actual content.
- **Planner `max_scrape_rounds`** cap raised from 2 to `len(bundles)` (max 5) so all generated bundles are reachable when early rounds underperform.
- **`_call_groq` failure mode** — raises `RuntimeError` on full pool exhaustion instead of silently returning `""` (which caused callers to parse JSON from empty string).
- **Log noise** silenced (Streamlit watcher off, internal logger at error level).
- PDF parser repairs mis-decoded bullet glyphs from symbol fonts.

### Why

Real users on the deployed app reported that rebuild-path output had no summary, `-` glyphs instead of bullets, missing contact details, and the wrong page count. The fabrication framing also bled into cover-letter placeholder text, which was hardcoded to one specific user's CV.

---

## v1.1 — LLM Stack Hardening (21 Apr 2026)

**Bet under test:** Will dual-provider routing (Groq for structured, Gemini for prose) produce noticeably better tailored output without burning the free-tier daily envelope?

### Shipped

- **Gemma fully removed.** v1.0 used Gemma 4 for creative tasks; quality and throttling trade-offs swung negative.
- **Dual-LLM architecture:** Gemini 2.5 Flash for writing (CV summary, bullets, cover letters); Groq Llama-3.3-70B for structured tasks (matching, planning, reviewers, supervisor).
- **Centralised LLM routing** in `agents/llm_client.py` — no ad-hoc clients anywhere.
- **Groq 3-key rotation pool** — testing burned through one key per afternoon; rotation triples the daily envelope on the free tier.
- **GDPR baseline** — consent gate, PII redaction in snapshots, `docs/PRIVACY.md` published.
- **Live Mixpanel dashboard** (5 reports) with privacy-safe `distinct_id`.
- **Token budgets tightened**; no-drop bullet policy; bullet glyph + wrap-width fixes.

### Why

External Perplexity audit surfaced 5 critical issues: wrong Gemma model ID, 60s min-gap (should have been 3s), debug prints in production paths, CV diff tailor on the wrong model, API keys not using `secret_or_env()`. v1.1 was a focused response to that audit plus a friend's "the cover letters feel generic" feedback.

---

## v1.0 — Core Pipeline (20 Apr 2026)

**Bet under test:** Can a multi-agent LangGraph pipeline take a CV + role description and produce 10 tailored applications faster than a human can produce 1?

### Shipped

- **Multi-agent LangGraph pipeline** — supervisor + 9 worker/reviewer agents.
- **RAG over CV** with ChromaDB + Sentence-Transformers.
- **Live job scraping** across LinkedIn, Indeed, Glassdoor, Jobs.ie, Builtin with board-fallback.
- **Diff-based CV tailoring** with fabrication sanitizer; aggressive bullet rewrite / drop / reorder mode.
- **Reviewer agent** with retry loop.
- **PDF in-place editing** via PyMuPDF — preserves original layout, fonts, margins, colours.
- **Cover letter generator** using LLM with grounded prompt.
- **Email delivery** via Gmail SMTP (app password).
- **Experience-level dropdown + YOE-based early-exit matcher** — saves 30-50% matcher LLM budget on broad scrapes.
- **Bulk-send button** with progress UI.
- **Crash-safe snapshots**, capped rate-limit waits, per-run LLM budget cap.

### Why

Started from the founder's own pain (50-application job hunt, 30-minute-per-application tailoring). The first usable version aimed to make the full loop work end-to-end on a real CV + real job board, not to be polished.

---

## Pre-v1.0 — Prototype Iterations

| Iteration | What | Why killed |
|---|---|---|
| v0.1 | Single-agent prompt chain | Couldn't survive a multi-job batch — state management was ad-hoc. |
| v0.2 | LangChain agents | Too much abstraction; debugging which tool was called when was painful. |
| v0.3 | Plain Python pipeline | Worked but couldn't reroute on failure (e.g. one bad scrape killed the run). |
| v0.4 | LangGraph supervisor + workers | Kept. Stateful routing was the unlock. |
| v0.5 | Reorder-only diff tailoring | Shipped, but cross-job CV similarity was 93.5%. Friend feedback: "looks lazy." Replaced in v1.0 with rewrite/drop/reorder mode. |
| v0.6 | Full CV rewrite mode | Killed before shipping — fabrication rate too high. Diff-based tailoring won. |
| v0.7 | Auto-infer experience level from CV | Killed — broke for career-switchers (high seniority title, low domain YOE). User-declared dropdown won. |

---

## Looking ahead

Forward bets and the conditional roadmap live in `ROADMAP.md`. The current
product hypothesis and falsifiable tests live in `PM_CASE_STUDY.md`.
