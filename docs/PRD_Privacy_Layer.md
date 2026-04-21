# PRD: Privacy Layer (v1.1)

> **Product Requirements Document**  
> **Release:** v1.1 — post-launch compliance upgrade  
> **Author:** Rishav Singh  
> **Status:** Draft (forward-looking; to be built after v1.0 portfolio launch)  
> **Last updated:** April 20, 2026

---

## TL;DR

Before ApplySmart AI accepts non-portfolio traffic, ship a privacy layer that: **(a)** redacts PII from all third-party telemetry (LangSmith, provider logs), **(b)** gives users an explicit consent flow on first run, **(c)** supports data deletion on request, and **(d)** documents data flow transparently.

This is the minimum bar for touching EU/UK users without GDPR exposure and for being sharable with strangers beyond my personal circle.

---

## 1. Context & Problem

### 1.1 Current state (v1.0)
- User uploads a PDF CV containing name, email, phone, employers, dates, education
- CV text is included in LLM prompts → sent to Groq / Google Gemma
- By default (v1.0), LangSmith tracing is **disabled** via `.env` → no third-party telemetry of prompts
- If user opts in via consent banner (v1.0), prompts including PII flow to LangSmith
- Session data (CV, generated PDFs) lives in `sessions/{uuid}/` indefinitely

### 1.2 Problem
v1.0's consent-banner approach is **portfolio-grade** — legally defensible for a small LinkedIn demo, but insufficient for any of the following scenarios:

1. A stranger in the EU uses the app → their CV flows to a US-based LLM provider without a data transfer assessment
2. A user asks "can you delete my data?" → currently no way to honor that request without manual file deletion
3. A recruiter asks during an interview: *"How did you handle GDPR?"* → "Consent banner" is weak; "redaction layer + right-to-erasure + DPA" is strong
4. The tool gets picked up beyond my circle → we hit the compliance risk we've been deferring

### 1.3 Trigger for v1.1
Any one of these ships v1.1:
- **T1:** Reaches 25+ unique users
- **T2:** Any EU/UK user identified (geo-inferred from browser locale)
- **T3:** Any external party (not a friend) reaches out asking about data handling
- **T4:** Legal review recommends before monetization

---

## 2. Target Users

### 2.1 Primary
- EU/UK residents using ApplySmart AI (GDPR applies)
- Privacy-conscious users in any jurisdiction

### 2.2 Secondary
- Security-conscious hiring managers evaluating the project as a portfolio artifact
- My future self (commercial version)

### 2.3 Jobs-to-be-done
1. *"When I upload my CV, I want to know what data leaves my machine and where it goes."*
2. *"When I change my mind, I want to delete everything I submitted within a reasonable time."*
3. *"When I don't want third-party telemetry, I want a default-off stance — not an opt-out buried in settings."*

---

## 3. Goals

### 3.1 Product goals
- **G1:** Zero PII leaves the user's machine without explicit consent
- **G2:** When consent is given, PII is redacted before being sent to observability providers (LangSmith), even if not redacted from LLM providers (Groq, Gemma)
- **G3:** Users can delete their session data in <5 seconds via UI
- **G4:** Data flow is fully documented in a plain-English privacy page linked from the UI

### 3.2 Compliance goals
- **C1:** GDPR Articles 5, 6, 13, 17, 25, 30, 44 — addressed in documentation
- **C2:** Meet the "privacy-by-design" standard (Article 25) for any new feature
- **C3:** Data retention policy explicit (session data auto-purged after 7 days)

### 3.3 Non-goals
- **NG1:** Not pursuing CCPA, LGPD, or PIPEDA compliance in this release (GDPR-first)
- **NG2:** Not encrypting session data at rest (local filesystem is the user's own; no shared storage)
- **NG3:** Not onboarding a DPO (Data Protection Officer) — out of scope for a solo project
- **NG4:** Not implementing a full identity-access layer (that's v2.0)

---

## 4. User Stories

**US-01:** *As an EU user, I want the app to NOT send my CV anywhere I didn't explicitly authorize, so I stay compliant with local laws.*

**US-02:** *As any user, I want to see exactly what data is collected, who receives it, and for how long, on a single page.*

**US-03:** *As any user, I want a "Delete my data" button that removes my session from the server within seconds.*

**US-04:** *As a privacy-conscious user, I want tracing to be OFF unless I turn it on, not a hidden opt-out.*

**US-05:** *As a user who consented, I want to know that even with consent, my name and email are redacted from telemetry — only the CV body (skills, bullets) is logged for debugging.*

**US-06:** *As a recruiter reviewing this project, I want to see privacy handled with the same rigor as the core product.*

---

## 5. Functional Requirements

### 5.1 PII Redaction Layer

| ID | Requirement | Priority |
|---|---|---|
| FR-01 | Create `agents/privacy.py` with a `redact_pii(text, candidate_name, user_email)` function | P0 |
| FR-02 | Redactor replaces full name (case-insensitive, first+last variants) with `[CANDIDATE]` | P0 |
| FR-03 | Redactor replaces user's email address with `[EMAIL]` | P0 |
| FR-04 | Redactor replaces phone number patterns (international + local) with `[PHONE]` | P0 |
| FR-05 | Redactor replaces postal addresses using regex for postcodes + common city patterns with `[ADDRESS]` | P1 |
| FR-06 | Redactor preserves CV body (skills, employers, job titles, bullet text) intact — these are useful for debugging and less uniquely identifying | P0 |
| FR-07 | `llm_client.py` wrappers call redactor before passing text to LangSmith tracer (but NOT before LLM providers — they need full context to produce output) | P0 |
| FR-08 | Redaction is idempotent and reversible only via a local salt stored in session (not in telemetry) | P1 |

### 5.2 Consent Flow

| ID | Requirement | Priority |
|---|---|---|
| FR-09 | First-run modal / banner explains data flow in plain English | P0 |
| FR-10 | Three explicit choices: *"Allow anonymized traces" / "Disable all tracing" / "Cancel"* | P0 |
| FR-11 | Consent decision persisted per-session (Streamlit session_state), not globally | P0 |
| FR-12 | Consent can be revoked from the sidebar at any time → immediately disables tracing for rest of session | P0 |
| FR-13 | Re-prompt consent on every new session (no cookies/persistence) | P0 |

### 5.3 Data Deletion

| ID | Requirement | Priority |
|---|---|---|
| FR-14 | Sidebar has a "Delete my session data" button | P0 |
| FR-15 | Deletion removes the session dir (`sessions/{id}/`) including CV uploads, tailored outputs, snapshots | P0 |
| FR-16 | Deletion confirmation: "This will permanently erase your CV and all generated files. Continue?" | P0 |
| FR-17 | After deletion, Streamlit session is reset to welcome screen | P0 |
| FR-18 | Automatic retention: session dirs older than 7 days are purged by a scheduled job (or on next app start) | P1 |

### 5.4 Documentation & Transparency

| ID | Requirement | Priority |
|---|---|---|
| FR-19 | Create `docs/PRIVACY.md` with: data collected, recipients (Groq, Google, LangSmith, Resend), retention, deletion process, contact for requests | P0 |
| FR-20 | Link to `PRIVACY.md` from sidebar footer | P0 |
| FR-21 | README section on privacy with links to policy and PRD | P0 |
| FR-22 | Machine-readable `privacy.json` at repo root summarizing data flows (for automated compliance scanners) | P2 |

---

## 6. Non-Functional Requirements

### 6.1 Performance
- **NFR-P1:** Redaction overhead <50ms per LLM call (regex-based, no LLM)
- **NFR-P2:** Session deletion <5s regardless of generated file count
- **NFR-P3:** Consent check on every LLM call has zero user-visible latency

### 6.2 Correctness
- **NFR-C1:** 100% of `LANGCHAIN_TRACING_V2=true` events pass through the redactor
- **NFR-C2:** Redactor has ≥95% recall on PII in a test set of 20 synthetic CVs
- **NFR-C3:** Redactor has 0 false-positives on job titles (e.g., "Product Manager" not flagged as a name)

### 6.3 Usability
- **NFR-U1:** Consent modal dismissible in ≤3 clicks
- **NFR-U2:** Plain-English privacy page readable at 8th-grade level
- **NFR-U3:** Delete button always visible (not buried in settings)

### 6.4 Reliability
- **NFR-R1:** Redactor wrapper fails-closed — if redaction errors, tracing is disabled for that call (fail safe > fail informative)
- **NFR-R2:** Auto-purge job runs daily; failure logged but does not crash app

---

## 7. Technical Design (high-level)

### 7.1 Architecture

```
User input
    ↓
[UI consent state] ──────┐
    ↓                      ↓
agents/llm_client.py    agents/privacy.py
    ↓                      ↑ (redact)
Full PII prompt     Sanitized copy
    ↓                      ↓
Groq/Gemma API   LangSmith trace
(needs context)  (if consented)
```

### 7.2 Key file changes

| File | Change |
|---|---|
| `agents/privacy.py` | NEW — redactors, purge job, consent check helper |
| `agents/llm_client.py` | Wrap trace calls with `privacy.redact_pii()` when consent=true |
| `agents/runtime.py` | Consent-check function; auto-purge on app start |
| `app.py` | First-run consent modal; sidebar delete button; privacy link |
| `docs/PRIVACY.md` | NEW — user-facing privacy policy |
| `tests/test_privacy.py` | NEW — 20 synthetic CV redaction tests |

### 7.3 Redaction approach

Not using an LLM for redaction (cost + latency + ironic "sending PII to redact PII" problem). Instead:

1. **Known-value redaction** — replace exact matches of `candidate_name`, `user_email` (from state)
2. **Pattern-based redaction** — regex for phone numbers, postcodes, common city names
3. **Whitelist approach** — never redact employer names or job titles (too useful for debugging, and less uniquely identifying)

### 7.4 Fallbacks
If the redactor crashes mid-request, `llm_client` catches the exception and **disables tracing for that call only** (fail-closed). Error logged locally but not propagated to the user — degraded observability, not degraded user experience.

---

## 8. Success Metrics

### 8.1 Launch metrics
- **M1:** 0 PII tokens detected in LangSmith traces during 1-week monitoring post-launch
- **M2:** 100% of sessions show a consent decision in logs (no bypasses)
- **M3:** "Delete my data" button used successfully ≥1 time end-to-end in testing
- **M4:** `docs/PRIVACY.md` page views > 5% of app landings

### 8.2 Ongoing metrics
- **M5:** 0 GDPR-related complaints / takedown requests in 90 days post-launch
- **M6:** Redactor false-positive rate <5% on production traces (sampled weekly)

---

## 9. Out of Scope (v1.2+)

- ❌ Right to data portability (GDPR Article 20) — export user data in machine-readable format
- ❌ Data Processing Agreements (DPAs) with Groq, Google, LangSmith, Resend — legal, not product
- ❌ Geographic routing (EU users → EU-region providers) — infra
- ❌ Encrypted storage at rest — infra  
- ❌ Audit log of who accessed session data — requires auth layer (v2.0)
- ❌ Child-safe UI / COPPA compliance
- ❌ Consent cookies persisting across sessions

---

## 10. Dependencies

- **Feature dependency:** v1.0 must be launched and stable before v1.1 work starts
- **External:** none (regex-based redaction is local; LangChain SDK supports tracer wrappers)
- **Legal:** informal review of `docs/PRIVACY.md` by someone with GDPR knowledge (friend-of-friend lawyer if possible)

---

## 11. Risks & Mitigations

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | Redactor misses unusual name formats (e.g., non-Latin, hyphenated) | Medium | Medium | Fall back to token-level match of any uppercase-start word cluster; add test cases from real CVs |
| R2 | Phone regex catches false positives (e.g., years like "2019-2023") | Medium | Low | Phone pattern requires country code or 7+ consecutive digits |
| R3 | Redactor breaks CV context and degrades LLM output quality | Low (only applies to telemetry, not LLM call) | Medium | Redaction happens AFTER LLM call, only before trace |
| R4 | User clicks "Delete my data" but email is already in flight (Resend queue) | Low | Medium | Warn in confirmation dialog: "Applications already sent cannot be recalled" |
| R5 | Auto-purge deletes active user's session mid-batch | Low | Medium | Purge only sessions with `last_modified > 7d` AND `status == "completed"` |
| R6 | LangSmith changes their API; our redaction wrapper breaks silently | Low | High | Unit tests in CI; monitor LangSmith changelog |
| R7 | User expects "Delete my data" to also delete already-sent emails from recruiters | N/A | Expectation mismatch | Explicit disclaimer in UI copy |

---

## 12. Open Questions

1. **Should we redact BEFORE the LLM call too?** No — the LLM needs context (candidate name, email for cover letter salutation). Redacting before LLM breaks the product.
2. **Can we self-host LangSmith?** Yes (Enterprise tier), but cost-prohibitive for portfolio. Keep cloud LangSmith + redact.
3. **Do we redact in the crash-safe snapshot too?** Yes — snapshots are persisted locally but we should assume they could be shared for debugging. Apply same redactor.
4. **How do we handle consent for already-running sessions when user toggles off?** Consent change takes effect for the NEXT LLM call onward; in-flight calls still use the prior consent state. Document clearly in UI.
5. **Should we rate-limit the delete button?** Probably not — it's idempotent and local. But show a toast on success so users don't spam.

---

## 13. Implementation Plan

### Week 1 (post-v1.0 launch)

| Day | Task | Deliverable |
|---|---|---|
| D1 | Write redactor + tests | `agents/privacy.py` with 95%+ recall on test set |
| D2 | Wrap `llm_client.py` tracer; wire consent check | All LangSmith events pass through redactor |
| D3 | Build consent modal + sidebar toggle + delete button | UI shipped in `app.py` |
| D4 | Write `docs/PRIVACY.md`; link from UI footer | Privacy page live |
| D5 | End-to-end test with 3 synthetic users; verify 0 PII in traces | Launch-ready |

### Week 2
- Monitor traces for 1 week; fix any leaks caught
- Write v1.1 retro and scope v1.2

---

## 14. Rollback Plan

If the redactor over-redacts and breaks observability:
1. Temporarily set `LANGCHAIN_TRACING_V2=false` globally via env
2. Roll back the `llm_client.py` wrapper to the v1.0 version
3. Fix redactor offline; ship v1.1.1 patch

If the consent flow crashes the app on first run:
1. Revert `app.py` consent modal
2. Fall back to v1.0's simpler `.env`-based default-off approach
3. Ship UI fix in next release

---

## 15. Sign-off

- [ ] Product owner (Rishav) — approved
- [ ] Tests pass — redactor test suite
- [ ] Manual verification — 3-user end-to-end check
- [ ] `PRIVACY.md` reviewed by GDPR-literate person
- [ ] v1.0 launch stable for ≥2 weeks before starting this work

---

### Change log
- 2026-04-20 — Initial draft, scoped post-v1.0

### References
- GDPR Articles: 5 (principles), 6 (lawful basis), 13 (info to data subject), 17 (erasure), 25 (by design), 30 (records), 44 (transfers)
- `PM_CASE_STUDY.md §7` — risks section flagged GDPR as v2 work
- `HANDOFF_SUMMARY.md §6.3` — privacy listed as medium-priority pending
