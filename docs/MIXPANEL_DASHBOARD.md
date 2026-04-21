# ApplySmart AI — Mixpanel Dashboard

Last updated: 2026-04-21

This is the live product-analytics setup: event schema, configured dashboard,
and how to reproduce it from scratch. The code hooks live in
`agents/analytics.py` and call sites across `app.py`, `agents/job_agent.py`,
and `agents/llm_client.py`.

---

## 1. Setup (once per environment)

### 1.1 Create a Mixpanel project

1. [mixpanel.com](https://mixpanel.com) → sign up → new project `applysmart-ai`.
2. Choose **EU data residency** for GDPR alignment (matches `docs/PRIVACY.md`).
   You cannot change region later — pick deliberately.
3. Project Settings → Access Keys → copy the **Project Token**.

### 1.2 Wire the token

Add to `.env` (or `st.secrets` on Streamlit Cloud):

```bash
MIXPANEL_TOKEN=<project_token>
MIXPANEL_REGION=EU   # omit or set US if not EU-residency
```

Restart Streamlit. The app auto-detects the token via
`agents/analytics.analytics_enabled()` and starts emitting events.
If the token is missing, every `track_event()` call silently no-ops
(safe default — never breaks product flow).

### 1.3 Verify end-to-end

```powershell
venv\Scripts\python -c "from dotenv import load_dotenv; load_dotenv(override=True); from agents.analytics import analytics_enabled, _MIXPANEL_TRACK_URL; print('enabled:', analytics_enabled(), '| endpoint:', _MIXPANEL_TRACK_URL)"
```

Expected output:
```
enabled: True | endpoint: https://api-eu.mixpanel.com/track
```

Then fire a probe:

```powershell
venv\Scripts\python -c "from dotenv import load_dotenv; load_dotenv(override=True); from agents.analytics import track_event; track_event('diagnostic_test', 'cli_probe', {'source': 'manual'}); print('sent')"
```

Hard-refresh Mixpanel → **Data → Events** → filter **Today** → you should
see `diagnostic_test` within ~30-90 seconds.

---

## 2. Event schema

All events include a privacy-safe `distinct_id`:

- **Anonymous:** `session_<uuid>` — before the user types their email.
- **Identified:** `user_<sha256(email)[:20]>` — after email is entered.
  Raw email is never sent to Mixpanel.

See `agents/analytics.distinct_id()` for the exact hashing.

### 2.1 Product events (fired from `app.py`)

| Event | Fires when | Key properties |
|---|---|---|
| `session_opened` | First Streamlit page load of a session | `has_trace_consent` |
| `cv_uploaded` | User drops a CV file (once per session) | `file_size_bytes` |
| `run_started` | User clicks **Run agent** | `experience_level`, `num_jobs`, `match_threshold`, `preview_mode`, `source`, `has_cv_upload` |
| `run_completed` | Agent pipeline finishes | `status`, `duration_sec`, `jobs_scraped`, `matched_jobs`, `skipped_jobs`, `best_match_score`, `median_match_score`, `match_threshold`, `matches_above_threshold_count`, `llm_calls_used`, `llm_calls_limit`, `preview_mode` |
| `send_attempted` | User clicks the bulk-send button | `mode`, `jobs_count` |
| `send_completed` | Email delivery finished (success or fail) | `mode`, `attempted_count`, `sent_count`, `failed_count` |
| `cv_downloaded` | User clicks "Download tailored CV" | `company`, `match_score` |
| `cover_letter_downloaded` | User clicks "Download cover letter" | `company`, `match_score` |
| `job_marked_applied` | User ticks the "I applied" checkbox | `company`, `source`, `match_score` |
| `job_unmarked_applied` | User unticks "I applied" | `company`, `source`, `match_score` |
| `privacy_tracing_consent_updated` | User toggles LangSmith consent | `enabled`, `source` |
| `session_data_deleted` | User clicks "Delete my session data" | `had_trace_consent` |
| `theme_changed` | User flips the sidebar theme toggle | `theme` (`"light"` / `"dark"`), `source` |

### 2.2 Infra events (fired from the agent layer)

| Event | Fires from | Purpose |
|---|---|---|
| `llm_rate_limit_hit` | `agents/llm_client._rotate_groq_key()` | Catches Groq 429 / 401 events → tells us when we need more keys or caching |
| `review_retry_triggered` | `agents/job_agent._do_cover_letter()` | Cover-letter review failed → tailor prompt quality signal |

Both infra events use the special distinct_id `"system_infra"` so they
don't pollute user funnels.

---

## 3. Live dashboard

Board name in Mixpanel: **ApplySmart AI Dashboard**.

Five reports (free-plan cap). Every chart answers exactly one PM question.

### 3.1 Outcome funnel — upload to applied ⭐

- **Type:** Funnel (5 steps)
- **Steps:** `cv_uploaded` → `run_started` → `run_completed` → `send_completed` → `job_marked_applied`
- **Window:** 7 days
- **PM question:** Does our tool drive real applications, not just generated PDFs?
- **Caveat:** Apply rate is user-reported via the in-app tracker. True apply
  count is likely 1.5-2× higher. Call this out in case-study write-ups.

### 3.2 Weekly return — session → run

- **Type:** Retention
- **Starting event:** `session_opened`
- **Return event:** `run_started`
- **Period:** Week, 8 periods, 3M range
- **PM question:** Does the product create a weekly habit or just one-off utility?

### 3.3 Match quality — median best score

- **Type:** Insight (line)
- **Metric:** `run_completed` → **Aggregate Property** → `best_match_score` → **Median**
- **Range:** 7D, granularity Day
- **PM question:** Is the matcher producing genuinely useful matches?
- **Healthy benchmark:** Median best-match ≥ 65. Below 55 = matcher is struggling.
- **Bonus:** Duplicate with aggregation = **P90** to see ceiling quality.

### 3.4 Runs per day

- **Type:** Insight (line)
- **Metric:** `run_completed` → **Totals**
- **Range:** 30D, granularity Day
- **PM question:** Is usage growing?
- **Note:** Totals (activity) not Uniques (people) — because a returning user
  running 5× is a stronger growth signal than 5 one-off users.

### 3.5 Runs by experience level

- **Type:** Insight (bar / line)
- **Metric:** `run_started` → **Totals**
- **Breakdown:** property `experience_level`
- **PM question:** Who are our users?

---

## 4. Annotations (ship-date narrative)

Every chart supports ship-date markers. Adding these turns a dashboard from
a data display into a **PM narrative tool**: "match quality stepped up
*here* — that's where we shipped aggressive tailoring."

Current annotations:

| Date | Label | Color |
|---|---|---|
| 2026-04-20 | v1.0 — aggressive CV tailoring + YOE filter | Yellow |
| 2026-04-21 | v1.1 — Groq rotation + no-drop bullets | Green |

Add via: any chart → Annotations tab → **+ Add Annotation**. Annotations
appear as vertical lines across **every** chart on the project.

---

## 5. Free-plan constraints

Mixpanel Free (current tier):

| Resource | Limit | Impact |
|---|---|---|
| Saved reports / project | **5** | Cap is why we picked the 5 above — any additions force trade-offs |
| Events / month | 20M | Unreachable for our volume |
| Data retention | 3 years | Fine |
| Projects / workspace | Multiple | Use a 2nd project for infra-only charts if you need more |
| Report types | Insights, Funnels, Flows, Retention | No SQL, no cohorts |

### Workarounds if we outgrow the 5-report cap

1. **Second project** for infra: dedicate it to `llm_rate_limit_hit` +
   `review_retry_triggered` trends. Use a second Mixpanel token.
2. **Upgrade** to Growth plan (~$25/mo) — unlocks unlimited reports + cohorts.
3. **Screenshot-based case study** — for portfolio, the 10 insights live
   once as images in the write-up, and only the top 5 live in Mixpanel.

---

## 6. Reproducing the dashboard from scratch

If you create a fresh Mixpanel project, seed realistic data first so the
charts render meaningfully during setup:

```powershell
venv\Scripts\python -c "from dotenv import load_dotenv; load_dotenv(override=True); from agents.analytics import track_event; import random; levels = ['Entry/Associate', 'Mid-level', 'Senior', 'Lead/Manager']; users = [f'user_seed_{i:02d}' for i in range(10)]; [track_event('cv_uploaded', u, {'file_size_bytes': 120000}) for u in users]; [track_event('run_started', u, {'experience_level': random.choice(levels), 'num_jobs': 3, 'match_threshold': 60}) for u in users[:9]]; [track_event('run_completed', u, {'status': 'success', 'best_match_score': random.randint(58, 88), 'median_match_score': random.randint(45, 70), 'matches_above_threshold_count': random.randint(1, 4), 'duration_sec': random.randint(60, 180)}) for u in users[:8]]; [track_event('send_completed', u, {'mode': 'bulk', 'attempted_count': 3, 'sent_count': 3, 'failed_count': 0}) for u in users[:6]]; [track_event('job_marked_applied', u, {'company': 'Acme', 'match_score': 75}) for u in users[:4]]; print('seeded')"
```

This produces a healthy funnel shape (100 → 90 → 80 → 60 → 40%) so you can
screenshot the dashboard the same day you build it.

Wait 60-90s for Mixpanel indexing, then follow the schema in §3 to build
each of the 5 reports.

---

## 7. Privacy considerations

All tracking is covered by `docs/PRIVACY.md`. Key properties:

- **No PII** in event properties — emails are hashed, CV text never sent,
  job descriptions never sent.
- **Consent-gated** — user must approve tracing in the first-run modal
  before any event fires. Consent toggle lives in sidebar.
- **Session delete** — `session_data_deleted` event + local cleanup
  wipe all per-session files.
- **Fail-safe** — `track_event()` catches every exception silently;
  analytics failures never break a real agent run.

See `agents/analytics.track_event()` for the safe-props allowlist
(primitive types only; complex objects get `str()`-cast).
