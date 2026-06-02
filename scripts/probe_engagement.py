"""
C-diagnostic: WHY does the LLM-primary reader decline (fall back to heuristic)
on real CVs? Runs the reader (consensus n=3, verbose) per real CV, captures its
own log, and classifies: ENGAGED vs decline-reason. Histogram at the end so we
fix the dominant cause, not guess.
"""
from __future__ import annotations
import io, os, sys, glob, time
from contextlib import redirect_stdout
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
except Exception:
    pass
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
os.environ["CV_READER_SAMPLES"] = "3"
import agents.pdf_editor as pe                            # type: ignore
from agents.cv_structure_reader import read_outline_llm     # type: ignore

CVS = sorted(glob.glob(str(ROOT / "CVs" / "_real_corpus" / "*.pdf")))


def classify(cv):
    # designer/2-col/etc route to rebuild — not an LLM decline, exclude.
    try:
        compat = pe.detect_replica_compatibility(cv)
        if not compat.get("compatible", True):
            return "rebuild-route", ""
    except Exception as e:
        return "compat-crash", str(e)[:50]
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            out = read_outline_llm(cv, verbose=True, n_samples=1)  # n=1: diagnose single-shot failure modes, light on TPM
    except Exception as e:
        return "reader-crash", f"{type(e).__name__}:{e}"[:50]
    log = buf.getvalue()
    if out and out.get("_geometry"):
        return "ENGAGED", ""
    # decline — find why
    issues = sorted({ln.strip().lstrip("! ").strip()
                     for ln in log.splitlines() if ln.strip().startswith("!")})
    if "geometry bridge: unavailable" in log:
        return "geometry-gate-failed", " | ".join(issues)[:90]
    if "no valid candidate" in log or "ok=True" not in log:
        return "no-valid-parse", " | ".join(issues)[:90]
    if out is None:
        return "declined-other", " | ".join(issues)[:90]
    return "valid-but-no-geometry", " | ".join(issues)[:90]


def main():
    hist = Counter()
    print(f"engagement diagnostic on {len(CVS)} real CVs\n" + "=" * 80)
    for cv in CVS:
        name = os.path.basename(cv)[:46]
        reason, detail = classify(cv)
        hist[reason] += 1
        print(f"  {name:46} {reason:22} {detail}")
        time.sleep(3)   # pace: stay under ~12K tok/key/min TPM
    print("=" * 80)
    for r, n in hist.most_common():
        print(f"  {r:24} {n}")
    repl = sum(n for r, n in hist.items() if r != "rebuild-route")
    eng = hist.get("ENGAGED", 0)
    print(f"\n  LLM engagement (excl. rebuild-route): {eng}/{repl} = "
          f"{(100*eng/repl if repl else 0):.0f}%")


if __name__ == "__main__":
    raise SystemExit(main())
