"""
diagnostics.telemetry
=====================

Per-call LLM telemetry sink. Two backends, both write at the same time:

    1. Local JSONL (always on)  — diagnostics/runs/<timestamp>/traces.jsonl
       Survives even if Langfuse is unreachable / unconfigured.

    2. Langfuse  (when LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY are set)
       Pretty UI, persistent, shareable. Pure addition over JSONL.

The public API is one function:

    record_llm_call(
        agent: str,           # e.g. "cv_diff_tailor", "reviewer"
        provider: str,        # "groq" or "gemini"
        model: str,
        prompt: str,
        response: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        duration_ms: float,
        truncated: bool,
        error: str | None,
        job_id: str | None,
        metadata: dict | None,
    ) -> None

Plus two ContextVars the instrumentation layer can use to thread metadata
through the existing call chain without changing any agent module:

    current_agent       -- set by the patched track_llm_call()
    current_job_id      -- set by the batch runner / supervisor

All disk + network IO is fire-and-forget; this module never raises into
the main app. A failure to log is silent (printed to stderr) so a broken
diagnostic layer can never break a real user run.
"""

from __future__ import annotations

import contextvars
import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


def _config(key: str, default: Optional[str] = None) -> Optional[str]:
    """
    Read a config value from Streamlit `st.secrets` first (Streamlit Cloud),
    then `os.environ`, else `default`.

    Mirrors `agents.runtime.secret_or_env`. Inlined here so the diagnostics
    package stays standalone and importable even if `agents` isn't on the
    path during static analysis.
    """
    val = os.environ.get(key)
    if val not in (None, ""):
        return val
    try:
        import streamlit as st  # local import — non-Streamlit callers don't pay
        if hasattr(st, "secrets") and key in st.secrets:           # type: ignore[attr-defined]
            v = st.secrets[key]                                    # type: ignore[index]
            if v not in (None, ""):
                return str(v)
    except Exception:
        pass
    return default


# ─────────────────────────────────────────────────────────────
# ContextVars threaded through the call chain
# ─────────────────────────────────────────────────────────────
# `current_agent` is set when an agent module calls `track_llm_call(agent=...)`.
# `current_job_id` is set when a job pipeline starts (per-job grouping).
#
# Both default to None so non-instrumented call paths produce empty values
# rather than crashes.
current_agent: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "diagnostics_current_agent", default=None
)
current_job_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "diagnostics_current_job_id", default=None
)


# ─────────────────────────────────────────────────────────────
# Run directory + JSONL sink
# ─────────────────────────────────────────────────────────────

_DIAG_ROOT = Path(__file__).resolve().parent
_RUNS_DIR = _DIAG_ROOT / "runs"
_RUN_TIMESTAMP = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
_RUN_DIR = _RUNS_DIR / _RUN_TIMESTAMP
_TRACES_PATH = _RUN_DIR / "traces.jsonl"
_RUN_ID = uuid.uuid4().hex[:12]

_jsonl_lock = threading.Lock()
_jsonl_initialised = False


def _ensure_run_dir() -> None:
    global _jsonl_initialised
    if _jsonl_initialised:
        return
    try:
        _RUN_DIR.mkdir(parents=True, exist_ok=True)
        # Drop a manifest so the analyzer can find the latest run easily.
        manifest = {
            "run_id": _RUN_ID,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "diagnostics_version": "0.1.0",
            "process_pid": os.getpid(),
        }
        (_RUN_DIR / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        _jsonl_initialised = True
    except Exception as e:
        print(f"   ⚠️  diagnostics: failed to create run dir: {e}", file=sys.stderr)


def run_dir() -> Path:
    """Return the absolute path of the current run's output directory."""
    _ensure_run_dir()
    return _RUN_DIR


def run_id() -> str:
    """Return the short identifier for the current run."""
    return _RUN_ID


# ─────────────────────────────────────────────────────────────
# Langfuse client (lazy, optional)
# ─────────────────────────────────────────────────────────────

_langfuse_client: Any = None
_langfuse_attempted: bool = False
# Per-job trace_id cache: job_id → deterministic trace_id string. In Langfuse
# v4 the API is OTel-style: observations are grouped under a trace via a
# trace_context dict carrying the trace_id. We compute a stable id per
# (run_id, job_id) using `create_trace_id(seed=...)` so all generations from
# one job land under the same trace in the Langfuse UI.
_lf_trace_ids: Dict[str, str] = {}
_lf_trace_ids_lock = threading.Lock()


def _get_langfuse() -> Any:
    """
    Lazy-init the Langfuse client. Returns None if:
      - the langfuse package is not installed, OR
      - LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY are not both set.

    Never raises. A failure is logged once and then silenced.
    """
    global _langfuse_client, _langfuse_attempted
    if _langfuse_attempted:
        return _langfuse_client
    _langfuse_attempted = True

    pub = _config("LANGFUSE_PUBLIC_KEY")
    sec = _config("LANGFUSE_SECRET_KEY")
    if not (pub and sec):
        print(
            "   ℹ️  diagnostics: LANGFUSE_PUBLIC_KEY/SECRET_KEY not set — "
            "Langfuse disabled, JSONL only.",
            file=sys.stderr,
        )
        return None

    try:
        from langfuse import Langfuse  # type: ignore
        host = _config("LANGFUSE_HOST", "https://cloud.langfuse.com") or "https://cloud.langfuse.com"
        _langfuse_client = Langfuse(
            public_key=pub, secret_key=sec, host=host,
        )
        print(
            f"   ✅ diagnostics: Langfuse client initialised (host={host})",
            file=sys.stderr,
        )
    except ImportError:
        print(
            "   ⚠️  diagnostics: `langfuse` package not installed — "
            "run `pip install langfuse`. JSONL fallback active.",
            file=sys.stderr,
        )
    except Exception as e:
        print(
            f"   ⚠️  diagnostics: Langfuse init failed ({type(e).__name__}: {e}) "
            f"— JSONL fallback active.",
            file=sys.stderr,
        )
    return _langfuse_client


def _get_lf_trace_id(client: Any, job_id: Optional[str]) -> Optional[str]:
    """
    Return a deterministic Langfuse trace_id for `job_id`, generating one
    on first request and caching for subsequent calls.

    In Langfuse v4 we don't create Trace objects — we just allocate a
    trace_id and pass it into every `start_observation(trace_context=...)`.
    All observations sharing the same trace_id are grouped into one trace
    in the UI.
    """
    if client is None:
        return None
    key = job_id or "default"
    with _lf_trace_ids_lock:
        cached = _lf_trace_ids.get(key)
        if cached is not None:
            return cached
        try:
            seed = f"{_RUN_ID}:{key}"
            trace_id = client.create_trace_id(seed=seed)
            _lf_trace_ids[key] = trace_id
            return trace_id
        except Exception as e:
            print(
                f"   ⚠️  diagnostics: Langfuse create_trace_id failed: {e}",
                file=sys.stderr,
            )
            return None


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────

def record_llm_call(
    *,
    agent: str,
    provider: str,
    model: str,
    prompt: str,
    response: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    duration_ms: float = 0.0,
    truncated: bool = False,
    error: Optional[str] = None,
    job_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Persist a single LLM call to JSONL (+ Langfuse if configured).

    Fire-and-forget. Never raises into the caller — any sink failure is
    logged to stderr and swallowed so the main app's hot path is unaffected.

    The `prompt` and `response` are persisted only when
    DIAGNOSTICS_FULL_PAYLOAD=1; otherwise truncated to 500 chars each
    (head + tail) so traces stay small but still grep-friendly.
    """
    ts = datetime.now(timezone.utc).isoformat()
    full_payload = _config("DIAGNOSTICS_FULL_PAYLOAD") == "1"

    def _trim(s: str, head: int = 250, tail: int = 250) -> str:
        if not s:
            return ""
        if full_payload or len(s) <= head + tail + 32:
            return s
        return s[:head] + f"\n\n[... {len(s) - head - tail} chars elided ...]\n\n" + s[-tail:]

    prompt_for_log = _trim(prompt or "")
    response_for_log = _trim(response or "")

    record = {
        "ts": ts,
        "run_id": _RUN_ID,
        "job_id": job_id,
        "agent": agent,
        "provider": provider,
        "model": model,
        "prompt_chars": len(prompt or ""),
        "response_chars": len(response or ""),
        "prompt_tokens": int(prompt_tokens or 0),
        "completion_tokens": int(completion_tokens or 0),
        "total_tokens": int(total_tokens or 0),
        "duration_ms": round(float(duration_ms or 0.0), 2),
        "truncated": bool(truncated),
        "error": error,
        "metadata": metadata or {},
        "prompt": prompt_for_log,
        "response": response_for_log,
    }

    # ── 1. JSONL sink (always on) ──
    _ensure_run_dir()
    try:
        with _jsonl_lock:
            with open(_TRACES_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(
            f"   ⚠️  diagnostics: JSONL write failed: {e}", file=sys.stderr
        )

    # ── 2. Langfuse sink (when configured) ──
    client = _get_langfuse()
    trace_id = _get_lf_trace_id(client, job_id) if client is not None else None
    if client is not None and trace_id is not None:
        try:
            # Langfuse v4 OTel-style API: open a generation observation,
            # populate it, end it. The trace_context.trace_id groups all
            # observations from the same job under one trace in the UI.
            gen = client.start_observation(
                trace_context={"trace_id": trace_id},
                name=agent,
                as_type="generation",
                model=model,
                input=prompt if full_payload else prompt_for_log,
                output=response if full_payload else response_for_log,
                usage_details={
                    "input":  int(prompt_tokens or 0),
                    "output": int(completion_tokens or 0),
                    "total":  int(total_tokens or 0),
                },
                metadata={
                    "provider":       provider,
                    "duration_ms":    round(float(duration_ms or 0.0), 2),
                    "truncated":      bool(truncated),
                    "prompt_chars":   len(prompt or ""),
                    "response_chars": len(response or ""),
                    "run_id":         _RUN_ID,
                    "job_id":         job_id,
                    **(metadata or {}),
                },
                level="ERROR" if error else "DEFAULT",
                status_message=error or None,
            )
            # Closing immediately marks the generation complete. Without
            # this the observation stays "open" in the UI and never shows
            # final usage / output.
            try:
                gen.end()
            except Exception:
                pass
        except Exception as e:
            print(
                f"   ⚠️  diagnostics: Langfuse start_observation failed: "
                f"{type(e).__name__}: {e}",
                file=sys.stderr,
            )


def flush() -> None:
    """
    Flush any buffered Langfuse events. Call from app shutdown.
    JSONL is unbuffered (one fsync per line) so nothing to flush there.
    """
    client = _get_langfuse()
    if client is None:
        return
    try:
        client.flush()
    except Exception:
        pass
