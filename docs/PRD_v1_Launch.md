# PRD: ApplySmart AI v1.0 Launch

> **Product Requirements Document**  
> **Release:** v1.0 — LinkedIn portfolio launch  
> **Author:** Rishav Singh  
> **Status:** Draft → Ready for build → **Active** (target launch: within 7 days)  
> **Last updated:** April 20, 2026

---

## TL;DR

Ship ApplySmart AI v1.0 as a public LinkedIn portfolio demo. Scope is locked: **single-column ATS CVs**, **six job boards**, **aggressive tailoring**, **YOE-based matching**, **consent-gated tracing**. Everything not in §5 is v1.1+.

A run succeeds if a user uploads a PDF CV, picks a role, and within 5 minutes receives tailored CV + cover letter PDFs for 3-10 matched jobs with zero fabricated facts.

---

## 1. Context & Problem

### 1.1 Problem
Job-seekers applying to 20-50 roles per month spend 25+ hours on tailoring and still send generic content that fails ATS filters. Existing tools solve individual steps (scraping, keyword-matching, template-rebuild) but don't pipeline them end-to-end while preserving CV layout and preventing fabrication.

### 1.2 Why launch now
- Core pipeline (scrape → match → tailor → PDF → email) is shipped and works on my own CV.
- Recent additions (aggressive tailoring + YOE filter) address the two biggest user complaints ("CVs look identical" and "matched to over-leveled roles").
- Demand signal: three friends have already asked to use it.
- Portfolio value: LinkedIn recruiters will see it *if* it's live; a shelved project scores zero.

### 1.3 Strategic frame
This is a **portfolio launch**, not a commercial launch. Success is measured in recruiter visibility and user feedback — not revenue. That shapes every trade-off: ship with rough edges where recruiters won't notice, lock down where a single bad experience would torpedo signal.

---

## 2. Target Users

### 2.1 Primary
Early/mid-career professionals applying to white-collar roles (PM, engineering, marketing, operations, data) who:
- Own an existing PDF CV they like
- Apply to 10+ roles/month
- Are comfortable with a tech-forward tool

### 2.2 Secondary
- Hiring managers / recruiters viewing the LinkedIn post (evaluating me, not using the tool)
- Friends and colleagues I personally send the link to

### 2.3 Not the target
- Non-English CVs (scope)
- Designer CVs with multi-column layouts (technical limitation)
- Users without a PDF CV (blocked by upload gate)

### 2.4 Jobs-to-be-done
1. *"When I'm applying to a specific role, I want to tailor my CV in 2 minutes instead of 30, so I can actually apply to the volume I need without burning out."*
2. *"When I'm unsure if my CV fits a role, I want a score and explanation, so I can decide whether to apply at all."*
3. *"When I share this with a hiring manager on LinkedIn, I want them to see professional craft, so it acts as a portfolio artifact."*

---

## 3. Goals

### 3.1 Product goals (v1.0)
- **G1:** End-to-end run success rate >90% on single-column ATS CVs
- **G2:** Cross-job CV similarity <70% (measurable: token-level diff between two tailored CVs from same batch)
- **G3:** Zero fabricated facts across 10 manually-reviewed tailored CVs
- **G4:** Total run time <5 minutes for 10-job batches

### 3.2 Portfolio goals
- **P1:** LinkedIn post published within 7 days of PRD approval
- **P2:** ≥10 hands-on demo runs from LinkedIn viewers within first week
- **P3:** ≥3 external (non-friend) users complete at least one batch
- **P4:** ≥2 recruiter DMs referencing the project within 14 days

### 3.3 Non-goals (explicit)
- Not targeting commercial revenue
- Not building user accounts / auth
- Not optimizing for mobile UI
- Not building analytics dashboards for end users
- Not supporting DOCX, ODT, or Google Docs input

---

## 4. User Stories

Numbered for traceability to acceptance criteria in §8.

**US-01:** *As a job-seeker, I want to upload my existing CV and see the system parse it correctly, so I trust it understood my background before I invest further.*

**US-02:** *As a job-seeker, I want to select my experience level and target role, so the system filters out over/under-leveled matches.*

**US-03:** *As a job-seeker, I want to see match scores with reasoning, so I can prioritize applications.*

**US-04:** *As a job-seeker, I want each matched role's CV and cover letter to be tailored to that role specifically, not the same content swapped across jobs.*

**US-05:** *As a job-seeker, I want a preview before anything is emailed, so I catch mistakes before they reach recruiters.*

**US-06:** *As a job-seeker, I want to send all matched applications with one click, so I don't have to repeat the action per-job.*

**US-07:** *As an EU-resident user, I want to control whether my data is traced to third-party services, so I'm not unknowingly subject to cross-border data transfer.*

**US-08:** *As a LinkedIn viewer, I want the demo to be usable within 3 clicks of landing on the app, so I can evaluate the project without reading setup docs.*

---

## 5. Functional Requirements (In Scope for v1.0)

Tagged with priority: **P0** (must ship) / **P1** (should ship if time permits) / **P2** (nice to have).

### 5.1 Core pipeline

| ID | Requirement | Priority | Status |
|---|---|---|---|
| FR-01 | Accept PDF CV upload ≤5MB via Streamlit file uploader | P0 | ✅ Shipped |
| FR-02 | Parse CV text + extract layout structure (sections, role blocks, bullets, skills) | P0 | ✅ Shipped |
| FR-03 | Validate CV pre-flight (warn if sections undetected, bullets < threshold) | P0 | ✅ Shipped |
| FR-04 | Scrape jobs from ≥1 of: LinkedIn, Indeed, Glassdoor, Jobs.ie, Builtin with fallback sequence | P0 | ✅ Shipped |
| FR-05 | Score each job against CV using Groq LLM + RAG over CV embeddings | P0 | ✅ Shipped |
| FR-06 | Filter jobs by user-selected experience level with level-gap penalty + YOE early-exit | P0 | ✅ Shipped |
| FR-07 | Generate per-job diff-tailor (summary rewrite, bullet reorder/rewrite/drop, skills order) | P0 | ✅ Shipped |
| FR-08 | Apply diff to PDF in-place preserving original layout | P0 | ✅ Shipped |
| FR-09 | Generate per-job cover letter | P0 | ✅ Shipped |
| FR-10 | Second LLM pass (reviewer) checks for fabrication; triggers retry if score < threshold | P0 | ✅ Shipped |
| FR-11 | Send tailored CV + cover letter as email attachments via Resend API | P0 | ✅ Shipped |
| FR-12 | Save crash-safe snapshot of inputs, state, errors per run | P0 | ✅ Shipped |

### 5.2 UI / UX

| ID | Requirement | Priority | Status |
|---|---|---|---|
| FR-13 | Sidebar captures: CV, full name, email, job title, location, job board, experience level, num jobs, match threshold, preview toggle | P0 | ✅ Shipped |
| FR-14 | Main area shows matched jobs as cards with score, reasoning, and actions | P0 | ✅ Shipped |
| FR-15 | Bulk "Send all" button with progress indicator and per-job error handling | P0 | ✅ Shipped |
| FR-16 | Error banner and retry button on crash | P0 | ✅ Shipped |
| FR-17 | Consent banner for third-party tracing (off by default) | **P0** | **⏳ Pending** |

### 5.3 Observability & safety

| ID | Requirement | Priority | Status |
|---|---|---|---|
| FR-18 | LLM budget cap per run with soft abort on exceed | P0 | ✅ Shipped |
| FR-19 | Rate-limit handling with capped waits (max 60s per retry) | P0 | ✅ Shipped |
| FR-20 | Prompt-injection hardening (fenced untrusted blocks, preambles) | P0 | ✅ Shipped |
| FR-21 | Sanitizer enforces numeric preservation and length bounds on bullet rewrites | P0 | ✅ Shipped |

### 5.4 Documentation & launch

| ID | Requirement | Priority | Status |
|---|---|---|---|
| FR-22 | `.env.example` with all required keys placeholder-ed | **P0** | **⏳ Pending** |
| FR-23 | README with setup, run, known limitations, privacy note | **P0** | **⏳ Pending** |
| FR-24 | `PM_CASE_STUDY.md` public | P0 | ✅ Shipped |
| FR-25 | `HANDOFF_SUMMARY.md` for engineer continuity | P0 | ✅ Shipped |
| FR-26 | Demo video (90 sec, screen recording) | P1 | Pending |

---

## 6. Non-Functional Requirements

### 6.1 Performance
- **NFR-P1:** End-to-end 10-job batch completes in <5 min on a consumer laptop (not counting user time reviewing).
- **NFR-P2:** First match card renders within 30s of clicking "Run".
- **NFR-P3:** PDF generation per job <10s.

### 6.2 Reliability
- **NFR-R1:** Run success rate >90% on single-column ATS CVs (measured across 20 test runs).
- **NFR-R2:** Individual job failure does not crash the batch (caught per-job, logged, shown in UI).
- **NFR-R3:** LLM provider failure falls back to secondary provider or degrades gracefully.

### 6.3 Privacy (portfolio-grade)
- **NFR-Priv1:** `LANGCHAIN_TRACING_V2=false` by default in `.env.example`.
- **NFR-Priv2:** User must explicitly opt into third-party tracing via sidebar checkbox.
- **NFR-Priv3:** README clearly states what data is collected and where it flows.
- **NFR-Priv4:** No PII is logged to terminal at INFO level (email masked in logs).

> Note: Full GDPR compliance (PII redaction, right-to-erasure, DPA with providers) is **v1.1 scope**. See `docs/PRD_Privacy_Layer.md`.

### 6.4 Security
- **NFR-S1:** API keys loaded from `.env` — never hard-coded.
- **NFR-S2:** API keys rotated before LinkedIn launch (current keys already exposed in chat history).
- **NFR-S3:** `.env` in `.gitignore`; `.env.example` with placeholders committed.

### 6.5 Observability
- **NFR-O1:** Every run produces a snapshot JSON in `sessions/{id}/snapshot.json`.
- **NFR-O2:** LLM budget counters visible in terminal output.
- **NFR-O3:** Errors bubble up to UI banner with actionable message.

---

## 7. Out of Scope (Deferred to v1.1+)

Explicit anti-scope. If it's here, it's not shipping in v1.0.

- ❌ Designer CV multi-column layouts (Novoresume, Canva) → `HANDOFF_SUMMARY.md §11`
- ❌ Full PII redaction layer → `docs/PRD_Privacy_Layer.md`
- ❌ DOCX / ODT / Google Docs input
- ❌ Multi-tenant auth or user accounts
- ❌ Paid tier / usage limits per user
- ❌ Chrome extension for one-click apply
- ❌ ATS system direct integration (Greenhouse, Lever, etc.)
- ❌ Mobile-optimized UI
- ❌ Analytics dashboard for end users
- ❌ Multi-language CV support
- ❌ Real-time match score streaming (currently waits for all before rendering)
- ❌ Non-English job boards
- ❌ Salary negotiation assistance
- ❌ Interview prep features

---

## 8. Acceptance Criteria

Maps back to user stories in §4. Each criterion must pass before launch.

| ID | Criterion | How verified |
|---|---|---|
| AC-01 | Upload PDF → see sections extracted in log within 5s | Manual test with 3 sample CVs |
| AC-02 | Experience level dropdown affects match filter | Run same batch with two different levels, compare matched counts |
| AC-03 | Each matched job shows score ≥ threshold + reasoning text | UI inspection |
| AC-04 | Tailored CVs from same batch have cross-job similarity <70% | `scripts/diag_tailor_diff.py` |
| AC-05 | Preview mode default ON; "Run agent" with preview on does NOT send emails | Manual test |
| AC-06 | Bulk Send button sends all matched jobs; errors shown per-job if any fail | Manual test with 3 matches |
| AC-07 | Consent banner visible; tracing disabled unless user opts in | Code inspection + env var check |
| AC-08 | First-run user can complete a batch without reading docs | Usability test with 1 friend, no guidance |

---

## 9. Dependencies

### 9.1 External services (must be working on launch day)
- **Groq API** — matcher, supervisor, reviewer (fast tasks)
- **Gemini 2.5 Flash API** — CV tailoring, cover letter generation (writing tasks)
- **Resend API** — email delivery
- **ChromaDB** — local, bundled (no external dependency)
- **SerpAPI / JSearch / Adzuna** — scraping augmentation

### 9.2 Internal dependencies
- Python 3.10+
- Streamlit 1.30+
- PyMuPDF (fitz) 1.23+
- LangGraph 0.2+

### 9.3 Blocked on
- **v1.0 BLOCKING** — FR-17 (consent banner), FR-22 (env.example), FR-23 (README), NFR-S2 (key rotation)

---

## 10. Risks & Mitigations

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | Live run exposes regression from today's changes | High | High | Do full pipeline test pre-launch (pending task) |
| R2 | LinkedIn viewer uses a Novoresume/Canva CV → butchered output → reputational damage | Medium | High | README banner warning; v1.0 restricts to single-column ATS |
| R3 | LLM provider quota exhausted on launch day | Medium | High | Both Groq and Gemma keys valid; capped budget per run |
| R4 | GDPR complaint from EU user without consent | Low (given consent banner) | Medium | Consent banner + tracing-off default |
| R5 | API keys leaked in chat history abused | Medium (keys already exposed) | High | **Rotate all keys before launch** (NFR-S2) |
| R6 | Ugly PDF output on non-standard fonts | Medium | Medium | Font fallback chain already in `_font_can_render` |
| R7 | Email deliverability (Resend sandbox restricts sender domain) | Medium | Medium | Document known limitation; instruct users to use whitelisted email |
| R8 | Supervisor LLM hallucinates routing and loops | Low | Medium | Hard cap on supervisor_cycles in state |

---

## 11. Open Questions

1. **Should the consent banner persist across sessions, or re-prompt every time?** → Re-prompt every session for v1.0 (simpler, more conservative).
2. **Do I gate the LinkedIn demo behind a password for week 1 to control who tries it?** → No; defeats the portfolio-signal purpose.
3. **Should I seed ChromaDB with a sample CV to make the first-run experience less empty?** → No; user uploads their own CV on start.
4. **How do I handle users who try to upload 10MB CVs?** → Reject at upload with friendly message. File size cap = 5MB.

---

## 12. Launch Plan

### Milestones

| Day | Deliverable | Owner |
|---|---|---|
| D-3 | Full live pipeline test; regression triage | Rishav |
| D-2 | Consent banner (FR-17), `.env.example` (FR-22), key rotation (NFR-S2) | Rishav |
| D-1 | README v1 (FR-23), demo video recording (FR-26) | Rishav |
| D0 | LinkedIn post + repo made public | Rishav |
| D+1..7 | Monitor feedback, triage issues into v1.1 backlog | Rishav |
| D+7 | Retro — ship v1.1 scope doc | Rishav |

### LinkedIn post structure (draft)

> *"I built an end-to-end agentic AI system that automates job application tailoring while preserving your CV's original layout. Four months. Seven architecture decisions. Two PRDs. One honest case study. Links in comments."*
>
> Attach: short screen recording + link to repo + link to `PM_CASE_STUDY.md`.

---

## 13. Rollback Plan

If something critical breaks post-launch:

1. **Immediate (< 1hr):** Pin `README.md` badge to "under maintenance"; point app's welcome screen to a static "be right back" page.
2. **Within 24hr:** Diagnose via crash snapshots; hotfix + redeploy.
3. **If unrecoverable:** Take repo private, post LinkedIn update acknowledging the issue. Not worse than doing nothing — shows real-world rigor.

No rollback to a previous version is needed since this is the first release.

---

## 14. Sign-off

- [ ] Product owner (Rishav) — approved
- [ ] Eng review (self) — PR #... merged
- [ ] Live pipeline test — passed
- [ ] Privacy checklist — consent banner shipped; keys rotated; README privacy note live
- [ ] LinkedIn post drafted and scheduled

---

*This PRD is a living document. Changes after approval must be logged here with date + rationale.*

### Change log
- 2026-04-20 — Initial draft
