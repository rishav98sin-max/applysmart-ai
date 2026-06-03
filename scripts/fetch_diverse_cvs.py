"""Pull LAYOUT-DIVERSE resume PDFs from known-different GitHub template repos
into CVs/_diverse_corpus/. Targets one PDF per template family so the
harness exercises distinct parser branches (not the same builder 2,400 times).

See feedback memory `breadth_means_layouts`: 2,400 same-template CVs is
weaker breadth than 50 layout-diverse ones. This script picks builders that
are KNOWN to differ on the fragile axes — fonts, columns, sidebars, table
borders, bullet glyphs, page geometry — so harness PASS counts mean
"this parser branch is robust", not "this one template is robust".

Usage:
    venv\\Scripts\\python.exe scripts\\fetch_diverse_cvs.py [max_per_repo]
"""
from __future__ import annotations
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "CVs" / "_diverse_corpus"
DEST.mkdir(parents=True, exist_ok=True)

# Per-repo cap (each repo may have many sample PDFs across branches/dirs;
# 2 is enough to capture the template character without dataset bloat).
PER_REPO = int(sys.argv[1]) if len(sys.argv) > 1 else 2
UA = {"User-Agent": "applysmart-diverse-fetch"}

# Curated builders. Each entry = (repo, "what makes it different" tag).
# The repo names are STABLE — these are well-known public template repos.
# If GitHub search/raw fetch fails, that repo is silently skipped so the
# overall run keeps going.
TEMPLATES = [
    # ── LaTeX families (different geometry engines, distinct font sets) ──
    ("posquit0/Awesome-CV",              "latex-colored-header"),
    ("sb2nov/resume",                    "latex-classic-twocolumn"),
    ("jakegut/resume",                   "latex-modern-minimal"),
    ("billryan/resume",                  "latex-bilingual-minimal"),
    ("liweitianux/resume",               "latex-academic"),
    ("salomonelli/best-resume-ever",     "vue-html-colorblock"),
    ("xitanggg/open-resume",             "react-canvas-export"),
    ("darwiin/yaac-another-awesome-cv",  "latex-sidebar"),
    ("geoffreylgv/cv",                   "latex-variant"),
    ("dnl-blkv/mcdowell-cv",             "latex-academic-detailed"),
    # ── Markdown/HTML-to-PDF families (different layout engine output) ──
    ("mnjul/html-resume",                "html-pdfprint"),
    ("sproogen/modern-resume-theme",     "jekyll-html"),
    # ── Other languages / Word-derived ──────────────────────────────────
    ("LucasPickering/resume",            "typst-style"),
    ("denis-sokolov/resume",             "css-print"),
    ("zachscrivena/simple-resume-cv",    "css-print-min"),
    # ── Less-common, may not have a built PDF — fail-soft ──────────────
    ("jaywcjlove/awesome-resume",        "curated-listing"),
    ("dnl-blkv/mcdowell-cv",             "academic-cv"),
]


def _api_json(url: str):
    req = urllib.request.Request(url, headers=UA)
    return json.load(urllib.request.urlopen(req, timeout=20))


def _fetch_raw(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=30).read()


def _try_repo(full: str, tag: str) -> int:
    """Walk the default branch tree, pull up to PER_REPO PDFs. Returns count."""
    parts = full.split("/")
    if len(parts) != 2:
        return 0
    # Resolve default branch.
    try:
        meta = _api_json(f"https://api.github.com/repos/{full}")
        branch = meta.get("default_branch", "main")
    except Exception as e:
        print(f"  ⚠ {full}: meta failed ({type(e).__name__})")
        return 0
    try:
        tree = _api_json(
            f"https://api.github.com/repos/{full}/git/trees/{branch}?recursive=1"
        )
    except Exception as e:
        print(f"  ⚠ {full}: tree failed ({type(e).__name__})")
        return 0
    pdf_nodes = [
        n for n in tree.get("tree", [])
        if n.get("type") == "blob"
        and n.get("path", "").lower().endswith(".pdf")
        and (n.get("size") or 0) <= 3_000_000
    ]
    # Prefer paths that look like resumes (not e.g. invoices, certificates).
    def _rank(n):
        p = n["path"].lower()
        score = 0
        for kw in ("resume", "cv", "curriculum"):
            if kw in p: score -= 10
        if "/" not in p: score -= 5  # top-level samples usually canonical
        score += len(p)              # prefer shorter paths
        return score
    pdf_nodes.sort(key=_rank)
    moved = 0
    for n in pdf_nodes[:PER_REPO]:
        path = n["path"]
        raw = f"https://raw.githubusercontent.com/{full}/{branch}/{urllib.parse.quote(path)}"
        safe = f"{tag}__{full.replace('/', '_')}__{os.path.basename(path)}"[:140]
        out = DEST / safe
        if out.exists():
            moved += 1
            continue
        try:
            data = _fetch_raw(raw)
            if data[:4] != b"%PDF":
                continue
            out.write_bytes(data)
            moved += 1
        except Exception as e:
            print(f"  ⚠ {full}: {path} fetch failed ({type(e).__name__})")
    if moved:
        print(f"  ✓ {full:42s} [{tag}]  +{moved} PDF(s)")
    else:
        print(f"  · {full:42s} [{tag}]  no usable PDFs in repo")
    return moved


def main() -> int:
    total = 0
    for full, tag in TEMPLATES:
        total += _try_repo(full, tag)
    print(f"\n  ✓ DONE: {total} layout-diverse PDF(s) → {DEST}")
    return 0 if total > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
