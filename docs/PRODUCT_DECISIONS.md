# ApplySmart AI — Product Decisions Register

Last updated: 2026-04-22 (evening)
Owner: Rishav Singh

This document is the concise "why" layer for portfolio and team handoff.
It complements `PM_CASE_STUDY.md` (narrative) with crisp product decisions.

---

## Decision 1 — Layout Preservation Over Template Rebuild

- **Decision:** Tailored CV output prioritizes preserving the user's original visual structure.
- **Why:** User trust drops when CV design is replaced by generic templates.
- **Options considered:** Full PDF rebuild (faster), DOCX regenerate, in-place edit.
- **Chosen approach:** In-place edits when possible; controlled fallback only when required.
- **Trade-off:** Higher implementation complexity and edge cases for multi-column designer CVs.

## Decision 2 — Supervisor + Specialist Workers

- **Decision:** Use supervisor-worker orchestration instead of one monolithic prompt chain.
- **Why:** Better control over retries, quality checks, and fallback behavior.
- **Options considered:** Single sequential script, one giant LLM agent.
- **Chosen approach:** Coordinator routes to specialist workers (parse, style, scrape, match, tailor, review, send).
- **Trade-off:** More state management complexity.

## Decision 3 — Deterministic Filters Before LLM

- **Decision:** Apply level/YOE filters before expensive matching calls.
- **Why:** Prevent obvious mismatch calls and save model budget.
- **Options considered:** LLM-only scoring with soft hints.
- **Chosen approach:** Hard pre-checks + LLM for nuanced cases.
- **Trade-off:** Heuristic rules need maintenance and can miss implicit JD wording.

## Decision 4 — Reviewer Loop For Fabrication Risk

- **Decision:** Add second-pass reviewer with retry path for weak/fabricated outputs.
- **Why:** Tailoring without guardrails can invent claims and damage user trust.
- **Options considered:** No review, human-only review, rules-only review.
- **Chosen approach:** LLM reviewer + deterministic sanitizer checks.
- **Trade-off:** Extra latency and LLM calls per matched job.

## Decision 5 — Board Preference With Intelligent Fallback

- **Decision:** Respect selected primary board, then automatically try others if empty.
- **Why:** Users want control without manual board switching.
- **Options considered:** "All only", fixed single-board behavior.
- **Chosen approach:** Preferred-board-first fallback sequence.
- **Trade-off:** Cross-board scraping can increase runtime.

## Decision 6 — Preview-First Sending

- **Decision:** Keep "preview before send" default behavior and support explicit send action.
- **Why:** Reduces accidental outbound messages and improves trust.
- **Options considered:** Auto-send after generation.
- **Chosen approach:** Preview mode with explicit send controls.
- **Trade-off:** One extra user step.

## Decision 7 — Product-Led UI Hierarchy

- **Decision:** Promote brand and workflow clarity in the main view (not hidden in sidebar).
- **Why:** Portfolio product should feel like a real SaaS, not a utility script.
- **Options considered:** Sidebar brand + utilitarian layout.
- **Chosen approach:** Centered masthead, stronger hero hierarchy, cleaner sidebar system.
- **Trade-off:** Ongoing UI polish effort.

## Decision 8 — Snapshot-First Observability

- **Decision:** Persist run snapshots with state/errors/budget.
- **Why:** Enables debugging, reproducibility, and KPI backfill.
- **Options considered:** Logs only.
- **Chosen approach:** Structured snapshots plus in-app insight surfaces.
- **Trade-off:** Requires PII/privacy discipline before public scale.

## Decision 9 — Dual-LLM Architecture for Quality vs Speed

- **Decision:** Use Groq for fast tasks (matching, planning, review) and Gemini 2.5 Flash for writing tasks (CV tailoring, cover letters).
- **Why:** Gemini excels at creative writing with long context windows, while Groq provides faster inference for structured tasks. This split optimizes both quality and latency.
- **Options considered:** Single LLM for all tasks, Groq-only, Gemini-only.
- **Chosen approach:** Dual-LLM architecture with task-specific routing and automatic fallback to Groq if Gemini is unavailable.
- **Trade-off:** Requires managing two API keys and rate limits, but delivers better writing quality at acceptable latency with graceful degradation.

## Decision 10 — Per-Session Run Limit for Quota Protection

- **Decision:** Limit each browser session to 3 runs per day to prevent quota abuse.
- **Why:** Without authentication, a single user could drain the entire deployment-wide Groq quota by refreshing or opening new tabs.
- **Options considered:** No limit, IP-based tracking, authentication-based tracking.
- **Chosen approach:** Per-browser-session limit with daily reset via session state date tracking.
- **Trade-off:** Determined users can bypass by opening new tabs/incognito, but this stops the most common abuse pattern (refreshing loop).

## Decision 11 — File-Based Deployment-Wide Quota Tracking

- **Decision:** Store deployment-wide token usage in a JSON file (`.quota_cache.json`) instead of in-memory variables.
- **Why:** Session state is per-browser-tab and resets on new tabs, giving false quota readings. File-based storage ensures all tabs see the same quota counter.
- **Options considered:** In-memory global, session state, file-based cache, database.
- **Chosen approach:** File-based JSON cache with date-based daily reset.
- **Trade-off:** Race condition possible with simultaneous writes, but acceptable given per-session run limit as primary abuse prevention.

## Decision 12 — WeasyPrint HTML/CSS Rebuild Path Before ReportLab

- **Decision:** When in-place PDF editing is impossible (designer / multi-column templates), attempt WeasyPrint HTML+CSS rebuild first; only fall back to ReportLab when WeasyPrint is unavailable.
- **Why:** ReportLab rebuilds produced ATS-unfriendly output on designer CVs — no summary section, bullets rendered as `-`, missing contact details, often adding an unnecessary second page. WeasyPrint + Jinja2 templates give us semantic HTML (`h1`/`h2`/`ul`/`li`), standard fonts, and auto-scaled typography that preserves the original page count.
- **Options considered:** Keep ReportLab only (low effort), Playwright/Chromium render (heavyweight, slower), WeasyPrint (mid-weight but ATS-safe), DOCX then LibreOffice convert (platform coupling).
- **Chosen approach:** WeasyPrint primary, ReportLab safety net. System libs (`libpango`, `libcairo`, etc.) shipped via `packages.txt` for Streamlit Cloud.
- **Trade-off:** Extra dependency footprint on the deploy host; render quality depends on `packages.txt` installing cleanly. Graceful fallback keeps the app functional even if WeasyPrint is missing locally.

## Decision 13 — Canonical CV Section Order Independent of LLM Output

- **Decision:** Both PDF renderers reorder tailored sections into a fixed sequence — **Header → Professional Summary → Experience → Projects → Education → Skills → Certifications / Other** — before drawing anything.
- **Why:** Recruiters scan in a predictable order (6-second test). LLMs occasionally emit sections in an unconventional order, especially on the Groq fallback path; pure-Python sorting decouples layout from prompt variance.
- **Options considered:** Trust LLM ordering (brittle), re-prompt the LLM to reorder (wastes calls), hard-code the order in the renderer (chosen).
- **Chosen approach:** Python sort on the parsed section list using an explicit priority map.
- **Trade-off:** Users who *want* a non-standard order (e.g. Skills before Experience for career switchers) can't override via the prompt. Acceptable for v1 — standard order is what ATS + recruiters expect.

## Decision 14 — Dual-Provider Key Rotation for Resilience

- **Decision:** Both Groq and Gemini support up to 3 rotating API keys (`*_API_KEY`, `*_API_KEY_2`, `*_API_KEY_3`). On 429 / quota / auth errors, the client advances to the next key and retries immediately.
- **Why:** Free-tier quotas (Gemini 2.5 Flash in particular) are tight enough that a single developer testing the app can exhaust the daily envelope in an afternoon. Key rotation triples the daily envelope without paying for a tier upgrade.
- **Options considered:** Single key + exponential backoff (user-blocking stalls), paid tier upgrade (unnecessary at current scale), key rotation (chosen).
- **Chosen approach:** Identical rotation pattern on both providers. Gemini falls back to Groq only when all Gemini keys are exhausted, preserving quality where possible.
- **Trade-off:** Managing multiple developer accounts per provider. Mitigated because each key still belongs to the same human — no additional user data is involved.

## Decision 15 — Persistent Anonymous ID via URL Query Param

- **Decision:** Mixpanel distinct_id is stored in the URL (`?aid=<uuid>`), not in Streamlit session state.
- **Why:** Streamlit `session_state` resets on every tab refresh, reopen, or idle timeout. The Mixpanel funnel (cv_uploaded → run_started → run_completed → send_completed → job_marked_applied) was breaking on "Today" because users refreshed mid-funnel during testing, creating new "users" at each step. URL query params survive refreshes and back-navigation without a cookie dependency.
- **Options considered:** `session_state` only (breaks on refresh), cookies via `extra-streamlit-components` (extra dep + UX friction), `st.context.cookies` (Streamlit ≥1.37 only, read-only), URL query param (chosen).
- **Chosen approach:** Generate on first load, write back to `st.query_params`, reuse on subsequent loads.
- **Trade-off:** URL carries a visible opaque id. Doesn't persist across browsers / incognito / different devices — which is acceptable for an anonymous funnel; authenticated users would get a stable email-hash id anyway.

---

## Success Criteria Tied To Decisions

- Layout preservation accepted by users (`render_mode == in_place` preference).
- Reduced wasted calls via deterministic pre-filters.
- Reviewer catches low-quality/fabricated tailoring before send.
- Fallback board search increases successful job discovery.
- Preview mode reduces accidental sends.
- Gemini fallback ensures cover letters always generate even if Gemini is unavailable.
- Per-session limit prevents single-user quota drain.
- Deployment-wide quota accurately reflects total usage across all tabs.

---

## Next Validation Experiments

1. Compare application outcomes between in-place and fallback rebuilt CVs.
2. Measure early-exit filter savings as % of potential matcher calls.
3. Measure reviewer retry impact on final acceptance/send rate.
4. Measure fallback-board contribution to total matched jobs.
5. Measure Gemini fallback frequency and quality degradation.
