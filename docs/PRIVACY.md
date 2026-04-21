# ApplySmart AI — Privacy Notice (v1)

Last updated: 2026-04-20

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
  - Uploads, generated files, and run snapshots.
- **LLM providers** (required for generation):
  - Groq
  - Google (Gemma/Gemini, when configured)
- **Email provider** (when sending outputs):
  - Resend
- **Optional observability tracing**:
  - LangSmith (only if you explicitly enable tracing in this session)

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

