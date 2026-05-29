"""Targeted re-run of the two CVs fix #2 must repair, end-to-end through the
REAL production node (replica path). Reuses audit_e2e's machinery (env flags,
cover-letter stubs, scoring). Prints the before/after-relevant one-liner and
points at each node.log so we can confirm the guard/trim reverts are gone.
"""
import os
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
import sys, os.path as osp, contextlib

sys.path.insert(0, osp.dirname(osp.abspath(__file__)))
import audit_e2e as A   # noqa: E402  (module-level: sets flags, stubs, chdir)

TARGETS = ["DebayudhRoy_Resume.pdf", "CV_de.pdf"]
cvs = A.discover_cvs()

for key in TARGETS:
    path = cvs.get(key)
    print("=" * 70)
    print(key)
    if not path:
        print("  NOT FOUND")
        continue
    cv_text = A.parse_cv(path)
    cand = A.derive_name(cv_text, key.replace(".pdf", ""))
    jt, jd, kw = A.pick_jd(cv_text)
    parse = A.analyze_parse(path)
    out_dir = osp.join(A.OUT, "fix2_" + key)
    os.makedirs(out_dir, exist_ok=True)
    log = osp.join(out_dir, "node.log")
    with open(log, "w", encoding="utf-8") as lf, \
            contextlib.redirect_stdout(lf), contextlib.redirect_stderr(lf):
        job = A.run_node(path, cv_text, cand, jt, jd, out_dir)
    out_pdf = job.get("cv_pdf_path")
    s = A.score_output(path, out_pdf, parse, jd) if out_pdf and osp.exists(out_pdf) else {}
    rev = job.get("review") or {}
    print(f"  mode={job.get('render_mode')} review={rev.get('score')} "
          f"sim={s.get('text_similarity')} sum_changed={s.get('summary_changed')} "
          f"bullets_changed={s.get('bullets_changed')}/{s.get('orig_bullets')} "
          f"new_nums={s.get('n_new_numbers')}")
    print(f"  log: {log}")

print("=" * 70)
print("done")
