"""
agents.runtime
==============

Per-session runtime helpers. Exists because the app needs to be safe under
Streamlit-Cloud-style multi-user deployment where every browser tab is an
independent session but they all share one process, one filesystem, one
set of env vars.

Responsibilities:

1. `session_id()` + `session_dirs()` — give each user-session a UUID and
   isolated `uploads/<uuid>/` and `outputs/<uuid>/` directories so one user
   can never read or overwrite another's CV / tailored PDFs.

2. `safe_upload_path(upload_dir, original_filename)` — sanitise an untrusted
   uploaded filename into a path inside `upload_dir`. Protects against:
     * path traversal (`../../etc/passwd.pdf`)
     * absolute paths (`/etc/passwd.pdf`)
     * Windows drive letters (`C:\\...`)
     * null bytes / control chars
     * unicode homoglyphs (NFKC-normalised)

3. `secret_or_env(key, default=None)` — read a config value from Streamlit's
   `st.secrets` if available (Streamlit Cloud), else fall back to
   `os.environ`. Returns `default` if neither source has it.

4. `LLMBudget` — a bounded counter enforcing a hard-stop after N Groq calls
   per run. Prevents runaway loops burning API credit.

None of these helpers touches the LLM — they are pure plumbing. Import them
from any module without risk.
"""

from __future__ import annotations

import contextvars
import os
import re
import time
import uuid
import shutil
import threading
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional, Tuple


# ─────────────────────────────────────────────────────────────
# Directory layout
# ─────────────────────────────────────────────────────────────
# Root of all per-session work. Defaults to ./sessions beside the app, can
# be overridden with APPLYSMART_SESSIONS_ROOT for e.g. /tmp mounts on cloud.

_DEFAULT_ROOT = os.getenv("APPLYSMART_SESSIONS_ROOT", "sessions")

# Legacy fallback for callers that invoke run_agent() without a session-scoped
# output_dir (tests, CLI scripts). The Streamlit app always passes its own
# per-session path, so this is never touched in production.
OUTPUT_DIR = os.getenv("APPLYSMART_OUTPUT_DIR", "outputs")


def session_id() -> str:
    """Return a fresh UUIDv4 hex (32 chars) for use as a session key."""
    return uuid.uuid4().hex


def session_dirs(sid: str, root: Optional[str] = None) -> Tuple[str, str]:
    """
    Return `(uploads_dir, outputs_dir)` for `sid`. Creates both.

    Directory layout:
        <root>/<sid>/uploads/
        <root>/<sid>/outputs/
    """
    if not sid or not re.fullmatch(r"[a-zA-Z0-9_\-]{8,64}", sid):
        raise ValueError(f"refusing to create session dirs for unsafe sid: {sid!r}")
    base = Path(root or _DEFAULT_ROOT).resolve() / sid
    up   = base / "uploads"
    out  = base / "outputs"
    up.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    return str(up), str(out)


def cleanup_session(sid: str, root: Optional[str] = None) -> None:
    """Best-effort deletion of a session's workdir. Safe to call repeatedly."""
    if not sid:
        return
    base = Path(root or _DEFAULT_ROOT).resolve() / sid
    try:
        if base.exists():
            shutil.rmtree(base, ignore_errors=True)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# Filename sanitisation
# ─────────────────────────────────────────────────────────────

_SAFE_NAME_RX = re.compile(r"[^A-Za-z0-9._\- ]+")
_EMAIL_RX = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_PHONE_RX = re.compile(r"(?<!\d)(?:\+?\d[\d\-\s().]{7,}\d)(?!\d)")


def _sanitise_basename(name: str, default_stem: str = "cv") -> str:
    """
    Produce a safe basename (no directory components) from `name`.

    - Strip any path components (`os.path.basename`).
    - NFKC-normalise unicode then keep only `[A-Za-z0-9._\\- ]`.
    - Collapse whitespace to `_`.
    - Fall back to `default_stem` if nothing remains.
    - Enforce `.pdf` extension regardless of input.
    """
    # Drop any path parts an attacker might have supplied.
    raw = os.path.basename(str(name or "").strip())
    # Remove null bytes + control chars, NFKC-normalise unicode.
    raw = unicodedata.normalize("NFKC", raw)
    raw = raw.replace("\x00", "").replace("\r", "").replace("\n", "")
    # Whitelist allowed chars.
    raw = _SAFE_NAME_RX.sub("_", raw)
    raw = re.sub(r"\s+", "_", raw).strip("._ ")
    # Split stem+ext, enforce .pdf.
    stem, _, _ = raw.rpartition(".")
    if not stem:
        stem = default_stem
    # Cap stem length (filesystem limits, sanity).
    stem = stem[:80] or default_stem
    return f"{stem}.pdf"


def safe_upload_path(upload_dir: str, original_filename: str) -> str:
    """
    Return an absolute path inside `upload_dir` that is guaranteed to stay
    inside `upload_dir` (no traversal) regardless of what `original_filename`
    contains.

    The file is prefixed with a short UUID so repeat uploads of the same
    filename within a session don't overwrite each other.
    """
    upload_dir_abs = str(Path(upload_dir).resolve())
    safe_base = _sanitise_basename(original_filename)
    unique = f"{uuid.uuid4().hex[:8]}_{safe_base}"
    target = str((Path(upload_dir_abs) / unique).resolve())
    # Paranoia belt-and-braces: ensure the resolved target is still under the
    # upload dir (guards against any symlink trickery).
    if os.path.commonpath([target, upload_dir_abs]) != upload_dir_abs:
        raise ValueError(f"refusing to write outside upload dir: {target!r}")
    return target


# ─────────────────────────────────────────────────────────────
# Secrets / env config
# ─────────────────────────────────────────────────────────────

def secret_or_env(key: str, default: Optional[str] = None) -> Optional[str]:
    """
    Read `key` from Streamlit's `st.secrets` first (supports Streamlit
    Cloud), then from `os.environ`, else return `default`.

    Never raises. Importing Streamlit in non-Streamlit contexts (unit tests,
    CLI scripts) silently falls back to env vars.
    """
    # Env var first when explicitly set — lets local `.env` always win.
    val = os.environ.get(key)
    if val not in (None, ""):
        return val
    # Otherwise try st.secrets.
    try:
        import streamlit as st  # local import so non-Streamlit callers don't pay
        if hasattr(st, "secrets") and key in st.secrets:   # type: ignore[attr-defined]
            v = st.secrets[key]                            # type: ignore[index]
            if v not in (None, ""):
                return str(v)
    except Exception:
        pass
    return default


# ─────────────────────────────────────────────────────────────
# Per-run LLM-call budget
# ─────────────────────────────────────────────────────────────

class BudgetExceeded(RuntimeError):
    """Raised when a run exceeds its LLM call budget."""


@dataclass
class LLMBudget:
    """
    Bounded counter for LLM calls per agent run. Thread-safe (agent may run
    multiple LLM calls in parallel tailor sections later).

    Usage:
        budget = LLMBudget(limit=30)
        budget.spend(agent="tailor")   # increments, raises BudgetExceeded at limit
        print(budget.used)             # current count
    """
    limit: int = 20
    used:  int = 0
    by_agent: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def spend(self, agent: str = "", n: int = 1) -> int:
        with self._lock:
            self.used += n
            if agent:
                self.by_agent[agent] = self.by_agent.get(agent, 0) + n
            if self.used > self.limit:
                raise BudgetExceeded(
                    f"LLM budget exhausted: {self.used}/{self.limit} calls "
                    f"(by agent: {self.by_agent})"
                )
            return self.used

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "used":     self.used,
                "limit":    self.limit,
                "remaining": max(0, self.limit - self.used),
                "by_agent": dict(self.by_agent),
            }


# ─────────────────────────────────────────────────────────────
# ContextVar-based budget tracking
# ─────────────────────────────────────────────────────────────
# The agent fires LLM calls from ~6 different modules. Rather than thread an
# LLMBudget object through every function signature, we bind one for the
# current execution context via a ContextVar. Any LLM helper can call
# `track_llm_call(agent_name)` to charge the active budget.
#
# If no budget is active (e.g. during tests), the call is a silent no-op.

_CURRENT_BUDGET: contextvars.ContextVar[Optional[LLMBudget]] = \
    contextvars.ContextVar("applysmart_llm_budget", default=None)


def track_llm_call(agent: str = "unknown", n: int = 1) -> None:
    """
    Charge the current context's LLMBudget (if any) for `n` LLM calls.

    Raises `BudgetExceeded` if the charge pushes over the limit. Callers
    should call this BEFORE invoking the LLM so a rejected budget doesn't
    incur cost.
    """
    budget = _CURRENT_BUDGET.get()
    if budget is None:
        return   # no budget in scope (tests, scripts) — silent no-op
    budget.spend(agent=agent, n=n)


def current_budget() -> Optional[LLMBudget]:
    """Return the active LLMBudget (or None)."""
    return _CURRENT_BUDGET.get()


# ─────────────────────────────────────────────────────────────
# Rate-limit guard
# ─────────────────────────────────────────────────────────────
# Groq's free tier can return "retry-after" values of 10+ minutes once
# the per-minute or daily token budget is exhausted. Blindly sleeping
# through those hangs the whole Streamlit session. Instead, we cap the
# wait: if Groq says "wait longer than MAX_RATE_LIMIT_WAIT", we raise
# BudgetExceeded and let the run abort gracefully.

MAX_RATE_LIMIT_WAIT: int = int(os.getenv("MAX_RATE_LIMIT_WAIT", "60"))


def handle_rate_limit(wait_seconds: float, agent: str = "") -> None:
    """
    Called by any LLM caller when a 429 / RateLimitError is received.

    - If `wait_seconds <= MAX_RATE_LIMIT_WAIT`: sleep through it, then return.
    - If `wait_seconds > MAX_RATE_LIMIT_WAIT`: raise `BudgetExceeded` so the
      run aborts cleanly instead of hanging for 10-35 minutes.
    """
    cap = MAX_RATE_LIMIT_WAIT
    if wait_seconds > cap:
        raise BudgetExceeded(
            f"Groq rate-limited {agent or 'agent'} for {wait_seconds:.0f}s "
            f"(cap: {cap}s). The run has been stopped to avoid a long hang. "
            f"Wait a few minutes for your Groq quota to reset, then try again."
        )
    print(f"   ⏳ {agent or 'agent'} rate limit — sleeping {wait_seconds:.0f}s "
          f"(cap {cap}s)…")
    time.sleep(wait_seconds + 1)


# ─────────────────────────────────────────────────────────────
# Crash-safe run snapshot
# ─────────────────────────────────────────────────────────────
# On every agent run (success OR crash), write a JSON file next to the
# tailored PDFs that captures inputs, final state, budget usage, and any
# exception. This is pure observability: debugging a failed run is
# miserable without it, and on Streamlit Cloud the process logs may be
# scrubbed before you can read them.

def _jsonable(obj):
    """Best-effort conversion of arbitrary objects to JSON-safe values."""
    import datetime as _dt
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (_dt.datetime, _dt.date)):
        return obj.isoformat()
    # Fall back to repr — won't round-trip but is human-readable in logs.
    return repr(obj)


def _redact_text(value: str) -> str:
    """Best-effort generic redaction for persisted snapshots."""
    if not value:
        return value
    value = _EMAIL_RX.sub("[EMAIL]", value)
    value = _PHONE_RX.sub("[PHONE]", value)
    return value


def _redact_jsonable(obj):
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, str):
        return _redact_text(obj)
    if isinstance(obj, list):
        return [_redact_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _redact_jsonable(v) for k, v in obj.items()}
    return _redact_text(repr(obj))


def save_run_snapshot(
    output_dir:   str,
    session_id:   str,
    inputs:       dict,
    state:        Optional[dict]     = None,
    exception:    Optional[BaseException] = None,
    budget:       Optional[LLMBudget] = None,
    filename:     str = "run_snapshot.json",
) -> Optional[str]:
    """
    Write a structured JSON summary of an agent run to `<output_dir>/<filename>`.

    Safe to call from a `finally` block: never raises, returns the written
    path or None. Intended for post-mortem debugging, not as a resumable
    checkpoint.

    Captured fields:
      - started_at / finished_at (ISO 8601 UTC)
      - session_id
      - inputs   : role/location/threshold/... (redacted: no CV bytes)
      - outcome  : status + counts (matched/skipped/errors/cycles)
      - budget   : LLMBudget.snapshot() if present
      - error    : {type, message, traceback} if an exception happened
    """
    import datetime as _dt
    import json as _json
    import os as _os
    import traceback as _tb

    try:
        if not output_dir:
            return None
        _os.makedirs(output_dir, exist_ok=True)
        target = _os.path.join(output_dir, filename)

        outcome: dict = {}
        if state:
            outcome = {
                "status":           state.get("status"),
                "matched":          len(state.get("matched_jobs")  or []),
                "skipped":          len(state.get("skipped_jobs")  or []),
                "errors":           list(state.get("errors")       or []),
                "steps_taken":      state.get("steps_taken"),
                "supervisor_cycles": state.get("supervisor_cycles"),
                "scrape_round":     state.get("scrape_round"),
            }

        err_info: Optional[dict] = None
        if exception is not None:
            err_info = {
                "type":      type(exception).__name__,
                "message":   str(exception),
                "traceback": "".join(
                    _tb.format_exception(type(exception), exception, exception.__traceback__)
                ),
            }

        payload = {
            "finished_at": _dt.datetime.utcnow().isoformat() + "Z",
            "session_id":  session_id,
            "inputs":      _redact_jsonable(_jsonable(inputs or {})),
            "outcome":     _redact_jsonable(_jsonable(outcome)),
            "budget":      _redact_jsonable(budget.snapshot() if budget is not None else None),
            "error":       _redact_jsonable(err_info),
        }

        with open(target, "w", encoding="utf-8") as f:
            _json.dump(payload, f, indent=2, ensure_ascii=False)
        return target
    except Exception as e:
        # Snapshotting MUST never mask the original error. Log and swallow.
        print(f"   ⚠️  save_run_snapshot failed: {e}")
        return None


@contextmanager
def llm_budget_scope(budget: Optional[LLMBudget]) -> Iterator[Optional[LLMBudget]]:
    """
    Context manager that binds `budget` as the active LLMBudget for the
    duration of the `with` block. Resets to the prior value on exit.

        with llm_budget_scope(LLMBudget(limit=30)):
            run_agent(...)           # every track_llm_call() counts here
    """
    token = _CURRENT_BUDGET.set(budget)
    try:
        yield budget
    finally:
        _CURRENT_BUDGET.reset(token)
