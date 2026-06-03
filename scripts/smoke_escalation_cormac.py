"""
E2E smoke test: board escalation ON, Cormac's CV → Internal Auditor / Ireland.

Runs the FULL agent (scrape → match → tailor → render) with BOARD_ESCALATION=1
and preview_mode (no email). We want to see:
  1. Scrape rounds and whether they ESCALATE across boards (🪜 logs) when
     LinkedIn matches don't clear the bar.
  2. Cormac's CV parsed + tailored for the auditor role end-to-end.
  3. No crashes through the live agent loop.

Threshold set a bit high (70) to make matches harder, so escalation is more
likely to fire and be observable.
"""
import os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
except Exception:
    pass

# Flags under test
os.environ["BOARD_ESCALATION"]    = "1"
os.environ["REPLICA_LLM_PRIMARY"] = "1"   # match prod
os.environ["APPLYSMART_REAIM"]    = "1"
os.environ.setdefault("DIAGNOSTICS_ENABLED", "1")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from agents.job_agent import run_agent

CV   = str(ROOT / "CVs" / "Run25" / "Cormac Holleran CV 2026.pdf")
OUT  = str(ROOT / "CVs" / "Run_SmokeEscalation")

def main():
    print(f"CV exists: {os.path.exists(CV)}  -> {CV}")
    # MATCH_THRESHOLD env override: set 99 to FORCE zero LinkedIn matches so
    # escalation provably fires (boards_tried should then span LinkedIn →
    # Indeed → Jobs.ie → Builtin). Default 70 = realistic run.
    thr = int(os.getenv("MATCH_THRESHOLD", "70"))
    res = run_agent(
        cv_path         = CV,
        job_title       = "Internal Auditor",
        location        = "Ireland",
        num_jobs        = 3,
        match_threshold = thr,
        user_email      = "smoke@test.local",
        candidate_name  = "Cormac Holleran",
        source          = "LinkedIn",
        output_dir      = OUT,
        session_id      = "smoke_escalation_cormac",
        preview_mode    = True,         # no email send
        experience_level= "Senior (6+ yrs)",
    )
    print("\n" + "=" * 70)
    print(" SMOKE RESULT")
    print("=" * 70)
    print(f"  status           : {res.get('status')}")
    print(f"  scrape_round     : {res.get('scrape_round')}")
    print(f"  boards_tried     : {res.get('scrape_boards_tried')}")
    matched = res.get("matched_jobs") or []
    skipped = res.get("skipped_jobs") or []
    print(f"  matched          : {len(matched)}  skipped: {len(skipped)}")
    for j in matched:
        print(f"    ✓ {j.get('match_score')}/100 {j.get('title','')[:40]} @ {j.get('company','')[:25]} [{j.get('source','?')}]")
    for j in skipped:
        print(f"    ✗ {j.get('match_score')}/100 {j.get('title','')[:40]} @ {j.get('company','')[:25]} [{j.get('source','?')}]")
    print(f"  errors           : {res.get('errors')}")
    print(f"  output_dir       : {OUT}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
