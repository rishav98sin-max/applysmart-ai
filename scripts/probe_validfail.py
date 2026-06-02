"""Why does validate_llm_outline reject the single-shot parse on real CVs?
Runs ONE reader sample per CV and prints the validation report (issues/score/
roles) + what the LLM actually returned. Paced + light (1 call/CV)."""
import sys, time
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")
import glob
from agents import cv_structure_reader as R
from agents.llm_client import chat_fast

PATS = ["*sourabh_bajaj*", "*ScottClarkResume*", "*ice1000_resume__resume.pdf", "*Bharadwaj*"]
print("  (waiting 45s for TPM to clear before starting)")
time.sleep(45)   # clear any prior-run TPM spike
for pat in PATS:
    for cv in glob.glob("CVs/_real_corpus/" + pat):
        print("\n====", cv.split("\\")[-1].split("/")[-1])
        lines = R._collect_lines_with_ids(cv)
        print(f"  lines collected: {len(lines)}")
        prompt = R._build_reader_prompt(lines)
        try:
            raw = chat_fast(prompt, max_tokens=3000, temperature=0.0)
        except Exception as e:
            print("  chat_fast FAILED:", type(e).__name__, str(e)[:80]); continue
        data = R._extract_json(raw or "")
        if not isinstance(data, dict):
            print("  JSON parse FAILED. raw[:160]:", repr((raw or "")[:160])); time.sleep(3); continue
        print(f"  raw LLM: roles={len(data.get('roles',[]))} summary_ids={len(data.get('summary_line_ids',[]))}")
        outline = R._assemble_outline(data, lines)
        rep = R.validate_llm_outline(outline)
        print(f"  validate: ok={rep['ok']} score={rep['score']} n_roles={rep['n_roles']} n_bullets={rep['n_bullets']}")
        for iss in rep["issues"]:
            print("    ! ", iss)
        time.sleep(30)   # one key stays under ~12K tok/min TPM
