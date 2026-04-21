# agents/llm_client.py

import os
import time
import random
from typing import Optional

from dotenv import load_dotenv

load_dotenv(override=True)

try:
    from groq import Groq
except Exception:
    Groq = None


# ── Groq key rotation pool ──────────────────────────────────────────────────
# Load up to 3 keys from env. On rate-limit, rotate to the next key.
# Multiply daily budget: 3 keys × 100K = 300K tokens/day.
_GROQ_KEYS: list = []
_GROQ_KEY_INDEX: int = 0
_GROQ_CLIENTS: dict = {}  # key → Groq client instance

# Per-key quota snapshot captured from Groq's x-ratelimit-* response headers
# after every successful call. Keyed by `key_index` (int) so the UI can
# aggregate across all rotated keys. Fields mirror the Groq docs:
#   remaining_tokens      — tokens left in the current window (day or minute)
#   remaining_requests    — requests left in the current window
#   limit_tokens          — full quota for this key's window
#   reset_tokens          — e.g. "1h23m45s" or seconds-until-reset string
#   updated_at            — unix epoch of last measurement
_GROQ_QUOTA: dict = {}

GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")


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
    """
    try:
        if not headers:
            return
        # Prefer daily windows (`*-tokens`); Groq returns both per-minute and
        # per-day headers but the day values are what matters for UX.
        _GROQ_QUOTA[key_index] = {
            "remaining_tokens":   _parse_int(headers.get("x-ratelimit-remaining-tokens")),
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
    Aggregate the last-measured quota across all rotated Groq keys.

    Returns a dict the UI can render directly:
        {
            "keys_measured":  int,      # how many keys have at least 1 sample
            "keys_total":     int,      # total keys configured
            "remaining_tokens":   int,  # summed across measured keys
            "remaining_requests": int,
            "limit_tokens":   int,      # summed across measured keys
            "est_runs_left":  int,      # remaining_tokens // tokens_per_run
            "reset_tokens":   str,      # soonest reset (first-measured key)
            "ready":          bool,     # True once any key has been measured
        }
    """
    global _GROQ_KEYS
    if not _GROQ_KEYS:
        _GROQ_KEYS = _load_groq_keys()

    keys_measured = 0
    rem_tok = 0
    rem_req = 0
    limit_tok = 0
    first_reset: Optional[str] = None
    for idx, snap in _GROQ_QUOTA.items():
        if not snap:
            continue
        keys_measured += 1
        if snap.get("remaining_tokens") is not None:
            rem_tok += snap["remaining_tokens"]
        if snap.get("remaining_requests") is not None:
            rem_req += snap["remaining_requests"]
        if snap.get("limit_tokens") is not None:
            limit_tok += snap["limit_tokens"]
        if first_reset is None and snap.get("reset_tokens"):
            first_reset = snap["reset_tokens"]

    return {
        "keys_measured":     keys_measured,
        "keys_total":        len(_GROQ_KEYS),
        "remaining_tokens":  rem_tok,
        "remaining_requests": rem_req,
        "limit_tokens":      limit_tok,
        "est_runs_left":     rem_tok // _TOKENS_PER_RUN_AVG if _TOKENS_PER_RUN_AVG > 0 else 0,
        "reset_tokens":      first_reset or "",
        "ready":             keys_measured > 0,
    }


def chat_quality(prompt: str, max_tokens: int = 800, temperature: float = 0.2) -> str:
    print(f"   🤖 [GROQ / QUALITY] requesting {max_tokens} tokens...")
    return _call_groq(prompt, max_tokens=max_tokens, temperature=temperature)


def chat_fast(prompt: str, max_tokens: int = 500, temperature: float = 0.1) -> str:
    print(f"   🤖 [GROQ / FAST] requesting {max_tokens} tokens...")
    return _call_groq(prompt, max_tokens=max_tokens, temperature=temperature)