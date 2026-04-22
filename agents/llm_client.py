# agents/llm_client.py

import os
import time
import random
import json
from pathlib import Path
from typing import Optional

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


# ── Groq key rotation pool ──────────────────────────────────────────────────
# Load up to 3 keys from env. On rate-limit, rotate to the next key.
# Multiply daily budget: 3 keys × 100K = 300K tokens/day.
_GROQ_KEYS: list = []
_GROQ_KEY_INDEX: int = 0
_GROQ_CLIENTS: dict = {}  # key → Groq client instance

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


def _load_groq_keys() -> list:
    """Load all available Groq keys from env at startup."""
    from agents.runtime import secret_or_env
    keys = []
    for var in ("GROQ_API_KEY", "GROQ_API_KEY_2", "GROQ_API_KEY_3"):
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
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            if _is_rate_limit_error(e) or _is_auth_error(e):
                if _rotate_groq_key():
                    continue  # try next key immediately, no sleep
                # All keys exhausted — fall back to a short sleep
                time.sleep(30)
                _GROQ_KEY_INDEX = 0  # reset to key 1 after sleep
                continue
            raise
    return ""


# ── Public quota API (consumed by the sidebar UI) ───────────────────────────

# Rough average of a full agent run (scrape + match + tailor + review +
# cover-letter + reviewer) with default settings and 5 jobs. Empirical, not
# exact; used only to translate remaining-tokens → "estimated runs left".
_TOKENS_PER_RUN_AVG = int(os.getenv("APPLYSMART_TOKENS_PER_RUN", "20000"))


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


def chat_fast(prompt: str, max_tokens: int = 500, temperature: float = 0.1) -> str:
    print(f"   🤖 [GROQ / FAST] requesting {max_tokens} tokens...")
    return _call_groq(prompt, max_tokens=max_tokens, temperature=temperature)


def _call_gemini(prompt: str, max_tokens: int = 800, temperature: float = 0.2) -> str:
    """Call Gemini 2.5 Flash API for writing tasks. Falls back to Groq if unavailable."""
    if genai is None:
        print("   ⚠️  Gemini not available, falling back to Groq")
        return _call_groq(prompt, max_tokens=max_tokens, temperature=temperature)
    
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("   ⚠️  GEMINI_API_KEY not found, falling back to Groq")
        return _call_groq(prompt, max_tokens=max_tokens, temperature=temperature)
    
    # Safety check: warn if key doesn't look like a valid Google AI Studio key
    if not api_key.startswith("AIza"):
        print(f"   ⚠️  Gemini key format looks invalid (should start with 'AIza')")
    
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))
        
        response = model.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                max_output_tokens=max_tokens,
                temperature=temperature,
            )
        )
        
        # Track tokens used for quota calculations
        try:
            if hasattr(response, 'usage_metadata'):
                total_tokens = getattr(response.usage_metadata, 'total_token_count', 0)
                if isinstance(total_tokens, int) and total_tokens > 0:
                    _increment_gemini_tokens(total_tokens)
        except Exception:
            pass
        
        return response.text.strip() if response.text else ""
    except Exception as e:
        print(f"   ⚠️  Gemini call failed ({type(e).__name__}: {e}), falling back to Groq")
        return _call_groq(prompt, max_tokens=max_tokens, temperature=temperature)


def chat_gemini(prompt: str, max_tokens: int = 800, temperature: float = 0.2) -> str:
    """Use Gemini 2.5 Flash for writing tasks (CV tailoring, cover letters)."""
    print(f"   🤖 [GEMINI / WRITING] requesting {max_tokens} tokens...")
    return _call_gemini(prompt, max_tokens=max_tokens, temperature=temperature)