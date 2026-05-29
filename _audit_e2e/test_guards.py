"""Unit checks for the fix #2 guard changes (B1 hyphen, B2 language, trim)."""
import os
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from agents.cv_diff_tailor import (
    _looks_non_english, _proper_noun_core, _is_sentence_initial,
    _check_content_preserved, _foreign_capitalized_terms,
    _rewrite_is_safe, _trim_to_fit, _extract_fact_atoms,
)

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

print("== _looks_non_english ==")
chk("german true", _looks_non_english(
    "Verwaltung und Planung der Entwicklung von Software fuer Kunden"))
chk("english false (fact-dense, few stopwords)", not _looks_non_english(
    "Generated 200+ qualified B2B leads including retailers distributors HoReCa partners"))
chk("english false (normal)", not _looks_non_english(
    "Led a cross-functional team to ship the payments platform on time"))
chk("french true", _looks_non_english(
    "Gestion et planification du developpement des produits pour les clients"))

print("== _proper_noun_core (B1) ==")
chk("UGC-led -> '' (no glue)", _proper_noun_core("UGC-led") == "")
chk("Borecha, -> Borecha", _proper_noun_core("Borecha,") == "Borecha")
chk("AI/ML -> '' (slash)", _proper_noun_core("AI/ML") == "")
chk("Acme. -> Acme", _proper_noun_core("Acme.") == "Acme")

print("== _check_content_preserved (B1) ==")
# B1: a clean reframe that keeps 'UGC' must NOT revert on fabricated 'ugcled'.
o1 = "Drove UGC-led campaigns that lifted engagement"
r1 = "Scaled UGC campaigns, lifting engagement"
chk("UGC-led kept as UGC -> no revert", _check_content_preserved(o1, r1) is None)
# Control: dropping a genuine English proper noun still reverts.
o2 = "Authored initial PRDs for the Borecha launch"
r2 = "Wrote early specs for the launch"
chk("dropped PRD/Borecha -> revert", _check_content_preserved(o2, r2) is not None)

print("== _check_content_preserved (B2 translation) ==")
o3 = "Verwaltung und Planung der Entwicklung von Software fuer Kunden"
r3 = "Administered planning and delivery of customer software development"
chk("german source -> proper-noun arm skipped (no revert)",
    _check_content_preserved(o3, r3) is None)

print("== _foreign_capitalized_terms (B2 summary) ==")
cv_vocab = {"product", "growth", "manager", "led", "team", "the product growth"}
chk("sentence-initial 'Seeking' exempt",
    _foreign_capitalized_terms("Seeking a product role to drive growth", cv_vocab) == [])
chk("sentence-initial 'Focused' exempt",
    _foreign_capitalized_terms("Focused product manager who led growth", cv_vocab) == [])
# Control: a mid-sentence fabricated multi-word entity is still flagged.
flagged = _foreign_capitalized_terms(
    "Product manager from University of Mumbai driving growth", cv_vocab)
chk("mid-sentence fabricated entity still flagged", len(flagged) > 0)

print("== _trim_to_fit (Part A) ==")
# Realistic failure mode: LLM expands a bullet with trailing filler (facts
# stay earlier in the sentence). Trim must cut the filler and keep facts.
orig = ("Led the migration to AWS, cutting infrastructure cost by 30% "
        "for the platform team")
longrw = ("Led the complete end-to-end migration to AWS, cutting "
          "infrastructure cost by 30% for the entire platform engineering "
          "team across all global regions to drive long-term business impact")
lo, hi = max(45, round(len(orig) * 0.62)), round(len(orig) * 1.08) + 4
tr = _trim_to_fit(longrw, lo, hi, orig, len(orig))
chk("trim returns a fitting string", bool(tr) and len(tr) <= hi)
chk("trim keeps the AWS fact", bool(tr) and "AWS" in tr)
chk("trim keeps the 30% number", bool(tr) and "30%" in tr)
chk("trim passes _rewrite_is_safe", bool(tr) and _rewrite_is_safe(orig, tr, original_length=len(orig))[0])
print(f"    trimmed -> {tr!r} ({len(tr) if tr else 0}c, band {lo}-{hi})")

# Refusal case: when facts sit at the end with filler AFTER them, trimming
# would drop a fact — the guard must REFUSE (return None), not ship it.
orig2 = "Scaled revenue from 2L to 8L across India"
longrw2 = ("Scaled revenue substantially from 2L to 8L across the whole of "
           "India over a sustained multi-quarter growth period nationwide")
lo2, hi2 = max(45, round(len(orig2) * 0.62)), round(len(orig2) * 1.08) + 4
tr2 = _trim_to_fit(longrw2, lo2, hi2, orig2, len(orig2))
chk("trim REFUSES when fit would drop a trailing fact", tr2 is None)

print(f"\n{P} passed, {F} failed")
