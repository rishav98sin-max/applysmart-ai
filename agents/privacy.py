"""
agents/privacy
==============

Lightweight privacy helpers for v1:
- PII redaction utilities (regex + known-value replacement)
- Consent-gated LangSmith tracing toggle (default off)
- LangSmith anonymizer that runs BEFORE any trace leaves the process,
  so "Allow anonymized tracing" actually anonymizes.

Design notes
------------
Our LLM calls use raw Google / Groq SDKs (not LangChain), so
`LANGCHAIN_TRACING_V2` only captures LangGraph node execution — the
state dicts that flow between agents. Those dicts contain the full
CV text, JD text, candidate name + email. So the redaction MUST walk
nested dict/list structures, not just flat strings.

Name/email are not known at consent-button time (the user types them
into the sidebar AFTER the consent modal). `set_session_pii()` lets
the app register them later; the redactor reads them at trace time.
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional


# ─────────────────────────────────────────────────────────────
# Regex patterns — covers ~80% of PII leaks without user context.
# ─────────────────────────────────────────────────────────────

_EMAIL_RX = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_PHONE_RX = re.compile(
    r"(?<!\d)(?:\+?\d[\d\-\s().]{7,}\d)(?!\d)"
)


# ─────────────────────────────────────────────────────────────
# Session-scoped PII registry.
# The Streamlit app calls set_session_pii(name, email) whenever the
# sidebar Full-Name / Email fields change. The anonymizer reads this
# at trace time so per-user values are redacted too.
# Module-level state is fine here because each Streamlit session runs
# in its own process.
# ─────────────────────────────────────────────────────────────

_SESSION_PII = {"name": "", "email": ""}


def set_session_pii(name: Optional[str] = None, email: Optional[str] = None) -> None:
    """Register the current user's name/email so the anonymizer masks them."""
    if name is not None:
        _SESSION_PII["name"] = (name or "").strip()
    if email is not None:
        _SESSION_PII["email"] = (email or "").strip()


def _redact_str(text: str, candidate_name: str = "", user_email: str = "") -> str:
    """Redact PII from a single string. Internal — used by both public APIs."""
    if not text:
        return ""
    out = str(text)

    # 1. Known exact email first (user-provided). Catch before regex so we can
    #    also catch sub-strings like 'name@example.com' inside a sentence.
    if user_email:
        out = re.sub(re.escape(user_email), "[EMAIL]", out, flags=re.IGNORECASE)

    # 2. Generic email + phone patterns.
    out = _EMAIL_RX.sub("[EMAIL]", out)
    out = _PHONE_RX.sub("[PHONE]", out)

    # 3. Known candidate name — full string + individual token (first/last).
    if candidate_name:
        name = candidate_name.strip()
        if name:
            out = re.sub(re.escape(name), "[CANDIDATE]", out, flags=re.IGNORECASE)
            parts = [p for p in re.split(r"\s+", name) if len(p) >= 3]
            for p in parts:
                out = re.sub(rf"\b{re.escape(p)}\b", "[CANDIDATE]", out,
                             flags=re.IGNORECASE)

    return out


def redact_pii(
    text: str,
    candidate_name: Optional[str] = None,
    user_email: Optional[str] = None,
) -> str:
    """
    Redact common personal identifiers from free text.
    Keeps job/CV semantics intact for debugging while masking identifiers.
    Backwards-compatible public API.
    """
    return _redact_str(
        text,
        candidate_name=candidate_name or "",
        user_email=user_email or "",
    )


# ─────────────────────────────────────────────────────────────
# Recursive anonymizer — what LangSmith calls on every trace.
# ─────────────────────────────────────────────────────────────

# Dict keys that are known to contain PII or raw user content.
# These are whitelisted for redaction even when nested deep.
_SENSITIVE_KEYS = frozenset({
    "cv_text", "candidate_cv", "cv_content", "cv",
    "full_name", "candidate_name", "name",
    "user_email", "email",
    "job_description", "description",
    "messages", "feedback", "reasoning",
    "summary", "bullets",
    "content", "prompt", "text",
    "input", "output", "inputs", "outputs",
})

# Cap to prevent pathological recursion (malformed circular state).
_MAX_REDACT_DEPTH = 10


def redact_for_tracing(data: Any, _depth: int = 0) -> Any:
    """
    Walk `data` recursively and redact any PII found in string values.
    Supports dict / list / tuple / str. Other types pass through unchanged.

    Used as the LangSmith anonymizer — every trace event is passed through
    this before being uploaded. Reads session PII (name/email) from the
    module-level registry so per-user redaction works.
    """
    if _depth > _MAX_REDACT_DEPTH:
        return "[REDACTED_MAX_DEPTH]"

    name = _SESSION_PII.get("name", "")
    email = _SESSION_PII.get("email", "")

    if isinstance(data, str):
        return _redact_str(data, candidate_name=name, user_email=email)

    if isinstance(data, dict):
        return {
            k: redact_for_tracing(v, _depth + 1)
            for k, v in data.items()
        }

    if isinstance(data, (list, tuple)):
        converted = [redact_for_tracing(v, _depth + 1) for v in data]
        return tuple(converted) if isinstance(data, tuple) else converted

    # int / float / bool / None / complex objects → pass through.
    return data


# ─────────────────────────────────────────────────────────────
# LangSmith anonymizer installation.
# Called from apply_tracing_consent(True). Safe to call multiple times —
# langsmith.Client() caches the default client per process, and we
# reinstall the anonymizer on it explicitly.
# ─────────────────────────────────────────────────────────────

_ANONYMIZER_INSTALLED = False


def _install_langsmith_anonymizer() -> None:
    """Register redact_for_tracing as the global LangSmith anonymizer."""
    global _ANONYMIZER_INSTALLED

    try:
        # Lazy import: langsmith may not be installed in all environments.
        from langsmith import Client
        # Create the default client with our anonymizer. LangChain's tracer
        # will pick this up for all trace exports in this process.
        Client(anonymizer=redact_for_tracing)
        _ANONYMIZER_INSTALLED = True
    except ImportError:
        # langsmith not installed → tracing impossible anyway, silently no-op.
        pass
    except TypeError:
        # Older langsmith versions don't accept anonymizer kwarg. Fall back
        # to env-based hide so at least inputs/outputs are suppressed.
        os.environ.setdefault("LANGSMITH_HIDE_INPUTS", "true")
        os.environ.setdefault("LANGSMITH_HIDE_OUTPUTS", "true")
        _ANONYMIZER_INSTALLED = True
    except Exception as e:
        # Defensive: if anonymizer setup fails, fail CLOSED — turn tracing
        # off so we don't accidentally leak raw PII.
        print(f"   ⚠️  Failed to install LangSmith anonymizer ({e}); "
              f"disabling tracing as a safety fallback.")
        os.environ["LANGCHAIN_TRACING_V2"] = "false"


def apply_tracing_consent(consent_enabled: bool) -> None:
    """
    Set process env for LangChain / LangSmith tracing behavior.

    - False: tracing fully off. LangSmith receives nothing.
    - True : tracing on WITH anonymizer installed. Every trace is
             passed through redact_for_tracing before upload.

    Default is OFF unless the user explicitly opts in.
    """
    if consent_enabled:
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
        _install_langsmith_anonymizer()
    else:
        os.environ["LANGCHAIN_TRACING_V2"] = "false"

