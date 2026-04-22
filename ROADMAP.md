# ApplySmart AI — Roadmap

*Product owner: Rishav Singh · Last updated: 22 Apr 2026 (v1.2)*

This doc captures the path from "solo prototype that works on my laptop" to
"multi-user product other people can trust with their CV and job history".

It's deliberately **honest about gaps**: the current build is a functional
prototype deployed to Streamlit Cloud, not a production-grade service. The
items below are what separate the two, grouped by how soon they'd need to
land.

---

## Where we are today (22 Apr 2026, v1.2)

**Deployed on Streamlit Community Cloud** and fully usable end-to-end by a
real user:
- Upload a CV → scrape LinkedIn / Indeed / Glassdoor / Builtin / Jobs.ie →
  score against each JD → tailor the CV summary + bullets + cover letter →
  render to PDF → preview → email.
- Dual-LLM architecture: **Groq** (Llama 3.3 70B) for structured tasks,
  **Gemini 2.5 Flash** for writing. Both support up to 3 rotating API keys
  and cross-provider fallback.
- **PDF rendering pipeline:** PyMuPDF in-place edits first; **WeasyPrint**
  HTML/CSS rebuild as ATS-safe fallback; ReportLab as a last-resort safety
  net on hosts without WeasyPrint's native deps.
- **Fabrication defence in depth:** prompt-level bans + post-generation
  guards for both the summary and the cover letter. The guards run in
  pure Python so they protect the Groq fallback path too.
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
  prompt-injection fences.

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

### 12. Job-board coverage

**Problem:** Three boards (LinkedIn/Indeed/Glassdoor) miss specialist
markets (Wellfound/AngelList for startups, LeverAdmin for specific
company careers pages, Workable for SMBs).

**Proposed:** Add 3 more board scrapers, ranked by user region.
Roll-out guarded behind a feature flag so one flaky scraper doesn't
brick the pipeline.

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
