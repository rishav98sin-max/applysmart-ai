"""
CV parse regression gate (Phase A).

Locks ground-truth structure for our real CVs and scores any parser against
it, so no future change can silently regress one CV while fixing another.

Ground truth (user-confirmed 2026-06-01): projects ARE separate entries.
    cormac=7 roles, mohammed=3, rishav=5, shrestha=3 — all have a summary.

Each role is identified by a DISTINCTIVE keyword that appears only in that
role's header. Scoring uses longest-match 1:1 assignment (so "New Business
Development for GENESIS BCW" binds to "New Business Development", not the
shorter "Genesis BCW").

Metrics per CV, for BOTH parsers (heuristic + LLM reader):
    recall    = matched GT roles / total GT roles      (did we find them all?)
    precision = matched GT roles / parser roles         (any spurious roles?)
    summary   = parser captured a summary == GT expects it

A change REGRESSES if, for the LLM reader on any CV, recall or precision drops
below the saved baseline, or summary flips from ok to not-ok.

Usage:
    venv\\Scripts\\python.exe scripts\\cv_parse_gate.py            # score + compare to baseline
    venv\\Scripts\\python.exe scripts\\cv_parse_gate.py --update   # save current LLM scores as the baseline
"""
from __future__ import annotations
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass
os.environ.setdefault("REPLICA_LLM_READER", "1")
os.environ.setdefault("CV_READER_SAMPLES", "3")  # consensus / best-of-N in the reader

import agents.pdf_editor as pe                          # type: ignore
from agents.cv_structure_reader import read_outline_llm   # type: ignore

BASELINE = ROOT / "scripts" / "cv_parse_gate_baseline.json"

GROUND_TRUTH = {
    "cormac": {
        "cv": r"D:\Projects\job-application-agent\CVs\Run25\Cormac Holleran CV 2026.pdf",
        "roles": [
            "Business Support Analyst", "Transfer Agency Associate",
            "Finance Administrator", "HR Project Team Member",
            "General Manager", "Collections Analyst",
            "Credit Card Collections Agent",
        ],
        "summary": True,
    },
    "mohammed": {
        "cv": r"D:\Projects\job-application-agent\CVs\Run26\Resume.pdf",
        "roles": ["Electricity Supply Board", "Advanced Data Analyst", "Intern Data Analyst"],
        "summary": True,
    },
    "rishav": {
        "cv": r"D:\Projects\job-application-agent\CVs\Orignal Base CV\RishavSingh_ProductManagerCV.pdf",
        "roles": ["ApplySmart", "VoC Insight Hub", "Prithvi", "IBM", "Accenture"],
        "summary": True,
    },
    "shrestha": {
        "cv": r"D:\Projects\job-application-agent\CVs\Orignal Base CV\Shrestha Ghosh_CV.pdf",
        "roles": ["Ogilvy", "Genesis BCW", "New Business Development"],
        "summary": True,
    },
}


def _headers(outline) -> list:
    return [
        (r.get("header") or "").strip()
        for r in (outline.get("roles") or [])
        if (r.get("bullets") or [])
    ]


def _assign(gt_roles: list, headers: list) -> tuple:
    """Longest-match 1:1 assignment of GT keys to parser headers.
    Returns (matched_keys, missing_keys, spurious_header_idxs)."""
    hdr_low = [h.lower() for h in headers]
    used_hdr: set = set()
    matched: list = []
    # For each header, find the LONGEST GT key it contains (most specific).
    # Greedy over headers sorted so this is stable.
    pairs = []  # (key_len, key, hdr_idx)
    for hi, h in enumerate(hdr_low):
        for key in gt_roles:
            if key.lower() in h:
                pairs.append((len(key), key, hi))
    pairs.sort(reverse=True)  # longest key first
    used_key: set = set()
    for _, key, hi in pairs:
        if key in used_key or hi in used_hdr:
            continue
        used_key.add(key)
        used_hdr.add(hi)
        matched.append(key)
    missing = [k for k in gt_roles if k not in used_key]
    spurious = [headers[i] for i in range(len(headers)) if i not in used_hdr]
    return matched, missing, spurious


def _all_bullets(outline) -> list:
    out = []
    for r in (outline.get("roles") or []):
        for b in (r.get("bullets") or []):
            t = (b.get("text") or "").strip()
            if t:
                out.append(t)
    return out


def _norm_b(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").lower()).strip()


def _bullet_coverage(heur, llm) -> tuple:
    """Edit-floor: fraction of the heuristic's SUBSTANTIAL bullets that survive
    in the LLM output (are we tailoring at least as much as today?). Matches on
    a 40-char prefix either direction. Returns (coverage, missing_bullets)."""
    lb = [_norm_b(b) for b in _all_bullets(llm)]
    hb = [_norm_b(b) for b in _all_bullets(heur) if len(_norm_b(b)) > 15]
    def found(h):
        h40 = h[:40]
        return any((h40 in l) or (l[:40] in h) for l in lb)
    missing = [h for h in hb if not found(h)]
    cov = (len(hb) - len(missing)) / len(hb) if hb else 1.0
    return round(cov, 3), missing


def _score(outline, gt) -> dict:
    headers = _headers(outline)
    matched, missing, spurious = _assign(gt["roles"], headers)
    n_gt = len(gt["roles"])
    recall = len(matched) / n_gt if n_gt else 0.0
    precision = len(matched) / len(headers) if headers else 0.0
    summary_ok = bool((outline.get("summary") or "").strip()) == gt["summary"]
    bullets = _all_bullets(outline)
    # SAFETY INVARIANT: a company/title is a HEADER, never a tailorable bullet.
    # Flag any bullet that BEGINS with a GT company/title keyword (a bullet
    # merely mentioning a client mid-sentence is fine and must be preserved).
    leak = [b for b in bullets
            if any(b.lower().startswith(k.lower()) for k in gt["roles"])]
    return {
        "n_roles": len(headers), "n_gt": n_gt, "n_bullets": len(bullets),
        "recall": round(recall, 3), "precision": round(precision, 3),
        "summary_ok": summary_ok,
        "missing": missing, "spurious": spurious,
        "header_leak": leak,
    }


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    update = "--update" in sys.argv

    llm_scores = {}
    print("=" * 92)
    print(f"  {'CV':9} {'parser':10} {'roles':>6} {'recall':>7} {'prec':>6} {'summary':>8}  notes")
    print("=" * 92)
    for who, gt in GROUND_TRUTH.items():
        cv = gt["cv"]
        if not os.path.exists(cv):
            print(f"  {who:9} -- CV missing: {cv}")
            continue
        heur = llm = None
        try:
            heur = pe.build_outline(cv)
            hs = _score(heur, gt)
        except Exception as e:
            hs = None
            print(f"  {who:9} {'heuristic':10}  CRASH {type(e).__name__}: {e}")
        try:
            llm = read_outline_llm(cv)
            ls = _score(llm, gt) if llm else None
        except Exception as e:
            ls = None
            print(f"  {who:9} {'llm':10}  CRASH {type(e).__name__}: {e}")

        if hs:
            note = ("missing=" + ",".join(hs["missing"])) if hs["missing"] else "ALL ✓"
            print(f"  {who:9} {'heuristic':10} {hs['n_roles']:>4}/{hs['n_gt']:<1} "
                  f"{hs['recall']:>7} {hs['precision']:>6} {str(hs['summary_ok']):>8}  {note}")
        if ls:
            if heur is not None and llm is not None:
                bcov, bmiss = _bullet_coverage(heur, llm)
                ls["bullet_cov"] = bcov
                ls["bullet_missing"] = bmiss
            note = ("missing=" + ",".join(ls["missing"])) if ls["missing"] else "ALL ✓"
            if ls["spurious"]:
                note += " | spurious=" + str(len(ls["spurious"]))
            if ls["header_leak"]:
                note += f" | ⚠HEADER-LEAK={len(ls['header_leak'])}"
            bc = ls.get("bullet_cov")
            print(f"  {who:9} {'llm':10} {ls['n_roles']:>4}/{ls['n_gt']:<1} "
                  f"{ls['recall']:>7} {ls['precision']:>6} {str(ls['summary_ok']):>8}  "
                  f"bcov={bc if bc is not None else '-'}  {note}")
            for m in (ls.get("bullet_missing") or [])[:3]:
                print(f"             ↳ floor-gap (heuristic bullet dropped): {m[:68]}")
            llm_scores[who] = ls
        else:
            print(f"  {who:9} {'llm':10}  None (reader declined)")
        print("-" * 92)

    # ── Header-protection safety invariant (ABSOLUTE, not vs baseline) ──
    # A company/title parsed as a tailorable bullet = corrupting the
    # candidate's identity on a public CV. Hard fail no matter what.
    safety = [f"{who}: {ls['header_leak'][:1]}"
              for who, ls in llm_scores.items() if ls.get("header_leak")]
    if safety:
        print("\n  ⚠️  HEADER-PROTECTION VIOLATION — company/title as a tailorable bullet:")
        for s in safety:
            print(f"       - {s}")
    else:
        print("\n  🛡️  header-protection OK — no company/title in any tailorable bullet.")

    # ── Baseline compare / update ──────────────────────────────────────
    if update or not BASELINE.exists():
        BASELINE.write_text(json.dumps(llm_scores, indent=2), encoding="utf-8")
        print(f"  ✅ baseline {'updated' if update else 'created'}: {BASELINE.name} "
              f"({len(llm_scores)} CVs)")
        return 1 if safety else 0

    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    regressed = list(safety)  # header leaks are regressions too
    for who, ls in llm_scores.items():
        b = base.get(who)
        if not b:
            continue
        if ls["recall"] < b["recall"]:
            regressed.append(f"{who}: recall {b['recall']}→{ls['recall']}")
        if ls["precision"] < b["precision"]:
            regressed.append(f"{who}: precision {b['precision']}→{ls['precision']}")
        if b["summary_ok"] and not ls["summary_ok"]:
            regressed.append(f"{who}: summary ok→FAIL")
        if ls.get("bullet_cov", 1.0) < b.get("bullet_cov", 0.0):
            regressed.append(f"{who}: bullet-coverage {b.get('bullet_cov')}→{ls.get('bullet_cov')}")
    for who in base:
        if who not in llm_scores:
            regressed.append(f"{who}: LLM reader now returns None")

    print()
    if regressed:
        print("  ❌ FAIL:")
        for r in regressed:
            print(f"       - {r}")
        return 1
    print("  ✅ PASS — no regression, header-protection intact.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
