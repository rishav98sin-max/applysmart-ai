"""
agents.preflight
================

Boot-time validation of config + secrets, so the app fails *immediately*
with a human-readable message instead of halfway through an agent run.

Used by the Streamlit app:

    from agents.preflight import run_preflight, PreflightError
    try:
        run_preflight(strict=True)
    except PreflightError as e:
        st.error(f"Cannot start: {e}")
        st.stop()

Also exposes `preflight_report()` which returns a structured dict the UI
can render in a diagnostics panel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from agents.runtime import secret_or_env


class PreflightError(RuntimeError):
    """Raised by `run_preflight(strict=True)` when REQUIRED checks fail."""


@dataclass
class PreflightCheck:
    """One check result."""
    key:        str
    required:   bool
    ok:         bool
    message:    str
    remediation: str = ""


@dataclass
class PreflightReport:
    """Aggregated result of all checks."""
    ok:       bool
    checks:   List[PreflightCheck] = field(default_factory=list)
    errors:   List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────
# Check definitions
# ─────────────────────────────────────────────────────────────

def _check_secret(
    key:         str,
    required:    bool,
    description: str,
    remediation: str,
) -> PreflightCheck:
    """Pass iff `key` is present in `st.secrets` or `os.environ`."""
    val = secret_or_env(key)
    present = val not in (None, "")
    if present:
        masked = ("*" * max(0, len(str(val)) - 4)) + str(val)[-4:]
        msg = f"{description} present ({masked})"
    else:
        msg = f"{description} is MISSING"
    return PreflightCheck(
        key         = key,
        required    = required,
        ok          = bool(present),
        message     = msg,
        remediation = remediation if not present else "",
    )


def _default_checks() -> List[PreflightCheck]:
    return [
        _check_secret(
            key         = "GROQ_API_KEY",
            required    = True,
            description = "Groq API key (used by planner / tailor / reviewer / cover-letter agents)",
            remediation = (
                "Add GROQ_API_KEY to your .env file locally, or to the app's "
                "Secrets section on Streamlit Cloud."
            ),
        ),
        # Resend is only needed if the user actually wants emails. Not strictly
        # required — downgrade to a warning.
        _check_secret(
            key         = "RESEND_API_KEY",
            required    = False,
            description = "Resend API key (used to email tailored CVs + cover letters)",
            remediation = (
                "Optional. Add RESEND_API_KEY to enable email sending. Without it "
                "the app still produces PDFs and lets the user download them manually."
            ),
        ),
        _check_secret(
            key         = "RESEND_FROM_EMAIL",
            required    = False,
            description = "Resend verified sender email",
            remediation = (
                "Optional. Needed alongside RESEND_API_KEY. Must be a sender address "
                "that is verified in your Resend account."
            ),
        ),
    ]


# ─────────────────────────────────────────────────────────────
# Entry points
# ─────────────────────────────────────────────────────────────

def preflight_report() -> PreflightReport:
    """Run all checks and return a structured report. Never raises."""
    checks = _default_checks()
    errors: List[str] = []
    warnings: List[str] = []
    for c in checks:
        if not c.ok and c.required:
            errors.append(f"{c.key}: {c.message}. {c.remediation}")
        elif not c.ok and not c.required:
            warnings.append(f"{c.key}: {c.message}. {c.remediation}")
    return PreflightReport(
        ok       = not errors,
        checks   = checks,
        errors   = errors,
        warnings = warnings,
    )


def run_preflight(strict: bool = True) -> PreflightReport:
    """
    Run preflight. When `strict=True`, raise `PreflightError` on any required
    check failure. Warnings are always silent at the PreflightError level —
    the UI should surface them separately.
    """
    report = preflight_report()
    if strict and not report.ok:
        raise PreflightError("  " + "\n  ".join(report.errors))
    return report
