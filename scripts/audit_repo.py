"""
Deterministic repo leanness / dead-code audit — EVIDENCE, not vibes.

Why this exists: hand-grepping for "is X used?" produced a FALSE NEGATIVE
once (a shell-escaping bug hid a real `langgraph` import). This tool removes
that whole error class:

  • Imports are found by PARSING the AST (Python's own parser), not regex —
    so it can't miss `from x import y` / `import x.y` / aliased imports.
  • Dependency → import-name mapping comes from the INSTALLED package
    metadata (`importlib.metadata.packages_distributions()`), not a
    hand-written guess table — so `langchain-groq → langchain_groq`,
    `pymupdf → fitz`, `beautifulsoup4 → bs4`, `python-docx → docx`, etc.
    are resolved authoritatively.
  • Every "UNUSED" verdict is two-directional: it reports both the absence
    of any import AND (for deps) whether another *used* dependency requires
    it transitively (so you never delete a transitive need).

It does NOT delete anything. It prints an evidence report. Pair it with the
runtime smoke in the playbook before acting on any finding.

Run:
    venv\\Scripts\\python.exe scripts\\audit_repo.py
    venv\\Scripts\\python.exe scripts\\audit_repo.py --json > audit.json
"""
from __future__ import annotations
import argparse
import ast
import json
import sys
from pathlib import Path
from collections import defaultdict

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
# All first-party importable code. MUST include every dir that imports deps,
# or a dep imported only there gets a false "UNUSED" (e.g. langfuse lives in
# diagnostics/ — omitting it falsely flagged langfuse).
FIRST_PARTY_DIRS = ["agents", "diagnostics"]
APP_FILES = ["app.py"]                 # entrypoints
SCRIPTS_DIR = "scripts"


# ───────────────────────── AST helpers ─────────────────────────

def _py_files(*globs):
    out = []
    for g in globs:
        out += [p for p in g if p.suffix == ".py"]
    return sorted(set(out))


def _parse(path: Path):
    try:
        return ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
    except Exception as e:
        print(f"  ⚠ parse failed {path}: {e}", file=sys.stderr)
        return None


def _imports_of(tree) -> set:
    """Return the set of top-level + dotted module names imported by a file.
    Records both 'a.b.c' and its head 'a' so either match counts as use."""
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name)
                mods.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                mods.add(node.module)
                mods.add(node.module.split(".")[0])
                # `from agents import job_scraper` → record agents.job_scraper
                for alias in node.names:
                    mods.add(f"{node.module}.{alias.name}")
    return mods


def _defs_and_refs(tree):
    """Top-level def/class names defined, and ALL Name/attr refs used."""
    defined = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
    refs = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            refs.add(node.id)
        elif isinstance(node, ast.Attribute):
            refs.add(node.attr)
    return defined, refs


# ───────────────────────── audits ─────────────────────────

def audit_module_graph():
    """First-party module → who imports it (AST). Flags 0-import modules."""
    first_party = _py_files(
        Path(d).rglob("*.py") for d in FIRST_PARTY_DIRS
    ) if False else []
    # collect explicitly (rglob returns generator per dir)
    first_party = []
    for d in FIRST_PARTY_DIRS:
        first_party += [p for p in (ROOT / d).rglob("*.py")]
    consumers = []
    for d in FIRST_PARTY_DIRS:
        consumers += [p for p in (ROOT / d).rglob("*.py")]
    consumers += [ROOT / f for f in APP_FILES if (ROOT / f).exists()]
    consumers += [p for p in (ROOT / SCRIPTS_DIR).rglob("*.py")]

    mod_names = {}   # 'agents.job_scraper' -> path
    for p in first_party:
        rel = p.relative_to(ROOT).with_suffix("")
        mod_names[".".join(rel.parts)] = p

    imported_by = defaultdict(set)
    for c in consumers:
        tree = _parse(c)
        if not tree:
            continue
        imps = _imports_of(tree)
        for mod in mod_names:
            short = mod.split(".")[-1]
            if mod in imps or short in imps:
                if mod_names[mod] != c:
                    imported_by[mod].add(str(c.relative_to(ROOT)))

    rows = []
    for mod, p in sorted(mod_names.items()):
        if p.name == "__init__.py":
            continue
        loc = sum(1 for _ in open(p, encoding="utf-8", errors="replace"))
        rows.append({
            "module": mod, "loc": loc,
            "imported_by_count": len(imported_by[mod]),
            "imported_by": sorted(imported_by[mod]),
        })
    return rows


def audit_dependencies():
    """requirements.txt dist → is its import name actually imported?
    Uses installed metadata for authoritative dist→import mapping +
    transitive 'required-by' so we never flag a transitive need as removable."""
    import importlib.metadata as md

    req = ROOT / "requirements.txt"
    if not req.exists():
        return {"error": "no requirements.txt"}
    dists = []
    for line in req.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.split("#")[0].strip()
        if not s:
            continue
        name = (s.replace(">=", " ").replace("==", " ").replace("<", " ")
                 .replace(">", " ").replace("~=", " ").split()[0]).strip()
        if name:
            dists.append(name)

    # authoritative import-name → [dist] map, inverted to dist → [import tops]
    try:
        pkg_dists = md.packages_distributions()   # {import_top: [dist,...]}
    except Exception:
        pkg_dists = {}
    dist_to_imports = defaultdict(set)
    for imp_top, dist_list in pkg_dists.items():
        for d in dist_list:
            dist_to_imports[_norm(d)].add(imp_top)

    # collect ALL first-party import tops (AST)
    used_tops = set()
    scan = []
    for d in FIRST_PARTY_DIRS:
        scan += [p for p in (ROOT / d).rglob("*.py")]
    scan += [ROOT / f for f in APP_FILES if (ROOT / f).exists()]
    for p in scan:
        tree = _parse(p)
        if tree:
            for m in _imports_of(tree):
                used_tops.add(m.split(".")[0])

    # build "required-by" so transitive needs aren't called removable
    required_by = defaultdict(set)
    for dist in dists:
        try:
            reqs = md.requires(dist) or []
        except Exception:
            reqs = []
        for r in reqs:
            dep = _norm(r.split(";")[0].split("(")[0]
                        .replace(">=", " ").replace("==", " ").replace("<", " ")
                        .replace(">", " ").replace("~=", " ").split()[0]) if r else ""
            if dep:
                required_by[dep].add(dist)

    rows = []
    for dist in dists:
        nd = _norm(dist)
        imp_tops = dist_to_imports.get(nd, set())
        directly_used = bool(imp_tops & used_tops) if imp_tops else None
        # transitive: is this dist required by any OTHER dist we list+use?
        req_by = sorted(required_by.get(nd, set()))
        if directly_used:
            verdict = "USED (direct import)"
        elif req_by:
            verdict = f"TRANSITIVE (required by: {', '.join(req_by)})"
        elif imp_tops:
            verdict = "UNUSED — installed, import-name never imported"
        else:
            verdict = "UNKNOWN — not installed / no metadata (verify manually)"
        rows.append({
            "dist": dist, "import_names": sorted(imp_tops),
            "directly_used": directly_used, "required_by": req_by,
            "verdict": verdict,
        })
    return rows


def _norm(s: str) -> str:
    return (s or "").strip().lower().replace("_", "-")


def audit_dead_functions():
    """First-party top-level defs never referenced anywhere (candidates).
    HEURISTIC — flag, don't trust blindly: dynamic/getattr/string dispatch
    and test-only use can cause false positives. Verify each before deleting."""
    scan = []
    for d in FIRST_PARTY_DIRS:
        scan += [p for p in (ROOT / d).rglob("*.py")]
    scan += [ROOT / f for f in APP_FILES if (ROOT / f).exists()]
    scan += [p for p in (ROOT / SCRIPTS_DIR).rglob("*.py")]

    defs = {}        # name -> defining file
    all_refs = defaultdict(int)
    per_file_defs = {}
    for p in scan:
        tree = _parse(p)
        if not tree:
            continue
        d, r = _defs_and_refs(tree)
        per_file_defs[p] = d
        for name in r:
            all_refs[name] += 1

    rows = []
    for p, names in per_file_defs.items():
        if (ROOT / SCRIPTS_DIR) in p.parents:
            continue  # scripts define throwaway helpers; skip
        for name in sorted(names):
            if name.startswith("_") and name.endswith("_"):
                continue  # dunder
            # Flag ONLY names referenced NOWHERE by bare name/attribute across
            # the whole repo. (A function called even once anywhere has a ref,
            # so threshold ==0, not <=1 — the <=1 version false-flagged every
            # single-use public function like mark_applied / analytics_enabled.)
            # Still a HEURISTIC: misses string/getattr dispatch — verify each.
            if all_refs.get(name, 0) == 0:
                rows.append({"name": name, "file": str(p.relative_to(ROOT))})
    return rows


def audit_fallback_flags():
    """Surface env-flag reads with literal defaults → bypassed/off paths."""
    import re
    scan = []
    for d in FIRST_PARTY_DIRS:
        scan += [p for p in (ROOT / d).rglob("*.py")]
    pat = re.compile(
        r"""(?:os\.getenv|os\.environ\.get|secret_or_env)\(\s*["']([A-Z0-9_]+)["']\s*(?:,\s*["']?([^)"',]*)["']?)?""")
    rows = []
    seen = set()
    for p in scan:
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in pat.finditer(txt):
            flag, default = m.group(1), (m.group(2) or "").strip()
            key = (flag, default)
            if key in seen:
                continue
            seen.add(key)
            rows.append({"flag": flag, "default": default,
                         "file": str(p.relative_to(ROOT))})
    return sorted(rows, key=lambda r: r["flag"])


def audit_scripts():
    scripts = [p for p in (ROOT / SCRIPTS_DIR).rglob("*.py")]
    cats = defaultdict(list)
    for p in scripts:
        n = p.name
        cat = ("probe" if n.startswith("probe_") else
               "smoke" if n.startswith("smoke_") else
               "test" if n.startswith("test_") else
               "fetch" if n.startswith("fetch_") else
               "audit" if n.startswith("audit") else "other")
        cats[cat].append(n)
    return {k: sorted(v) for k, v in cats.items()}


# ───────────────────────── report ─────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    report = {
        "module_graph": audit_module_graph(),
        "dependencies": audit_dependencies(),
        "dead_function_candidates": audit_dead_functions(),
        "fallback_flags": audit_fallback_flags(),
        "scripts": audit_scripts(),
    }

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print("=" * 78)
    print(" REPO AUDIT — evidence report (AST-based; nothing deleted)")
    print("=" * 78)

    print("\n## 1. First-party modules imported by NOBODY (dead-module candidates)")
    dead = [r for r in report["module_graph"] if r["imported_by_count"] == 0]
    if not dead:
        print("   (none — every module is imported somewhere)")
    for r in dead:
        print(f"   ✗ {r['module']}  ({r['loc']} LOC)")

    print("\n## 2. Dependencies (requirements.txt) — verdicts")
    for r in report["dependencies"]:
        mark = {"USED (direct import)": "✅"}.get(r["verdict"][:20] + "", "")
        flag = ("✅" if r["directly_used"] else
                "↪" if r["required_by"] else
                "✗" if r["import_names"] else "?")
        print(f"   {flag} {r['dist']:30s} {r['verdict']}")
        if r["import_names"] and not r["directly_used"] and not r["required_by"]:
            print(f"        import-name(s) never imported: {r['import_names']}")

    print("\n## 3. Dead first-party function/class CANDIDATES (heuristic — verify each)")
    dfc = report["dead_function_candidates"]
    if not dfc:
        print("   (none flagged)")
    for r in dfc[:40]:
        print(f"   ? {r['name']:32s} {r['file']}")
    if len(dfc) > 40:
        print(f"   … +{len(dfc)-40} more")

    print("\n## 4. Fallback / bypass flags (defaults reveal off-by-default paths)")
    for r in report["fallback_flags"]:
        print(f"   • {r['flag']:28s} default={r['default']!r:10s} {r['file']}")

    print("\n## 5. scripts/ inventory")
    for cat, names in report["scripts"].items():
        print(f"   {cat:7s} ({len(names)}): {', '.join(names[:6])}{' …' if len(names)>6 else ''}")

    print("\n" + "=" * 78)
    print(" Next: for each flagged item, apply the two-directional + runtime check")
    print(" from playbook_applysmart_deep_audit before acting. Nothing here is")
    print(" a delete-without-verify verdict.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
