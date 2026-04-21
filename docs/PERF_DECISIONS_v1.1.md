# Performance v1.1 — Decision Log

*For merging into PRD_v1_Launch.md + PM_CASE_STUDY.md before launch.*

## Problem

Live demo (20 Apr 2026) of 3-job tailoring run took ~15–30 min. USP of the product is **automation that saves time**, so wall-clock is a first-class quality metric, not just a perf concern.

## Diagnosis

| Bottleneck | Impact | Cause |
|---|---|---|
| Sequential job processing | ~3x overhead | Each job runs start-to-finish before next starts |
| Cover letter + CV tailor serial within a job | ~1.5x overhead | No dependency between them |
| Reviewer-triggered retries always fire | ~1.5x overhead | Rubric retries even when first draft is borderline-good |
| Gemma free-tier TPM throttling | silent 30–60s stalls | 15000 TPM cap on `chat_quality` |

## Decisions

1. **Parallelize across jobs (2 concurrent).** Stays under Gemma RPM=30. Biggest single win.
2. **Parallelize cover-letter + CV-tailor within a job.** They read same JD but don't depend on each other.
3. **Smart retry gate:** only retry CV tailor if `score<70 AND (bullets_rewritten<3 OR fabrications>0)`. Skips diminishing-returns retries.
4. **Hard cap retries at 1** (down from 2).

## Quality-protection non-decisions (explicitly rejected)

- ❌ Move CV tailor to Groq primary — quality loss on creative bullet rewriting
- ❌ Move cover letter to Groq primary — narrative voice matters
- ❌ Loosen reviewer rubric — that's the quality floor
- ❌ Reduce bullets_rewritten target — tailoring USP depends on it

## Projected outcome (3 jobs, threshold 60)

| Metric | Before | After |
|---|---|---|
| Wall time | 15–30 min | **5–8 min** |
| CV tailor quality (Gemma) | High | Same |
| Cover letter quality (Gemma) | High | Same |
| Reviewer floor | 70 | Same |

## Trade-offs / risks

- Higher burst LLM rate → more Gemma 429s → more Groq fallback. Worst case: 2 jobs drop to Groq temporarily. Still acceptable quality.
- Parallel failures harder to debug — mitigated by per-job logging prefix.

## Launch comms

For portfolio post: *"3 job applications tailored in under 8 minutes — end-to-end, autonomous. Each CV has 6+ bullets rewritten to match the JD, reviewed by an AI reviewer, and shipped as a pixel-perfect PDF."*
