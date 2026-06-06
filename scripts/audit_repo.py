"""
Deterministic repo leanness / dead-code audit — EVIDENCE, not vibes (v2).

Why this exists: hand-grepping for "is X used?" produced a FALSE NEGATIVE
once (a shell-escaping bug hid a real `langgraph` import) AND, worse, even
when grep was correct it gave only MODULE-level signal — leaving function-
level dead exports (`chat_gemini`, `_BulletRevertsProxy`), dead env flags,
orphan tracked dirs (`_audit_e2e/`), stale doc symbols, and asset bloat
all invisible. v2 closes those gaps so a cold session running this gets
trustworthy depth without manual followups.

What it now reports (in order):
  1.  First-party modules imported by NOBODY              (file-level dead)
  2.  Dependencies — USED / TRANSITIVE / UNUSED            (requirements)
  3.  Dead first-party function/class candidates            (heuristic)
  4.  PUBLIC top-level defs with ZERO external callers     ★ v2
       (function-level dead exports — catches `chat_gemini` etc.)
  5.  Env flags defaulted-off AND never set in any .env    ★ v2
       (dead branches the code still carries)
  6.  .env entries NEVER read by code                      ★ v2
       (stale API keys after a scraper swap)
  7.  Tracked paths that aren't under any normal location  ★ v2
       (orphans like `_audit_e2e/`, `tmp_e2e_n8n.py`)
  8.  Top-level dirs by size                               ★ v2
       (asset bloat: CVs/, diagnostics/runs/, graphify-out/)
  9.  Doc symbols missing from code                        ★ v2
       (stale README / CHANGELOG / HANDOFF references)
  10. Fallback / bypass env flags inventory                 (defaults)
  11. scripts/ inventory                                    (categories)

All evidence is AST + filesystem + git-ls-files based. Nothing is deleted.
For every "removable" finding, apply the playbook's two-directional rule
before acting: prove absence-of-use AND show what is used instead.

Run:
    venv\\Scripts\\python.exe scripts\\audit_repo.py
    venv\\Scripts\\python.exe scripts\\audit_repo.py --json > audit.json
"""
from __future__ import annotations
import argparse
import ast
import json
import os
import re
import subprocess
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

# Paths that are EXPECTED at the repo root (not orphans). Anything tracked at
# the root not in this set or under a normal dir is flagged.
EXPECTED_ROOT_PATHS = {
    "app.py", "README.md", "requirements.txt", "runtime.txt",
    ".gitignore", ".env.example", ".python-version",
    "HANDOFF_SUMMARY.md", "LICENSE", "Procfile", "setup.cfg",
    "pyproject.toml", "packages.txt", "CLAUDE.md",
    "langgraph.json",       # LangGraph deploy spec — references
                            #   "./agents/job_agent.py:build_agent_graph" by
                            #   dotted path, so `build_agent_graph` looks dead
                            #   to AST but is dynamically invoked at deploy.
                            #   Cross-check every section-4 finding against
                            #   any *.json / *.yaml / *.toml that names a
                            #   `module.py:symbol` entrypoint.
}
# Top-level dirs that are part of normal structure.
EXPECTED_ROOT_DIRS = set(FIRST_PARTY_DIRS) | {SCRIPTS_DIR, "docs", ".streamlit",
                                              "schemas", "CVs", "data",
                                              "uploads", "outputs", "sessions",
                                              "graphify-out", "_r-adapt-render"}


# ───────────────────────── AST helpers ─────────────────────────

def _py_files(*globs):
    out = []
    for g in globs:
        out += [p for p in g if p.suffix == ".py"]
    return sorted(set(out))


def _parse(path: Path):
    try:
        return ast.parse(path.read_text(encoding="utf-8", errors="replace"),
                         filename=str(path))
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


def _public_top_defs(tree) -> set:
    """Public (non-underscore-prefixed) top-level def/class names."""
    out = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                out.add(node.name)
    return out


def _norm(s: str) -> str:
    return (s or "").strip().lower().replace("_", "-")


def _first_party_paths():
    out = []
    for d in FIRST_PARTY_DIRS:
        out += [p for p in (ROOT / d).rglob("*.py")]
    return out


def _all_py_paths():
    out = _first_party_paths()
    out += [ROOT / f for f in APP_FILES if (ROOT / f).exists()]
    if (ROOT / SCRIPTS_DIR).exists():
        out += [p for p in (ROOT / SCRIPTS_DIR).rglob("*.py")]
    return out


# ───────────────────────── existing audits ─────────────────────────

def audit_module_graph():
    """First-party module → who imports it (AST). Flags 0-import modules."""
    first_party = _first_party_paths()
    consumers = _all_py_paths()

    mod_names = {}
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

    try:
        pkg_dists = md.packages_distributions()
    except Exception:
        pkg_dists = {}
    dist_to_imports = defaultdict(set)
    for imp_top, dist_list in pkg_dists.items():
        for d in dist_list:
            dist_to_imports[_norm(d)].add(imp_top)

    used_tops = set()
    for p in _first_party_paths() + [ROOT / f for f in APP_FILES
                                     if (ROOT / f).exists()]:
        tree = _parse(p)
        if tree:
            for m in _imports_of(tree):
                used_tops.add(m.split(".")[0])

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


def audit_dead_functions():
    """First-party top-level defs never referenced anywhere (candidates).
    HEURISTIC — flag, don't trust blindly: dynamic/getattr/string dispatch
    and test-only use can cause false positives. Verify each before deleting."""
    scan = _all_py_paths()

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
            continue
        for name in sorted(names):
            if name.startswith("_") and name.endswith("_"):
                continue
            if all_refs.get(name, 0) == 0:
                rows.append({"name": name, "file": str(p.relative_to(ROOT))})
    return rows


def audit_fallback_flags():
    """Surface env-flag reads with literal defaults → bypassed/off paths."""
    scan = _first_party_paths()
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
    if not (ROOT / SCRIPTS_DIR).exists():
        return {}
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


# ───────────────────────── NEW v2 audits ─────────────────────────

def _docstring_nodes(tree) -> set:
    """Return id() of every AST Constant node that is a DOCSTRING (first Expr
    in module/function/class body). Docstrings often mention symbols by name
    even after those symbols are deleted — counting them as refs hides dead
    code. Excluding them keeps the string-literal rescue lane honest."""
    out = set()
    bodies = [getattr(tree, "body", [])]
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bodies.append(getattr(n, "body", []))
    for body in bodies:
        if body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            out.add(id(body[0].value))
    return out


def _refs_in_tree(tree, public_names: set) -> set:
    """Return the subset of `public_names` that are referenced in `tree`
    via ANY of: bare Name, Attribute, ImportFrom alias, or short string literal.
      - `from x import foo as _sani` → counts as ref to "foo"
      - `workflow.add_node("scrape_jobs_node", fn)` → counts as ref to
        "scrape_jobs_node" (LangGraph string dispatch).
    Excludes docstrings (so a docstring mentioning a deleted symbol doesn't
    pretend the symbol is still used) and long strings (>50 chars are prose,
    not single-identifier dispatch).
    """
    seen = set()
    docstrings = _docstring_nodes(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if node.id in public_names:
                seen.add(node.id)
        elif isinstance(node, ast.Attribute):
            if node.attr in public_names:
                seen.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            for alias in (node.names or []):
                # Only count ALIASED imports (`from x import foo as _z`). For
                # unaliased imports the actual call-site appears as a bare Name
                # elsewhere and is captured by the Name pass — counting the
                # import too double-counts and lets dead imports masquerade as
                # use (e.g., `from x import convert_pdf_to_docx` then no call).
                if alias.asname and alias.name in public_names:
                    seen.add(alias.name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            v = node.value
            if len(v) > 50:
                continue
            if v in public_names:
                seen.add(v)
    return seen


def audit_function_reachability():
    """PUBLIC top-level defs in first-party modules with ZERO callers ANYWHERE
    (including same-file). This is the strict intersection of (a) public name
    AND (b) name appears nowhere as Name/Attribute/ImportFrom-alias/short-string.

    Catches genuine dead exports the file-level module graph misses — e.g.,
    `chat_gemini` (300+ LOC of Gemini infra) lives in a heavily-imported file
    so the file graph calls llm_client "used", but the function itself has
    zero callers. Excludes:
      - LangGraph nodes called via `workflow.add_node(name, fn)` in the same
        file (the bare Name ref counts as use — they're alive).
      - Aliased imports (`from x import foo as _z` counts as use of `foo`).
      - String-dispatch identifiers in short literals.
      - Symbols mentioned in docstrings (those are prose, not use).

    Anything that survives all those rescues is truly dead. Still HEURISTIC:
    `getattr(mod, var_name)` where `var_name` is non-literal will false-positive —
    verify each finding by hand before deleting.
    """
    first_party = _first_party_paths()
    consumers = _all_py_paths()

    public_defs = {}
    per_file_publics = {}
    all_public_names = set()
    for p in first_party:
        tree = _parse(p)
        if not tree:
            continue
        pubs = _public_top_defs(tree)
        per_file_publics[p] = pubs
        for name in pubs:
            public_defs.setdefault(name, []).append(p)
            all_public_names.add(name)

    # Count refs to public names across ALL files (including the defining file
    # — same-file use is still real use). Excludes docstrings; long-string
    # literals; uses _refs_in_tree which handles Name/Attribute/Import/str-lit.
    ref_count_total = defaultdict(int)
    for c in consumers:
        tree = _parse(c)
        if not tree:
            continue
        for name in _refs_in_tree(tree, all_public_names):
            ref_count_total[name] += 1

    rows = []
    for p, names in per_file_publics.items():
        relp = str(p.relative_to(ROOT))
        for name in sorted(names):
            # Refs on the def line itself don't count — ast.Name only appears
            # for arguments/decorators/etc., not for the def keyword. So zero
            # really means zero.
            if ref_count_total.get(name, 0) == 0:
                rows.append({
                    "name": name,
                    "file": relp,
                    "callers_anywhere": 0,
                })
    return rows


def _parse_env_file(path: Path) -> dict:
    """Tolerant .env parser (no python-dotenv dep): KEY=VALUE per line,
    ignores blanks, comments, and `export ` prefix. Returns {KEY: value}.
    Empty values count as 'set' for our purposes (intent to set; default no-op)."""
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("export "):
            s = s[7:].lstrip()
        if "=" not in s:
            continue
        k, _, v = s.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k:
            out[k] = v
    return out


def _env_assignments_in_code(scan):
    """Find code lines that DO `os.environ["X"] = ...` or `os.environ.setdefault("X",...)`,
    so flags set inline by callers (e.g. scripts/_audit_e2e) still count as 'set somewhere'."""
    pat = re.compile(
        r"""os\.environ\s*(?:\[\s*["']([A-Z0-9_]+)["']\s*\]\s*=|\.setdefault\(\s*["']([A-Z0-9_]+)["'])""")
    found = set()
    for p in scan:
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in pat.finditer(txt):
            found.add(m.group(1) or m.group(2))
    return found


def audit_dead_env_flags():
    """Flags with default-off (or empty) that are NEVER set in any .env file
    AND NEVER assigned via os.environ in code → their non-default branch is
    dead at runtime. EVIDENCE PROOF: also lists where each flag is read so
    you can see what branch dies. (Streamlit Cloud secrets can't be audited
    from the repo — note manually if a flag is set there.)
    """
    flags = audit_fallback_flags()
    env_files = [ROOT / ".env", ROOT / ".env.example"]
    set_in_env = set()
    for e in env_files:
        set_in_env |= set(_parse_env_file(e).keys())
    set_in_code = _env_assignments_in_code(_all_py_paths())

    OFF = {"", "0", "false", "no", "off"}
    rows = []
    for r in flags:
        default = (r["default"] or "").strip().lower()
        is_off_default = default in OFF
        if not is_off_default:
            continue
        flag = r["flag"]
        if flag in set_in_env:
            continue
        if flag in set_in_code:
            continue
        rows.append({
            "flag": flag,
            "default": r["default"],
            "first_seen_in": r["file"],
            "note": "default-off + not set in .env / .env.example / code → "
                    "branch dies at runtime (unless set in Streamlit Cloud secrets)",
        })
    return rows


def audit_dead_env_keys():
    """Keys present in .env / .env.example that NO code reads via
    os.getenv / os.environ.get / secret_or_env / direct os.environ[X]. Likely
    leftover from a deprecated integration (e.g. old scraper API keys after
    a swap to python-jobspy). REMOVAL FROM .env IS SAFE; remember real keys
    are usually also leaked in shell history → rotate them too if sensitive.
    """
    env_keys = set()
    for e in (ROOT / ".env", ROOT / ".env.example"):
        env_keys |= set(_parse_env_file(e).keys())

    # Phase 1: keys read via the canonical accessors (high confidence).
    read_keys = set()
    pat_read = re.compile(
        r"""(?:os\.getenv|os\.environ\.get|secret_or_env)\(\s*["']([A-Z0-9_]+)["']""")
    pat_index = re.compile(r"""os\.environ\s*\[\s*["']([A-Z0-9_]+)["']\s*\]""")
    # Phase 2: KEY name appears anywhere as a word (catches dynamic-loader
    # patterns like f"GROQ_API_KEY_{i}" and third-party libs that auto-read
    # their own env vars — e.g., LANGFUSE_PUBLIC_KEY picked up by langfuse).
    all_source = []
    for p in _all_py_paths():
        try:
            all_source.append(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            pass
    blob = "\n".join(all_source)
    for m in pat_read.finditer(blob):
        read_keys.add(m.group(1))
    for m in pat_index.finditer(blob):
        read_keys.add(m.group(1))

    # For each candidate dead key, do the substring-as-word check before flagging.
    dead = []
    for k in sorted(env_keys - read_keys):
        # Word-boundary search across all source — catches "GROQ_API_KEY" inside
        # f"GROQ_API_KEY_{i}" patterns AND lib-reads of "LANGFUSE_PUBLIC_KEY".
        # Strip the trailing _N suffix on numbered-rotation keys before checking
        # the stem (so GROQ_API_KEY_2 hits on "GROQ_API_KEY").
        stem = re.sub(r"_(\d+|[A-Z])$", "", k)
        candidates = {k, stem}
        if any(re.search(rf"\b{re.escape(c)}\b", blob) for c in candidates):
            continue
        dead.append({"key": k})
    return dead


def audit_orphan_tracked_paths():
    """git-ls-files at repo root: anything tracked at the top level that
    isn't an EXPECTED_ROOT_PATHS file, an EXPECTED_ROOT_DIRS dir, or under
    one of them. Catches stuff like `_audit_e2e/`, `tmp_e2e_n8n.py`,
    `e7eec71-revert.bak`. Heuristic — review before deleting (you may
    have a legitimate root file the audit doesn't know about)."""
    try:
        out = subprocess.check_output(
            ["git", "ls-files"], cwd=str(ROOT), text=True,
            stderr=subprocess.DEVNULL)
    except Exception as e:
        return [{"error": f"git ls-files failed: {e}"}]
    top_level = set()
    for line in out.splitlines():
        if not line:
            continue
        top = line.split("/", 1)[0]
        top_level.add(top)
    orphans = []
    for top in sorted(top_level):
        p = ROOT / top
        if top in EXPECTED_ROOT_PATHS:
            continue
        if p.is_dir() and top in EXPECTED_ROOT_DIRS:
            continue
        if top.startswith(".github") or top == ".github":
            continue
        # Common docs / config we don't gate
        if top.endswith(".md") or top.endswith(".toml") or top.endswith(".cfg"):
            continue
        orphans.append({"path": top, "kind": "dir" if p.is_dir() else "file"})
    return orphans


def audit_asset_sizes(min_mb: float = 1.0):
    """Top-level dir total bytes, sorted desc. Spotlights asset bloat
    (CVs/, diagnostics/runs/, graphify-out/, _r-adapt-render/, sessions/)."""
    rows = []
    for entry in sorted(ROOT.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith(".") or entry.name in {"venv", "__pycache__",
                                                         ".venv", "env"}:
            continue
        total = 0
        files = 0
        for r, _, fs in os.walk(entry):
            for fname in fs:
                fp = Path(r) / fname
                try:
                    total += fp.stat().st_size
                    files += 1
                except OSError:
                    pass
        mb = total / (1024 * 1024)
        if mb >= min_mb:
            rows.append({"dir": entry.name, "size_mb": round(mb, 1),
                         "files": files})
    return sorted(rows, key=lambda r: r["size_mb"], reverse=True)


def audit_stale_doc_symbols():
    """Symbols mentioned in repo .md docs that do NOT exist in the codebase
    anywhere (def/class). Common case: README/CHANGELOG/HANDOFF mentions a
    function or module name long after it was deleted. Use to find which
    docs to update (or to confirm a symbol was deleted from the wrong half).
    Heuristic: only checks names with underscores (avoids matching common
    English words) and >= 6 chars.
    """
    all_defs = set()
    for p in _all_py_paths():
        tree = _parse(p)
        if not tree:
            continue
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                all_defs.add(n.name)
        for sub in _public_top_defs(tree):
            all_defs.add(sub)

    # Also count modules as 'defs' for purposes of doc reference checking
    for p in _first_party_paths():
        rel = p.relative_to(ROOT).with_suffix("")
        all_defs.add(rel.name)

    docs = list(ROOT.glob("*.md")) + list((ROOT / "docs").glob("*.md")) \
        if (ROOT / "docs").exists() else list(ROOT.glob("*.md"))
    sym_pat = re.compile(r"\b([a-z][a-z0-9_]{4,})\b")  # snake_case-ish
    stale = defaultdict(list)  # doc -> [symbol]
    for d in docs:
        try:
            txt = d.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        # find candidate symbols (underscore-bearing, len ≥ 6)
        for m in set(sym_pat.findall(txt)):
            if "_" not in m:
                continue
            if m in all_defs:
                continue
            # Skip flag-y / env-y names that aren't python symbols
            if m.isupper():
                continue
            stale[str(d.relative_to(ROOT))].append(m)
    rows = []
    for doc, syms in sorted(stale.items()):
        if not syms:
            continue
        rows.append({"doc": doc, "missing_symbols": sorted(set(syms))[:20],
                     "count": len(set(syms))})
    return rows


# ───────────────────────── report ─────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    report = {
        "module_graph": audit_module_graph(),
        "dependencies": audit_dependencies(),
        "dead_function_candidates": audit_dead_functions(),
        "public_defs_zero_external_callers": audit_function_reachability(),
        "dead_env_flags": audit_dead_env_flags(),
        "dead_env_keys": audit_dead_env_keys(),
        "orphan_tracked_paths": audit_orphan_tracked_paths(),
        "asset_sizes_mb": audit_asset_sizes(),
        "stale_doc_symbols": audit_stale_doc_symbols(),
        "fallback_flags": audit_fallback_flags(),
        "scripts": audit_scripts(),
    }

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print("=" * 78)
    print(" REPO AUDIT v2 — evidence report (AST + git + fs; nothing deleted)")
    print("=" * 78)

    print("\n## 1. First-party modules imported by NOBODY (file-level dead)")
    dead = [r for r in report["module_graph"] if r["imported_by_count"] == 0]
    if not dead:
        print("   (none — every module is imported somewhere)")
    for r in dead:
        print(f"   ✗ {r['module']:40s} ({r['loc']} LOC)")

    print("\n## 2. Dependencies (requirements.txt) — verdicts")
    for r in report["dependencies"]:
        flag = ("✅" if r["directly_used"] else
                "↪" if r["required_by"] else
                "✗" if r["import_names"] else "?")
        print(f"   {flag} {r['dist']:30s} {r['verdict']}")
        if r["import_names"] and not r["directly_used"] and not r["required_by"]:
            print(f"        import-name(s) never imported: {r['import_names']}")

    print("\n## 3. Dead first-party function/class CANDIDATES (heuristic)")
    dfc = report["dead_function_candidates"]
    if not dfc:
        print("   (none flagged)")
    for r in dfc[:40]:
        print(f"   ? {r['name']:34s} {r['file']}")
    if len(dfc) > 40:
        print(f"   … +{len(dfc)-40} more")

    print("\n## 4. ★ PUBLIC defs with ZERO callers anywhere (function-level dead)")
    pfd = report["public_defs_zero_external_callers"]
    if not pfd:
        print("   (none — every public def is called somewhere)")
    for r in pfd[:50]:
        print(f"   ✗ {r['name']:34s} {r['file']}")
    if len(pfd) > 50:
        print(f"   … +{len(pfd)-50} more")
    print("   ⚠ CAVEAT: dynamic dispatch via JSON/YAML config (e.g. "
          "langgraph.json's `build_agent_graph` reference) won't be visible "
          "to AST. grep *.json *.yaml *.toml for any candidate before deleting.")

    print("\n## 5. ★ Env flags default-OFF AND never set in .env / code")
    def_ = report["dead_env_flags"]
    if not def_:
        print("   (none — every off-by-default flag is set somewhere)")
    for r in def_:
        print(f"   ✗ {r['flag']:30s} default={r['default']!r}  in {r['first_seen_in']}")

    print("\n## 6. ★ .env keys NEVER read by any code")
    dek = report["dead_env_keys"]
    if not dek:
        print("   (none — every env key is read somewhere)")
    for r in dek:
        print(f"   ✗ {r['key']}")

    print("\n## 7. ★ Tracked paths that look orphan at repo root")
    op = report["orphan_tracked_paths"]
    if not op:
        print("   (none — every tracked top-level path is expected)")
    for r in op:
        print(f"   ✗ {r['path']:30s} ({r['kind']})")

    print("\n## 8. ★ Top-level dirs by size (asset bloat watch)")
    for r in report["asset_sizes_mb"]:
        marker = "⚠️" if r["size_mb"] > 50 else " "
        print(f"   {marker} {r['size_mb']:7.1f} MB   {r['files']:>6} files   {r['dir']}")

    print("\n## 9. ★ Doc symbols missing from code (stale README/CHANGELOG/etc.)")
    sd = report["stale_doc_symbols"]
    if not sd:
        print("   (none)")
    for r in sd:
        print(f"   ✗ {r['doc']:35s}  {r['count']} missing: "
              f"{', '.join(r['missing_symbols'][:6])}"
              f"{' …' if r['count']>6 else ''}")

    print("\n## 10. Fallback / bypass flags inventory")
    for r in report["fallback_flags"]:
        print(f"   • {r['flag']:30s} default={r['default']!r:10s} {r['file']}")

    print("\n## 11. scripts/ inventory")
    for cat, names in report["scripts"].items():
        print(f"   {cat:7s} ({len(names)}): {', '.join(names[:6])}{' …' if len(names)>6 else ''}")

    print("\n" + "=" * 78)
    print(" v2 sections marked ★ are new. For EACH finding apply the playbook's")
    print(" two-directional rule (absence-of-use + what's-used-instead) before")
    print(" acting. Tier removals + run the runtime smoke per tier.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
