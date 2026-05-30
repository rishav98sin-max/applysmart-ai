"""Integration check for fix #11 across ALL rebuild-mode CVs: run each through
the REAL production node with a SENTINEL identity, then extract the rendered
PDF and confirm (a) the candidate's FORM name/email won (not the LLM's
placeholder), (b) no placeholder string leaked, (c) the rebuild fab gate ran.

Reuses audit_e2e's machinery (env flags, cover-letter stubs). The three
targets all hit mode=rebuilt in run3 — Saumyadeep via the Groq LLM-reader
(parse_ok=False), so it stresses the recovery path too.
"""
import os, shutil
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
import sys, os.path as osp, contextlib

sys.path.insert(0, osp.dirname(osp.abspath(__file__)))
import audit_e2e as A   # noqa: E402  (module-level: sets flags, stubs, chdir)

TARGETS = ["Nikhil_Resume.pdf", "Nikhil_dell_cv.pdf",
           "Saumyadeep_Bhattacharjee_CV.pdf"]
SENTINEL_NAME  = "Zxqv Sentinelname"
# Valid TLD: the Typst (rendercv) renderer validates emails and rejects
# reserved TLDs like .invalid/.test, which would force a fall-through to the
# text path and mask whether structured-doc identity injection reached render.
SENTINEL_EMAIL = "sentinelqa@gmail.com"
PLACEHOLDERS   = ["Full Name", "email@example.com", "Candidate Name",
                  "linkedin.com/in/...", "Your Name"]

P = F = 0
def chk(name, cond):
    global P, F
    if cond: P += 1; print(f"  PASS  {name}")
    else:    F += 1; print(f"  FAIL  {name}")

cvs = A.discover_cvs()
for target in TARGETS:
    print("=" * 70)
    print(target)
    path = cvs.get(target)
    if not path:
        chk("CV discovered", False)
        continue

    cv_text = A.parse_cv(path)
    jt, jd, kw = A.pick_jd(cv_text)
    out_dir = osp.join(A.OUT, "ident_" + target)
    shutil.rmtree(out_dir, ignore_errors=True)   # clean slate per CV
    os.makedirs(out_dir, exist_ok=True)

    # Same state run_node builds, but with a DISTINCTIVE name/email to grep for.
    state = {
        "cv_path": path, "cv_text": cv_text, "candidate_name": SENTINEL_NAME,
        "matched_jobs": [{"company": "Acme Corp", "title": jt,
                          "description": jd, "url": "https://example.com/audit"}],
        "output_dir": out_dir, "session_id": "audit", "user_email": SENTINEL_EMAIL,
        "steps_taken": 0, "messages": [], "review_results": {}, "tailor_attempts": {},
        "style_profile": {}, "errors": [], "status": "matched",
    }

    log = osp.join(out_dir, "node.log")
    with open(log, "w", encoding="utf-8") as lf, \
            contextlib.redirect_stdout(lf), contextlib.redirect_stderr(lf):
        result = A.ja.tailor_and_generate_node(state)

    job = (result.get("matched_jobs") or [{}])[0]
    out_pdf = job.get("cv_pdf_path")
    mode = job.get("render_mode")
    rev = job.get("review") or {}

    print(f"  render_mode = {mode}")
    print(f"  review      = score={rev.get('score')} verdict={rev.get('verdict')} "
          f"rebuild_mode={rev.get('_rebuild_mode')}")
    print(f"  weaknesses  = {rev.get('weaknesses')}")
    print(f"  out_pdf     = {out_pdf}")
    print(f"  log         = {log}")

    if not (out_pdf and osp.exists(out_pdf)):
        chk("output PDF produced", False)
        continue

    doc = A.fitz.open(out_pdf)
    text = "\n".join(p.get_text() for p in doc)
    doc.close()

    chk("render mode is rebuilt", mode == "rebuilt")
    chk("sentinel NAME present in rendered PDF", SENTINEL_NAME.split()[0] in text)
    chk("sentinel EMAIL present in rendered PDF", SENTINEL_EMAIL in text)
    chk("no placeholder string leaked",
        all(ph not in text for ph in PLACEHOLDERS))
    chk("review came from the rebuild gate (_rebuild_mode)",
        rev.get("_rebuild_mode") is True)

print("=" * 70)
print(f"{P} passed, {F} failed")
