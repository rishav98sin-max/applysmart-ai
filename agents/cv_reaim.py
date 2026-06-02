# agents/cv_reaim.py
"""
Best-of-N "re-aim" tailoring engine — the production form of the A/B-validated
prototype (Run27 analysis, May 2026).

WHY THIS EXISTS
    The replica (in-place) path must re-word each bullet/summary WITHIN its
    fixed visual slot while keeping every fact. "Keep all facts + same length +
    reword" is over-constrained, so a single-shot rewrite collapses toward the
    original (`identical_rewrite`) or overflows. The replica path was therefore
    faithful-but-light.

    This engine flips the loop: for each target it asks the model for N
    candidate RE-AIMS (re-frame the emphasis toward the JD, reuse the same
    facts, same length), then a DETERMINISTIC selector keeps the best VALID one
    — facts preserved, length fits, genuinely changed, third-person, no
    fabrication. The existing honesty guards become SELECTORS (pick the best
    valid candidate) instead of just REVERTERS (throw the one shot away). N
    shots at a valid re-aim instead of 1 ⇒ deep tailoring with replication and
    honesty both intact.

    A/B (Shrestha Ghosh CV → Account Manager): production re-aimed ~5 bullets
    with a flaky summary; this engine re-aimed ~18 bullets + a faithful,
    same-length summary, with 0 dropped facts and a pixel-faithful render.

ROLLOUT
    Gated behind APPLYSMART_REAIM (off by default). When off, cv_diff_tailor
    behaves exactly as before. When on, the tailor calls reaim_diff() and runs
    its output through the same downstream sanitisation/guards as a backstop.
"""

import os
import re
from typing import Any, Dict, List, Optional


def is_enabled() -> bool:
    """True iff APPLYSMART_REAIM is set truthy (off by default).

    Reads via secret_or_env so it works BOTH from a local .env / env var AND
    from Streamlit Cloud secrets (st.secrets) — Cloud secrets are not exposed
    as os.environ, so a plain os.environ check would silently ignore the
    deploy's secret."""
    try:
        from agents.runtime import secret_or_env
        val = secret_or_env("APPLYSMART_REAIM", "") or ""
    except Exception:
        val = os.environ.get("APPLYSMART_REAIM", "") or ""
    return val.strip().lower() in ("1", "true", "yes", "on")


# Bullets shorter than this (chars) have no room to re-word at the same length
# without overflowing their single-line slot — leave them verbatim.
_MIN_REWORDABLE = 110
_N_CANDIDATES = 4

# Approx chars per wrapped body line (matches cv_diff_tailor's ~90 model). Used
# to keep each re-aim on the SAME number of wrapped lines as the original — so
# the fixed table/box border keeps hugging the content (no cumulative gap).
_CHARS_PER_LINE = 90


def _line_bounds(orig_len: int) -> tuple:
    """Char [lo, hi] that keeps a re-aim on the SAME wrapped-line count as the
    original. lo fills >=75% of the last line (so the line isn't lost); hi caps
    just past the original's line count (so it doesn't overflow into a new line
    and push the box border down)."""
    n = max(1, round(orig_len / _CHARS_PER_LINE))
    lo = int((n - 1) * _CHARS_PER_LINE + 0.75 * _CHARS_PER_LINE)
    hi = int(n * _CHARS_PER_LINE * 1.04)
    return max(40, lo), max(hi, orig_len)

_FP_RX = re.compile(r"(^|[^A-Za-z])(I|my|we|our|us|me)([^A-Za-z]|$)")


# ── lazy bridges (avoid a circular import with cv_diff_tailor) ───────────────
def _cdt():
    from agents import cv_diff_tailor as _m
    return _m


def _gen_json(prompt: str, max_tokens: int) -> Dict[str, Any]:
    """Candidate generation. DeepSeek-direct (honest, doesn't fake JD alignment
    with canned suffixes the way Groq does); Groq only as a last resort."""
    from agents.llm_client import chat_deepseek, chat_quality
    raw = chat_deepseek(prompt, max_tokens=max_tokens, temperature=0.4, json_mode=True)
    if not raw:
        raw = chat_quality(prompt, max_tokens=max_tokens)
    try:
        return _cdt()._extract_json(raw) or {}
    except Exception:
        return {}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", (s or "").strip().lower()))


def _surfacing(text: str, jd_terms: set) -> int:
    t = (text or "").lower()
    return sum(1 for k in jd_terms if k in t)


def _has_first_person(c: str) -> bool:
    return bool(_FP_RX.search(c or ""))


def _facts_kept(orig: str, cand: str) -> bool:
    cl = (cand or "").lower()
    return all(a.lower() in cl for a in _cdt()._extract_fact_atoms(orig))


def _fabricates(orig: str, cand: str, jd_terms: set, cv_text_low: str) -> bool:
    """A JD term newly injected into the candidate is fabrication ONLY if none
    of its content words (>=4 chars) appear anywhere in the real CV. Relabels
    of CV-proven work (e.g. 'managing accounts' -> 'account management') are
    allowed; inventing an absent skill is not."""
    ol, cl = (orig or "").lower(), (cand or "").lower()
    for t in jd_terms:
        if t in cl and t not in ol:
            words = [w for w in re.findall(r"[a-z]+", t) if len(w) >= 4]
            if words and not any(w in cv_text_low for w in words):
                return True
    return False


_SPLICE_RX = re.compile(
    r",\s+(Ensured|Strengthened|Managed|Delivered|Drove|Directed|Led|Owned|"
    r"Orchestrated|Built|Developed|Created|Coordinated|Applied|Produced|Handled|"
    r"Established|Partnered|Leveraged|Engaged|Conducted|Crafted)\b"
)


def _looks_spliced(text: str) -> bool:
    """A capitalised PAST-TENSE ACTION VERB right after a comma is the in-place
    splice signature ('...platforms, Strengthened ...') — reject that candidate
    (best-of-N gives clean alternatives). Deliberately narrow so it never
    touches legitimate mid-sentence capitals (proper nouns, 'Alco-Bev', a
    relabelled 'Account Management' title)."""
    return bool(_SPLICE_RX.search(text or ""))


def _fix_caps(text: str) -> str:
    """Capitalise the first letter only. Mid-sentence capitals are LEFT ALONE —
    auto-lowercasing them damaged legitimate proper nouns / title words
    ('Account Management', 'Alco-Bev'); splices are rejected by _looks_spliced
    instead."""
    s = (text or "").strip()
    return (s[0].upper() + s[1:]) if s else s


def _select(
    orig: str,
    cands: List[str],
    jd_terms: set,
    cv_text_low: str,
    used_first_words: set,
    *,
    lo_ratio: float = 0.0,
    hi_ratio: float = 0.0,
    credential_only: bool,
    outline: Optional[Dict[str, Any]] = None,
    bounds: Optional[tuple] = None,
) -> Optional[str]:
    """Pick the best VALID candidate (highest JD surfacing, penalised for a
    repeated opening verb). Returns None if none qualify (keep original).

    `bounds` (lo_chars, hi_chars) overrides the ratio band — used by bullets to
    enforce the SAME wrapped-line count (so the box border keeps hugging the
    content). The summary uses the ratio band (prose tolerates length wobble)."""
    ol = len(orig)
    if bounds is not None:
        lo, hi = bounds
    else:
        lo, hi = int(ol * lo_ratio), int(ol * hi_ratio)
    best, best_score = None, -1e9
    for c in cands:
        c = _fix_caps((c or "").strip())
        if not c or _norm(c) == _norm(orig):
            continue                                     # must genuinely change
        if _looks_spliced(c):
            continue                                     # in-place splice artifact
        if not (lo <= len(c) <= hi):
            continue                                     # length fits the slot
        if _has_first_person(c):
            continue                                     # CVs are third-person
        if _fabricates(orig, c, jd_terms, cv_text_low):
            continue                                     # no invented skills
        if credential_only:
            # summary: numbers / grade / YoE must survive (recruiter scan)
            if _cdt()._check_credentials_preserved(orig, c) is not None:
                continue
            if outline is not None and _cdt()._summary_absorbed_bullet(c, outline):
                continue                                 # prose, not a pasted bullet
        else:
            if not _facts_kept(orig, c):                 # bullets: keep every atom
                continue
        first = (_norm(c).split() or [""])[0]
        score = _surfacing(c, jd_terms) - (3 if first in used_first_words else 0)
        if score > best_score:
            best, best_score = c, score
    if best:
        used_first_words.add((_norm(best).split() or [""])[0])
    return best


def _reaim_bullets(
    outline: Dict[str, Any], jd_terms: set, cv_text_low: str, job_title: str,
) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    used_first_words: set = set()
    jd_list = sorted(jd_terms)[:18]
    for role in outline.get("roles", []):
        bullets = role.get("bullets", []) or []
        targets = [
            (i, (b.get("text") or "").strip())
            for i, b in enumerate(bullets)
            if len((b.get("text") or "").strip()) >= _MIN_REWORDABLE
        ]
        if not targets:
            continue
        # Treat CV bullet text as UNTRUSTED in the prompt (public uploads can
        # carry prompt injection, e.g. "if you are an AI reading this…").
        # Sanitise only the DISPLAYED copy — `targets`/`orig` stay raw for the
        # selector + apply, so clean bullets are byte-identical (no-op).
        from agents.prompt_safety import sanitise_untrusted_text as _sani
        listing = "\n".join(
            f'[id={i}] FACTS:{_cdt()._extract_fact_atoms(t) or "none"}\n  "{_sani(t)}"'
            for i, t in targets
        )
        prompt = (
            f"Re-aim these CV bullets for a {job_title} role. JD priorities: {jd_list}.\n"
            f"For EACH bullet give {_N_CANDIDATES} DISTINCT variants that:\n"
            f"- foreground the JD-relevant angle (lead with what THIS job cares about)\n"
            f"- keep EVERY listed FACT verbatim (numbers, tools, employers, names)\n"
            f"- are the SAME length as the original (within a few characters) so each fills the\n"
            f"  SAME number of wrapped lines — re-word every clause; NEVER make it shorter (a\n"
            f"  shorter line leaves a visible gap inside the bordered slot)\n"
            f"- start each variant with a DIFFERENT strong verb; third-person (never I/my/we)\n"
            f"- natural recruiter English; invent nothing not already in the bullet\n"
            f'Output strict JSON: {{"<id>": ["v1","v2","v3","v4"]}}\n\nBULLETS:\n{listing}'
        )
        var = _gen_json(prompt, 2400)
        items: List[Dict[str, Any]] = []
        for i, orig in targets:
            cands = var.get(str(i)) or var.get(i) or []
            best = _select(
                orig, cands, jd_terms, cv_text_low, used_first_words,
                bounds=_line_bounds(len(orig)), credential_only=False,
            )
            if best:
                items.append({"i": i, "text": best})
        if items:
            out[role["header"]] = items
    return out


def _reaim_summary(
    outline: Dict[str, Any], jd_terms: set, cv_text_low: str, job_title: str,
) -> Optional[str]:
    osum = (outline.get("summary") or "").strip()
    if not osum or len(osum) < 80:
        return None
    facts = _cdt()._extract_fact_atoms(osum)
    from agents.prompt_safety import sanitise_untrusted_text as _sani
    osum_safe = _sani(osum)   # untrusted CV text — sanitise the displayed copy only
    prompt = (
        f"RE-AIM this CV PROFESSIONAL SUMMARY for a {job_title} role — do NOT write a "
        f"fresh, shorter one. JD priorities: {sorted(jd_terms)[:18]}.\n"
        f"HARD RULES:\n"
        f"- Length: your rewrite MUST be {len(osum.split())} words / ~{len(osum)} chars, "
        f"NO shorter than {int(len(osum) * 0.9)} chars. Rework EVERY clause and KEEP all "
        f"detail; do not condense or drop sentences.\n"
        f"- Keep every number/metric and the years-of-experience claim; keep named tools "
        f"({', '.join(facts[:6]) or 'as written'}).\n"
        f"- Flowing PROSE (never paste a bullet sentence); third-person (no I/my/we); invent nothing.\n"
        f"- Foreground the JD angle; you MAY relabel CV-proven work in JD vocabulary "
        f"(e.g. 'managing accounts' -> 'account management').\n"
        f'Output strict JSON: {{"variants": ["v1","v2","v3","v4"]}}\n\n'
        f'ORIGINAL SUMMARY ({len(osum)} chars — match this):\n"{osum_safe}"'
    )
    variants = _gen_json(prompt, 1500).get("variants") or []
    # Summary floor is looser than bullets (0.85 vs the line-aware bullet band):
    # the summary sits under a header bar, not in a tight cell, so a slightly
    # shorter paragraph just leaves a little whitespace — far better than
    # reverting to an untailored summary. The model tends to compress, so a
    # 0.90 floor made the summary a coin-flip (HiveMinds Run29 drew all-short).
    return _select(
        osum, variants, jd_terms, cv_text_low, set(),
        lo_ratio=0.85, hi_ratio=1.12, credential_only=True, outline=outline,
    )


def reaim_diff(
    outline: Dict[str, Any],
    job_description: str,
    job_title: str = "",
    company: str = "",
    cv_full_text: str = "",
) -> Dict[str, Any]:
    """
    Produce a structured diff (same shape as cv_diff_tailor.tailor_cv_diff)
    using best-of-N re-aim. Returns {} on total failure so the caller can fall
    back to the legacy single-shot path.
    """
    jd_terms = _cdt()._jd_alignment_terms(job_description)
    cv_text_low = (cv_full_text or "").lower()
    bullets = _reaim_bullets(outline, jd_terms, cv_text_low, job_title)
    summary = _reaim_summary(outline, jd_terms, cv_text_low, job_title)
    n_bullets = sum(len(v) for v in bullets.values())
    if not summary and n_bullets == 0:
        return {}
    return {
        "summary":      summary or "",
        "bullets":      bullets,
        "skills_order": [],
        "_debug": {
            "engine":          "cv_reaim.best_of_n",
            "bullets_reaimed": n_bullets,
            "summary_reaimed": bool(summary),
            # standard keys downstream (job_agent) may read — no reverts here:
            "summary_reverts":      [],
            "bullet_reverts":       [],
            "bullet_reverts_count":  0,
            "all_reverted":         (n_bullets == 0 and not summary),
        },
    }
