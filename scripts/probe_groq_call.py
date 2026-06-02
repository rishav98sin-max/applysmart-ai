"""Make ONE tiny real Groq SDK call per key; report OK + remaining quota, or the
EXACT error (shows TPM vs daily + reset). Settles whether we actually have capacity."""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")
from agents import llm_client
from groq import Groq

keys = llm_client._load_groq_keys()
print("GROQ keys:", len(keys))
ok = 0
for i, k in enumerate(keys):
    try:
        c = Groq(api_key=k)
        r = c.chat.completions.with_raw_response.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": "say hi"}],
            max_tokens=5, timeout=30,
        )
        resp = r.parse()
        h = r.headers
        print(f"  key#{i+1}: OK  reply={resp.choices[0].message.content!r}  "
              f"rem_tokens={h.get('x-ratelimit-remaining-tokens')} "
              f"rem_reqs={h.get('x-ratelimit-remaining-requests')} "
              f"reset_tokens={h.get('x-ratelimit-reset-tokens')}")
        ok += 1
    except Exception as e:
        print(f"  key#{i+1}: FAIL {type(e).__name__}: {str(e)[:240]}")
print(f"\n{ok}/{len(keys)} keys have capacity RIGHT NOW")
