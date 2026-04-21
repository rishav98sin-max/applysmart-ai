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


def _call_groq(prompt: str, max_tokens: int = 800, temperature: float = 0.2) -> str:
    global _GROQ_KEY_INDEX, _GROQ_KEYS
    model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    attempts = max(1, len(_GROQ_KEYS) if _GROQ_KEYS else 1) + 1  # try all keys once + 1 sleep
    for attempt in range(attempts):
        try:
            client = _groq_client()
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=max_tokens,
            )
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


def chat_quality(prompt: str, max_tokens: int = 800, temperature: float = 0.2) -> str:
    print(f"   🤖 [GROQ / QUALITY] requesting {max_tokens} tokens...")
    return _call_groq(prompt, max_tokens=max_tokens, temperature=temperature)


def chat_fast(prompt: str, max_tokens: int = 500, temperature: float = 0.1) -> str:
    print(f"   🤖 [GROQ / FAST] requesting {max_tokens} tokens...")
    return _call_groq(prompt, max_tokens=max_tokens, temperature=temperature)