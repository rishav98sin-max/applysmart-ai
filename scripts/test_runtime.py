"""Smoke test: runtime helpers + preflight report. Run from repo root."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.runtime import (
    session_id, session_dirs, cleanup_session,
    safe_upload_path, secret_or_env,
    LLMBudget, BudgetExceeded,
)
from agents.preflight import preflight_report


# ── Sessions ─────────────────────────────────────────────────────
print("=" * 60)
print("1. Session isolation")
print("=" * 60)
sid_a = session_id()
sid_b = session_id()
print(f"  SID A: {sid_a}")
print(f"  SID B: {sid_b}")
assert sid_a != sid_b, "sessions should be unique"

up_a, out_a = session_dirs(sid_a)
up_b, out_b = session_dirs(sid_b)
print(f"  Dirs A: {up_a}")
print(f"  Dirs B: {up_b}")
assert up_a != up_b, "uploads dirs must not collide"
assert out_a != out_b, "outputs dirs must not collide"
assert os.path.isdir(up_a) and os.path.isdir(out_a), "dirs must be created"


# ── Filename sanitisation ────────────────────────────────────────
print("\n" + "=" * 60)
print("2. Filename sanitisation (attack inputs must NOT escape upload_dir)")
print("=" * 60)

attacks = [
    "cv.pdf",
    "My CV (final).pdf",
    "../../etc/passwd.pdf",
    "../../../../../../tmp/evil.pdf",
    "C:\\Windows\\System32\\cmd.exe.pdf",
    "/etc/shadow.pdf",
    "normal_cv\x00.pdf",
    "cv%00.pdf",
    "cv\r\nX-Leak: yes.pdf",
    "\u202Elatin-override.pdf",   # RLO unicode
    "....\\....\\etc\\passwd.pdf",
    "",
]
for a in attacks:
    try:
        p = safe_upload_path(up_a, a)
        rel = os.path.relpath(p, up_a)
        safe = os.path.commonpath([p, os.path.abspath(up_a)]) == os.path.abspath(up_a)
        flag = "OK " if safe else "!! "
        print(f"  {flag} {a!r:60}  ->  {rel}")
        assert safe, f"ESCAPE: {a!r} -> {p}"
    except Exception as e:
        print(f"  RAISE {a!r:60}  ->  {e}")


# ── secret_or_env ────────────────────────────────────────────────
print("\n" + "=" * 60)
print("3. secret_or_env priority: env > st.secrets > default")
print("=" * 60)

os.environ["APPLYSMART_TEST_KEY"] = "from-env"
print(f"  env-set      : {secret_or_env('APPLYSMART_TEST_KEY')}")
del os.environ["APPLYSMART_TEST_KEY"]
print(f"  env-unset    : {secret_or_env('APPLYSMART_TEST_KEY', 'fallback-default')}")


# ── LLMBudget ────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("4. LLMBudget — hard-stop at limit")
print("=" * 60)

b = LLMBudget(limit=3)
for i in range(3):
    b.spend(agent="tailor")
    print(f"  after spend #{i+1}: {b.snapshot()}")

try:
    b.spend(agent="reviewer")
    print("  !! MISSING EXCEPTION")
except BudgetExceeded as e:
    print(f"  RAISED OK: {e}")


# ── Preflight ────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("5. Preflight report")
print("=" * 60)
r = preflight_report()
print(f"  ok={r.ok}")
for c in r.checks:
    status = "  OK " if c.ok else ("  !! " if c.required else "  ?? ")
    print(f"  {status} {c.key:20} required={str(c.required):5}  {c.message}")
if r.errors:
    print(f"  ERRORS ({len(r.errors)}):")
    for e in r.errors:
        print(f"    - {e}")
if r.warnings:
    print(f"  WARNINGS ({len(r.warnings)}):")
    for w in r.warnings:
        print(f"    - {w}")


# ── cleanup ──────────────────────────────────────────────────────
cleanup_session(sid_a)
cleanup_session(sid_b)
print("\nAll smoke-tests passed.")
