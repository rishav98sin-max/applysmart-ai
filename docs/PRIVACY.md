# ApplySmart AI — Privacy Notice (v1)

Last updated: 2026-06-11

This page explains what data is processed by ApplySmart AI and how privacy controls work.

---

## What data is processed

When you run the app, it may process:

- CV content (from uploaded PDF)
- Name and email you enter in the UI
- Job preferences (title, location, board, threshold, experience level)
- Generated outputs (tailored CV PDFs, cover letters)
- Runtime metadata (status, counts, errors, budgets)

---

## Where data goes

- **Local machine/session folder** (`sessions/<session_id>/...`)
  - Uploads, generated files, and run snapshots. Deleted within 24 hours,
    or immediately via the "Delete my session data" button.
- **LLM providers** (required to generate your tailored output): see the
  detailed section below.
- **Email provider** (only when you choose to send outputs):
  - Resend
- **Optional observability tracing**:
  - LangSmith (only if you explicitly enable tracing in this session;
    PII is masked before any trace leaves the process).

---

## Your CV and the LLM providers (read this)

To tailor your CV and write cover letters, the **text of your CV is sent to
a third-party LLM provider's API** to generate the output. This is inherent
to how LLM tailoring works — the model has to read your CV to rewrite it.
We're transparent about exactly where it goes:

| Provider | Used for | Trains on your data? | Retention |
|---|---|---|---|
| **Groq** (Llama-3.3-70B) | structured tasks (matching, planning, reviewers); writing fallback | **No** — does not train on API inputs/outputs; zero retention by default | None by default |
| **DeepSeek** (V4-Flash) | currently the primary *writing* model (CV tailoring, cover letters) | **Yes — DeepSeek's API terms permit using inputs to improve/train models**; data stored on servers in China | ~30 days+ |
| **Google** (Gemini) | wired but **bypassed by default** (`GEMINI_BYPASS=1`) | per Google's API terms | — |

**What ApplySmart itself does NOT do:** we never sell your CV, and we never
train any ApplySmart-owned model on it. Your local copy is deleted within
24 hours.

**Honesty note (June 2026):** because the current primary *writing* model is
DeepSeek — whose API terms permit training on inputs — we do **not** claim
"your CV is never training data" on our marketing surfaces. We are migrating
all CV-content writing to providers that contractually do **not** train on
inputs (e.g. Groq's zero-retention API, or OpenAI/Anthropic commercial APIs,
which do not train on API data by default). Once that migration lands, this
notice and our claims will be updated to reflect it. Track this in
`docs/PRODUCT_DECISIONS.md`.

---

## Tracing consent (default-off)

- Tracing is **disabled by default**.
- On first session run, you can choose:
  - Allow anonymized tracing
  - Disable all tracing
- You can toggle tracing at any time in the sidebar.

---

## Redaction & snapshots

- Run snapshots are stored locally and use generic redaction for common email/phone patterns.
- Snapshot files are intended for debugging and are not uploaded automatically.

---

## Delete my data

- Use **"Delete my session data"** in the sidebar.
- This removes the current session folder (`sessions/<session_id>/`) including:
  - uploaded CV
  - generated files
  - snapshots
- Note: emails already sent cannot be recalled.

---

## GDPR status (portfolio release)

This project currently implements a practical GDPR baseline for demo use:

- Consent-gated tracing (default-off)
- Session deletion control
- Basic PII redaction in persisted snapshots

Planned hardening is documented in `docs/PRD_Privacy_Layer.md`.

