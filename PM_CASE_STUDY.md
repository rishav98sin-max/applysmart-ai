# ApplySmart AI — PM Case Study

> A product case study, not a build diary.
> What I'm betting on, why, and how I'd know I'm wrong.
>
> **Author:** Rishav Singh
> **Role:** Product Manager (sole operator / PM-engineer on this project)
> **Status:** v1.2 — deployed on Streamlit Community Cloud
> **Last updated:** 27 Apr 2026

> **Companion docs:**
> `docs/CHANGELOG.md` — what changed in each version and why.
> `ROADMAP.md` — forward-looking tactical backlog.
> `HANDOFF_SUMMARY.md` — engineering / architecture handoff.

---

## TL;DR (the one-page version)

|   |   |
|---|---|
| **Who I'm building for** | Experienced job seekers (3+ yrs) who apply to 10+ roles per month and already have a CV they trust. *Deliberately broad starting hypothesis — narrowing plan in §2.* |
| **The job they're hiring the product for** | "Help me submit many tailored, honest, layout-preserving applications without spending 30 minutes per role or sending a CV that lies about skills I don't have." |
| **The core bet** | Trust will outsell polish. A tool that visibly *won't* fabricate on the user's behalf will retain users better than a tool that produces prettier-but-inflated output. |
| **What would prove me wrong** | (a) Users prefer template-rebuild output even when told the original was preserved. (b) Fabrication doesn't actually deter usage — users accept inflated CVs as long as interviews come in. (c) Batch tailoring isn't a job-to-be-done — users tailor each one manually anyway. |
| **What I've shipped** | End-to-end multi-agent pipeline: scrape → match → tailor (in-place PDF) → review → cover letter → email. ~10-job batch in <5 min. Public on Streamlit Cloud. |
| **What I don't yet know** | Whether anyone outside my discovery cohort will use this. Whether trust is a real differentiator or just a founder hobby-horse. Whether anyone will pay. |

---

## 1. The Problem

### 1.1 The behaviour I observed

The discovery cohort, ranked by signal quality:

1. **Me** — applied to ~50 roles in 3 months. The full pain: copy-paste, reformat, write cover letters from scratch, give up by application 8 of the day.
2. **Family member** — entry-level IT support, applied to ~30 roles. Wasted half of them on Senior listings he wasn't qualified for, didn't realise until he started getting silent rejections.
3. **Close friend** — 3 years in marketing, switching to a content/strategy hybrid. Strong CV but for the "wrong" roles. Templates rebuilt his CV "in their own voice" and he abandoned them.
4. **Master's classmates (n≈8)** — same graduating cohort, same job market timing. Most volume-applying, all complaining about the same things on group chats: too generic, too slow, too much copy-paste.
5. **Undergrad classmates (n≈12, looser ties)** — earlier stage of the same arc. Less volume per person, but earlier-career so the template-CV pain was different (didn't have a "voice" yet to preserve).
6. **LinkedIn passive signal** — daily exposure to posts from people in the same job market. The pattern is consistent: complaints about ATS rejections, frustration with AI tools that "don't sound like me," and a steady stream of "applied to 200 jobs and got 2 callbacks" posts.

This is not formal research. It's three layers of overlapping anecdote: lived experience, peer cohort, and ambient market signal. **N is bigger than three but I'm not pretending it's representative.** It's enough to commit to a hypothesis and ship a v1; not enough to commit to a market size.

### 1.2 The non-obvious failure mode

The interesting part wasn't the time cost. It was that **multiple people in the cohort tried at least one existing tool — and abandoned it.** Resume.io, Zety, Rezi, the ChatGPT "rewrite my CV" prompt. The shared reaction across people who'd tried different tools:

> *"It made my CV look like everyone else's."*

They preferred their own CV — even when it was objectively worse-formatted than the template versions — because it was *theirs*. They'd put effort into it. They trusted it. A template-swap, however polished, felt like wearing someone else's clothes to a job interview.

This is the insight the product is built around. The job to be done isn't *"make my CV better."* It's **"sharpen my CV without stealing my voice."**

### 1.3 Why the existing tools don't solve this

| Tool category | What it does | Why it loses the cohort |
|---|---|---|
| **ChatGPT / Claude DIY** | Free-form rewrites | Loses PDF layout, manual copy-paste, no batch, no scraping |
| **Resume.io / Zety / Kickresume** | Template-driven CV builders | Forces a layout swap; the "voice" complaint above |
| **Rezi / Teal** | ATS-optimised builders | Better at ATS but still template-rebuild; limited batching |
| **Jobscan** | Match-score + keyword gap analyser | Diagnostic only; doesn't tailor or apply |
| **Simplify** | Browser extension for one-click apply | Same generic CV everywhere; no tailoring |
| **LinkedIn Easy Apply** | Mass-apply with the same CV | Antithesis of tailoring |
| **VAs / résumé writers** | Human service | $50-300 per CV, slow, doesn't scale to 30 applications/week |

The unmet job is **end-to-end + tailored + layout-preserving + batch + honest**. Each existing tool nails 1-2 of those five attributes. None nails all five.

---

## 2. The Customer Hypothesis

### 2.1 The current target (deliberately broad)

**Experienced professionals (3+ yrs of work) actively job-seeking who apply to 10+ roles in any given month.**

This is broader than my actual evidence supports — I've seen ~20 people in this segment up close, but the segment is much larger. I'm starting broad on purpose:

- **Why broad?** I don't yet know which sub-segment the differentiator ("trust > polish") lands hardest with. Career-switchers, mass-appliers, and senior individual contributors all *might* care about layout preservation. I'd rather under-segment now and let usage data narrow it.
- **The narrowing plan:** As soon as I have 50+ runs from non-discovery-cohort users, segment by (years of experience × applications per month × did they pay if asked) and find the highest-retention slice. That's the ICP.

### 2.2 Sub-segments I'm explicitly not targeting (yet)

- **New graduates** — they typically don't have a "CV they trust"; template-rebuild tools may actually serve them better.
- **Senior executives (10+ yrs)** — applications are network-driven, not volume-driven. The job-to-be-done is different.
- **Lifelong-in-domain professionals** (e.g. a 15-yr cardiologist) — low role-to-role variance; tailoring delivers less marginal value.
- **People applying to 1-2 dream roles** — they tailor manually with full attention; automation isn't the bottleneck.

### 2.3 Why now

Three things converged in the last 18 months:

1. **LLMs got cheap and good at structured tasks.** Tailoring 10 CVs costs ~$0.05-0.20 in tokens, not $50.
2. **Fabrication risk became visible.** "AI hallucinates" is now common vocabulary; users have learned to distrust black-box AI rewrites.
3. **The job market got harder.** Volume-applying as a strategy is now mainstream — there's a real cohort applying to 100+ roles per search rather than 10.

Without (1), the product is too expensive. Without (2), nobody cares about fabrication guardrails. Without (3), the volume use-case isn't real.

---

## 3. The Core Bet

> **Trust will outsell polish.** Users who already have a CV they're proud of will pick a tool that visibly preserves and sharpens their work over a tool that produces prettier-but-fabricated output.

If this bet is right, ApplySmart's three architectural choices (in-place PDF, fabrication guardrails, diff-based tailoring) are core differentiation. If it's wrong, they're over-engineered safety nets that don't matter to users.

### 3.1 Sub-bets and their kill criteria

Each sub-bet is falsifiable. Each has a metric I'd watch and a threshold that would kill it.

| # | Sub-bet | Metric I'd watch | Kill criteria |
|---|---|---|---|
| **B1** | **Layout preservation > template polish.** Users prefer their original layout, even if a template would look "better." | Among users who see both the in-place edit and a rebuild fallback, % who keep the in-place version. | If <60% keep the in-place version, the template-aversion was anecdotal. Pivot to "user-picks-template" mode. |
| **B2** | **Diff > rewrite.** Minimal targeted edits beat full-CV regens because they fabricate less and respect the user's voice. | Reviewer-flagged fabrication rate; user edit rate after preview. | If users edit >40% of the diff output before sending, the diff is too aggressive *or* too conservative — investigate. If fabrication-flag rate is similar to a full rewrite, the bet is dead and I shouldn't pay the diff-engineering complexity tax. |
| **B3** | **Determinism > LLM judgment for hard filters.** Regex YOE / level filters catch out-of-range jobs faster and cheaper than asking an LLM. | Save rate (% of jobs filtered before LLM call) and false-positive rate (filtered jobs that *should* have matched). | Save rate <20% means the deterministic filter isn't earning its complexity. False-positive rate >5% means it's discarding good matches. |
| **B4** | **Honesty is a product, not a feature.** Users will pick a more-honest tool over a more-feature-rich one *if they're shown the difference.* | A/B test (long-term): tailored output with vs without fabrication guardrails, measure retention. | If retention is statistically indistinguishable, honest tailoring is a founder preference, not a market preference. The product still ships, but the marketing message has to change. |

### 3.2 Why these and not others

I'm explicitly *not* betting on:

- **"Better LLM = better product."** Model choice is a swap-out variable; if Gemini-3 ships tomorrow with a 10x quality bump, I want the architecture to absorb it without restructuring.
- **"More job boards = more value."** Marginal coverage drops fast. LinkedIn + Indeed covers ~80% of job listings in the target markets.
- **"Pretty UI = differentiation."** The current UI is Streamlit. It's ugly. It also ships, and the UX-vs-ship trade-off is the right one for a single-operator product at this stage.

---

## 4. What Would Have to Be True for This to Fail

The risks I can name. Each has a plausible scenario and a kill signal.

### 4.1 Product risks

| Risk | Scenario | Kill signal |
|---|---|---|
| **Trust is not the differentiator I think it is** | Users complain about fabrication in surveys but actually pick fast-fabricating tools when given a choice. Stated vs revealed preference. | A/B with vs without guardrails shows no retention lift. |
| **Volume tailoring isn't a job-to-be-done** | Users prefer to apply to 5 carefully-tailored roles than 30 LLM-tailored ones. Quality > quantity is the real preference. | Average jobs-per-batch stays at 2-3 across users; no usage of the bulk-send path. |
| **Layout preservation matters less than I think** | When shown a polished template-rebuild and a preserved in-place edit side-by-side, users pick the rebuild. Aesthetic outranks ownership. | <60% of users keep the in-place version when both are offered. |
| **The cohort doesn't generalise** | The "I don't want my CV templated" reaction is a millennial / Master's-cohort artifact and doesn't replicate in other segments. | First 50 non-cohort users show no template-aversion in their feedback. |

### 4.2 Business / commercial risks

| Risk | Scenario | Kill signal |
|---|---|---|
| **Free tier is the product** | Users use the free tier, never convert. Token cost grows linearly with users; revenue doesn't. | Token cost per active user > willingness-to-pay at any plausible price point. |
| **LLM cost regression** | Token prices drop, but quality demands grow faster. Per-run cost stays at $0.05-0.20 but users expect $0.01. | Margin per paid run goes negative at the price point users will pay. |
| **LinkedIn / Indeed block scrapers** | Scraping is a legal grey zone (the *hiQ Labs v. LinkedIn* precedent permits public-data scraping but LinkedIn keeps fighting). If they harden anti-bot, the multi-board strategy collapses to API-only sources. | LinkedIn block rate exceeds 50% over 30-day window. |
| **Existing tools add the missing features** | Teal or Rezi adds in-place PDF editing + fabrication guardrails. Distribution beats differentiation. | Any major competitor ships in-place PDF editing within 6 months. |

### 4.3 Technical risks (covered, but listed for completeness)

| Risk | Mitigation status |
|---|---|
| LLM rate limits mid-run | ✅ 3-key rotation, dual provider fallback, capped waits |
| PDF font-subset corruption (NBSP) | ✅ Glyph-advance check in `_font_can_render` |
| Fabrication slips past sanitizer | ✅ Reviewer agent + retry; v1.2 added cover-letter post-gen guard |
| Designer / multi-column CVs | ⚠️ Partial — WeasyPrint rebuild fallback ships but doesn't preserve original |
| GDPR / PII leakage via LangSmith | ✅ Consent-gated; redaction in snapshots |
| Multi-tenant auth | ❌ Not built — session-scoped only; blocks any commercial v3 |

---

## 5. Trust as Differentiator (the deeper argument)

Most "honest tailoring" pitches stop at *"our AI doesn't lie."* That's not a product, it's a tagline. Here's the underlying mechanism I'm actually betting on:

### 5.1 Why fabrication is the trust-killer

When a CV is tailored by an LLM, the user faces a choice they didn't have to make before:

- **Pre-LLM:** *Did I represent myself accurately?* (Honest by default, because they wrote every word.)
- **Post-LLM:** *Did the AI represent me accurately?* (Black-box; user has to verify every word.)

The act of verifying *eliminates the time savings the tool was supposed to deliver.* If users have to re-read every bullet to check for fabrication, they might as well have written it themselves.

The fabrication guardrails (sanitizer + reviewer) aren't a feature — they're the **mechanism that makes the time saving real**. If a user can trust the output without verifying every word, the value proposition holds. If they can't, the tool collapses back to ChatGPT-with-extra-steps.

### 5.2 How I'd actually measure trust (the gap)

The current product has *system* metrics for fabrication: sanitizer-flagged rate, reviewer-rejected rate, retry count. Those are mechanism metrics. They don't measure trust.

Real trust metrics I'd want before claiming the bet has landed:

- **% of users who edit the tailored output before sending.** High edit rate = low trust. Target: <20% at the bullet level.
- **Time-from-tailor-to-send.** If users sit on output for 10 minutes, they're verifying. If they send within 60 seconds, they trusted it.
- **Qualitative: "did this CV represent you accurately?" survey post-send.** N=30 with at least 80% saying yes.
- **Long-term: did the user come back?** Trust is a retention story, not a single-session story.

I have **none of these instrumented yet.** This is the biggest gap between the bet I'm claiming and the evidence I can produce. Section 7 expands on this.

---

## 6. Competitive Landscape

| Tool | Tailoring depth | Fabrication handling | Layout preservation | Batching | Price |
|---|---|---|---|---|---|
| **Resume.io / Zety** | Template fields | None (no tailoring) | Forced template | One-at-a-time | $2-7/mo |
| **Kickresume** | Template fields + AI suggestions | Suggestion-only, user-curated | Forced template | One-at-a-time | $4-10/mo |
| **Rezi** | Per-JD bullet rewrite | Light (numeric checks) | Forced template | Limited | $4-29/mo |
| **Teal** | Per-JD bullet rewrite | Light (user reviews each suggestion) | Forced template | Limited | Free / $9/mo |
| **Jobscan** | Diagnostic only (gap analysis) | N/A | N/A | N/A | $20-40/mo |
| **Simplify** | None (one CV everywhere) | N/A | User-uploaded preserved | Mass-apply via extension | Free / $20/mo |
| **ChatGPT DIY** | Open-ended | None | Lost (text only) | None | $0-20/mo |
| **ApplySmart (this)** | Per-JD diff (summary + bullets) | Sanitizer + reviewer + retry | **In-place edit on user's PDF** | Built-in (10-15 jobs/run) | Free (currently) |

### Where I bet differently

- **Layout preservation:** No competitor does in-place PDF editing. They all force a template swap. This is the most architecturally distinct choice.
- **Active fabrication defence:** Most competitors trust the LLM output. I treat the LLM as untrusted and build a sanitizer + reviewer loop around it.
- **Batch as default:** Tailoring 1 CV is a feature; tailoring 10 is a workflow. Competitors are 1-CV-tools.

### Where I'm weaker

- **Polish:** Teal and Rezi look like SaaS products. ApplySmart looks like a Python prototype (because it is one).
- **Job board breadth on premium tiers:** Teal Pro covers job-tracking across 10+ boards with browser extensions. ApplySmart scrapes 5 boards.
- **No paid tier yet.** Pricing is an unsolved question (see §8.4).

---

## 7. Discovery & Validation — What I Know vs Don't

### 7.1 What I've validated (and how strongly)

| Claim | Evidence | Strength |
|---|---|---|
| Mid-career job-seekers reject template-rebuild tools | 3 people in cohort tried, all abandoned within 1 session; matches LinkedIn complaint patterns | **Medium** — anecdotal but consistent across independent users |
| Volume tailoring is painful enough to want automation | Lived experience + classmate group-chat complaints | **Medium-strong** for the cohort; unknown outside it |
| Fabrication is a real problem in current AI tools | Personal spot-checks of ChatGPT output: invented "Kubernetes," "led team of 8" | **Strong** for the failure mode existing; **weak** for whether users care enough to switch tools over it |
| Layout preservation matters | "Don't butcher my CV" — exact quote from one beta user | **Weak** — single direct quote, supported by tool-abandonment behaviour |

### 7.2 What I haven't validated

- **Anyone outside my discovery cohort.** Family + close friends + classmates + LinkedIn-passive-signal is ~20 people. I have not tested with strangers.
- **Willingness to pay.** Zero pricing conversations. No waitlist with payment intent. No teardown of competitor pricing → demand curves.
- **Whether the differentiator works in practice.** Trust is the bet, but I don't have a single trust metric instrumented.
- **The right ICP within "experienced job seekers."** Section 2.1 explicitly admits this is a starting hypothesis to narrow, not a validated segment.

### 7.3 The next 3 validation moves

If I had two free weeks specifically for validation work (not engineering):

1. **Land 30 non-cohort users via LinkedIn / Reddit / IndieHackers post.** Track: did they finish a run? Did they send the output?
2. **Run a 5-question post-run survey** asking specifically about trust ("did this represent you accurately?") and edit-rate ("how much did you change before sending?").
3. **Pricing test:** "If this were $9/month for unlimited runs, would you pay?" — binary answer, then a follow-up on price elasticity.

None of these need engineering work. All of them are blocking proper claims about the product.

---

## 8. Metrics — Predefined vs Reactive (an honest accounting)

A criticism of the v1.1 case study was that some "success metrics" were defined *after* I observed a failure and fixed it. That's bug-fixing dressed as KPIs. This section separates the two honestly.

### 8.1 Predefined metrics (set before shipping)

| Metric | Target | Why this number | Status |
|---|---|---|---|
| End-to-end latency | <5 min for 10 jobs | Time-budget for one coffee break | ✅ Hits target |
| LLM budget per run | <60 calls for 10 jobs | Daily free-tier ceiling math | ✅ Hits target |
| Crash rate | 0 unhandled exceptions in 10 runs | Crash-safe is a v1 requirement, not a stretch goal | ✅ Hits target |

### 8.2 Reactive metrics (defined after a user complaint)

| Metric | Target | Origin | Honest framing |
|---|---|---|---|
| Cross-job CV similarity | <70% | Beta user said "these CVs look 95% identical" | This is a bug-fix target, not a pre-defined KPI. The right pre-shipping question would have been *"how different should two tailored CVs be from the same source?"* I didn't ask it. I should have. |
| YOE early-exit save rate | >25% | Beta user matched against 10-YOE roles despite 3-YOE | Same — defined post-hoc to validate a fix. |
| Match-score / human-judgment agreement | >80% on n=10 spot-checks | Personal sanity check | Self-as-gold-standard. Not real validation. |

### 8.3 Metrics I should have but don't

These are the ones tied to the actual product bets (§3) and I haven't instrumented them:

- **Edit rate** — % of bullets the user changes before sending (proxy for trust)
- **Time-from-preview-to-send** — fast = trusted, slow = verifying
- **Apply-rate after send** — did the user actually apply, or just generate and abandon?
- **Retention** — runs per user per week over a 4-week window
- **NPS / "would you recommend"** — single-question survey post-run

The system instrumentation (Mixpanel funnel, LLM budget counter) is in place. The product instrumentation is not.

### 8.4 Mixpanel funnel — current state

A 5-step funnel is live (`cv_uploaded → run_started → run_completed → send_completed → job_marked_applied`). The current shape shows ~33% end-to-end conversion. **This number is noise, not signal.** N is <30 runs, most of them are my own test runs. Citing it as a real conversion rate would be intellectually dishonest.

What the dashboard *can* honestly tell me: the funnel is correctly tracking events, and the stage-to-stage shape is plausible (most drop-off at `send → applied`, which suggests the apply-tracking step is the friction). I'll quote real conversion rates after 50+ independent runs from non-cohort users.

---

## 9. Roadmap — Forward Bets

> Past versions live in `docs/CHANGELOG.md`. This section is forward-only and bet-organized — what I'd test next, why, and the kill criteria.

### Next bet (v2): Validation, not features

Stop building. Start validating. Two weeks on:

- 30 non-cohort users via outbound (LinkedIn, Reddit, university alumni networks)
- Trust survey instrumented (edit rate + post-send "did this represent you?")
- Pricing-intent test (waitlist with $5 deposit option)

**Kill criteria for the whole product:**
- <40% of users finish a run end-to-end
- <30% of finished runs result in a sent application
- 0 willingness-to-pay signal

If any of those triggers fire, the product hypothesis is wrong and I'd archive the project as a portfolio piece, not iterate further.

### Conditional v2.x (only if validation lands)

- Designer / multi-column CV in-place editing (current rebuild fallback is ATS-safe but not pixel-faithful)
- Per-user vector DB scoping + auth (prerequisite for any multi-user state)
- Outcome tracking — did the user get a reply? Did they get an interview?

### Conditional v3 (only if there's commercial signal)

- Multi-tenant auth (currently session-scoped)
- Pricing experiment (free tier + $X/month paid tier)
- Chrome extension for one-click apply
- Resume-linked LinkedIn profile scraping

### Anti-roadmap (what I'm explicitly not doing)

- ❌ **Full CV regeneration** — fabrication risk. This is a religious "no."
- ❌ **DOCX input** — PDF-only keeps scope manageable.
- ❌ **Auto-inferring level from CV dates** — fails for career-switchers.
- ❌ **ATS vendor integrations** — too fragmented; no leverage.
- ❌ **Video CV / Loom CV tailoring** — out of scope.

---

## 10. What This Taught Me About PM Work

1. **The differentiator is the mechanism, not the message.** "We don't fabricate" is a tagline. The sanitizer + reviewer loop is the *thing that makes the tagline true.* Without the mechanism, the message is just marketing.
2. **Anti-roadmap is harder than roadmap.** Saying "we won't do DOCX" is more useful than saying "we'll do YOE filtering." Clarity on what's out of scope is a PM deliverable.
3. **Predefined metrics > reactive metrics.** A KPI set before shipping is a hypothesis. A KPI set after shipping to validate a fix is a rationalization. Both are useful; only one counts as evidence.
4. **N=20 anecdotes is bigger than n=3 personas, but smaller than a market.** Naming the gap honestly is itself credibility.
5. **Trust is the actual product.** Everything in the engineering layer (sanitizer, reviewer, in-place PDF, layout preservation) is in service of trust, not in service of "AI features." Once I saw it that way, every architectural choice became falsifiable.

---

## 11. Self-Assessment

If a PM panel reviewed this case study tomorrow, what would they say?

**Strengths:**
- ✅ Clear customer hypothesis with explicit narrowing plan
- ✅ Falsifiable bets with kill criteria (B1-B4)
- ✅ Honest about validation gaps (§7.2 names what I haven't done)
- ✅ Honest about reactive vs predefined metrics (§8.2)
- ✅ Real anti-roadmap, not a wishlist

**Gaps the panel will spot immediately:**
- ⚠️ N=20 is still pre-validation. The product hasn't been tested with a single stranger.
- ⚠️ Zero pricing exploration. Even a back-of-napkin unit-economics sketch is missing.
- ⚠️ Trust metrics (the actual product bet) aren't instrumented yet.
- ⚠️ Competitive landscape is shallow — needs a real teardown of Teal and Rezi specifically (their pricing pages, their fabrication policy, their user reviews).
- ⚠️ Self as gold-standard for match-score validation. This is a portfolio-acceptable shortcut, not a real PM methodology.

**What I'd do with two more weeks (and one more PM):**
1. Land 30 non-cohort users; instrument the trust metrics.
2. Build a competitive teardown doc (Teal, Rezi, Kickresume) with screenshots and pricing analysis.
3. Run the willingness-to-pay test.
4. Write a v2 brief that's gated on (1)-(3) — i.e., I won't ship more features until I have validation data.

That's the honest version of the case study. The build is solid. The bets are sharp. The validation is incomplete and I'd rather say so than fake it.

---

*Document type: product case study. Build details and ADRs live in `docs/CHANGELOG.md` and `HANDOFF_SUMMARY.md`. Tactical roadmap in `ROADMAP.md`. Treat any inconsistency between this doc and code as a bug in the doc.*
