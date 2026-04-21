# ApplySmart AI — Product Decisions Register

Last updated: 2026-04-22
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
