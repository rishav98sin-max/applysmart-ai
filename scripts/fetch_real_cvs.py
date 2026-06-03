"""Pull a batch of REAL resume PDFs from public GitHub repos into
CVs/_real_corpus/ for the invariant harness. No auth (unauth GitHub API).
Best-effort: skips on rate-limit / errors. Filters to plausible resume PDFs."""
from __future__ import annotations
import json, os, sys, urllib.request, urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "CVs" / "_real_corpus"
DEST.mkdir(parents=True, exist_ok=True)
MAX = int(sys.argv[1]) if len(sys.argv) > 1 else 40
UA = {"User-Agent": "cv-harness"}


def gj(url):
    req = urllib.request.Request(url, headers=UA)
    return json.load(urllib.request.urlopen(req, timeout=20))


def main():
    queries = ["resume in:name language:pdf", "resume pdf sample resume",
               "cv resume example", "resume dataset"]
    repos = {}
    for q in queries:
        try:
            r = gj(f"https://api.github.com/search/repositories?q={urllib.parse.quote(q)}&per_page=12")
            for it in r.get("items", []):
                repos[it["full_name"]] = it.get("default_branch", "main")
        except Exception as e:
            print(f"  search '{q}' failed: {type(e).__name__}")
    print(f"  candidate repos: {len(repos)}")

    got = 0
    for full, branch in repos.items():
        if got >= MAX:
            break
        try:
            tree = gj(f"https://api.github.com/repos/{full}/git/trees/{branch}?recursive=1")
        except Exception:
            continue
        for node in tree.get("tree", []):
            if got >= MAX:
                break
            p = node.get("path", "")
            pl = p.lower()
            if not pl.endswith(".pdf"):
                continue
            if not any(k in pl for k in ("resume", "cv", "curriculum")):
                continue
            if node.get("size", 0) > 3_000_000:  # skip huge
                continue
            raw = f"https://raw.githubusercontent.com/{full}/{branch}/{urllib.parse.quote(p)}"
            safe = f"{full.replace('/','_')}__{os.path.basename(p)}"[:120]
            out = DEST / safe
            if out.exists():
                continue
            try:
                req = urllib.request.Request(raw, headers=UA)
                data = urllib.request.urlopen(req, timeout=25).read()
                if data[:4] != b"%PDF":
                    continue
                out.write_bytes(data)
                got += 1
                print(f"  [{got}] {safe}  ({len(data)//1024}KB)")
            except Exception:
                continue
    print(f"\n  downloaded {got} real resume PDFs -> {DEST}")


if __name__ == "__main__":
    import urllib.parse
    main()
