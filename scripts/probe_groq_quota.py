"""Ask Groq directly which limit we hit (TPM/RPM = seconds reset, vs TPD/RPD =
daily). One tiny call per key; reads x-ratelimit-* headers + the 429 body."""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")
import json, urllib.request, urllib.error
from agents import llm_client

keys = llm_client._load_groq_keys()
print("GROQ keys loaded:", len(keys))
HDRS = ["x-ratelimit-limit-tokens", "x-ratelimit-remaining-tokens", "x-ratelimit-reset-tokens",
        "x-ratelimit-limit-requests", "x-ratelimit-remaining-requests", "x-ratelimit-reset-requests",
        "retry-after"]
for ki, key in enumerate(keys[:4]):
    body = json.dumps({"model": "llama-3.3-70b-versatile",
                       "messages": [{"role": "user", "content": "hi"}],
                       "max_tokens": 1}).encode()
    req = urllib.request.Request("https://api.groq.com/openai/v1/chat/completions",
                                 data=body,
                                 headers={"Authorization": "Bearer " + key,
                                          "Content-Type": "application/json"})
    print(f"\n--- key #{ki+1} ---")
    try:
        r = urllib.request.urlopen(req, timeout=20)
        print("  STATUS", r.status, "(OK — this key has capacity)")
        for h in HDRS:
            print("   ", h, "=", r.headers.get(h))
    except urllib.error.HTTPError as e:
        print("  STATUS", e.code)
        for h in HDRS:
            print("   ", h, "=", e.headers.get(h))
        try:
            print("  BODY:", e.read().decode()[:400])
        except Exception:
            pass
    except Exception as e:
        print("  ERR", type(e).__name__, str(e)[:120])
