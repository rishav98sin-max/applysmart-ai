#!/usr/bin/env python
"""End-to-end audit of ApplySmart CV tailoring over ALL distinct CVs.

Drives the REAL production node `tailor_and_generate_node` with the
recovery pipeline ENABLED (REPLICA_PARSE_GATE + REPLICA_LLM_READER) so we
see the best the app can currently do. Cover-letter LLM calls are stubbed
(the audit is about the CV, not the letter) to keep token cost on the
tailoring path. For each distinct CV we render the output PDF and score:

  (a) routing decision      replica (in_place) vs rebuilt vs failed, OFF vs ON
  (b) parse correctness     heuristic roles, parse-integrity score, LLM rescue
  (c) replication fidelity  output-vs-input structural similarity (replica)
  (d) content completeness  role/header survival in output (rebuild)
  (e) tailoring quality     summary changed? bullets changed? non-degenerate?
  (f) factual safety        numbers in output absent from CV+JD, review verdict
"""
import os, sys, glob, json, re, difflib, traceback, contextlib
from datetime import datetime

# ── Enable the recovery pipeline (best case) BEFORE importing agents ──
os.environ["REPLICA_PARSE_GATE"] = "1"
os.environ["REPLICA_LLM_READER"] = "1"
os.environ.setdefault("TAILOR_JOB_CONCURRENCY", "1")
os.environ["PYTHONIOENCODING"] = "utf-8"

ROOT = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(ROOT)
sys.path.insert(0, PROJ)
os.chdir(PROJ)
OUT = os.path.join(ROOT, "out")
os.makedirs(OUT, exist_ok=True)

import fitz  # PyMuPDF

# ── Stub the cover-letter LLM path (cheap, audit is about the CV) ──
import agents.cover_letter_generator as _clg
import agents.cover_letter_reviewer as _clr
import agents.job_agent as ja

def _stub_cl(*a, **k):
    return ("Dear Hiring Manager,\n\nI am writing to express my interest in this "
            "role. My background aligns well with the requirements.\n\nSincerely,\nCandidate")

def _stub_cl_review(*a, **k):
    return {"score": 85, "verdict": "accept", "feedback": "stubbed for audit",
            "fabrications": [], "strengths": [], "weaknesses": []}

_clg.generate_cover_letter = _stub_cl
ja.generate_cover_letter = _stub_cl          # job_agent imported it by-name
_clr.review_cover_letter = _stub_cl_review

from agents.cv_parser import parse_cv
from agents.pdf_editor import (
    build_outline, extract_structure, validate_parse_integrity,
    detect_replica_compatibility,
)
from agents.cv_structure_reader import read_outline_llm, llm_reader_enabled

# ──────────────────────────────────────────────────────────────────────
# Job descriptions — domain-matched so tailoring is judged fairly.
# ──────────────────────────────────────────────────────────────────────
JD_PM = ("Senior Product Manager — B2B SaaS. Own the product roadmap end to end: "
         "discovery, prioritization, and delivery. Partner with engineering, design, "
         "and go-to-market to ship features that move activation and retention. Write "
         "crisp PRDs, run experiments, define success metrics (north-star + guardrails), "
         "and synthesize qualitative user research with product analytics. Manage a "
         "backlog across multiple squads, drive stakeholder alignment, and communicate "
         "trade-offs to executives. 5+ years in product, strong SQL/data fluency, and a "
         "track record of growing adoption of a platform product.")

JD_SWE = ("Software Engineer (Backend / Data). Design, build, and operate scalable "
          "services and data pipelines in Python. Own APIs end to end, write clean "
          "tested code, and ship to cloud (AWS/GCP). Work with SQL and NoSQL stores, "
          "build ETL/streaming jobs, and collaborate with product and ML teams to put "
          "models into production. Strong CS fundamentals, distributed systems, "
          "observability, and CI/CD. 3+ years building production backend or data "
          "systems with measurable reliability and latency improvements.")

JD_GENERIC = ("Business / Operations Analyst. Drive process improvement and data-informed "
              "decisions across the organization. Build dashboards and reports, analyze "
              "performance, manage cross-functional projects to deadlines, and present "
              "recommendations to leadership. Strong Excel/SQL, stakeholder management, "
              "and clear written communication. 3+ years in analytics, operations, or "
              "consulting with a record of measurable impact.")

PM_KW = ["product", "roadmap", "stakeholder", "backlog", "prioriti", "user research",
         "go-to-market", "gtm", "prd", "north star", "north-star", "activation",
         "retention", "product manager", "discovery"]
SWE_KW = ["python", "java", "javascript", "sql", "api", "backend", "cloud", "aws",
          "gcp", "azure", "docker", "kubernetes", "machine learning", "data pipeline",
          "etl", "engineer", "software", "react", "node", "microservice", "model"]

def pick_jd(cv_text):
    t = (cv_text or "").lower()
    pm = sum(t.count(k) for k in PM_KW)
    swe = sum(t.count(k) for k in SWE_KW)
    if pm == 0 and swe == 0:
        return "Business Analyst", JD_GENERIC, {"pm": pm, "swe": swe}
    if pm >= swe:
        return "Senior Product Manager", JD_PM, {"pm": pm, "swe": swe}
    return "Software Engineer", JD_SWE, {"pm": pm, "swe": swe}

# ──────────────────────────────────────────────────────────────────────
def derive_name(cv_text, fallback):
    for ln in (cv_text or "").splitlines():
        s = ln.strip()
        if not s:
            continue
        words = s.split()
        if 1 < len(words) <= 4 and all(re.match(r"^[A-Za-z.\-']+$", w) for w in words):
            return s
        break
    return fallback

def discover_cvs():
    seen = {}
    for p in sorted(glob.glob(os.path.join(PROJ, "sessions", "*", "uploads", "*.pdf"))):
        key = os.path.basename(p).split("_", 1)[-1]
        seen.setdefault(key, p)
    return seen

def pdf_text(path):
    try:
        d = fitz.open(path)
        t = "\n".join(pg.get_text() for pg in d)
        d.close()
        return t
    except Exception as e:
        return f"<<pdf_text error: {e}>>"

def norm(s):
    return re.sub(r"\s+", " ", (s or "").lower()).strip()

def nums(s):
    return set(re.findall(r"\$?\d[\d,\.]*%?", s or ""))

# ── No-LLM structural analysis mirroring the node's outline logic ──
def analyze_parse(cv_path):
    out = {}
    try:
        secs = extract_structure(cv_path)
        pi = validate_parse_integrity(secs)
        out["parse_score"] = pi.get("score")
        out["parse_ok"] = pi.get("ok")
        out["parse_issues"] = pi.get("issues", [])[:6]
    except Exception as e:
        out["parse_error"] = f"{type(e).__name__}: {e}"
    try:
        heur = build_outline(cv_path)
    except Exception as e:
        heur = {"roles": [], "summary": ""}
        out["heur_error"] = f"{type(e).__name__}: {e}"
    out["heuristic_roles"] = len(heur.get("roles", []))
    out["heuristic_bullets"] = sum(len(r.get("bullets", [])) for r in heur.get("roles", []))
    out["original_summary"] = (heur.get("summary") or "").strip()
    rescued = None
    if not heur.get("roles") and llm_reader_enabled():
        try:
            rescued = read_outline_llm(cv_path)
        except Exception as e:
            out["llm_reader_error"] = f"{type(e).__name__}: {e}"
    if rescued and rescued.get("roles"):
        out["llm_roles"] = len(rescued["roles"])
        out["llm_bullets"] = sum(len(r.get("bullets", [])) for r in rescued["roles"])
        out["llm_source"] = rescued.get("_source")
        out["effective_outline"] = rescued
        if not out.get("original_summary"):
            out["original_summary"] = (rescued.get("summary") or "").strip()
    else:
        out["llm_roles"] = 0
        out["effective_outline"] = heur
    return out

def routing(cv_path):
    res = {}
    try:
        os.environ["REPLICA_PARSE_GATE"] = "0"
        off = detect_replica_compatibility(cv_path)
        os.environ["REPLICA_PARSE_GATE"] = "1"
        on = detect_replica_compatibility(cv_path)
        res = {
            "off_compatible": off.get("compatible"), "off_reason": off.get("reason"),
            "on_compatible": on.get("compatible"), "on_reason": on.get("reason"),
            "n_columns": on.get("n_columns"), "image_ratio": on.get("image_ratio"),
        }
    except Exception as e:
        res["routing_error"] = f"{type(e).__name__}: {e}"
    finally:
        os.environ["REPLICA_PARSE_GATE"] = "1"
    return res

def score_output(cv_path, out_pdf, parse_info, jd):
    orig = pdf_text(cv_path)
    out = pdf_text(out_pdf)
    no, nu = norm(orig), norm(out)
    res = {
        "orig_chars": len(orig), "out_chars": len(out),
        "char_ratio": round(len(out) / max(1, len(orig)), 3),
        "text_similarity": round(difflib.SequenceMatcher(None, no, nu).ratio(), 3),
    }
    osum = norm(parse_info.get("original_summary"))
    if osum and len(osum) > 40:
        res["summary_changed"] = osum not in nu
    else:
        res["summary_changed"] = None
    eff = parse_info.get("effective_outline") or {}
    obul = []
    for r in eff.get("roles", []):
        for b in r.get("bullets", []):
            tx = norm(b.get("text"))
            if len(tx) > 25:
                obul.append(tx)
    if obul:
        verbatim = sum(1 for b in obul if b in nu)
        res["orig_bullets"] = len(obul)
        res["bullets_verbatim"] = verbatim
        res["bullets_changed"] = len(obul) - verbatim
        res["bullets_changed_ratio"] = round((len(obul) - verbatim) / len(obul), 3)
    heads = [norm(r.get("header")) for r in eff.get("roles", []) if r.get("header")]
    if heads:
        present = sum(1 for h in heads if h and h[:40] in nu)
        res["headers_total"] = len(heads)
        res["headers_present"] = present
        res["header_survival"] = round(present / len(heads), 3)
    src_nums = nums(orig) | nums(jd)
    new_nums = sorted(n for n in nums(out) if n not in src_nums and len(n) > 1)
    res["new_numbers_in_output"] = new_nums[:15]
    res["n_new_numbers"] = len(new_nums)
    # tailored summary as rendered (re-parse output)
    try:
        oo = build_outline(out_pdf)
        res["tailored_summary"] = (oo.get("summary") or "").strip()[:600]
        res["tailored_first_bullets"] = [
            b.get("text", "")[:200]
            for r in oo.get("roles", [])[:1]
            for b in r.get("bullets", [])[:3]
        ]
    except Exception:
        res["tailored_summary"] = out[:600]
    return res

def run_node(cv_path, cv_text, cand, jd_title, jd_text, out_dir):
    state = {
        "cv_path": cv_path, "cv_text": cv_text, "candidate_name": cand,
        "matched_jobs": [{"company": "Acme Corp", "title": jd_title,
                          "description": jd_text, "url": "https://example.com/audit"}],
        "output_dir": out_dir, "session_id": "audit", "user_email": "audit@local",
        "steps_taken": 0, "messages": [], "review_results": {}, "tailor_attempts": {},
        "style_profile": {}, "errors": [], "status": "matched",
    }
    result = ja.tailor_and_generate_node(state)
    jobs = result.get("matched_jobs", [])
    return jobs[0] if jobs else {}

# ──────────────────────────────────────────────────────────────────────
def main():
    cvs = discover_cvs()
    print(f"Discovered {len(cvs)} distinct CV(s):")
    for k in cvs:
        print(f"  - {k}")
    records = []
    for i, (key, cv_path) in enumerate(cvs.items(), 1):
        print(f"\n{'='*70}\n[{i}/{len(cvs)}] {key}\n{'='*70}")
        rec = {"key": key, "path": cv_path}
        try:
            cv_text = parse_cv(cv_path)
        except Exception as e:
            cv_text = ""
            rec["parse_cv_error"] = f"{type(e).__name__}: {e}"
        rec["cv_text_chars"] = len(cv_text or "")
        cand = derive_name(cv_text, key.replace(".pdf", ""))
        rec["candidate_name"] = cand
        jd_title, jd_text, kw = pick_jd(cv_text)
        rec["jd_title"] = jd_title
        rec["jd_keyword_hits"] = kw

        rec["parse"] = analyze_parse(cv_path)
        rec["routing"] = routing(cv_path)

        cv_out = os.path.join(OUT, re.sub(r"[^A-Za-z0-9._-]", "_", key))
        os.makedirs(cv_out, exist_ok=True)
        log_path = os.path.join(cv_out, "node.log")
        print(f"  running node → {cv_out} (log: node.log)")
        try:
            with open(log_path, "w", encoding="utf-8") as lf:
                with contextlib.redirect_stdout(lf), contextlib.redirect_stderr(lf):
                    job = run_node(cv_path, cv_text, cand, jd_title, jd_text, cv_out)
        except Exception as e:
            rec["node_error"] = f"{type(e).__name__}: {e}"
            rec["node_traceback"] = traceback.format_exc()[-1500:]
            job = {}

        rec["render_mode"] = job.get("render_mode")
        rec["cv_pdf_path"] = job.get("cv_pdf_path")
        rec["tailor_error"] = job.get("_tailor_error")
        rev = job.get("review") or {}
        rec["review"] = {
            "score": rev.get("score"), "verdict": rev.get("verdict"),
            "feedback": (rev.get("feedback") or "")[:300],
            "fabrications": rev.get("fabrications") or rev.get("_fabrications") or [],
        }
        tcv = job.get("tailored_cv") or ""
        rec["tailored_cv_chars"] = len(tcv)

        out_pdf = job.get("cv_pdf_path")
        if out_pdf and os.path.exists(out_pdf):
            rec["score"] = score_output(cv_path, out_pdf, rec["parse"], jd_text)
        else:
            rec["score"] = {"error": "no output PDF produced"}

        # console one-liner
        s = rec["score"]
        print(f"  mode={rec['render_mode']} roles(heur={rec['parse'].get('heuristic_roles')}"
              f"/llm={rec['parse'].get('llm_roles')}) parse_ok={rec['parse'].get('parse_ok')}"
              f" review={rec['review'].get('score')}"
              f" sim={s.get('text_similarity')} sum_changed={s.get('summary_changed')}"
              f" bullets_changed={s.get('bullets_changed')}/{s.get('orig_bullets')}"
              f" new_nums={s.get('n_new_numbers')}")
        records.append(rec)

    # ── dump JSON + MD ──
    with open(os.path.join(OUT, "audit_report.json"), "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, default=str)
    write_md(records)
    print(f"\n✅ Audit complete. Report: {os.path.join(OUT, 'audit_report.md')}")

def write_md(records):
    L = []
    L.append(f"# ApplySmart End-to-End CV Audit\n")
    L.append(f"_Generated {datetime.now().isoformat(timespec='seconds')} — "
             f"flags REPLICA_PARSE_GATE=1 REPLICA_LLM_READER=1, cover-letter stubbed._\n")
    # summary table
    L.append("## Routing & scoring matrix\n")
    L.append("| CV | mode | heur→llm roles | parse_ok | OFF→ON compat | review | sim | sum chg | bullets chg | new # |")
    L.append("|----|------|----------------|----------|---------------|--------|-----|---------|-------------|-------|")
    for r in records:
        p = r.get("parse", {}); rt = r.get("routing", {}); s = r.get("score", {}); rv = r.get("review", {})
        L.append("| {k} | {m} | {hr}→{lr} | {ok} | {off}→{on} | {rev} | {sim} | {sc} | {bc}/{bt} | {nn} |".format(
            k=r["key"][:34], m=r.get("render_mode"),
            hr=p.get("heuristic_roles"), lr=p.get("llm_roles"), ok=p.get("parse_ok"),
            off=rt.get("off_compatible"), on=rt.get("on_compatible"),
            rev=rv.get("score"), sim=s.get("text_similarity"),
            sc=s.get("summary_changed"), bc=s.get("bullets_changed"), bt=s.get("orig_bullets"),
            nn=s.get("n_new_numbers")))
    L.append("")
    # per-CV detail
    for r in records:
        p = r.get("parse", {}); s = r.get("score", {}); rv = r.get("review", {})
        L.append(f"\n## {r['key']}\n")
        L.append(f"- path: `{r['path']}`")
        L.append(f"- candidate: {r.get('candidate_name')} | JD: {r.get('jd_title')} "
                 f"(kw {r.get('jd_keyword_hits')})")
        L.append(f"- render_mode: **{r.get('render_mode')}**  | review: "
                 f"{rv.get('score')} / {rv.get('verdict')}")
        if r.get("tailor_error"): L.append(f"- ⚠️ tailor_error: {r['tailor_error']}")
        if r.get("node_error"): L.append(f"- ❌ node_error: {r['node_error']}")
        L.append(f"- parse: heuristic_roles={p.get('heuristic_roles')} "
                 f"llm_roles={p.get('llm_roles')} ({p.get('llm_source')}) "
                 f"parse_score={p.get('parse_score')} ok={p.get('parse_ok')}")
        if p.get("parse_issues"): L.append(f"  - issues: {p.get('parse_issues')}")
        L.append(f"- routing OFF compatible={r.get('routing',{}).get('off_compatible')} "
                 f"({r.get('routing',{}).get('off_reason')}) → "
                 f"ON compatible={r.get('routing',{}).get('on_compatible')} "
                 f"({r.get('routing',{}).get('on_reason')})")
        L.append(f"- fidelity: text_similarity={s.get('text_similarity')} "
                 f"char_ratio={s.get('char_ratio')} header_survival={s.get('header_survival')} "
                 f"({s.get('headers_present')}/{s.get('headers_total')})")
        L.append(f"- tailoring: summary_changed={s.get('summary_changed')} "
                 f"bullets_changed={s.get('bullets_changed')}/{s.get('orig_bullets')} "
                 f"(ratio {s.get('bullets_changed_ratio')})")
        L.append(f"- factual: new_numbers={s.get('n_new_numbers')} {s.get('new_numbers_in_output')} "
                 f"| fabrications={rv.get('fabrications')}")
        L.append(f"- review feedback: {rv.get('feedback')}")
        L.append(f"\n**Original summary:**\n\n> {(p.get('original_summary') or '(none)')[:700]}\n")
        L.append(f"**Tailored summary (rendered):**\n\n> {(s.get('tailored_summary') or '(none)')[:700]}\n")
        tb = s.get("tailored_first_bullets") or []
        if tb:
            L.append("**Tailored first-role bullets (rendered):**\n")
            for b in tb:
                L.append(f"- {b}")
        L.append(f"\n- output PDF: `{r.get('cv_pdf_path')}`")
    with open(os.path.join(OUT, "audit_report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))

if __name__ == "__main__":
    main()
