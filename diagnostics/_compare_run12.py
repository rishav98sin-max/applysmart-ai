import json, sys, os
sys.path.insert(0, r"d:\Projects\job-application-agent")
from agents.pdf_editor import build_outline

p=r"d:\Projects\job-application-agent\diagnostics\LangFuseLogsRun12.jsonl"
rows=[json.loads(l) for l in open(p,encoding="utf-8")]
tailor_calls = [r for r in rows if r.get("name")=="cv_diff_tailor"]
d = json.loads(tailor_calls[-1]["output"])

outline = build_outline(r"d:\Projects\job-application-agent\CVs\Orignal Base CV\Shrestha Ghosh_CV.pdf")
roles_by_hdr = {r["header"]: r for r in outline.get("roles", [])}

def diff_words(a: str, b: str):
    aw = set(a.lower().split()); bw = set(b.lower().split())
    return sorted(bw - aw), sorted(aw - bw)

for role, entries in (d.get("bullets") or {}).items():
    role_obj = roles_by_hdr.get(role)
    print(f"\n=== ROLE: {role[:90]} ===")
    if not role_obj:
        # try fuzzy
        for hdr, r in roles_by_hdr.items():
            if hdr and (hdr.lower().startswith(role.lower()[:30]) or role.lower().startswith(hdr.lower()[:30])):
                role_obj = r; break
    bullets_orig = (role_obj or {}).get("bullets", [])
    for e in entries:
        if not (isinstance(e, dict) and e.get("text")):
            continue
        idx = e["i"]
        new = e["text"].strip()
        orig = bullets_orig[idx] if idx < len(bullets_orig) else "<missing>"
        orig_s = (orig.get("text") if isinstance(orig, dict) else str(orig)).strip()
        if new == orig_s:
            verdict = "IDENTICAL (no real rewrite)"
        else:
            added, removed = diff_words(orig_s, new)
            verdict = f"DIFFERENT  +{len(added)} words / -{len(removed)} words"
        print(f"\n  [#{idx}] {verdict}")
        print(f"    ORIG : {orig_s[:240]}")
        print(f"    NEW  : {new[:240]}")
