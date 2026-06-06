# agents/llm_client.py

import os
import time
import random
import json
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from dotenv import load_dotenv

load_dotenv(override=True)

try:
    from groq import Groq
except Exception:
    Groq = None

try:
    import requests as _requests  # for DeepSeek HTTP calls (no extra SDK)
except Exception:
    _requests = None


# ── Groq key rotation pool ──────────────────────────────────────────────────
# Load up to 7 keys from env. On rate-limit, rotate to the next key.
# Multiply daily budget: 8 keys × 100K = 800K tokens/day.
_GROQ_KEYS: list = []
_GROQ_KEY_INDEX: int = 0
_GROQ_CLIENTS: dict = {}  # key → Groq client instance

# ── Last-successful-LLM-source tracking ─────────────────────────────────────
# Set by _call_groq / _call_deepseek on every successful return so callers
# can log "which model produced this kept output". Values:
#   "GROQ key#1"   / "GROQ key#2" ... "GROQ key#8"     → live Groq call
#   "DEEPSEEK (deepseek-chat)"                          → live DeepSeek call
#   "unknown"                                          → no successful call yet
# Read via last_llm_source(). Module-global rather than per-call return value
# so we don't have to refactor every caller's signature.
_LAST_LLM_SOURCE: str = "unknown"

# Per-key quota snapshot captured from Groq's x-ratelimit-* response headers
# after every successful call. Kept mostly for the reset_tokens timestamp —
# the primary "remaining" display is driven by file-based quota cache below.
_GROQ_QUOTA: dict = {}

# Per-key cooldowns for Groq (analog of _GEMINI_KEY_COOLDOWN_UNTIL).
# Populated from `x-ratelimit-reset-tokens` / "Please try again in Xs" on 429.
# Honoured by `_next_available_groq_index()` so proactive round-robin skips a
# key that just blew its per-minute TPM (~12K tok/min on free tier) while the
# other 7 keys continue serving real work. Reads/writes are dict-atomic under
# the GIL — no additional lock needed.
_GROQ_KEY_COOLDOWN_UNTIL: Dict[int, float] = {}

# File-based quota cache for deployment-wide token tracking.
# Stored in the OUTPUT_DIR (writable on Streamlit Cloud) so it survives
# process restarts within the same deployment. A new redeploy wipes it,
# which is fine — we then rely on Groq response headers to resync.
def _quota_file_path() -> Path:
    try:
        from agents.runtime import OUTPUT_DIR as _RUNTIME_OUT
        base = Path(_RUNTIME_OUT)
    except Exception:
        base = Path(__file__).parent.parent / "outputs"
    base.mkdir(parents=True, exist_ok=True)
    return base / ".quota_cache.json"

_QUOTA_LOG_ERRORS = os.getenv("APPLYSMART_DEBUG_QUOTA", "0") == "1"

def _get_tokens_used_session() -> dict:
    """Get tokens used from file cache. Tracks Groq (free-tier quota) and
    DeepSeek (paid out-of-band — observability only)."""
    try:
        qf = _quota_file_path()
        if qf.exists():
            data = json.loads(qf.read_text())
            # Reset when the day rolls over.
            today = __import__('datetime').datetime.now().date().isoformat()
            if data.get("date") != today:
                return {"groq": 0, "deepseek": 0}
            return {
                "groq": int(data.get("groq_tokens", 0)),
                # Back-compat: prior versions stored DeepSeek usage under the
                # 'gemini_tokens' bucket because Gemini was the legacy quality
                # provider. Read both keys so a deploy mid-rollover doesn't
                # lose observability.
                "deepseek": int(data.get("deepseek_tokens",
                                          data.get("gemini_tokens", 0))),
            }
    except Exception as e:
        if _QUOTA_LOG_ERRORS:
            print(f"   Quota read failed: {e}")
    return {"groq": 0, "deepseek": 0}

def _set_tokens_used_session(groq: int, deepseek: int = 0) -> None:
    """Set tokens used in file cache for Groq + DeepSeek."""
    try:
        today = __import__('datetime').datetime.now().date().isoformat()
        data = {"date": today,
                "groq_tokens": int(groq),
                "deepseek_tokens": int(deepseek)}
        _quota_file_path().write_text(json.dumps(data))
    except Exception as e:
        if _QUOTA_LOG_ERRORS:
            print(f"   Quota write failed: {e}")

def _increment_groq_tokens(delta: int) -> None:
    """Increment Groq tokens used in file cache."""
    try:
        current = _get_tokens_used_session()
        _set_tokens_used_session(current["groq"] + delta, current["deepseek"])
    except Exception:
        pass

# Cumulative tokens consumed across ALL keys since process start (or since
# the last quota-reset detection). Incremented from `resp.usage.total_tokens`
# after every successful call. Drives the sidebar "tokens used / runs left"
# display. On Streamlit Cloud the process persists across user sessions, so
# this is effectively a shared deployment-wide counter until the daily
# Groq quota rolls over (at which point we detect the reset and zero it).
# Uses file-based storage for persistence across process restarts.

# Groq free-tier daily budget per API key. 100K tokens/day matches the
# free-tier cap on llama-3.3-70b-versatile. Override via env if you're on
# a paid tier with a higher per-key budget.
_GROQ_TOKENS_PER_KEY_PER_DAY: int = int(
    os.getenv("GROQ_TOKENS_PER_KEY_PER_DAY", "100000")
)

GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

# ── DeepSeek configuration ──────────────────────────────────────────────────
# DeepSeek V4 (released Apr 24, 2026) offers stronger instruction-following
# than Llama 3.3 70B at ~$0.28 / 1M output tokens — effectively free for
# our per-job tailor footprint (~7K in + ~1K out ≈ $0.001 per call). Direct
# DeepSeek API at api.deepseek.com (~3-10s/call, full JSON mode, full SLA).
# Used for the writing path (cv_diff_tailor, cv_reaim, cover_letter,
# strategist). The NVIDIA NIM provider option was removed Jun 2026
# (LLM_PROVIDER=nvidia was never set in prod; the branch was unreachable).
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_BASE_URL = os.getenv(
    "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
).rstrip("/")


def _load_groq_keys() -> list:
    """Load all available Groq keys from env at startup."""
    from agents.runtime import secret_or_env
    keys = []
    for var in ("GROQ_API_KEY", "GROQ_API_KEY_2", "GROQ_API_KEY_3", "GROQ_API_KEY_4", "GROQ_API_KEY_5", "GROQ_API_KEY_6", "GROQ_API_KEY_7", "GROQ_API_KEY_8"):
        k = secret_or_env(var)
        if k and k.startswith("gsk_") and k not in keys:
            keys.append(k)
    return keys


def _groq_client(key: str = None):
    """Return a Groq client for the given key (or the current rotation key)."""
    global _GROQ_KEYS, _GROQ_KEY_INDEX, _GROQ_CLIENTS
    if not _GROQ_KEYS:
        _GROQ_KEYS = _load_groq_keys()
    if not _GROQ_KEYS:
        raise RuntimeError("No valid GROQ_API_KEY found in environment")
    if key is None:
        key = _GROQ_KEYS[_GROQ_KEY_INDEX % len(_GROQ_KEYS)]
    if key not in _GROQ_CLIENTS:
        _GROQ_CLIENTS[key] = Groq(api_key=key)
    return _GROQ_CLIENTS[key]


def _parse_groq_reset_seconds(reset_str) -> float:
    """Parse Groq's reset header values like '210ms', '2.5s', '1m30s', '7.66s'.
    Returns seconds as float. Defaults to 1.0 on parse failure (short enough
    to retry quickly, long enough not to thrash)."""
    if reset_str is None:
        return 1.0
    s = str(reset_str).strip().lower()
    if not s:
        return 1.0
    # Plain numeric → treat as seconds.
    try:
        return max(0.0, float(s))
    except Exception:
        pass
    import re as _re
    total = 0.0
    for val, unit in _re.findall(r"([\d.]+)\s*(ms|s|m|h)", s):
        try:
            v = float(val)
        except Exception:
            continue
        if unit == "ms":
            total += v / 1000.0
        elif unit == "s":
            total += v
        elif unit == "m":
            total += v * 60.0
        elif unit == "h":
            total += v * 3600.0
    return total if total > 0 else 1.0


def _parse_groq_retry_after(exc: Exception) -> float:
    """Extract retry-after seconds from a Groq 429 exception. Looks at
    response headers first (retry-after / x-ratelimit-reset-tokens), then
    falls back to scraping the error message ('Please try again in X.Xs').
    Returns 1.0 if no signal found — never blocks indefinitely."""
    try:
        resp = getattr(exc, "response", None)
        if resp is not None:
            hdr = getattr(resp, "headers", None)
            if hdr is not None and hasattr(hdr, "get"):
                for h in (
                    "retry-after",
                    "x-ratelimit-reset-tokens",
                    "x-ratelimit-reset-requests",
                ):
                    v = hdr.get(h)
                    if v:
                        return _parse_groq_reset_seconds(v)
    except Exception:
        pass
    import re as _re
    s = str(exc)
    # Groq's TPD message looks like "Please try again in 37m18.123s.". Capture
    # the WHOLE compound span so `_parse_groq_reset_seconds` can sum every
    # unit, not just the first one (matters when daily quota hits and the
    # delay is minutes+seconds, not just seconds).
    m = _re.search(
        r"try again in\s+((?:[\d.]+\s*(?:ms|h|m|s)\s*)+)",
        s, _re.IGNORECASE,
    )
    if m:
        return _parse_groq_reset_seconds(m.group(1))
    return 1.0


def _mark_groq_key_cooldown(key_index: int, retry_delay_s: float) -> None:
    """Mark a Groq key as cooling until now+retry_delay_s. Picked up by
    `_next_available_groq_index()` so subsequent calls skip this key.
    Distinguishes TPM (per-minute, <90s) from TPD (per-day, minutes/hours)
    so logs don't mislead the operator into thinking a daily-quota hit is
    just a transient minute burst."""
    deadline = time.time() + max(0.1, retry_delay_s)
    _GROQ_KEY_COOLDOWN_UNTIL[key_index] = deadline
    window = "TPM" if retry_delay_s < 90.0 else "TPD"
    print(
        f"   ⏱  Groq key #{key_index + 1} cooling for "
        f"{retry_delay_s:.2f}s ({window} reset)"
    )


def _next_available_groq_index(advance: bool = True) -> Tuple[int, float]:
    """Pick the next non-cooling Groq key, round-robin.
    Returns (key_index, wait_seconds).
    - wait_seconds == 0 → key is ready right now.
    - wait_seconds > 0  → all keys are cooling; this is the earliest-ready
      one, and caller should sleep wait_seconds before using it.
    Returns (0, 0) when no keys are configured (caller will then raise)."""
    global _GROQ_KEY_INDEX, _GROQ_KEYS
    if not _GROQ_KEYS:
        _GROQ_KEYS = _load_groq_keys()
    n = len(_GROQ_KEYS)
    if n == 0:
        return 0, 0.0
    now = time.time()
    # Advance from current index — this is the "proactive round-robin": each
    # call lands on a DIFFERENT key, spreading load across the pool instead
    # of pinning one key until it explodes.
    start = (_GROQ_KEY_INDEX + (1 if advance else 0)) % n
    for offset in range(n):
        idx = (start + offset) % n
        cd = _GROQ_KEY_COOLDOWN_UNTIL.get(idx, 0.0)
        if cd <= now:
            return idx, 0.0
    # All keys cooling — pick the soonest-recovering one.
    earliest_idx = min(
        range(n),
        key=lambda i: _GROQ_KEY_COOLDOWN_UNTIL.get(i, 0.0),
    )
    wait_s = max(0.0, _GROQ_KEY_COOLDOWN_UNTIL[earliest_idx] - now)
    return earliest_idx, wait_s


def _rotate_groq_key() -> bool:
    """Rotate to the next Groq key. Returns False if no more keys available."""
    global _GROQ_KEY_INDEX, _GROQ_KEYS
    # Infra-health telemetry — never block real work on analytics failures.
    try:
        from agents.analytics import track_event
        track_event(
            "llm_rate_limit_hit",
            "system_infra",
            {
                "exhausted_key_index": _GROQ_KEY_INDEX + 1,
                "total_keys_configured": len(_GROQ_KEYS),
            },
        )
    except Exception:
        pass
    if not _GROQ_KEYS:
        _GROQ_KEYS = _load_groq_keys()
    _GROQ_KEY_INDEX += 1
    if _GROQ_KEY_INDEX < len(_GROQ_KEYS):
        next_key = _GROQ_KEYS[_GROQ_KEY_INDEX % len(_GROQ_KEYS)]
        print(f"   🔄 Groq key rotated → key #{_GROQ_KEY_INDEX + 1} of {len(_GROQ_KEYS)}")
        return True
    print(f"   ⚠️  All {len(_GROQ_KEYS)} Groq key(s) exhausted — waiting for quota reset")
    return False


def _is_rate_limit_error(exc: Exception) -> bool:
    s = str(exc).lower()
    return any(x in s for x in ["429", "too many requests", "resource_exhausted", "quota", "rate limit"])


def _is_auth_error(exc: Exception) -> bool:
    """401 / invalid API key — rotate to next key instead of failing."""
    s = str(exc).lower()
    return any(x in s for x in ["401", "invalid api key", "invalid_api_key", "authentication"])


def _sleep_with_jitter(seconds: float):
    jitter = random.uniform(0, min(2.5, seconds * 0.2))
    time.sleep(seconds + jitter)


def _parse_int(val) -> Optional[int]:
    try:
        return int(str(val).strip())
    except Exception:
        return None


def _capture_quota_from_headers(headers, key_index: int) -> None:
    """
    Read Groq's `x-ratelimit-*` response headers and cache them against the
    active key index. Runs after every successful call. All failures are
    swallowed — quota tracking is observability, never mission-critical.

    Also detects the daily Groq quota reset: if the remaining-tokens value
    jumps UP sharply between two consecutive calls on the same key (e.g.
    5K → 95K), the daily window rolled over and we zero the deployment-wide
    counter so the UI reflects the fresh 300K pool.
    """
    try:
        if not headers:
            return
        new_rem = _parse_int(headers.get("x-ratelimit-remaining-tokens"))

        # Daily-reset detection — fires when the new remaining jumps above
        # the previous snapshot by more than half the per-key daily cap.
        # That's only possible if Groq refilled the bucket between calls.
        prev = _GROQ_QUOTA.get(key_index) or {}
        prev_rem = prev.get("remaining_tokens")
        if (
            isinstance(new_rem, int) and isinstance(prev_rem, int)
            and new_rem > prev_rem + (_GROQ_TOKENS_PER_KEY_PER_DAY // 2)
        ):
            print(
                f"   🔄 Groq daily reset detected on key #{key_index + 1} "
                f"({prev_rem} → {new_rem} tokens) — zeroing usage counter."
            )
            _set_tokens_used_session(0, 0)

        # Prefer daily windows (`*-tokens`); Groq returns both per-minute and
        # per-day headers but the day values are what matters for UX.
        _GROQ_QUOTA[key_index] = {
            "remaining_tokens":   new_rem,
            "remaining_requests": _parse_int(headers.get("x-ratelimit-remaining-requests")),
            "limit_tokens":       _parse_int(headers.get("x-ratelimit-limit-tokens")),
            "limit_requests":     _parse_int(headers.get("x-ratelimit-limit-requests")),
            "reset_tokens":       headers.get("x-ratelimit-reset-tokens"),
            "reset_requests":     headers.get("x-ratelimit-reset-requests"),
            "updated_at":         time.time(),
        }
    except Exception:
        pass


def _call_groq(prompt: str, max_tokens: int = 800, temperature: float = 0.2) -> str:
    """Make a Groq completion call with proactive round-robin + TPM backoff.

    Why round-robin BEFORE the call (not just on failure):
      Free-tier Groq ≈ 12K tok/min/key. Reader-style calls are ≈5K tokens
      each, so 2-3 back-to-back calls on the SAME key burn that key's
      per-minute TPM. The legacy "reactive rotate on 429" pattern pinned
      one key until it cratered, then cascaded — looking like "all 8 keys
      exhausted" when really 7 keys were idle. Spreading each call across
      the pool keeps every key well below its TPM ceiling.

    Why short backoff+retry on 429 (instead of bailing to heuristic):
      Groq's TPM reset window is sub-second to ~60s (header carries
      `x-ratelimit-reset-tokens`, often "210ms"). For most bursts a brief
      sleep beats falling back to a parser that silently degrades.
      Bounded by GROQ_MAX_WAIT_S (default 10s) so we never block a Streamlit
      thread for long — caller's per-job try/except handles a final failure.
    """
    global _GROQ_KEY_INDEX, _GROQ_KEYS, _LAST_LLM_SOURCE
    if not _GROQ_KEYS:
        _GROQ_KEYS = _load_groq_keys()
    if not _GROQ_KEYS:
        raise RuntimeError("No valid GROQ_API_KEY found in environment")
    model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    n_keys = len(_GROQ_KEYS)
    # Each key may TPM once, then recover — so try up to 2× the pool size.
    attempts = max(1, n_keys * 2)
    max_total_wait_s = float(os.getenv("GROQ_MAX_WAIT_S", "10.0"))
    waited_s = 0.0
    last_err: Optional[Exception] = None
    for attempt in range(attempts):
        idx, wait_s = _next_available_groq_index(advance=True)
        if wait_s > 0:
            # All keys cooling. Sleep until the earliest one is ready, but
            # never burn more than max_total_wait_s across the whole call.
            remaining = max_total_wait_s - waited_s
            if remaining <= 0:
                break
            actual = min(wait_s, remaining)
            print(
                f"   ⏱  All Groq keys cooling — waiting {actual:.2f}s for "
                f"key #{idx + 1} (cap {max_total_wait_s:.0f}s)"
            )
            time.sleep(actual)
            waited_s += actual
            if time.time() < _GROQ_KEY_COOLDOWN_UNTIL.get(idx, 0.0):
                continue  # still cooling, try the loop again
        _GROQ_KEY_INDEX = idx
        try:
            client = _groq_client()
            raw = client.chat.completions.with_raw_response.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=60.0,
            )
            try:
                _capture_quota_from_headers(getattr(raw, "headers", None), idx)
            except Exception:
                pass
            resp = raw.parse()
            try:
                usage = getattr(resp, "usage", None)
                if usage is not None:
                    total = getattr(usage, "total_tokens", None)
                    if isinstance(total, int) and total > 0:
                        _increment_groq_tokens(total)
            except Exception:
                pass
            _LAST_LLM_SOURCE = f"GROQ key#{idx + 1}"
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            last_err = e
            if _is_rate_limit_error(e):
                retry_s = _parse_groq_retry_after(e)
                _mark_groq_key_cooldown(idx, retry_s)
                try:
                    from agents.analytics import track_event
                    cooling_now = sum(
                        1 for v in _GROQ_KEY_COOLDOWN_UNTIL.values()
                        if v > time.time()
                    )
                    track_event(
                        "llm_rate_limit_hit",
                        "system_infra",
                        {
                            "key_index": idx + 1,
                            "retry_delay_s": round(retry_s, 3),
                            "keys_cooling": cooling_now,
                            "keys_total": n_keys,
                        },
                    )
                except Exception:
                    pass
                continue  # round-robin to next key
            if _is_auth_error(e):
                # Bad/revoked key — park it for a long time so we don't
                # waste round-robin slots on it for the rest of the session.
                _mark_groq_key_cooldown(idx, 3600.0)
                continue
            raise
    print(
        f"   ⚠️  All {n_keys} Groq key(s) rate-limited "
        f"(waited {waited_s:.1f}s, cap {max_total_wait_s:.0f}s)"
    )
    raise RuntimeError(
        f"Groq call failed after {attempts} attempts across {n_keys} key(s) "
        f"— all rate-limited (waited {waited_s:.1f}s). Last error: "
        f"{type(last_err).__name__ if last_err else 'None'}"
    )


# ── Public quota API (consumed by the sidebar UI) ───────────────────────────

# Average GROQ-ONLY token footprint of a full agent run. This divides the
# Groq daily budget into "estimated runs left" for the sidebar UI.
#
# CRITICAL: this must be a Groq-only number. `remaining` in
# get_quota_summary() depletes by `groq_used` alone — Groq's free tier is
# the only hard daily ceiling. DeepSeek does all the writing (strategist,
# tailor, cover letter) but is paid and uncapped, so it must NOT be folded
# into this figure. The earlier 110,000 value wrongly included the DeepSeek
# footprint, making the dial divide the Groq budget by a number ~5× too
# large (e.g. 800K // 110K = 7 instead of the real ~36).
#
# May 2026 measurement — from the Run 22 and Run 23 Langfuse exports, the
# Groq (llama-3.3-70b) footprint of a full 3-job run:
#
#   matcher    : 3 × ~2,900 = ~8,700
#   supervisor : 4 × ~0,830 = ~3,300
#   planner    : 1 × ~1,900 = ~1,900
#   reviewer   : 2 × ~3,700 = ~7,400
#                              ───────
#   Run 22 total: 20,730   Run 23 total: 21,539
#
# 22,000 ≈ measured mean + modest headroom for reviewer retries.
#
# Override via env var if your deployment hits different averages
# (different number of jobs per run, different reviewer retry rates).
_TOKENS_PER_RUN_AVG = int(os.getenv("APPLYSMART_TOKENS_PER_RUN", "22000"))


def get_quota_summary() -> dict:
    """
    Compute the deployment-wide daily budget.

    Model:
      • The full daily pool is (num_keys × per_key_daily_limit) — by default
        3 keys × 100K = 300K tokens/day on the free tier.
      • Every successful Groq call adds `resp.usage.total_tokens` to a
        process-wide counter (_TOKENS_USED_SESSION).
      • `remaining = max(0, total_budget − used)`.
      • `est_runs_left = remaining // _TOKENS_PER_RUN_AVG`.

    This is intentionally simpler than per-key header arithmetic: it shows
    users the full pool upfront (ready=True from the first page-load) and
    deducts deterministically as calls land. On Streamlit Cloud the process
    stays alive across user sessions, so this counter effectively tracks
    the whole day's deployment usage until the Groq daily reset.

    Edge case — process restart mid-day:
      The counter zeroes, so the UI briefly over-reports available quota.
      After the first call completes, header cross-check kicks in and we
      reconcile (see `headers_remaining` vs `computed_remaining` below).
    """
    global _GROQ_KEYS
    if not _GROQ_KEYS:
        _GROQ_KEYS = _load_groq_keys()

    n_keys         = len(_GROQ_KEYS)
    total_budget   = n_keys * _GROQ_TOKENS_PER_KEY_PER_DAY
    used_dict      = _get_tokens_used_session()
    groq_used      = used_dict.get("groq", 0)
    deepseek_used  = used_dict.get("deepseek", 0)
    total_used     = groq_used + deepseek_used
    computed_rem   = max(0, total_budget - groq_used)

    # Cross-check with Groq's server-side view — sum of remaining_tokens
    # across keys we've already touched. If the server says we have LESS
    # than our computed remaining (because the process restarted or an
    # external caller used the same keys), trust the server.
    headers_rem_sum:   Optional[int] = None
    keys_with_headers: int           = 0
    first_reset:       Optional[str] = None
    for snap in _GROQ_QUOTA.values():
        if not snap:
            continue
        rt = snap.get("remaining_tokens")
        if rt is not None:
            headers_rem_sum = (headers_rem_sum or 0) + rt
            keys_with_headers += 1
        if first_reset is None and snap.get("reset_tokens"):
            first_reset = snap["reset_tokens"]

    # Only trust the header sum when we've touched ALL keys, otherwise the
    # un-hit keys contribute 0 and understate the real remaining.
    remaining = computed_rem
    if headers_rem_sum is not None and keys_with_headers >= n_keys and n_keys > 0:
        remaining = min(computed_rem, headers_rem_sum)

    est_runs_left = remaining // _TOKENS_PER_RUN_AVG if _TOKENS_PER_RUN_AVG > 0 else 0
    pct_used      = int(100 * groq_used / total_budget) if total_budget > 0 else 0

    return {
        # Deployment-wide numbers (what the UI displays)
        "total_budget":     total_budget,
        "used":             total_used,
        "groq_used":        groq_used,
        "deepseek_used":    deepseek_used,
        # Back-compat: a few UI / analytics consumers still read `gemini_used`
        # (the legacy field name from when DeepSeek wasn't broken out). Mirror
        # `deepseek_used` here for one release cycle so nothing crashes.
        "gemini_used":      deepseek_used,
        "remaining":        remaining,
        "pct_used":         pct_used,
        "est_runs_left":    est_runs_left,
        "tokens_per_run":   _TOKENS_PER_RUN_AVG,
        "tokens_per_key":   _GROQ_TOKENS_PER_KEY_PER_DAY,
        "keys_total":       n_keys,
        "reset_tokens":     first_reset or "",
        # Ready from the first page load — we always know the full pool.
        "ready":            n_keys > 0,
    }


def reset_session_counter() -> None:
    """
    Manually zero the deployment-wide token counter. Useful for test harnesses
    and for an admin 'reset now' button once the Groq daily window rolls over.
    """
    _set_tokens_used_session(0, 0)


def chat_quality(prompt: str, max_tokens: int = 800, temperature: float = 0.2) -> str:
    print(f"   🤖 [GROQ / QUALITY] requesting {max_tokens} tokens...")
    return _call_groq(prompt, max_tokens=max_tokens, temperature=temperature)


# ── DeepSeek call (V4-Flash, OpenAI-compatible HTTP) ─────────────────────────

def _load_deepseek_key() -> Optional[str]:
    """Load DEEPSEEK_API_KEY from env or Streamlit secrets. Returns None
    if no key is configured — callers must handle this and fall back."""
    try:
        from agents.runtime import secret_or_env
        k = secret_or_env("DEEPSEEK_API_KEY")
        if k and k.strip():
            return k.strip()
    except Exception:
        pass
    return None


def _resolve_deepseek_provider() -> Optional[Dict[str, Any]]:
    """
    Resolve the DeepSeek provider config (Direct DeepSeek only as of Jun 2026
    — NVIDIA NIM branch removed since LLM_PROVIDER=nvidia was never set in
    prod and the env-var-gated branch was unreachable).

    Returns a dict {api_key, base_url, model, timeout, label} when a key is
    configured, or None (caller falls back to Groq). The `label` is used in
    log lines so users can see which provider produced any given response.
    """
    ds_key = _load_deepseek_key()
    if ds_key:
        return {
            "api_key": ds_key,
            "base_url": DEEPSEEK_BASE_URL,
            "model": DEEPSEEK_MODEL,
            "timeout": 90.0,
            "label": f"DEEPSEEK ({DEEPSEEK_MODEL})",
        }
    return None


def _increment_deepseek_tokens(delta: int) -> None:
    """Track DeepSeek token usage in the shared file-cache (observability
    only — DeepSeek is paid out-of-band, not gated by the Groq daily quota
    UI)."""
    try:
        current = _get_tokens_used_session()
        _set_tokens_used_session(current["groq"], current["deepseek"] + delta)
    except Exception:
        pass


# Last DeepSeek call's token usage breakdown. Populated inside
# `_call_deepseek` from the OpenAI-compatible `usage` field on the
# response. Read by the diagnostics instrumentation patch
# (diagnostics/instrumentation.py::_patch_deepseek_caller) to emit
# EXACT token counts to LangFuse — no estimates.
#
# Schema (when populated):
#   {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int}
#
# Reset to {} between calls; remains last-call-only since DeepSeek calls
# do not run in parallel within this agent.
_LAST_DEEPSEEK_USAGE: Dict[str, int] = {}


def _call_deepseek(
    prompt: str,
    max_tokens: int = 2000,
    temperature: float = 0.2,
    json_mode: bool = False,
) -> Optional[str]:
    """
    Call DeepSeek V4 via OpenAI-compatible HTTP. Returns the response
    content string on success, or None on any failure (caller falls back).

    Why None on failure (not raise):
      DeepSeek is positioned as a *quality enhancement* over the Groq
      free-tier path, not a critical-path provider. If the key is missing,
      the network is down, the account is out of credit, etc. — we want
      the caller to silently fall through to the Groq flow rather than
      crash the run.

    Args:
        json_mode: When True, request `response_format={"type":"json_object"}`.
                   Useful for the cv_diff_tailor / strategist callers that
                   need strict JSON output.
    """
    if _requests is None:
        return None
    cfg = _resolve_deepseek_provider()
    if cfg is None:
        return None
    try:
        payload = {
            "model": cfg["model"],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        resp = _requests.post(
            f"{cfg['base_url']}/chat/completions",
            headers={
                "Authorization": f"Bearer {cfg['api_key']}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=cfg["timeout"],
        )
        if resp.status_code != 200:
            print(
                f"   ⚠️  {cfg['label']} HTTP {resp.status_code}: "
                f"{resp.text[:200]} — falling back"
            )
            return None
        data = resp.json()
        content = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            or ""
        ).strip()
        # Token accounting (both providers mirror OpenAI's usage shape).
        # We capture the FULL split into `_LAST_DEEPSEEK_USAGE` so the
        # diagnostics instrumentation can emit prompt/completion/total
        # tokens to LangFuse with the exact numbers DeepSeek charged for
        # — no chars-per-token estimates anywhere in the cost trail.
        global _LAST_DEEPSEEK_USAGE, _LAST_LLM_SOURCE
        try:
            usage = data.get("usage") or {}
            pt = int(usage.get("prompt_tokens", 0) or 0)
            ct = int(usage.get("completion_tokens", 0) or 0)
            total = int(usage.get("total_tokens", 0) or (pt + ct))
            _LAST_DEEPSEEK_USAGE = {
                "prompt_tokens":     pt,
                "completion_tokens": ct,
                "total_tokens":      total,
            }
            if total > 0:
                _increment_deepseek_tokens(total)
        except Exception:
            pass
        _LAST_LLM_SOURCE = cfg["label"]
        return content
    except Exception as e:
        print(
            f"   ⚠️  {cfg['label']} call failed "
            f"({type(e).__name__}: {str(e)[:200]}) — falling back"
        )
        return None


def chat_deepseek(
    prompt: str,
    max_tokens: int = 2000,
    temperature: float = 0.2,
    json_mode: bool = False,
) -> Optional[str]:
    """Public entry point for DeepSeek calls. Returns None if no provider
    is configured (no key) or the call fails — caller is responsible for
    falling back to Groq."""
    cfg = _resolve_deepseek_provider()
    if cfg is None:
        return None
    print(f"   🤖 [{cfg['label']}] requesting {max_tokens} tokens...")
    return _call_deepseek(
        prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        json_mode=json_mode,
    )


def chat_fast(prompt: str, max_tokens: int = 500, temperature: float = 0.1) -> str:
    print(f"   🤖 [GROQ / FAST] requesting {max_tokens} tokens...")
    return _call_groq(prompt, max_tokens=max_tokens, temperature=temperature)


def last_llm_source() -> str:
    """
    Returns the source of the most recently successful LLM call as a short
    human-readable tag (e.g. "DEEPSEEK (deepseek-chat)" or "GROQ key#1").
    Useful for success-path logging — callers print the kept output's actual
    provider after the response passes their guards. Returns "unknown" if
    no successful call has happened yet (e.g. before the first call).
    """
    return _LAST_LLM_SOURCE


# ─────────────────────────────────────────────────────────────────────────────
# Diagnostics hook (deletable; no-op when disabled)
# ─────────────────────────────────────────────────────────────────────────────
# When DIAGNOSTICS_ENABLED=1 is set in the environment, the diagnostics
# package monkey-patches _call_groq, _call_deepseek, and track_llm_call to
# emit per-call telemetry to JSONL (always) and Langfuse (when configured).
#
# When the env var is not set, this block does nothing — diagnostics is
# never imported and has zero runtime cost.
#
# To remove diagnostics entirely later:
#   1. rm -rf diagnostics/
#   2. Delete this entire block.
#   3. Remove `langfuse` from requirements.txt.
#   4. Remove DIAGNOSTICS_* / LANGFUSE_* env vars from .env.
def _diagnostics_enabled() -> bool:
    """Check DIAGNOSTICS_ENABLED in env first, then Streamlit secrets."""
    val = os.environ.get("DIAGNOSTICS_ENABLED")
    if val is not None:
        return val == "1"
    try:
        import streamlit as st  # local import — non-Streamlit callers don't pay
        if hasattr(st, "secrets") and "DIAGNOSTICS_ENABLED" in st.secrets:  # type: ignore[attr-defined]
            return str(st.secrets["DIAGNOSTICS_ENABLED"]) == "1"            # type: ignore[index]
    except Exception:
        pass
    return False


if _diagnostics_enabled():
    try:
        from diagnostics.instrumentation import patch as _diagnostics_patch
        _diagnostics_patch()
    except ImportError as _diag_imp_err:
        print(
            f"   diagnostics: package not importable ({_diag_imp_err}); "
            f"continuing without instrumentation."
        )
    except Exception as _diag_err:
        print(
            f"   ⚠️  diagnostics: patch failed "
            f"({type(_diag_err).__name__}: {_diag_err}); "
            f"continuing without instrumentation."
        )