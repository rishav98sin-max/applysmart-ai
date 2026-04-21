# Supported CV Formats

What ApplySmart AI can and cannot do with different CV formats. Read this
before uploading an unusual CV, or before reporting a bug about output
quality.

## TL;DR

| You uploaded a... | What happens |
|---|---|
| Standard text PDF (Word / Docs / LaTeX export) | ✅ Full support: in-place layout edits, bullet reordering, summary rewrite |
| Two-column or designer template (Canva, Novorésumé, etc.) | ⚠ Runs but likely falls back to a rebuild — **visual layout will change** |
| Scanned / photographed CV (image-only PDF) | ✗ Blocked at upload. Please export from a text source |
| Password-protected PDF | ✗ Blocked at upload. Remove the password and re-upload |
| Non-English CV | ⚠ Runs but section detection is English-only; expect degraded output |
| Very short (< 400 chars) | ⚠ Runs but tailor agent has little material to work with |
| Corrupt / malformed PDF | ✗ Blocked at upload |
| PDF with tables for experience | ⚠ Bullets may not be detected; text is still parsed |
| PDF > 25 MB or > 12 pages | ✗ Blocked / ⚠ warned (unusually large) |

## How the pre-flight validator works

Every uploaded CV goes through `agents.cv_validator.validate_cv()` before
the agent runs. The validator produces:

- **Errors** — hard blockers (the app refuses to run the agent)
- **Warnings** — soft flags (the app shows them to the user but allows
  them to proceed at their own risk)
- **Score** — a 0-100 compatibility estimate that feeds into the UI banner
- **Details** — page count, char count, section hits, bullet count,
  ASCII ratio, dominant fonts

The validator is intentionally conservative: it blocks only when we're
very confident the CV cannot be processed. Borderline cases emit
warnings so the user keeps agency.

## Render modes

After the agent runs, each matched job carries a `render_mode` field
that tells you how the CV PDF was produced:

| Mode | Meaning | Visual fidelity |
|---|---|---|
| `in_place` | Original PDF replicated and edited via PyMuPDF | ✅ Matches original exactly |
| `rebuilt` | Full rebuild via ReportLab from tailored text | ⚠ Different layout from original |
| `failed`  | Neither path produced a PDF | ✗ Error — no output |

This is surfaced on each match card as a chip so users know what they're
looking at.

## How to extend the supported-formats list

1. Drop a diverse PDF into `test_cvs/` (create the directory if missing)
2. Run the corpus checker:
   ```
   python scripts\test_cv_corpus.py
   ```
3. A fresh `docs/CV_COMPATIBILITY.md` is written with the pass/warn/block
   result for every file
4. If a format you care about shows `✗ block` unexpectedly, open
   `agents/cv_validator.py` and relax the relevant check

## Recommended test corpus

For a deployment-quality check, assemble at least the following:

- **LaTeX** — `moderncv`, `awesome-cv`, `altacv`
- **Canva / designer 2-column** — pick a popular template
- **Overleaf academic** — long-form with publications section
- **Word ATS template** — the kind corporate recruiters recommend
- **Plain minimalist** — text-only, single column
- **Non-English** — French, German, Spanish; tests the ASCII-ratio warning
- **Scanned** — photo or screenshot of a printed CV; MUST be blocked
- **Password-protected** — export with a password; MUST be blocked
- **Too short** — a 1-line "portfolio page"; should warn

Put all of those in `test_cvs/`, run the script, commit the generated
`docs/CV_COMPATIBILITY.md` to the repo. Redo after every change to
`cv_validator.py` or the PDF editor.
