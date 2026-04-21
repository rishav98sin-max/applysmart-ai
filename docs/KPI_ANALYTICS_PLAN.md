# ApplySmart AI — KPI & Analytics Plan

Last updated: 2026-04-21

> **Status: ✅ Implemented (v1.1, Apr 21 2026)**
>
> This plan is live. Event instrumentation ships in `agents/analytics.py`
> and is wired from `app.py`, `agents/job_agent.py`, and
> `agents/llm_client.py`. The live Mixpanel dashboard + event schema +
> reproduction steps are documented in
> [`MIXPANEL_DASHBOARD.md`](./MIXPANEL_DASHBOARD.md).
>
> The five live reports on the board (free-plan cap) are:
> 1. Outcome funnel — upload → applied (north-star funnel)
> 2. Weekly return — session → run (retention)
> 3. Match quality — median best score
> 4. Runs per day
> 5. Runs by experience level
>
> This doc below is retained as the **KPI definitions reference** — the
> source of truth for what each metric means in case-study writeups.

This doc defines what to track, where to see it, and which free tools to use.

---

## 1) KPI Stack (What To Track)

### A. Acquisition / Usage

- **Runs Started**: count of `run_started` events.
- **Runs Completed**: count of `run_completed` events.
- **Run Completion Rate**: `runs_completed / runs_started`.
- **Weekly Active Users (WAU)**: unique users running at least once/week.

### B. Pipeline Quality

- **Jobs Scraped Per Run**: distribution of jobs found.
- **Match Yield**: `matched_jobs / jobs_scraped`.
- **Reviewer Average Score**: average tailored CV reviewer score.
- **Retry Rate**: `% matched jobs requiring reviewer-triggered retry`.

### C. Efficiency / Cost

- **LLM Calls Per Run**: from budget snapshot.
- **Early-Exit Save Rate**: `% jobs skipped before matcher LLM`.
- **Time To Results**: run start -> run complete duration.
- **Send Success Rate**: successful sends / attempted sends.

### D. User Outcome

- **Preview-to-Send Conversion**: runs with preview -> at least one send.
- **Jobs Marked Applied Rate**: `jobs_marked_applied / matched_jobs`.
- **Application Throughput**: applications sent per active user per week.

---

## 2) KPI Definitions For Portfolio

Use these in your case study and dashboards:

- **Agentic Efficiency Index**  
  `(jobs_matched + jobs_filtered_early) / llm_calls_used`  
  Shows decision quality per unit model spend.

- **Automation Value Score**  
  `applications_sent / total_user_clicks_after_run`  
  Approximates workflow automation depth.

- **Quality Confidence Score**  
  Weighted average of reviewer score, fabrication flags, and retry outcomes.

- **Output Fidelity Ratio**  
  `% tailored CVs rendered in preferred in-place mode vs fallback rebuild`.

---

## 3) Where You Can See KPIs

### In-App (already feasible now)

- Extend Insight tab to show:
  - Calls used / budget
  - Early exits
  - Match yield
  - Reviewer score distribution
  - Send success/failure counts

### From Snapshots (already available)

- Parse `sessions/*/snapshot.json` daily and aggregate:
  - completion rate, median latency, call count, send rate.

### In External Analytics Tool (recommended)

- Real-time funnel and retention dashboards.
- Cohort views by source board, experience level, and job title family.

---

## 4) Free Tool Options (Best Fit)

## Recommended: PostHog (Free Tier)

- **Why:** Generous free tier, event tracking + funnels + retention + dashboards.
- **Good for:** Product analytics and PM portfolio screenshots.
- **Free scope:** Enough for early portfolio product usage.
- **How to integrate:** server-side event capture from Streamlit using HTTP API.

## Also viable: Mixpanel (Free Tier)

- **Why:** Strong funnels/cohorts and PM-friendly UI.
- **Good for:** Clean event analysis and conversion tracking.
- **Watch-outs:** Free limits are lower than PostHog for some workloads.

## Minimal option: Plausible (self-host) / Umami

- **Why:** Lightweight privacy-friendly analytics.
- **Good for:** Traffic-level analytics; weaker for deep event funnels.

---

## 5) Event Taxonomy (Implement This)

Core events:

- `run_started`
  - props: `session_id`, `job_title`, `location`, `source`, `experience_level`, `num_jobs`, `match_threshold`

- `run_completed`
  - props: `status`, `jobs_scraped`, `matched_jobs`, `skipped_jobs`, `duration_sec`, `llm_calls_used`, `llm_calls_limit`

- `job_filtered_early`
  - props: `reason` (`level_gap`/`yoe_gap`), `job_title`, `company`

- `tailor_reviewed`
  - props: `job_key`, `review_score`, `verdict`, `retry_count`, `fabrication_flags`

- `docs_generated`
  - props: `job_key`, `render_mode`, `has_cv_pdf`, `has_cover_letter_pdf`

- `send_attempted`
  - props: `mode` (`bulk`/`single`), `jobs_count`

- `send_completed`
  - props: `sent_count`, `failed_count`

- `job_marked_applied`
  - props: `job_key`, `source`

---

## 6) First Dashboard (Build This First)

Dashboard: **ApplySmart PM Core**

Tiles:

1. Runs Started (7d)
2. Run Completion Rate (7d)
3. Median Time To Results
4. LLM Calls Per Run (median, p90)
5. Early-Exit Save Rate
6. Match Yield
7. Reviewer Avg Score
8. Preview-to-Send Conversion
9. Applied Rate (jobs marked applied / matched)

Funnels:

- `run_started -> run_completed -> send_completed`
- `run_completed -> job_marked_applied`

Breakdowns:

- by `source`
- by `experience_level`
- by `status`

---

## 7) Implementation Notes

- Start with one analytics provider (PostHog recommended).
- Emit events only at major milestones (avoid noisy per-line logs).
- Include a stable `user_id` when available (email hash if needed), else session id.
- Keep PII minimal in analytics properties (avoid full CV/job text).

---

## 8) Portfolio-Ready KPI Targets (v1)

- Run completion rate: **>= 85%**
- Early-exit save rate: **>= 25%**
- Match yield: **>= 20%** (depends on threshold/source)
- Reviewer average score: **>= 75**
- Preview-to-send conversion: **>= 40%**
- Applied rate on matched jobs: **>= 25%**

