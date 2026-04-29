"""
diagnostics
===========

Per-call LLM telemetry layer. Fully isolated from the main app:

    diagnostics/                  ← this folder
    agents/llm_client.py          ← +5-line conditional hook (deletable)

Activated only when DIAGNOSTICS_ENABLED=1 is set in the environment.
When disabled, this package is never imported and has zero runtime cost.

To remove diagnostics entirely later:
    1. rm -rf diagnostics/
    2. Delete the "Diagnostics hook" block at the bottom of
       agents/llm_client.py
    3. Remove `langfuse` from requirements.txt
    4. Remove DIAGNOSTICS_* and LANGFUSE_* env vars from .env

See diagnostics/README.md for setup and usage.
"""

__version__ = "0.1.0"
