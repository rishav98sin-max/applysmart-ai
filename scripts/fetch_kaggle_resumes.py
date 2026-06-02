"""Pull resume PDFs from a Kaggle dataset into CVs/_kaggle_corpus/ for the
breadth-test invariant harness. Default dataset = snehaanbhawal/resume-dataset
(~2.4k PDFs). Skips on missing creds with a clear next step.

Usage:
    venv\\Scripts\\python.exe scripts\\fetch_kaggle_resumes.py             # 200 default
    venv\\Scripts\\python.exe scripts\\fetch_kaggle_resumes.py 500
    KAGGLE_DATASET=anothuser/dset venv\\Scripts\\python.exe scripts\\fetch_kaggle_resumes.py 100

Auth (in order of precedence):
  1. ~/.kaggle/kaggle.json           (preferred — kaggle SDK default)
  2. KAGGLE_USERNAME + KAGGLE_KEY    (env vars)

Output: CVs/_kaggle_corpus/<safe-flat-name>.pdf  (gitignored).
"""
from __future__ import annotations
import json
import os
import sys
import shutil
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "CVs" / "_kaggle_corpus"
STAGE = ROOT / "CVs" / "_kaggle_stage"
DATASET = os.getenv("KAGGLE_DATASET", "snehaanbhawal/resume-dataset")
MAX = int(sys.argv[1]) if len(sys.argv) > 1 else 200


def _ensure_creds() -> bool:
    """Honour env-var auth by writing kaggle.json on the fly if missing.
    Returns True if Kaggle can authenticate."""
    home = Path.home()
    cred_file = home / ".kaggle" / "kaggle.json"
    if cred_file.exists():
        return True
    user = os.getenv("KAGGLE_USERNAME")
    key = os.getenv("KAGGLE_KEY")
    if user and key:
        cred_file.parent.mkdir(parents=True, exist_ok=True)
        cred_file.write_text(json.dumps({"username": user, "key": key}))
        try:
            os.chmod(cred_file, 0o600)
        except Exception:
            pass
        return True
    print(
        "  ✘ Kaggle creds missing.\n"
        f"     Drop kaggle.json at {cred_file}\n"
        "     OR set KAGGLE_USERNAME / KAGGLE_KEY env vars.\n"
        "     Get the file at https://www.kaggle.com/settings/account (Create New API Token)."
    )
    return False


def main() -> int:
    if not _ensure_creds():
        return 2
    try:
        from kaggle.api.kaggle_api_extended import KaggleApi  # type: ignore
    except Exception as e:
        print(f"  ✘ kaggle pkg not installed: {e}\n     pip install kaggle")
        return 2
    DEST.mkdir(parents=True, exist_ok=True)
    STAGE.mkdir(parents=True, exist_ok=True)
    api = KaggleApi()
    api.authenticate()
    print(f"  ↓ downloading {DATASET} → {STAGE} (will keep PDFs only)")
    try:
        api.dataset_download_files(DATASET, path=str(STAGE), unzip=True, quiet=False)
    except Exception as e:
        print(f"  ✘ kaggle download failed: {type(e).__name__}: {e}")
        return 2
    # Walk stage, copy plausible PDF resumes to DEST flat, deduped.
    moved = 0
    for p in STAGE.rglob("*.pdf"):
        if moved >= MAX:
            break
        try:
            if p.stat().st_size > 3_000_000:
                continue
            with open(p, "rb") as fh:
                if fh.read(4) != b"%PDF":
                    continue
        except Exception:
            continue
        rel = p.relative_to(STAGE).as_posix().replace("/", "__")
        safe = ("kaggle__" + rel)[:140]
        out = DEST / safe
        if out.exists():
            continue
        try:
            shutil.copyfile(p, out)
            moved += 1
        except Exception as e:
            print(f"   skip {p.name}: {type(e).__name__}")
    print(f"  ✓ {moved} PDF(s) copied to {DEST}")
    # Cleanup stage to save disk
    try:
        shutil.rmtree(STAGE, ignore_errors=True)
    except Exception:
        pass
    return 0 if moved > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
