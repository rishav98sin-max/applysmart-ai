# ApplySmart AI — Changelog

> Version-by-version build log. **What** changed and **why** it changed.
> Customer hypothesis and product bets live in `PM_CASE_STUDY.md`;
> engineering handoff details live in `HANDOFF_SUMMARY.md`.
>
> Format: each version notes the bet it was testing, the change, and the
> evidence that drove it.

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
