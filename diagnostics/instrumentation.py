"""
diagnostics.instrumentation
===========================

Monkey-patches the existing LLM call chain to emit telemetry events.

Strategy:

    1. `agents.runtime.track_llm_call(agent=...)` is wrapped to additionally
       set the `current_agent` ContextVar in diagnostics.telemetry.

    2. `agents.llm_client._call_groq` and `_call_gemini` are replaced with
       timing+capture wrappers that:
         - read the existing _TOKENS_USED_SESSION counter before/after to
           measure tokens consumed by THIS call (the existing code already
           accurately deducts via SDK `usage.total_tokens` / `usage_metadata`);
         - measure wall-clock duration;
         - detect likely truncation (response length vs requested max_tokens,
           plus mid-string JSON end heuristic);
         - call diagnostics.telemetry.record_llm_call(...) to persist.

    3. No agent module is modified. Patches are applied at import time of
       this module, behind the env-flag check at the bottom of llm_client.py.

Removal: delete this file (and the entire diagnostics/ folder). Nothing
else needs to change because the hook in agents/llm_client.py is wrapped
in a try/except ImportError.

The patch is idempotent — calling patch() twice is a no-op.
"""

from __future__ import annotations

import sys
import time
from typing import Any, Callable, Optional

from diagnostics import telemetry as _t


_PATCHED: bool = False


def patch() -> None:
    """Apply all monkey-patches. Idempotent — safe to call multiple times."""
    global _PATCHED
    if _PATCHED:
        return

    _patch_track_llm_call()
    _patch_groq_caller()
    _patch_gemini_caller()
    _PATCHED = True
    print(
        "   ✅ diagnostics: instrumentation active "
        f"(run_id={_t.run_id()}, run_dir={_t.run_dir()})",
        file=sys.stderr,
    )


# ─────────────────────────────────────────────────────────────
# 1. track_llm_call → also set current_agent ContextVar
# ─────────────────────────────────────────────────────────────

def _patch_track_llm_call() -> None:
    try:
        from agents import runtime as _rt
    except ImportError:
        print(
            "   ⚠️  diagnostics: agents.runtime not importable — "
            "agent-name tagging disabled.",
            file=sys.stderr,
        )
        return

    original = _rt.track_llm_call

    def wrapped(agent: str = "unknown", n: int = 1) -> None:
        # Stamp the current agent into our telemetry contextvar so the
        # patched LLM callers see it. We use set() not a context-manager
        # because the agent name is the LAST track_llm_call before the
        # actual chat_*() invocation in every existing call site.
        _t.current_agent.set(agent or "unknown")
        return original(agent=agent, n=n)

    _rt.track_llm_call = wrapped  # type: ignore[assignment]


# ─────────────────────────────────────────────────────────────
# 2. _call_groq → measure + record
# ─────────────────────────────────────────────────────────────

def _patch_groq_caller() -> None:
    try:
        from agents import llm_client as _lc
    except ImportError:
        print(
            "   ⚠️  diagnostics: agents.llm_client not importable — "
            "Groq instrumentation disabled.",
            file=sys.stderr,
        )
        return

    original = _lc._call_groq
    model_name = lambda: __import__("os").getenv(
        "GROQ_MODEL", "llama-3.3-70b-versatile"
    )

    def _read_groq_tokens_used() -> int:
        # _get_tokens_used_session is private but stable. Defensive: any
        # exception falls back to 0 so instrumentation can't break the run.
        try:
            return int(_lc._get_tokens_used_session().get("groq", 0))
        except Exception:
            return 0

    def wrapped(prompt: str, max_tokens: int = 800, temperature: float = 0.2) -> str:
        agent = _t.current_agent.get() or "unknown"
        job_id = _t.current_job_id.get()
        before_tokens = _read_groq_tokens_used()
        t0 = time.perf_counter()
        error: Optional[str] = None
        response: str = ""
        try:
            response = original(prompt, max_tokens=max_tokens, temperature=temperature)
            return response
        except Exception as e:
            error = f"{type(e).__name__}: {str(e)[:300]}"
            raise
        finally:
            duration_ms = (time.perf_counter() - t0) * 1000.0
            after_tokens = _read_groq_tokens_used()
            tokens_this_call = max(0, after_tokens - before_tokens)
            truncated = _looks_truncated(response, max_tokens)
            try:
                _t.record_llm_call(
                    agent=agent,
                    provider="groq",
                    model=model_name(),
                    prompt=prompt,
                    response=response or "",
                    prompt_tokens=0,                    # SDK split unavailable here
                    completion_tokens=0,                # (would need deeper SDK patch)
                    total_tokens=tokens_this_call,
                    duration_ms=duration_ms,
                    truncated=truncated,
                    error=error,
                    job_id=job_id,
                    metadata={
                        "max_tokens_requested": max_tokens,
                        "temperature": temperature,
                        "last_llm_source": getattr(_lc, "_LAST_LLM_SOURCE", "?"),
                    },
                )
            except Exception as log_err:
                print(
                    f"   ⚠️  diagnostics: record_llm_call failed for groq: "
                    f"{log_err}",
                    file=sys.stderr,
                )

    _lc._call_groq = wrapped  # type: ignore[assignment]


# ─────────────────────────────────────────────────────────────
# 3. _call_gemini → measure + record
# ─────────────────────────────────────────────────────────────

def _patch_gemini_caller() -> None:
    try:
        from agents import llm_client as _lc
    except ImportError:
        return

    original = _lc._call_gemini
    gemini_model = lambda: getattr(_lc, "GEMINI_MODEL", "gemini-2.5-flash")

    def _read_gemini_tokens_used() -> int:
        try:
            return int(_lc._get_tokens_used_session().get("gemini", 0))
        except Exception:
            return 0

    def _read_groq_tokens_used() -> int:
        try:
            return int(_lc._get_tokens_used_session().get("groq", 0))
        except Exception:
            return 0

    def wrapped(prompt: str, max_tokens: int = 800, temperature: float = 0.2) -> str:
        agent = _t.current_agent.get() or "unknown"
        job_id = _t.current_job_id.get()

        # Record the BOTH counters before — _call_gemini falls back to
        # _call_groq internally, in which case the call's token usage shows
        # up under groq_used, not gemini_used. We pick the larger delta.
        before_gemini = _read_gemini_tokens_used()
        before_groq   = _read_groq_tokens_used()
        t0 = time.perf_counter()
        error: Optional[str] = None
        response: str = ""
        try:
            response = original(prompt, max_tokens=max_tokens, temperature=temperature)
            return response
        except Exception as e:
            error = f"{type(e).__name__}: {str(e)[:300]}"
            raise
        finally:
            duration_ms = (time.perf_counter() - t0) * 1000.0
            d_gemini = max(0, _read_gemini_tokens_used() - before_gemini)
            d_groq   = max(0, _read_groq_tokens_used()   - before_groq)
            # If Gemini fell back to Groq mid-call, d_groq carries the cost.
            fell_back = d_groq > 0 and d_gemini == 0
            tokens_this_call = d_gemini if not fell_back else d_groq
            provider = "groq_fallback" if fell_back else "gemini"
            model = (
                __import__("os").getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
                if fell_back
                else gemini_model()
            )
            truncated = _looks_truncated(response, max_tokens)
            try:
                _t.record_llm_call(
                    agent=agent,
                    provider=provider,
                    model=model,
                    prompt=prompt,
                    response=response or "",
                    prompt_tokens=0,
                    completion_tokens=0,
                    total_tokens=tokens_this_call,
                    duration_ms=duration_ms,
                    truncated=truncated,
                    error=error,
                    job_id=job_id,
                    metadata={
                        "max_tokens_requested": max_tokens,
                        "temperature": temperature,
                        "last_llm_source": getattr(_lc, "_LAST_LLM_SOURCE", "?"),
                        "fell_back_to_groq": fell_back,
                    },
                )
            except Exception as log_err:
                print(
                    f"   ⚠️  diagnostics: record_llm_call failed for gemini: "
                    f"{log_err}",
                    file=sys.stderr,
                )

    _lc._call_gemini = wrapped  # type: ignore[assignment]


# ─────────────────────────────────────────────────────────────
# Heuristics
# ─────────────────────────────────────────────────────────────

def _looks_truncated(response: str, max_tokens_requested: int) -> bool:
    """
    Heuristic truncation detection. We do NOT have direct access to
    `finish_reason` from the wrapped layer, so we use these signals:

      - Response is non-empty AND its length is suspiciously close to
        the requested max_tokens (≥85% of 4-chars-per-token estimate).
      - JSON-shaped responses that don't end with `}` or `]` after
        whitespace stripping.

    Either signal triggers a truncated=True flag. False positives are
    acceptable; this drives diagnosis filtering, not correctness.
    """
    if not response:
        return False
    # Approximate: 1 token ≈ 4 chars for English text. If response is
    # ≥ 85% of (max_tokens × 4) chars, the LLM most likely hit the cap.
    char_budget = max_tokens_requested * 4
    if char_budget > 0 and len(response) >= int(char_budget * 0.85):
        return True
    # Mid-string JSON truncation pattern (frequent on Gemini Flash free tier).
    stripped = response.rstrip()
    if stripped.startswith("{") and not stripped.endswith("}"):
        return True
    if stripped.startswith("[") and not stripped.endswith("]"):
        return True
    # Markdown-fenced JSON that didn't close
    if "```json" in stripped[:50] and not stripped.endswith("```"):
        return True
    return False
