"""
Test PII redaction and LangSmith anonymizer.

This validates:
1. redact_pii() masks emails, phones, and known name/email in free text.
2. redact_for_tracing() walks nested dicts/lists and redacts string values.
3. set_session_pii() registers per-user values that the anonymizer uses.
4. apply_tracing_consent() toggles LANGCHAIN_TRACING_V2 env var.
"""

import os
import sys
import json
from pathlib import Path

# Add project root to path so we can import agents
sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.privacy import (
    redact_pii,
    redact_for_tracing,
    set_session_pii,
    apply_tracing_consent,
)


def test_flat_string_redaction():
    """Test redact_pii on plain strings."""
    s = "Contact Rishav Singh at rishav@example.com or +44 7700 900123."
    out = redact_pii(s, candidate_name="Rishav Singh", user_email="rishav@example.com")
    assert "[CANDIDATE]" in out
    assert "[EMAIL]" in out
    assert "[PHONE]" in out
    assert "Rishav" not in out
    assert "rishav@example.com" not in out
    print("✓ Flat string redaction works")


def test_nested_state_redaction():
    """Test redact_for_tracing on a nested AgentState-like dict."""
    set_session_pii(name="Rishav Singh", email="rishav@example.com")
    state = {
        "candidate_name": "Rishav Singh",
        "user_email": "rishav@example.com",
        "cv_text": "RISHAV SINGH\nrishav@example.com | +44 7700 900123\nProduct Manager at Foo.",
        "jobs": [
            {"title": "PM", "description": "Report to Jane Doe jane@foo.com"}
        ],
        "messages": ("hello Rishav", {"role": "user", "content": "my phone is +91-9876543210"}),
        "match_score": 87,
        "trace_consent": True,
    }
    red = redact_for_tracing(state)
    # Top-level fields
    assert red["candidate_name"] == "[CANDIDATE]"
    assert red["user_email"] == "[EMAIL]"
    # Nested strings
    assert "[CANDIDATE]" in red["cv_text"]
    assert "[EMAIL]" in red["cv_text"]
    assert "[PHONE]" in red["cv_text"]
    # List of dicts
    assert "[EMAIL]" in red["jobs"][0]["description"]
    # Tuple/list mixed
    assert "[CANDIDATE]" in red["messages"][0]
    assert "[PHONE]" in red["messages"][1]["content"]
    # Non-string fields unchanged
    assert red["match_score"] == 87
    assert red["trace_consent"] is True
    print("✓ Nested state redaction works")


def test_consent_toggles_env():
    """Test apply_tracing_consent toggles LANGCHAIN_TRACING_V2."""
    # Off
    apply_tracing_consent(False)
    assert os.environ.get("LANGCHAIN_TRACING_V2") == "false"
    # On
    apply_tracing_consent(True)
    assert os.environ.get("LANGCHAIN_TRACING_V2") == "true"
    print("✓ Consent toggles LANGCHAIN_TRACING_V2")


def test_session_pii_registry():
    """Test set_session_pii updates the module-level registry."""
    set_session_pii(name="Alice Smith", email="alice@bar.com")
    state = {"candidate_name": "Alice Smith", "user_email": "alice@bar.com"}
    red = redact_for_tracing(state)
    assert red["candidate_name"] == "[CANDIDATE]"
    assert red["user_email"] == "[EMAIL]"
    print("✓ Session PII registry works")


if __name__ == "__main__":
    test_flat_string_redaction()
    test_nested_state_redaction()
    test_consent_toggles_env()
    test_session_pii_registry()
    print("\nAll redaction tests passed.")
