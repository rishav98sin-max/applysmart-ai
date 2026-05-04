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
    import google.generativeai as genai
except Exception:
    genai = None

try:
    import requests as _requests  # for DeepSeek HTTP calls (no extra SDK)
except Exception:
    _requests = None


# ── Groq key rotation pool ──────────────────────────────────────────────────
# Load up to 3 keys from env. On rate-limit, rotate to the next key.
# Multiply daily budget: 3 keys × 100K = 300K tokens/day.
_GROQ_KEYS: list = []
_GROQ_KEY_INDEX: int = 0
_GROQ_CLIENTS: dict = {}  # key → Groq client instance

# ── Gemini key rotation pool ────────────────────────────────────────────────
# Same idea as Groq. Free tier on gemini-2.5-flash is tight (req/day limit),
# so multiplying 3 keys triples the daily envelope for CV tailoring + cover
# letter work. On quota / 429 errors we rotate, and fall back to Groq only
# when every key is exhausted.
_GEMINI_KEYS: list = []
_GEMINI_KEY_INDEX: int = 0
_GEMINI_CONFIGURED_KEY: Optional[str] = None  # last key passed to genai.configure

# Rate limiting for Gemini free tier (P6 redesign — Apr 28).
#
# gemini-2.5-flash free tier: 10 RPM per Google Cloud project per model
# (was 5 RPM historically; some projects may still be on the lower cap).
# Strategy:
#   • Global gap of `_GEMINI_MIN_GAP_S` (7s default ≈ 8.5 RPM) between any
#     two call STARTS. Slot is reserved atomically inside the lock; the
#     actual sleep happens outside so concurrent threads serialise without
#     blocking each other on the lock.
#   • Per-key cooldown table populated from Google's `retry_delay` field on
#     429 responses. Cooled-down keys are skipped entirely until they
#     recover. When ALL keys are cooling, we sleep until the soonest one
#     recovers instead of falling straight to Groq.
#   • `_LAST_GEMINI_CALL_TIME` is the FUTURE-RESERVED slot of the most
#     recent call. We do NOT overwrite this after a successful call (that
#     was the old race condition: an in-flight call's completion would
#     clobber a later thread's reservation, letting the next caller squeeze
#     in <gap seconds after the previous one and busting the RPM window).
_LAST_GEMINI_CALL_TIME: float = 0.0
_GEMINI_RATE_LIMIT_LOCK = threading.Lock()

# ── Last-successful-LLM-source tracking (Apr 28 follow-up) ──────────────────
# Set by _call_gemini and _call_groq on every successful return so callers
# can log "which model produced this kept output". Values:
#   "GEMINI key#1" / "GEMINI key#2" / "GEMINI key#3"  → live Gemini call
#   "GROQ key#1"   / "GROQ key#2"                      → live Groq call
#                                                        (also after Gemini fallback)
#   "unknown"                                          → no successful call yet
# Read via last_llm_source(). Module-global rather than per-call return value
# so we don't have to refactor every caller's signature.
_LAST_LLM_SOURCE: str = "unknown"

# Per-key cooldowns. Map: key_index → unix_timestamp_when_key_recovers.
# Updated on 429 with `retry_delay` from Google. Honoured by
# `_gemini_configure_current()` (skips cooled-down keys) and by
# `_call_gemini` (sleeps until earliest recovery when ALL keys are cooling).
# Reads/writes are dict-atomic under the GIL — no additional lock needed.
_GEMINI_KEY_COOLDOWN_UNTIL: Dict[int, float] = {}

# Global inter-call gap. 7s ≈ 8.5 RPM, comfortably under the 10 RPM tier
# while leaving headroom for clock skew. If your project is still on the
# legacy 5 RPM cap, the per-key cooldown logic + retry_delay parser will
# detect 429s and back off automatically. Configurable via env so power
# users with paid keys can tighten it (e.g. 1.0s for 60 RPM tier).
_GEMINI_MIN_GAP_S: float = float(os.getenv("GEMINI_MIN_GAP_S", "7.0"))

# Per-key quota snapshot captured from Groq's x-ratelimit-* response headers
# after every successful call. Kept mostly for the reset_tokens timestamp —
# the primary "remaining" display is driven by file-based quota cache below.
_GROQ_QUOTA: dict = {}

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
    """Get tokens used from file cache, defaulting to 0 for both providers."""
    try:
        qf = _quota_file_path()
        if qf.exists():
            data = json.loads(qf.read_text())
            # Reset when the day rolls over.
            today = __import__('datetime').datetime.now().date().isoformat()
            if data.get("date") != today:
                return {"groq": 0, "gemini": 0}
            return {
                "groq": int(data.get("groq_tokens", 0)),
                "gemini": int(data.get("gemini_tokens", 0)),
            }
    except Exception as e:
        if _QUOTA_LOG_ERRORS:
            print(f"   Quota read failed: {e}")
    return {"groq": 0, "gemini": 0}

def _set_tokens_used_session(groq: int, gemini: int) -> None:
    """Set tokens used in file cache for both providers."""
    try:
        today = __import__('datetime').datetime.now().date().isoformat()
        data = {"date": today, "groq_tokens": int(groq), "gemini_tokens": int(gemini)}
        _quota_file_path().write_text(json.dumps(data))
    except Exception as e:
        if _QUOTA_LOG_ERRORS:
            print(f"   Quota write failed: {e}")

def _increment_groq_tokens(delta: int) -> None:
    """Increment Groq tokens used in file cache."""
    try:
        current = _get_tokens_used_session()
        _set_tokens_used_session(current["groq"] + delta, current["gemini"])
    except Exception:
        pass

def _increment_gemini_tokens(delta: int) -> None:
    """Increment Gemini tokens used in file cache."""
    try:
        current = _get_tokens_used_session()
        _set_tokens_used_session(current["groq"], current["gemini"] + delta)
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
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# ── DeepSeek configuration (May 2026) ───────────────────────────────────────
# DeepSeek V4 (released Apr 24, 2026) offers stronger instruction-following
# than Llama 3.3 70B at ~$0.28 / 1M output tokens — effectively free for
# our per-job tailor footprint (~7K in + ~1K out ≈ $0.001 per call).
#
# Two providers are supported, selected by `LLM_PROVIDER` env var:
#
#   LLM_PROVIDER=direct  (default)
#     Direct DeepSeek API at api.deepseek.com. Fast (~3-10s/call),
#     full JSON mode, full SLA. Costs ~$0.001 per CV tailor call.
#     Use for production launches and dev iteration.
#
#   LLM_PROVIDER=nvidia
#     NVIDIA NIM (build.nvidia.com) at integrate.api.nvidia.com. Free
#     (1K-5K credits lifetime), slower (~30-90s/call on shared GPU
#     queue), 40 req/min rate limit. Use for extended testing phases
#     where burning paid credits isn't desirable. Same OpenAI-compatible
#     wire format so the only changes are base_url, api_key, model id.
#
# Both providers are OpenAI-compatible so we use plain HTTP via `requests`
# (no extra SDK dependency). The `_resolve_deepseek_provider()` helper
# returns the (api_key, base_url, model, timeout) tuple based on env.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "direct").lower().strip()

# Direct DeepSeek (default).
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_BASE_URL = os.getenv(
    "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
).rstrip("/")

# NVIDIA NIM hosted DeepSeek.
NVIDIA_MODEL = os.getenv("NVIDIA_MODEL", "deepseek-ai/deepseek-v4-flash")
NVIDIA_BASE_URL = os.getenv(
    "NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"
).rstrip("/")


def _load_groq_keys() -> list:
    """Load all available Groq keys from env at startup."""
    from agents.runtime import secret_or_env
    keys = []
    for var in ("GROQ_API_KEY", "GROQ_API_KEY_2", "GROQ_API_KEY_3", "GROQ_API_KEY_4"):
        k = secret_or_env(var)
        if k and k.startswith("gsk_") and k not in keys:
            keys.append(k)
    return keys


def _load_gemini_keys() -> list:
    """Load all available Gemini keys from env/secrets at startup."""
    from agents.runtime import secret_or_env
    keys = []
    for var in ("GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3"):
        k = secret_or_env(var)
        if k and k.startswith("AIza") and k not in keys:
            keys.append(k)
    return keys


def _gemini_configure_current() -> Optional[str]:
    """
    Ensure `genai` is configured with the currently-selected key. Returns
    the active key (or None if no keys are configured / current key is
    cooling down). Reconfigures lazily only when the active key changes
    to avoid redundant SDK calls.

    Defence-in-depth for P6 cooldowns: even if a caller forgets to use
    `_next_available_gemini_key()` to pick a non-cooling key, this returns
    None so the call falls through to Groq cleanly instead of hammering a
    rate-limited key.
    """
    global _GEMINI_KEYS, _GEMINI_KEY_INDEX, _GEMINI_CONFIGURED_KEY
    if not _GEMINI_KEYS:
        _GEMINI_KEYS = _load_gemini_keys()
    if not _GEMINI_KEYS:
        return None
    idx = _GEMINI_KEY_INDEX % len(_GEMINI_KEYS)
    cooldown = _GEMINI_KEY_COOLDOWN_UNTIL.get(idx, 0.0)
    if cooldown > time.time():
        return None
    key = _GEMINI_KEYS[idx]
    if key != _GEMINI_CONFIGURED_KEY:
        genai.configure(api_key=key)
        _GEMINI_CONFIGURED_KEY = key
    return key


def _rotate_gemini_key() -> bool:
    """Rotate to the next Gemini key. Returns False when pool is exhausted."""
    global _GEMINI_KEY_INDEX, _GEMINI_KEYS
    try:
        from agents.analytics import track_event
        track_event(
            "llm_rate_limit_hit",
            "system_infra",
            {
                "provider": "gemini",
                "exhausted_key_index": _GEMINI_KEY_INDEX + 1,
                "total_keys_configured": len(_GEMINI_KEYS),
            },
        )
    except Exception:
        pass
    if not _GEMINI_KEYS:
        _GEMINI_KEYS = _load_gemini_keys()
    _GEMINI_KEY_INDEX += 1
    if _GEMINI_KEY_INDEX < len(_GEMINI_KEYS):
        print(
            f"   🔄 Gemini key rotated → key #{_GEMINI_KEY_INDEX + 1} "
            f"of {len(_GEMINI_KEYS)}"
        )
        return True
    print(f"   ⚠️  All {len(_GEMINI_KEYS)} Gemini key(s) exhausted — "
          "falling back to Groq")
    return False


# ────────────────────────────────────────────────────────────
# P6 — Per-key cooldown helpers
# ────────────────────────────────────────────────────────────

def _parse_retry_delay_seconds(err: Exception) -> float:
    """
    Parse Google's `retry_delay { seconds: N }` field from a 429 error.
    Falls back to regex on the stringified error, then to 60s default.
    """
    try:
        details = getattr(err, "details", None)
        if callable(details):
            for d in details():
                rd = getattr(d, "retry_delay", None)
                secs = getattr(rd, "seconds", None) if rd is not None else None
                if isinstance(secs, int) and secs > 0:
                    return float(secs)
    except Exception:
        pass
    import re as _re
    s = str(err)
    m = _re.search(
        r"retry[_ ]delay\s*\{[^}]*?seconds:\s*(\d+)",
        s, _re.IGNORECASE | _re.DOTALL,
    )
    if m:
        return float(m.group(1))
    m = _re.search(r"Please retry in (\d+(?:\.\d+)?)s", s)
    if m:
        return float(m.group(1))
    return 60.0


def _mark_gemini_key_cooldown(key_index: int, retry_delay_s: float) -> None:
    """Set per-key cooldown — won't be eligible until the deadline."""
    _GEMINI_KEY_COOLDOWN_UNTIL[key_index] = time.time() + retry_delay_s
    print(
        f"   ⏱  Gemini key #{key_index + 1} cooling for "
        f"{retry_delay_s:.1f}s (until quota window resets)"
    )


def _next_available_gemini_key():
    """
    Find the next non-cooling key starting from the current rotation index.
    Returns (key_index, 0.0) when one is ready immediately, or
    (earliest_recovering_index, wait_seconds) when ALL keys are cooling.
    Returns (None, 0.0) when no keys are configured at all.
    """
    if not _GEMINI_KEYS:
        return None, 0.0
    n = len(_GEMINI_KEYS)
    now = time.time()
    for offset in range(n):
        idx = (_GEMINI_KEY_INDEX + offset) % n
        cooldown = _GEMINI_KEY_COOLDOWN_UNTIL.get(idx, 0.0)
        if cooldown <= now:
            return idx, 0.0
    earliest_idx = min(
        range(n),
        key=lambda i: _GEMINI_KEY_COOLDOWN_UNTIL.get(i, 0.0),
    )
    wait_s = max(0.0, _GEMINI_KEY_COOLDOWN_UNTIL[earliest_idx] - now)
    return earliest_idx, wait_s


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
    global _GROQ_KEY_INDEX, _GROQ_KEYS
    model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    attempts = max(1, len(_GROQ_KEYS) if _GROQ_KEYS else 1) + 1  # try all keys once + 1 sleep
    for attempt in range(attempts):
        try:
            client = _groq_client()
            # `.with_raw_response` exposes the underlying HTTP headers so we
            # can read `x-ratelimit-*` for the daily-budget UI. The parsed
            # completion is then extracted from `raw.parse()`.
            raw = client.chat.completions.with_raw_response.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=60.0,  # 60 second timeout to prevent indefinite hanging
            )
            # Defensive header capture — never block the response path.
            try:
                _capture_quota_from_headers(getattr(raw, "headers", None), _GROQ_KEY_INDEX)
            except Exception:
                pass
            resp = raw.parse()
            # Deduct actual tokens consumed from the Groq budget.
            # Groq's response mirrors OpenAI's shape: usage.total_tokens covers
            # prompt + completion. Never let an accounting error break the
            # real LLM response — hence the broad try/except.
            try:
                usage = getattr(resp, "usage", None)
                if usage is not None:
                    total = getattr(usage, "total_tokens", None)
                    if isinstance(total, int) and total > 0:
                        _increment_groq_tokens(total)
            except Exception:
                pass
            global _LAST_LLM_SOURCE
            _LAST_LLM_SOURCE = f"GROQ key#{_GROQ_KEY_INDEX + 1}"
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            if _is_rate_limit_error(e) or _is_auth_error(e):
                if _rotate_groq_key():
                    continue  # try next key immediately, no sleep
                # All keys exhausted — break out and raise the typed
                # RuntimeError below instead of blocking 30s. Daily quotas
                # don't reset in 30s, so the sleep was just guaranteeing a
                # hung Streamlit session before the inevitable re-raise.
                # Caller's per-job try/except in job_agent.py handles the
                # exception cleanly as a job-level failure.
                break
            raise
    # Loop exhausted without success. Raise instead of returning "" so callers
    # don't silently parse JSON from empty string and produce garbage outputs.
    raise RuntimeError(
        f"Groq call failed after {attempts} attempts across "
        f"{len(_GROQ_KEYS)} key(s) — all rate-limited or invalid."
    )


# ── Public quota API (consumed by the sidebar UI) ───────────────────────────

# Rough average of a full agent run (scrape + match + tailor + review +
# cover-letter + reviewer) with default settings and 3 jobs. Empirical,
# used only to translate remaining-tokens → "estimated runs left" in the
# sidebar UI.
#
# May 2026 recalibration: bumped from 20,000 to 45,000 to reflect what
# Groq actually consumes per run AFTER the DeepSeek migration. DeepSeek
# tokens are paid out-of-band and do NOT count toward the daily Groq
# quota — only Groq stages do (planner, supervisor, scrape rerank, match
# scorer, cv reviewer, cover reviewer). Empirical Groq-only breakdown
# per 3-job run:
#
#   match scorer    : 3 ×  2,950 =  8,850
#   cv_reviewer     : 3 ×  3,300 =  9,900    (template + JD + diff render)
#   cover_reviewer  : 3 ×  3,800 = 11,400    (template + CV + JD + letter)
#   supervisor      : 4 ×  1,300 =  5,200    (cycle decisions)
#   planner         : 1 ×  3,000 =  3,000
#   scrape_rerank   : 4 ×  1,200 =  4,800
#                                  ───────
#                                  ~43,000 tokens / 3-job run, no retries
#                                  ~47,000 tokens with one reviewer retry
#
# At 45,000 tokens/run estimate against the default 4-key × 100K daily
# pool (400K), the UI displays ~9 runs available — which matches reality
# rather than the previous misleading "20 runs" claim.
#
# Override via env var if your deployment hits different averages
# (different number of jobs per run, different reviewer retry rates).
_TOKENS_PER_RUN_AVG = int(os.getenv("APPLYSMART_TOKENS_PER_RUN", "45000"))


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
    gemini_used    = used_dict.get("gemini", 0)
    total_used     = groq_used + gemini_used
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
        "gemini_used":      gemini_used,
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


def _load_nvidia_key() -> Optional[str]:
    """Load NVIDIA_API_KEY (NIM, prefix `nvapi-`) from env or Streamlit
    secrets. Returns None if no key is configured."""
    try:
        from agents.runtime import secret_or_env
        k = secret_or_env("NVIDIA_API_KEY")
        if k and k.strip():
            return k.strip()
    except Exception:
        pass
    return None


def _resolve_deepseek_provider() -> Optional[Dict[str, Any]]:
    """
    Resolve the active DeepSeek provider based on `LLM_PROVIDER`.

    Returns a dict {api_key, base_url, model, timeout, label} on success,
    or None if the configured provider has no key (caller falls back to
    Groq). The `label` is used in log lines so users can see which
    provider produced any given response.

    Selection rules:
      - LLM_PROVIDER=nvidia → require NVIDIA_API_KEY; fall back to direct
        if NVIDIA key missing (better than total fail).
      - LLM_PROVIDER=direct (or any other value) → require DEEPSEEK_API_KEY.
      - Both keys missing → return None (callers route straight to Groq).
    """
    provider = LLM_PROVIDER

    if provider == "nvidia":
        nv_key = _load_nvidia_key()
        if nv_key:
            return {
                "api_key": nv_key,
                "base_url": NVIDIA_BASE_URL,
                "model": NVIDIA_MODEL,
                # NVIDIA NIM free tier shares GPU queues — calls can take
                # 30-120s. Bump timeout so we don't kill long-but-eventually-
                # successful responses.
                "timeout": 180.0,
                "label": f"NVIDIA NIM ({NVIDIA_MODEL})",
            }
        # NVIDIA configured but key missing — fall back to direct silently
        # so the user isn't blocked when they forget the env var.
        print(
            "   ⚠️  LLM_PROVIDER=nvidia but NVIDIA_API_KEY missing — "
            "falling back to direct DeepSeek"
        )

    # Direct DeepSeek (default + nvidia-fallback)
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
    """Track DeepSeek token usage in the shared file-cache. Stored under
    the gemini bucket since the quota panel only displays groq+gemini —
    DeepSeek is paid out-of-band so this is observability only."""
    try:
        current = _get_tokens_used_session()
        # Reuse gemini bucket (UI-side) so usage shows up; add a separate
        # bucket later if we want to break it out.
        _set_tokens_used_session(current["groq"], current["gemini"] + delta)
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
      the caller to silently fall through to the existing Groq/Gemini
      flow rather than crash the run.

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
    falling back. Honours `LLM_PROVIDER` to route to direct API or NVIDIA NIM."""
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


def _call_gemini(prompt: str, max_tokens: int = 800, temperature: float = 0.2) -> str:
    """
    Call Gemini via the active key in the rotation pool.

    Rate-limit strategy (P6 redesign — Apr 28):
      • Global gap of `_GEMINI_MIN_GAP_S` (7s default ≈ 8.5 RPM) between
        any two call STARTS. The slot is reserved atomically inside a lock;
        the actual sleep happens outside so concurrent threads serialise.
      • Per-key cooldown table populated from Google's `retry_delay` field
        on 429 responses. Cooled-down keys are skipped entirely until they
        recover.
      • When ALL keys are cooling, we sleep until the soonest recovery
        rather than punting straight to Groq.
      • The post-call timestamp is NOT updated — the reservation is the
        source of truth. (Old bug: post-call overwrite let late-arriving
        threads compute their slot from a stale `_LAST` value, squeezing
        2-3 calls into a single 14s window and busting 5 RPM.)
    """
    global _GEMINI_KEYS, _LAST_GEMINI_CALL_TIME, _GEMINI_KEY_INDEX

    if genai is None:
        print("   ⚠️  Gemini SDK not installed, falling back to Groq")
        return _call_groq(prompt, max_tokens=max_tokens, temperature=temperature)

    if not _GEMINI_KEYS:
        _GEMINI_KEYS = _load_gemini_keys()
    if not _GEMINI_KEYS:
        print("   ⚠️  No GEMINI_API_KEY* found, falling back to Groq")
        return _call_groq(prompt, max_tokens=max_tokens, temperature=temperature)

    n_keys = len(_GEMINI_KEYS)
    last_err: Optional[Exception] = None

    # Try every key once. On 429 we mark the key cooling and rotate.
    for _ in range(n_keys):
        # Pick a non-cooling key. If all are cooling, sleep until the
        # soonest one recovers — better than punting to Groq when the
        # wait is short (5-30s typically).
        key_idx, wait_s = _next_available_gemini_key()
        if key_idx is None:
            break
        if wait_s > 0:
            print(
                f"   ⏳ All {n_keys} Gemini key(s) cooling — "
                f"sleeping {wait_s:.1f}s until key #{key_idx + 1} recovers..."
            )
            time.sleep(wait_s)
        _GEMINI_KEY_INDEX = key_idx

        # Reserve the global call slot (serialised across threads).
        with _GEMINI_RATE_LIMIT_LOCK:
            now = time.time()
            # slot = max(now, _LAST + gap). Guarantees ≥ gap between
            # consecutive call STARTS regardless of how long any individual
            # call takes. Critical: we never write a value SMALLER than
            # the existing _LAST (which would be the post-call overwrite
            # bug we removed).
            slot = max(now, _LAST_GEMINI_CALL_TIME + _GEMINI_MIN_GAP_S)
            sleep_time = max(0.0, slot - now)
            _LAST_GEMINI_CALL_TIME = slot
        if sleep_time > 0:
            print(
                f"   ⏳ Gemini rate limit: sleeping {sleep_time:.1f}s "
                f"before call..."
            )
            time.sleep(sleep_time)

        active = _gemini_configure_current()
        if not active:
            # Current key is cooling (or no keys). Loop will pick the next
            # non-cooling key on the following iteration via
            # _next_available_gemini_key().
            continue
        try:
            model = genai.GenerativeModel(GEMINI_MODEL)
            response = model.generate_content(
                prompt,
                generation_config=genai.types.GenerationConfig(
                    max_output_tokens=max_tokens,
                    temperature=temperature,
                ),
            )
            try:
                if hasattr(response, "usage_metadata"):
                    total_tokens = getattr(
                        response.usage_metadata, "total_token_count", 0
                    )
                    if isinstance(total_tokens, int) and total_tokens > 0:
                        _increment_gemini_tokens(total_tokens)
            except Exception:
                pass
            # NB: do NOT update _LAST_GEMINI_CALL_TIME here. The reservation
            # we made above is authoritative; overwriting with time.time()
            # would let queued threads compute fresh slots from a stale
            # baseline and bust the RPM window (the old race condition).
            global _LAST_LLM_SOURCE
            _displayed_key = (_GEMINI_KEY_INDEX % len(_GEMINI_KEYS)) + 1 if _GEMINI_KEYS else 1
            _LAST_LLM_SOURCE = f"GEMINI key#{_displayed_key}"
            return response.text.strip() if response.text else ""

        except Exception as e:
            last_err = e
            if _is_rate_limit_error(e):
                # Honour Google's retry hint and mark this key cooling
                retry_s = _parse_retry_delay_seconds(e)
                _mark_gemini_key_cooldown(_GEMINI_KEY_INDEX, retry_s)
                # rotate — next iteration picks a fresh key (or sleeps if
                # all are cooling)
                _rotate_gemini_key()
                continue
            if _is_auth_error(e):
                # Auth = key broken; cool it for an hour so we don't
                # retry it this run, and try the next key
                _mark_gemini_key_cooldown(_GEMINI_KEY_INDEX, 3600.0)
                if _rotate_gemini_key():
                    continue
                break
            # Non-RL / non-auth failure — try next Gemini key once,
            # then fall through to Groq if no more keys
            print(
                f"   ⚠️  Gemini call failed ({type(e).__name__}: {e}), "
                f"trying next Gemini key before Groq fallback"
            )
            if _rotate_gemini_key():
                continue
            time.sleep(3)
            return _call_groq(
                prompt, max_tokens=max_tokens, temperature=temperature
            )

    if last_err is not None:
        print(
            f"   ⚠️  All Gemini keys exhausted/cooling "
            f"({type(last_err).__name__}: {str(last_err)[:200]}) — "
            f"falling back to Groq"
        )
    return _call_groq(prompt, max_tokens=max_tokens, temperature=temperature)


# ── Gemini bypass switch (Apr 30) ───────────────────────────────────────────
# Default behaviour: route all `chat_gemini()` calls straight to Groq.
#
# Why bypass-on by default:
#   Gemini 2.5 Flash on the free tier was producing truncated JSON (~200-400
#   chars) on cv_diff_tailor and cover_letter calls — every attempt failed
#   parse-validation and forced a Groq fallback anyway. Each failed attempt
#   wasted 9-11s. Run-2 telemetry (Apr 30 13:01) showed 0 successful Gemini
#   structured outputs across 4 CVs. Bypassing removes that dead-time.
#
# To re-enable Gemini at runtime: set GEMINI_BYPASS=0 in env / Streamlit
# secrets. The original Gemini path (with key rotation, cooldowns, etc.)
# is preserved verbatim in `_call_gemini()` — only the public wrapper is
# rewired. Flip the env var, redeploy, no code change needed.
def _gemini_bypass_enabled() -> bool:
    val = os.environ.get("GEMINI_BYPASS")
    if val is not None:
        return val != "0"
    try:
        import streamlit as st  # local import — non-Streamlit callers don't pay
        if hasattr(st, "secrets") and "GEMINI_BYPASS" in st.secrets:  # type: ignore[attr-defined]
            return str(st.secrets["GEMINI_BYPASS"]) != "0"            # type: ignore[index]
    except Exception:
        pass
    return True  # default: bypass ON


def chat_gemini(prompt: str, max_tokens: int = 800, temperature: float = 0.2) -> str:
    """
    Public entry point for "writing" calls (CV tailoring, cover letters).

    With GEMINI_BYPASS=1 (default since Apr 30): routes directly to Groq.
    With GEMINI_BYPASS=0: original Gemini-first / Groq-fallback behaviour.
    """
    if _gemini_bypass_enabled():
        print(f"   🤖 [GROQ / WRITING — gemini bypassed] requesting {max_tokens} tokens...")
        return _call_groq(prompt, max_tokens=max_tokens, temperature=temperature)
    print(f"   🤖 [GEMINI / WRITING] requesting {max_tokens} tokens...")
    return _call_gemini(prompt, max_tokens=max_tokens, temperature=temperature)


def last_llm_source() -> str:
    """
    Returns the source of the most recently successful LLM call as a short
    human-readable tag (e.g. "GEMINI key#2" or "GROQ key#1"). Useful for
    success-path logging — callers print the kept output's actual provider
    after the response passes their guards. Returns "unknown" if no
    successful call has happened yet (e.g. before the first call).
    """
    return _LAST_LLM_SOURCE


# ─────────────────────────────────────────────────────────────────────────────
# Diagnostics hook (deletable; no-op when disabled)
# ─────────────────────────────────────────────────────────────────────────────
# When DIAGNOSTICS_ENABLED=1 is set in the environment, the diagnostics
# package monkey-patches _call_groq, _call_gemini, and track_llm_call to
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