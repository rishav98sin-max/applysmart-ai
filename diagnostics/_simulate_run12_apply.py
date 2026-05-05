"""Simulate Run 12's LLM diff against the new outline and report what would land."""
import json, sys
sys.path.insert(0, r"d:\Projects\job-application-agent")
from agents.pdf_editor import build_outline

LANGFUSE = r"d:\Projects\job-application-agent\diagnostics\LangFuseLogsRun12.jsonl"
CV       = r"d:\Projects\job-application-agent\CVs\Orignal Base CV\Shrestha Ghosh_CV.pdf"

rows = [json.loads(l) for l in open(LANGFUSE, encoding="utf-8")]
tailor_out = json.loads([r for r in rows if r.get("name") == "cv_diff_tailor"][-1]["output"])

outline = build_outline(CV)
roles_by_hdr = {r["header"]: r for r in outline.get("roles", [])}

print("=== OUTLINE ROLES ===")
for r in outline["roles"]:
    print(f"  {r['header'][:80]!r:80s}  bullets={len(r['bullets'])}")
print()

def _norm(s: str) -> str:
    return " ".join(s.lower().split()) if s else ""

print("=== SIMULATION: applying Run 12 LLM diff against new outline ===")
total_landings = 0
total_attempted = 0
for role_key, entries in (tailor_out.get("bullets") or {}).items():
    # Tolerant match like _sanitise_diff
    rk_l = role_key.strip().lower()
    real = {h.strip().lower(): h for h in roles_by_hdr}
    match = real.get(rk_l)
    if not match:
        for h_l, h in real.items():
            if h_l.startswith(rk_l) or rk_l.startswith(h_l) or rk_l in h_l:
                match = h; break
    print(f"\nROLE strategist key: {role_key[:80]!r}")
    if not match:
        print("  -> NO MATCH against outline.")
        continue
    print(f"  -> matched outline role: {match[:80]!r}  ({len(roles_by_hdr[match]['bullets'])} bullets)")
    bullets_orig = roles_by_hdr[match]["bullets"]
    for e in entries:
        if not (isinstance(e, dict) and e.get("text")):
            continue
        idx = e["i"]
        new = e["text"].strip()
        total_attempted += 1
        if not (0 <= idx < len(bullets_orig)):
            print(f"  [#{idx}] OUT-OF-RANGE (n_bullets={len(bullets_orig)}) -> dropped")
            continue
        orig = bullets_orig[idx]
        orig_text = (orig.get("text") if isinstance(orig, dict) else str(orig)).strip()
        if _norm(orig_text) == _norm(new):
            print(f"  [#{idx}] IDENTICAL (no real rewrite)")
        else:
            total_landings += 1
            print(f"  [#{idx}] WOULD LAND:")
            print(f"      ORIG: {orig_text[:140]}")
            print(f"      NEW : {new[:140]}")

print(f"\n=== TOTALS: {total_landings} real changes / {total_attempted} attempted rewrites ===")
