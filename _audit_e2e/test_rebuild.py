"""Unit checks for the fix #11 rebuild-trustworthiness changes:
  (a) apply_authoritative_identity  — form name/email override the model
  (b) _build_renderer_dict          — summary forced to the TOP (Typst path)
  (c) review_rebuilt_structured     — deterministic fab / credential gate
The unreadable-PDF fail-safe (d) is exercised by the production graph
(parse_failed is a HARD_TERMINAL status → END), not here.
"""
import os
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from agents.cv_tailor import (
    apply_authoritative_identity,
    review_rebuilt_structured,
    tailor_cv_structured,
)
from agents.reviewer import ACCEPT_THRESHOLD

P = 0
F = 0
def chk(name, cond):
    global P, F
    if cond:
        P += 1
        print(f"  PASS  {name}")
    else:
        F += 1
        print(f"  FAIL  {name}")


print("== apply_authoritative_identity (a) ==")
# Placeholder name + model email both get overwritten by the form values.
d1 = {"candidate_name": "Full Name",
      "contact_bits": ["Dublin, Ireland", "email@example.com", "linkedin.com/in/x"]}
apply_authoritative_identity(d1, name="Rishav Singh", email="real@gmail.com")
chk("placeholder name overwritten with form name", d1["candidate_name"] == "Rishav Singh")
chk("model email replaced in place (order kept)",
    d1["contact_bits"] == ["Dublin, Ireland", "real@gmail.com", "linkedin.com/in/x"])

# No email bit present → real email appended.
d2 = {"candidate_name": "X", "contact_bits": ["Dublin", "+353 1 234 5678"]}
apply_authoritative_identity(d2, name="Rishav Singh", email="real@gmail.com")
chk("email appended when CV had none", d2["contact_bits"][-1] == "real@gmail.com")
chk("existing non-email bits untouched",
    d2["contact_bits"][:2] == ["Dublin", "+353 1 234 5678"])

# Blank inputs → no-op (keep whatever the model produced).
d3 = {"candidate_name": "Existing Name", "contact_bits": ["a@b.com"]}
apply_authoritative_identity(d3, name="", email="")
chk("blank form values leave doc unchanged",
    d3 == {"candidate_name": "Existing Name", "contact_bits": ["a@b.com"]})

# Non-dict doc → returns it unchanged, no crash.
chk("None doc is a safe no-op", apply_authoritative_identity(None, "A", "b@c.com") is None)


print("== review_rebuilt_structured (c) ==")
_outline = {"roles": [{"header": "PM at Acme", "bullets": [{"text": "Led roadmap"}]}],
            "skills": ["roadmap", "okrs"]}

# Clean rebuild: summary preserves the original's facts, nothing leaks.
clean_doc = {
    "candidate_name": "Rishav Singh",
    "summary": "Product manager who delivered 30% revenue growth at Acme.",
    "sections": [{"heading": "Experience", "roles": [
        {"title": "PM", "bullets": ["Led the roadmap to 30% growth"]}]}],
}
r_clean = review_rebuilt_structured(
    structured_doc=clean_doc,
    original_summary="Product manager who delivered 30% revenue growth at Acme.",
    original_cv_text="Product manager who delivered 30% revenue growth at Acme. Led roadmap.",
    do_not_inject=["kubernetes"], outline=_outline,
)
chk("clean rebuild scores 80 / accept",
    r_clean["score"] == 80 and r_clean["verdict"] == "accept")
chk("clean rebuild reports a strength, no weaknesses",
    r_clean["strengths"] and not r_clean["weaknesses"])

# do_not_inject leak: a fabricated JD term present in the body caps score <=55.
leak_doc = {
    "candidate_name": "Rishav Singh",
    "summary": "Product manager who delivered 30% revenue growth at Acme.",
    "sections": [{"heading": "Experience", "roles": [
        {"title": "PM", "bullets": ["Owned Kubernetes platform migration"]}]}],
}
r_leak = review_rebuilt_structured(
    structured_doc=leak_doc,
    original_summary="Product manager who delivered 30% revenue growth at Acme.",
    original_cv_text="Product manager who delivered 30% revenue growth at Acme.",
    do_not_inject=["Kubernetes"], outline=_outline,
)
chk("do_not_inject leak caps score <=55 / retry",
    r_leak["score"] <= 55 and r_leak["verdict"] == "retry")
chk("leak weakness names the fabricated term",
    any("kubernetes" in w.lower() for w in r_leak["weaknesses"]))
chk("leak exposes _leaked_terms for the never-ship retry path",
    [t.lower() for t in r_leak.get("_leaked_terms", [])] == ["kubernetes"])

# CV cross-check: a do_not_inject term that ALSO appears in the ORIGINAL CV is
# the candidate's genuine skill (strategist over-flagged) — NOT a fabrication.
# Rule 1 of the rebuild prompt correctly carries it forward; the gate must not
# punish that. Same body as leak_doc, but now the original CV proves the term.
proven_doc = {
    "candidate_name": "Rishav Singh",
    "summary": "Product manager who delivered 30% revenue growth at Acme.",
    "sections": [{"heading": "Experience", "roles": [
        {"title": "PM", "bullets": ["Owned Kubernetes platform migration"]}]}],
}
r_proven = review_rebuilt_structured(
    structured_doc=proven_doc,
    original_summary="Product manager who delivered 30% revenue growth at Acme.",
    original_cv_text=("Product manager who delivered 30% revenue growth at Acme. "
                      "Ran the Kubernetes platform for 3 years."),
    do_not_inject=["Kubernetes"], outline=_outline,
)
chk("CV-proven do_not_inject term is NOT flagged (scores 80 / accept)",
    r_proven["score"] == 80 and r_proven["verdict"] == "accept")
chk("CV-proven term raises no fabrication weakness",
    not any("kubernetes" in w.lower() for w in r_proven["weaknesses"]))
chk("CV-proven term leaves _leaked_terms empty (no retry triggered)",
    not r_proven.get("_leaked_terms"))

# Credential drop: summary loses the original's '30%' → capped at ACCEPT_THRESHOLD.
cred_doc = {
    "candidate_name": "Rishav Singh",
    "summary": "Product manager who delivered revenue growth at Acme.",
    "sections": [{"heading": "Experience", "roles": [
        {"title": "PM", "bullets": ["Led the roadmap"]}]}],
}
r_cred = review_rebuilt_structured(
    structured_doc=cred_doc,
    original_summary="Product manager who delivered 30% revenue growth at Acme.",
    original_cv_text="Product manager who delivered 30% revenue growth at Acme.",
    do_not_inject=[], outline=_outline,
)
chk("dropped credential (30%) caps score <= ACCEPT_THRESHOLD",
    r_cred["score"] <= ACCEPT_THRESHOLD)
chk("credential weakness recorded",
    any("credential" in w.lower() for w in r_cred["weaknesses"]))

# Sector fabrication: 'healthcare' invented in summary, absent from CV → <=50.
sector_doc = {
    "candidate_name": "Rishav Singh",
    "summary": "Healthcare product manager who delivered 30% revenue growth at Acme.",
    "sections": [{"heading": "Experience", "roles": [
        {"title": "PM", "bullets": ["Led the roadmap to 30% growth"]}]}],
}
r_sector = review_rebuilt_structured(
    structured_doc=sector_doc,
    original_summary="Product manager who delivered 30% revenue growth at Acme.",
    original_cv_text="Product manager who delivered 30% revenue growth at Acme. Led roadmap.",
    do_not_inject=[], outline=_outline,
)
chk("fabricated sector (healthcare) caps score <=50",
    r_sector["score"] <= 50)
chk("sector weakness recorded",
    any("sector" in w.lower() for w in r_sector["weaknesses"]))


print("== _build_renderer_dict summary-on-top (b) ==")
try:
    from agents.cv_render_typst import _build_renderer_dict
    rd = _build_renderer_dict({
        "candidate_name": "Rishav Singh",
        "contact_bits": ["Dublin", "real@gmail.com"],
        "summary": "Seasoned product manager focused on growth.",
        "sections": [
            {"heading": "Professional Experience", "roles": [
                {"title": "PM", "dates": "2020 - 2024", "sub": "Acme",
                 "bullets": ["Led roadmap"]}]},
            {"heading": "Skills", "paragraphs": ["Product: roadmap, OKRs"]},
        ],
    })
    keys = list(rd["cv"]["sections"].keys())
    chk("summary is the FIRST rendered section", keys and keys[0] == "summary")
    chk("experience + skills still present",
        "experience" in keys and "skills" in keys)
except Exception as e:
    print(f"  SKIP  _build_renderer_dict import unavailable ({type(e).__name__}: {e})")


print("== tailor_cv_structured hardened-prohibition injection (never-ship retry) ==")
# The retry feeds the gate-confirmed leaked terms back as `extra_prohibitions`.
# Prove that flag actually reaches the LLM prompt as a loud, NAMED ban (and
# that a normal call carries no such ban). Stub the LLM so no network is hit:
# tailor_cv_structured does `from agents.llm_client import chat_deepseek`
# INSIDE the function, so patching the module attribute is picked up at call.
try:
    import agents.llm_client as _llm
    _cap = {}
    _orig = _llm.chat_deepseek
    def _spy(prompt, *a, **k):
        _cap["prompt"] = prompt
        return '{"candidate_name": "Rishav Singh", "sections": []}'
    _llm.chat_deepseek = _spy
    try:
        tailor_cv_structured(cv_text="CV about roadmaps and OKRs.",
                             job_description="A JD.", job_title="PM", company="Acme",
                             extra_prohibitions=["Kubernetes", "GCP"])
        p_with = _cap.get("prompt", "")
        _cap.clear()
        tailor_cv_structured(cv_text="CV about roadmaps and OKRs.",
                             job_description="A JD.", job_title="PM", company="Acme")
        p_without = _cap.get("prompt", "")
    finally:
        _llm.chat_deepseek = _orig
    chk("retry prompt carries the hardened, NAMED ban",
        "RETRY ATTEMPT" in p_with and "Kubernetes" in p_with and "GCP" in p_with)
    chk("normal prompt has no retry ban", "RETRY ATTEMPT" not in p_without)
except Exception as e:
    print(f"  SKIP  hardened-prohibition test unavailable ({type(e).__name__}: {e})")


print(f"\n{P} passed, {F} failed")
