import sys
sys.path.insert(0, r"d:\Projects\job-application-agent")
from agents.pdf_editor import extract_structure, _line_is_bold, _is_bullet

secs = extract_structure(r"d:\Projects\job-application-agent\CVs\Orignal Base CV\Shrestha Ghosh_CV.pdf")
for s in secs:
    if s["type"] in ("experience", "projects"):
        pages = set(ln["page"] for ln in s["lines"])
        print(f"SECTION type={s['type']} heading={s['heading']!r} pages={pages} n_lines={len(s['lines'])}")
        for ln in s["lines"]:
            bb = ln["bbox"]; bold = _line_is_bold(ln); bul = _is_bullet(ln["text"]); mk = ln.get("preceded_by_marker", False)
            print(f'  bold={str(bold):5s} bul={str(bul):5s} mk={str(mk):5s} x0={bb[0]:6.1f} y0={bb[1]:6.1f} pg={ln["page"]}  {ln["text"][:100]}')
        print()
