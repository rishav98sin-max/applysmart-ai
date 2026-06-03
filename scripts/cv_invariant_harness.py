"""
CV invariant harness — metamorphic / property-based testing, NO ground truth.

The launch fear is "must not break on a stranger's CV." That's a SAFETY
property, checkable on ANY real CV without a human answer key. For each CV we
run the LLM-PRIMARY parse + the real in-place apply (with a cheap deterministic
verb-swap edit instead of the costly LLM tailor — we're testing parse+apply
robustness, not tailoring quality) and assert invariants that must hold for
EVERY CV:

  no-crash · pages preserved · text not lost · borders preserved ·
  headers UNTOUCHED (company/title never altered) · edits landed

Designer / 2-col / scanned CVs are expected to route to REBUILD — for those
the only invariant is "routes correctly, no crash" (replica is skipped).

Usage:
    venv\\Scripts\\python.exe scripts\\cv_invariant_harness.py <dir-or-glob> [...]
    venv\\Scripts\\python.exe scripts\\cv_invariant_harness.py            # defaults to CVs/**.pdf
"""
from __future__ import annotations
import glob as _glob
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass
os.environ.setdefault("REPLICA_LLM_PRIMARY", "1")
os.environ.setdefault("CV_READER_SAMPLES", "3")   # consensus, matches prod wiring

import fitz                                                # type: ignore
import agents.pdf_editor as pe                             # type: ignore
from agents.cv_structure_reader import read_outline_llm      # type: ignore

OUT = ROOT / "_r-adapt-render" / "invariant"
OUT.mkdir(parents=True, exist_ok=True)


def _norm(t):
    t = (t or "").lower()
    for a, b in [("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"'),
                 ("–", "-"), ("—", "-"), ("•", " "), ("▪", " "), ("○", " ")]:
        t = t.replace(a, b)
    return re.sub(r"\s+", " ", t).strip()


def _stats(path):
    doc = fitz.open(path)
    pages = doc.page_count
    text, draws = "", 0
    for p in doc:
        text += p.get_text("text")
        try:
            draws += len(p.get_drawings())
        except Exception:
            pass
    doc.close()
    return pages, text, draws


def _synthetic_diff(outline):
    """Cheap, deterministic, clearly-different, length-bounded edits (verb-swap
    on the first ≤2 bullets per role) — exercises redaction+rewrite without any
    LLM cost. Summary left untouched."""
    bullets = {}
    for r in outline.get("roles", []):
        hdr = r.get("header", "")
        edits = []
        for i, b in enumerate(r.get("bullets") or []):
            if i >= 2:
                break
            t = (b.get("text") or "").strip()
            if len(t) < 20:
                continue
            w = t.split()
            verb = "Drove" if w and w[0].lower().startswith("spearhead") else "Spearheaded"
            variant = (verb + " " + " ".join(w[1:])) if len(w) > 1 else t
            if len(variant) > len(t):
                variant = variant[:len(t)]
            edits.append({"i": i, "text": variant})
        if edits:
            bullets[hdr] = edits
    return {"summary": "", "bullets": bullets}


def _collect(args):
    files = []
    pats = args or [str(ROOT / "CVs" / "**" / "*.pdf")]
    for pat in pats:
        p = Path(pat)
        if p.is_dir():
            files += _glob.glob(str(p / "**" / "*.pdf"), recursive=True)
        else:
            files += _glob.glob(pat, recursive=True)
    # drop our own tailored outputs
    return sorted({f for f in files
                   if not os.path.basename(f).startswith(("CV_", "CoverLetter_", "render_"))})


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    heuristic_mode = "--heuristic" in sys.argv
    files = _collect([a for a in sys.argv[1:] if a != "--heuristic"])
    print(f"invariant harness: {len(files)} CV(s)  "
          f"mode={'HEURISTIC' if heuristic_mode else 'LLM-PRIMARY'}\n" + "=" * 80)

    rows = []
    for cv in files:
        name = os.path.basename(cv)
        rec = {"cv": name, "verdict": "", "invariants": {}}
        try:
            compat = pe.detect_replica_compatibility(cv)
        except Exception as e:
            rec["verdict"] = f"compat-crash:{type(e).__name__}"
            rows.append(rec); print(f"  ❌ {name[:48]:48} {rec['verdict']}"); continue

        if not compat.get("compatible", True):
            rec["verdict"] = f"rebuild-route ({compat.get('reason')})"
            rec["invariants"] = {"routes_ok": True}
            rows.append(rec); print(f"  ➜  {name[:48]:48} {rec['verdict']}"); continue

        # Parse: LLM-primary (consensus) unless --heuristic comparison mode.
        used_llm = False
        if heuristic_mode:
            try:
                outline = pe.build_outline(cv)
            except Exception as e:
                rec["verdict"] = f"parse-crash:{type(e).__name__}"
                rows.append(rec); print(f"  ❌ {name[:48]:48} {rec['verdict']}"); continue
        else:
            try:
                outline = read_outline_llm(cv, n_samples=3)
            except Exception as e:
                outline = None
                rec["reader_error"] = f"{type(e).__name__}: {e}"
            used_llm = bool(outline and outline.get("roles") and outline.get("_geometry"))
            if not used_llm:
                try:
                    outline = pe.build_outline(cv)
                except Exception as e:
                    rec["verdict"] = f"parse-crash:{type(e).__name__}"
                    rows.append(rec); print(f"  ❌ {name[:48]:48} {rec['verdict']}"); continue
        geo = outline.get("_geometry") if used_llm else None

        diff = _synthetic_diff(outline)
        out_pdf = str(OUT / f"inv_{re.sub(r'[^A-Za-z0-9]+','_',name)}.pdf")
        crashed = None
        try:
            pe.apply_edits(cv, diff, out_pdf, structure_override=geo)
        except Exception as e:
            crashed = f"{type(e).__name__}: {e}"

        ip, it, idr = _stats(cv)
        if crashed or not os.path.exists(out_pdf):
            inv = {"no_crash": False}
            rec.update(verdict=f"apply-crash:{crashed}", invariants=inv, used_llm=used_llm)
            rows.append(rec); print(f"  ❌ {name[:48]:48} apply-crash"); continue
        op, ot, odr = _stats(out_pdf)
        nin, nout = _norm(it), _norm(ot)

        # Header-untouched: apply NEVER edits header glyphs, so every header's
        # significant words must survive in the output. Token-based (not a
        # contiguous substring) so multi-line / reordered headers don't false-
        # fail; a genuinely MISSING word = a header got caught in redaction.
        headers = [_norm(r.get("header", "")) for r in outline.get("roles", []) if r.get("header")]
        hdr_missing = {}
        for h in headers:
            if len(h) <= 6:
                continue
            toks = re.findall(r"[a-z0-9]{4,}", h)
            miss = [w for w in toks if w not in nout]
            if toks and len(miss) > 0.2 * len(toks):
                hdr_missing[h[:45]] = miss[:8]
        hdr_ok = not hdr_missing
        if hdr_missing:
            rec["header_missing"] = hdr_missing
        edited = [_norm(e["text"]) for v in diff["bullets"].values() for e in v]
        landed = sum(1 for e in edited if e[:24] and e[:24] in nout)

        inv = {
            "no_crash":          True,
            "pages_preserved":   op == ip,
            "text_not_lost":     len(nout) >= 0.80 * max(1, len(nin)),
            "borders_preserved": odr >= 0.80 * max(1, idr),
            "headers_untouched": hdr_ok,
            "edits_landed":      (landed >= max(1, len(edited) // 2)) if edited else True,
        }
        rec.update(verdict="PASS" if all(inv.values()) else "FAIL",
                   invariants=inv, used_llm=used_llm,
                   detail=f"pages {ip}->{op} text {len(nin)}->{len(nout)} "
                          f"draw {idr}->{odr} edits {landed}/{len(edited)}")
        rows.append(rec)
        mark = "✅" if rec["verdict"] == "PASS" else "❌"
        fails = [k for k, v in inv.items() if not v]
        print(f"  {mark} {name[:48]:48} {'LLM' if used_llm else 'heur'}  "
              f"{rec['verdict']}{('  ✗' + ','.join(fails)) if fails else ''}")

    # ── aggregate ──────────────────────────────────────────────────────
    replica = [r for r in rows if r["verdict"] in ("PASS", "FAIL")]
    pas = [r for r in replica if r["verdict"] == "PASS"]
    print("=" * 80)
    print(f"  replica-path CVs: {len(replica)}   PASS: {len(pas)}   "
          f"FAIL: {len(replica) - len(pas)}")
    if replica:
        keys = ["no_crash", "pages_preserved", "text_not_lost",
                "borders_preserved", "headers_untouched", "edits_landed"]
        for k in keys:
            ok = sum(1 for r in replica if r["invariants"].get(k))
            print(f"    {k:18} {ok}/{len(replica)}")
    rebuild = [r for r in rows if r["verdict"].startswith("rebuild-route")]
    print(f"  routed-to-rebuild: {len(rebuild)}   crashes: "
          f"{sum(1 for r in rows if 'crash' in r['verdict'])}")
    side = OUT / "invariant_report.json"
    side.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"\n  report: {side}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
