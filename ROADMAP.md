# ApplySmart AI — Roadmap

*Product owner: Rishav Singh · Last updated: 3 June 2026 (v1.5 — robustness milestone)*

> **Companion docs:**
> `PM_CASE_STUDY.md` — customer hypothesis, product bets, falsifiable tests.
> `docs/CHANGELOG.md` — version-by-version build log (what shipped in v1.0 → v1.4.2).
> `HANDOFF_SUMMARY.md` — engineering / architecture handoff.

This doc captures the path from "solo prototype that works on my laptop" to
"multi-user product other people can trust with their CV and job history".

It's deliberately **honest about gaps**: the current build is a functional
prototype deployed to Streamlit Cloud, not a production-grade service. The
items below are what separate the two, grouped by how soon they'd need to
land.

> **Note on prioritisation:** the v2 product hypothesis (in `PM_CASE_STUDY.md` §9)
> is *"validate before you build."* The P0 items below should be read as the
> minimum compliance/safety floor before letting non-cohort users in — not as
> features to ship before validation. If validation fails, none of the P1/P2
> items happen.

---

## Where we are today (3 June 2026, v1.5)

**Deployed on Streamlit Community Cloud** and fully usable end-to-end by a
real user:
- Upload a CV (PDF **or** native `.docx`) → scrape across live job boards
  → score against each JD → tailor the CV summary + bullets + cover letter
  → render to PDF → preview → email.
- **Three terminal output paths**, picked per CV: (A) **DOCX in-place** —
  native `.docx` edited at paragraph/run level *including table cells*,
  rendered by LibreOffice headless; (B) **PDF in-place replica** — PyMuPDF
  coordinate-level edits preserving the original layout/fonts/colours; (C)
  **structured rebuild** — for designer/colour-block/multi-column CVs that
  can't be edited in place, an ATS-clean rebuild (canonical section names,
  placeholder scrub, Typst → WeasyPrint → ReportLab renderers). Designer
  and multi-column CVs that earlier versions *rejected* are now handled by
  path C.
- **LLM-primary structure parsing (v1.5, default on):** the in-place paths
  are driven by an LLM reader (best-of-N consensus, layout-enriched
  prompt, line-index-grounded output) rather than bbox/font heuristics
  alone. The heuristic parser is the fallback. This is what makes the
  in-place path generalise to unseen layouts instead of breaking per
  template.
- Multi-LLM architecture: **DeepSeek V4-Flash** is the primary writing LLM
  (CV strategy, bullet tailoring, cover letters); **Groq** (Llama 3.3 70B)
  handles fast structured tasks (matcher, planner, reviewers, supervisor,
  the structure reader) and is the automatic writing fallback. Gemini 2.5
  Flash is wired but bypassed by default (`GEMINI_BYPASS=1`). Up to 8 Groq
  keys are used **proactive round-robin** with per-key TPM/TPD cooldown
  parsed from rate-limit headers — a different key per call, so bursty
  traffic doesn't cascade one key into "all keys exhausted".
- **Job boards (v1.5): four live** — LinkedIn, Indeed, Jobs.ie, Builtin.
  **Board escalation** (default on): when a board comes up short of the
  match target, the agent automatically widens the search across the other
  live boards (titles-per-board, then escalate), paying the extra cost
  *only* while under-matched. Glassdoor was dropped (Cloudflare/captcha
  challenge to server-side scrapers).
- **Tailoring yield:** the strategist targets every bullet that genuinely
  needs a rewrite (no volume cap); a `lead_with` guard plus an
  `identical_rewrite` retry stop cosmetic near-copies.
- **Fabrication defence in depth:** prompt-level bans + deterministic
  post-generation guards. The **in-place path reverts** any rewrite that
  drops a fact/number or breaks grammar, in real time. The **rebuild path**
  runs a credential/sector/JD-leak gate (cross-checked against the real
  CV) and retries once with a hardened prohibition on any confirmed
  fabrication. Both run in pure Python, so they protect the Groq fallback
  path too. Prompt-injection fences sanitise CV + JD text at every LLM
  surface.
- **Canonical CV section order** enforced by the renderers — Header →
  Summary → Experience → Education → Skills → Other — regardless of what
  the LLM emits.
- **Live Mixpanel dashboard** (5-step outcome funnel, retention, match
  quality, runs-per-day, experience-level breakdown) with a refresh-proof
  anonymous id stored in `?aid=<uuid>`.
- **Deployment-wide daily usage counter** backed by a file-based cache so
  all users and tabs see the same "runs left today" value.
- Crash-safe session snapshots, capped rate-limit waits, hard LLM budget
  per run, consent-gated LangSmith tracing, pre-flight CV validator,
  optional diagnostics trace capture (`DIAGNOSTICS_ENABLED=1`).

**What exists for repeat usage:**
- `application_tracker.py` keyed on **user email** remembers which job URLs
  the user has already applied to; next run skips those.

**What does not exist yet:**
- Authentication of any kind (anyone can type any email into the UI).
- Per-user isolation of CV embeddings (vector DB is keyed on CV content
  hash, not on user identity).
- Data retention policy (session PDFs stay on disk forever).
- Feedback loop / learning (no memory of what cover-letter style worked).
- GDPR subject-request endpoints (no export, no delete).

---

## Now — P0 (before any multi-user deploy)

These are **non-negotiable** before a second human uses the service. They
aren't feature work; they're the compliance and safety floor.

### 1. Real authentication

**Problem:** The app asks for email as plaintext input. Typing
`someone.else@gmail.com` shows that person's applied-URLs history.

**Proposed:** Magic-link email verification (Resend already wired in).
OAuth via Google is a fast-follow. No passwords.

**Why P0:** Impersonation is trivial today. One bad demo kills trust.

---

### 2. Per-user vector DB scoping

**Problem:** `agents/cv_embeddings.py` names collections
`cv_<sha256[:16]>` — content-hashed, not user-scoped. Two users with the
same template CV share a collection. An attacker who knows a target's CV
text can compute the hash and retrieve their embeddings.

**Proposed:** Collection name becomes `user_<user_id>_cv_<cv_id>`.
Retrieval is gated by `user_id == session.user_id`.

**Why P0:** Data leak risk, low probability today but irreversible if it
happens. Fix is ~30 lines in `cv_embeddings.py`.

---

### 3. Session data retention

**Problem:** `sessions/<uuid>/` directories accumulate forever. CVs and
tailored PDFs live on the server indefinitely. That's a GDPR
data-minimisation violation in the EU.

**Proposed:** Nightly cron deletes sessions older than 7 days. User-
configurable in `.env` (`SESSION_TTL_DAYS=7`).

**Why P0:** EU users can file subject-access requests. Without retention,
every prior user's PDFs are technically in scope of the next DPA audit.

---

### 4. GDPR subject-request endpoints

**Problem:** No way for a user to export or delete their data.

**Proposed:**
- `POST /api/export` → zip of session PDFs + application history + CV
  embeddings metadata. Emailed to the authenticated user.
- `POST /api/delete` → wipes session data + history row + vector
  collection. Confirmation email, 7-day cancel window.

**Why P0:** Legal requirement. Two endpoints, ~100 lines total.

---

## Next — P1 (first 30 days post-launch)

The service works for multiple users; now harden it.

### 5. Encrypt stored PDFs at rest

**Problem:** Session PDFs on disk are plaintext. Anyone with host access
(including cloud-provider staff) can read them.

**Proposed:** Fernet symmetric encryption keyed on a per-user key,
transparent to the agent pipeline (wrap `open()` calls in
`session_io.py`).

---

### 6. Migrate `applications.json` to Postgres

**Problem:** A single JSON file holds every user's application history,
loaded and rewritten on every request. Breaks past ~500 concurrent users
(file lock contention) and any crash mid-write corrupts it.

**Proposed:** `applications` table in Postgres with row-level security
(`WHERE user_id = current_user`). Migration script preserves existing
data.

---

### 7. Opt-in LangSmith tracing

**Problem:** `LANGCHAIN_TRACING_V2=true` is unconditional. Every LLM
prompt (which includes the full CV text) goes to LangSmith Cloud. Users
never consented to that data leaving the infrastructure.

**Proposed:** Per-user setting. Default off. On-toggle shows a clear
disclosure: *"Your prompts and CV content will be sent to LangSmith for
debugging. Turn off any time."*

---

### 8. Status-reporting taxonomy

**Problem:** When Groq hits a daily token ceiling, the UI shows
"LLM budget exhausted" — which implies *our app* was too chatty, not
that *Groq's free tier* is capped. Users confuse the two.

**Proposed:** Split `final_state.status` into distinct values:
- `budget_exceeded` — our per-run LLM-call cap hit
- `rate_limited_minute` — Groq RPM/TPM, retry in seconds
- `rate_limited_day` — Groq RPD/TPD, retry tomorrow

Error banner text changes accordingly. ~10 lines in
`runtime.py` + `job_agent.py`.

---

## Later — P2 (real product features)

These are the ones that turn a one-off tool into a **sticky product**.

### 9. Outcome tracking + feedback loop

**Problem:** Today's product has state (applied URLs) but no learning.
Same cover-letter style whether you got interviews or rejections.

**Proposed:**
- New column in applications table: `outcome` (`interview`, `rejected`,
  `ghosted`, `offer`, `withdrawn`).
- Email the user 14 days after each application: *"Any reply from
  {company} yet?"* with one-click outcome buttons.
- Matcher + tailor read `outcome` history of past applications and
  adjust: roles similar to interview-winners score higher, cover-letter
  style shifts toward what produced replies.

**Why P2:** This is the feature that makes users come back in month two.
It's also the hardest (needs a ML layer, not just prompts), so it waits
until the P0/P1 foundation is solid.

---

### 10. User preference memory

**Problem:** If a user rejects a match 3 times in a row (e.g. "not
interested in blockchain"), the agent keeps surfacing similar roles.

**Proposed:** A "Not interested" button on each match card. Adds the
role's keywords to a user-scoped stop-list that the matcher subtracts
from future scores.

---

### 11. Multi-CV support

**Problem:** One user, one CV. Most senior candidates target 2–3 role
families (PM, PgM, founding engineer) with different CV variants.

**Proposed:** User can upload multiple CVs, label them, and pick which
one the agent uses for a given run.

---

### 12. Job-board coverage — PARTIALLY DONE (v1.5)

**Shipped:** four live boards (LinkedIn, Indeed, Jobs.ie, Builtin) plus
**board escalation** — when one board comes up short of the match target,
the agent automatically widens across the others (bounded, and only while
under-matched). Glassdoor was dropped (Cloudflare challenge).

**Still open:** specialist markets (Wellfound/AngelList for startups,
Lever/Greenhouse-backed company career pages, Workable for SMBs) and
non-Irish regional boards. Each new scraper plugs into the existing
`SOURCE_MAP` + escalation order; the `_DEAD_BOARDS` set already lets a
known-broken board be parked without code deletion.

---

## Tailoring quality — polish backlog (post v1.4.2)

Batches 10-15 took bullet rewrites from cosmetic reorders → grammatical
→ JD-vocabulary-integrated → preservation-first. Items 13-16 below all
**shipped in v1.4.3**.

### 13. Summary credential-drop retry — ✅ DONE (v1.4.3)

A one-shot retry now restores a dropped credential before falling back
to reverting the whole summary.

### 14. Keyword-quality on the reframe — ✅ DONE (v1.4.3)

A deterministic post-check blanks long role-phrase `jd_keyword` values
("0-to-1 product ownership") so they cannot bolt onto the verb
redundantly; the strategist is also biased toward tight-noun keywords.

### 15. Tailor output variance — ✅ DONE (v1.4.3, partial)

Tailor temperature lowered 0.2 → 0.1. Run-twice-keep-better and a
stronger tailor model remain available if variance is still material
after real-world runs — measure first.

### 16. Matcher false-misses — ✅ DONE (v1.4.3)

The matcher flagged skills "missing" that the CV had under a word-order
variant ("Automation workflows" vs CV "workflow automation").
`_drop_false_misses` now recovers them deterministically; the matcher
prompt also infers domain from named clients/employers.

### 17. Bloated-bullet handling — OPEN

A very long run-on bullet (ApplySmart i=0, ~400 chars) the LLM wants to
tighten keeps getting rejected by the same-length floor. Needs a
strategist signal that a bloated bullet may be tightened below the
floor. Low priority — affects 1 bullet on 1 CV.

### 18. Optional "extra context" input — OPEN

A UI box where the user can paste current project facts (live URLs,
updated counts) so the tailor can match a careful hand-tailor on facts
not in the base CV. See discussion in PM notes.

### 19. Near-copy bullets — yield, not guard — OPEN

Run 24 Optum reverted 9 of 16 bullets. Batch 20 fixed the *blunt* half
(the concrete-term guard now accepts acronym expansions). The other
half — 6× `identical_rewrite` reverts — is NOT a guard fault: the
tailor genuinely produced near-copies (a comma moved, an article
dropped). The guard correctly demotes those to "keep original".

So this is a **draft-quality / yield** problem, not a guard problem.
On a weak-variance run the tailor returns mostly near-copies and yield
collapses to thin, cosmetic tailoring. The `identical_rewrite` retry
already exists but is clearly not lifting enough drafts.

**Proposed (pick one, measure first):**
- Run-twice-keep-better on the whole diff — generate two tailor passes,
  keep the one with more genuine (guard-passing) rewrites.
- A stronger second-pass model only for bullets that came back as
  near-copies, instead of re-prompting the same model.
- Tighter retry directive: show the LLM its near-copy next to the
  original and demand a structural change (verb swap, clause reorder),
  not a reword.

**Why not bundled with batch 20:** changing draft generation risks
re-opening fabrication/word-salad failure modes that batches 10–16
closed. Needs its own run + review cycle, not a same-day change.

---

## Reliability backlog

### R1. Quota counter survives redeploy — OPEN

The deployment-wide "runs left today" counter lives in
`outputs/.quota_cache.json` on Streamlit Cloud's **ephemeral**
filesystem. It survives normal process restarts but a redeploy (every
git push) or a cold wake from inactivity sleep wipes it — the counter
then jumps back to full and over-reports available Groq quota until the
header cross-check resyncs (which needs all 8 keys touched first).

**Proposed:** move the counter off the ephemeral file — a tiny external
KV store (e.g. Upstash Redis free tier), or lean fully on Groq's
`x-ratelimit-remaining-tokens` response headers as the source of truth.

**Why not P0:** not launch-blocking for a prototype; the dial is a
soft guide, not a hard gate. Matters once multi-user traffic makes the
reset visibly wrong mid-day.

### R2. Job-board scraper resilience — PARTIALLY ADDRESSED (v1.5)

`python-jobspy` API drift historically took down several boards. A
v1.5 scraper-health audit found only LinkedIn + Indeed returning jobs;
Jobs.ie and Builtin were repaired (Jobs.ie: 2026 site rewrite needed a
new URL pattern + `data-testid` selectors; Builtin: the old jobspy
"google" proxy returned nothing and was replaced with a real
builtin.com scraper). Glassdoor stays dead (Cloudflare). The board layer
now degrades gracefully — `_DEAD_BOARDS` parks a known-broken board, and
`live_boards_for()` keeps the escalation order to working boards only, so
one flaky board no longer brakes the pipeline.

**Still open:** the custom HTML scrapers (LinkedIn, Jobs.ie, Builtin) are
selector-coupled and will drift when those sites redesign. A scheduled
scraper-health canary (the `live_boards_for` probe run on a cron) would
surface a dead board before users hit it. `python-jobspy` is still
unpinned for the Indeed path.

### R3. Diff-tailor retry re-sends the whole prompt — OPEN

`cv_diff_tailor` retries 2–3× per job (identical-rewrite retry,
length-fix retry, restructure-or-omit retry). Each retry re-sends the
*entire* ~15K-token prompt — full CV outline + compressed JD +
strategist plan + forbidden-terms list — even though only a handful of
bullets were rejected.

Measured from the Run 22 / 23 Langfuse exports: DeepSeek burns ~61K
tokens per application, of which `cv_diff_tailor` is ~46K (~76%). A
clean single-pass tailor would be ~30K/application — so the retry
re-send is roughly 2× overhead. Cost today is trivial (~1–2¢ per
application), so this is an efficiency item, not a cost emergency.

**Proposed:** on a retry, send only the rejected bullets + their
drafts + the fit-to-slot directive — not the whole CV+JD prompt again.
Estimated saving ~20–30K tokens/application. DeepSeek context caching
already softens the repeat-input cost somewhat; a delta retry would
make it explicit and provider-independent.

**Why not P0:** at ~1–2¢/application the spend is immaterial for a
prototype. Worth doing if volume grows or if the writing LLM moves to a
pricier tier.

---

## Things deliberately **not** on the roadmap

Worth noting because an interviewer may ask:

- **Auto-apply** (submit applications to the ATS directly). Possible,
  but the guardrail cost is high: one bad application with fabricated
  details damages a real user's reputation. Keeping
  preview-before-send is a trust boundary I don't want to cross until
  the fabrication reviewer is proven over thousands of applications.
- **Training a custom model.** Overkill. Groq + good prompting +
  retrieval is cheaper and quality is already acceptable.
- **Mobile app.** Web works on mobile. Native doesn't add product
  value today.

---

## Principles I'm optimising for

When prioritising inside a tier, I break ties on:

1. **User trust** beats feature count. I'd rather ship 3 solid features
   than 10 half-broken ones that shake confidence.
2. **Reversibility.** Privacy and data-handling mistakes can't be
   un-done. Those are P0. Missing features can always be added next
   sprint.
3. **Shipping speed on the free tier.** If a feature requires paid
   infrastructure, it waits until there's a reason (paying users) to
   spend the money.
