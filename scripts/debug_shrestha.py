"""Quick debug script for Shrestha CV parsing pipeline."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.cv_parser import parse_cv
from agents.pdf_formatter_weasy import _parse_cv

t = parse_cv(r'Error SS\Shrestha Ghosh.CV_.pdf')
parsed = _parse_cv(t)

print("NAME:", repr(parsed['candidate_name']))
print("CONTACT:", parsed['contact_bits'])
print("SUMMARY:", (parsed['summary'] or '[EMPTY]')[:300])
print()
print("SECTIONS:")
for s in parsed['sections']:
    total_bullets = sum(len(r.get("bullets", [])) for r in s["roles"])
    print(f"  kind={s['kind']:<14} heading={s['heading']!r:<40} roles={len(s['roles'])} bullets_total={total_bullets}")
    for r in s['roles']:
        print(f"      role={r.get('title')!r:<50}   bullets={len(r.get('bullets',[]))}")
        for b in r.get('bullets', [])[:3]:
            print(f"        • {b[:80]}")
