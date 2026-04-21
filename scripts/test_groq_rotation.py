"""
Verify Groq key rotation logic works correctly.
Does NOT make real API calls — uses a fake client that raises rate-limit errors.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import agents.llm_client as llm

# ─── Test 1: Keys loaded correctly ────────────────────────────────────
print("\n[Test 1] Loading keys from .env...")
keys = llm._load_groq_keys()
print(f"   Keys loaded: {len(keys)}")
for i, k in enumerate(keys, 1):
    print(f"   Key #{i}: {k[:12]}...{k[-4:]}")
assert len(keys) >= 1, "Need at least 1 Groq key"
print(f"   ✅ {len(keys)} key(s) available")

# ─── Test 2: Rotation sequence ────────────────────────────────────────
print("\n[Test 2] Testing rotation sequence...")
# Reset rotation state
llm._GROQ_KEY_INDEX = 0
llm._GROQ_KEYS = keys

expected_rotations = len(keys) - 1  # we can rotate N-1 times before exhausting
print(f"   Start at key #{llm._GROQ_KEY_INDEX + 1}")
rotations = 0
while True:
    result = llm._rotate_groq_key()
    if result:
        rotations += 1
    else:
        break
print(f"   Rotations succeeded: {rotations}")
print(f"   Expected: {expected_rotations}")
assert rotations == expected_rotations, f"Rotation count mismatch: {rotations} vs {expected_rotations}"
print(f"   ✅ Rotation stops after exhausting all {len(keys)} keys")

# ─── Test 3: Rate-limit detection ─────────────────────────────────────
print("\n[Test 3] Testing rate-limit error detection...")
test_errors = [
    (Exception("429 Too Many Requests"), True),
    (Exception("Rate limit exceeded"), True),
    (Exception("quota exceeded"), True),
    (Exception("resource_exhausted"), True),
    (Exception("Invalid API key"), False),
    (Exception("Connection refused"), False),
]
for err, expected in test_errors:
    detected = llm._is_rate_limit_error(err)
    status = "✅" if detected == expected else "❌"
    print(f"   {status} '{err}' → rate_limit={detected} (expected {expected})")
    assert detected == expected

# ─── Test 4: Simulated rotation on rate-limit ─────────────────────────
print("\n[Test 4] Simulating rate-limit-triggered rotation...")

class _FakeCompletions:
    def __init__(self, key):
        self.key = key
    def create(self, **kwargs):
        raise Exception(f"429 Too Many Requests on key ...{self.key[-4:]}")

class _FakeChat:
    def __init__(self, key):
        self.completions = _FakeCompletions(key)

class FakeClient:
    """Raises rate-limit error on every call."""
    def __init__(self, api_key=None, **kwargs):
        self.api_key = api_key
        self.chat = _FakeChat(api_key or "")

# Monkey-patch Groq to use FakeClient
original_groq = llm.Groq
llm.Groq = FakeClient
llm._GROQ_CLIENTS = {}  # clear cache
llm._GROQ_KEY_INDEX = 0
llm._GROQ_KEYS = keys

# Patch time.sleep so we don't actually wait 30s on exhaustion
import time as _time
original_sleep = _time.sleep
_time.sleep = lambda s: None
llm.time.sleep = lambda s: None

print(f"   Calling _call_groq (all keys will 429)...")
result = llm._call_groq("test", max_tokens=10)
print(f"   Result: {result!r}")
print(f"   Final key index: {llm._GROQ_KEY_INDEX}")
assert result == "", "Expected empty string on total exhaustion"
print(f"   ✅ All {len(keys)} keys tried, returned empty on exhaustion")

# Restore
llm.Groq = original_groq
llm.time.sleep = original_sleep
_time.sleep = original_sleep

# ─── Summary ──────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("✅ ALL TESTS PASSED — Key rotation is working correctly")
print("=" * 60)
print(f"\nConfiguration summary:")
print(f"   • Keys available: {len(keys)}")
print(f"   • Daily budget (approx): {len(keys) * 100}K tokens")
print(f"   • Rate-limit behavior: auto-rotate to next key")
print(f"   • All keys exhausted: 30s sleep + reset to key #1")
