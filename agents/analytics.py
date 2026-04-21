"""
agents.analytics
================

Lightweight Mixpanel tracking (optional, fail-safe, privacy-conscious).
If MIXPANEL_TOKEN is not configured, all tracking calls no-op.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests

from agents.runtime import secret_or_env


MIXPANEL_TOKEN = (
    secret_or_env("MIXPANEL_TOKEN")
    or secret_or_env("MIXPANEL_PROJECT_TOKEN")
    or ""
).strip()

# Mixpanel project region. Set MIXPANEL_REGION=EU in .env if you chose
# EU data residency when creating the project — otherwise events are
# silently dropped by the US endpoint for EU projects.
_MIXPANEL_REGION = (secret_or_env("MIXPANEL_REGION") or "US").strip().upper()
_MIXPANEL_TRACK_URL = (
    "https://api-eu.mixpanel.com/track"
    if _MIXPANEL_REGION == "EU"
    else "https://api.mixpanel.com/track"
)


def analytics_enabled() -> bool:
    return bool(MIXPANEL_TOKEN)


def distinct_id(
    session_id: str,
    user_email: Optional[str] = None,
) -> str:
    """
    Build a stable, privacy-safe distinct id.
    - Uses hashed email when available.
    - Falls back to session id otherwise.
    """
    if user_email and "@" in user_email:
        h = hashlib.sha256(user_email.strip().lower().encode("utf-8")).hexdigest()[:20]
        return f"user_{h}"
    return f"session_{session_id}"


def _safe_props(props: Dict[str, Any]) -> Dict[str, Any]:
    # Keep payload simple + avoid nested huge objects.
    out: Dict[str, Any] = {}
    for k, v in (props or {}).items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, (list, tuple)):
            out[k] = [x for x in v if isinstance(x, (str, int, float, bool))]
        else:
            out[k] = str(v)
    return out


def track_event(
    event: str,
    distinct_id_value: str,
    props: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Send a Mixpanel event. Never raises (safe to call from UI flow).
    """
    if not MIXPANEL_TOKEN:
        return

    payload = [{
        "event": event,
        "properties": {
            "token": MIXPANEL_TOKEN,
            "distinct_id": distinct_id_value,
            "time": int(datetime.now(tz=timezone.utc).timestamp()),
            **_safe_props(props or {}),
        },
    }]

    try:
        requests.post(
            _MIXPANEL_TRACK_URL,
            json=payload,
            timeout=4,
        )
    except Exception:
        # Analytics must never break product flow.
        pass

