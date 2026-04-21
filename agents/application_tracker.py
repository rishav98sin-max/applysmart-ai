# agents/application_tracker.py
"""
Per-user application history.

File-backed JSON store used to remember which jobs the user has already
APPLIED to, so subsequent agent runs can skip them (no re-scoring, no
re-tailoring, no re-emailing).

Design notes
────────────
- Keyed by user email (normalised to lowercase). One JSON file for the whole
  app. Thread-safe via a module-level lock + atomic rename on save.
- Keyed inside each user by a NORMALISED URL (tracking params stripped,
  scheme/host lowercased, trailing slash removed) so the same job reached
  via LinkedIn vs a re-post is still recognised.
- Three application states per job:
    applied = True   → user confirmed they applied → SKIP on future runs
    applied = False  → user explicitly said "not yet" → show again
    applied = None   → user hasn't answered yet     → show again
- `mark_shown()` never downgrades an existing `applied=True` flag — it only
  refreshes `shown_at` and bumps `seen_count`.

Streamlit UI will (in a later phase) call `mark_applied` / `mark_not_applied`
when the user ticks a checkbox next to a match.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# ── Paths ────────────────────────────────────────────────────
_HERE       = os.path.dirname(os.path.abspath(__file__))
HISTORY_DIR = os.path.join(os.path.dirname(_HERE), "data")
HISTORY_FILE = os.environ.get(
    "APPLICATION_HISTORY_FILE",
    os.path.join(HISTORY_DIR, "applications.json"),
)

# ── Concurrency ──────────────────────────────────────────────
_LOCK = threading.Lock()

# ── URL normalisation ────────────────────────────────────────
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "ref", "refId", "source",
    "trk", "trkCampaign", "trackingId", "currentJobId",
    "originalSubdomain",
}


def _normalize_url(url: str) -> str:
    if not url:
        return ""
    try:
        p = urlparse(url.strip())
        qs = [
            (k, v) for k, v in parse_qsl(p.query, keep_blank_values=False)
            if k.lower() not in _TRACKING_PARAMS
        ]
        path = p.path.rstrip("/")
        return urlunparse((
            p.scheme.lower(),
            p.netloc.lower(),
            path,
            "",
            urlencode(qs),
            "",
        ))
    except Exception:
        return url.strip()


def _email_key(email: str) -> str:
    return (email or "").strip().lower()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Load / save ──────────────────────────────────────────────
def _load_all() -> Dict[str, Dict[str, Any]]:
    if not os.path.exists(HISTORY_FILE):
        return {}
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_all(data: Dict[str, Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    tmp = HISTORY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=False)
    os.replace(tmp, HISTORY_FILE)


# ── Public read APIs ─────────────────────────────────────────
def load_history(email: str) -> Dict[str, Dict[str, Any]]:
    """Return the full {normalized_url: record} map for a user (may be empty)."""
    if not email:
        return {}
    with _LOCK:
        data = _load_all()
    return dict(data.get(_email_key(email), {}))


def applied_urls(email: str) -> set:
    """Set of normalised URLs the user has CONFIRMED applied to."""
    hist = load_history(email)
    return {u for u, rec in hist.items() if rec.get("applied") is True}


def is_applied(email: str, url: str) -> bool:
    return _normalize_url(url) in applied_urls(email)


def filter_out_applied(
    email: str,
    jobs: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Split `jobs` into (kept, skipped) where `skipped` are those the user has
    already marked as applied. Safe no-op if email is empty or list empty.
    """
    if not email or not jobs:
        return list(jobs), []
    applied = applied_urls(email)
    if not applied:
        return list(jobs), []
    kept, skipped = [], []
    for j in jobs:
        nu = _normalize_url(j.get("url") or "")
        if nu and nu in applied:
            skipped.append(j)
        else:
            kept.append(j)
    return kept, skipped


# ── Public write APIs ────────────────────────────────────────
def mark_shown(email: str, jobs: Iterable[Dict[str, Any]]) -> int:
    """
    Record that these jobs were SHOWN to the user in a run. Upserts records
    without changing any existing `applied` state. Returns the number of
    records upserted.
    """
    if not email:
        return 0
    jobs_list = [j for j in (jobs or []) if isinstance(j, dict)]
    if not jobs_list:
        return 0
    key = _email_key(email)
    now = _now_iso()
    count = 0
    with _LOCK:
        data = _load_all()
        user = data.setdefault(key, {})
        for j in jobs_list:
            url = _normalize_url(j.get("url") or "")
            if not url:
                continue
            rec = user.get(url) or {}
            rec["url_original"] = j.get("url") or rec.get("url_original", "")
            rec["title"]        = j.get("title",   rec.get("title", ""))
            rec["company"]      = j.get("company", rec.get("company", ""))
            rec["source"]       = j.get("source",  rec.get("source", ""))
            rec["match_score"]  = int(j.get("match_score", rec.get("match_score", 0)) or 0)
            rec["shown_at"]     = now
            rec["seen_count"]   = int(rec.get("seen_count", 0)) + 1
            if "applied" not in rec:
                rec["applied"] = None
            if "first_seen_at" not in rec:
                rec["first_seen_at"] = now
            user[url] = rec
            count += 1
        _save_all(data)
    return count


def mark_applied(email: str, urls: Iterable[str]) -> int:
    """Mark a batch of URLs as applied=True. Returns count updated."""
    return _set_applied(email, urls, True)


def mark_not_applied(email: str, urls: Iterable[str]) -> int:
    """Mark a batch of URLs as applied=False (explicit 'not yet')."""
    return _set_applied(email, urls, False)


def _set_applied(
    email: str,
    urls: Iterable[str],
    applied: bool,
) -> int:
    if not email:
        return 0
    urls_list = [u for u in (urls or []) if u]
    if not urls_list:
        return 0
    key = _email_key(email)
    now = _now_iso()
    count = 0
    with _LOCK:
        data = _load_all()
        user = data.setdefault(key, {})
        for u in urls_list:
            nu = _normalize_url(u)
            if not nu:
                continue
            rec = user.get(nu) or {"url_original": u}
            rec["applied"] = bool(applied)
            if applied:
                rec["applied_at"] = now
            # Leave applied_at untouched on a "not yet" flip (user may change mind later).
            user[nu] = rec
            count += 1
        _save_all(data)
    return count


# ── Convenience: summary for the UI ──────────────────────────
def history_summary(email: str) -> Dict[str, Any]:
    hist = load_history(email)
    applied = [r for r in hist.values() if r.get("applied") is True]
    pending = [r for r in hist.values() if r.get("applied") is None or r.get("applied") is False]
    return {
        "total":      len(hist),
        "applied":    len(applied),
        "pending":    len(pending),
        "records":    hist,
    }
