# agents/cv_structure_reader.py
"""
Tier-2 CV structure reader  (Task 5 — "verify, don't predict").

WHEN THIS RUNS
--------------
The heuristic parser in `pdf_editor.extract_structure` / `_role_blocks` is
fast and free, but on some CVs it mis-classifies whole sections — e.g. it
dumps the entire experience block into one oversized "header" or "skills"
section, so `build_outline` yields ZERO roles and the tailor has nothing to
re-aim (corpus evidence: DebayudhRoy, CV_de, Saumyadeep). `pdf_editor.
validate_parse_integrity` detects exactly these catastrophes. When it fails
AND `REPLICA_LLM_READER=1`, the rebuild path can call this module to RE-DERIVE
the structure with a single Groq call instead of shipping an empty CV.

WHY IT IS SAFE
--------------
  • The LLM returns ONLY line-id groupings — never text, never coordinates,
    never paraphrase. Every piece of text and geometry comes from the line
    dicts we already extracted deterministically; the model just says "which
    lines belong together".
  • The reconstructed outline is VALIDATED deterministically before use
    (ids in range, single-assignment, role has a real header + ≥1 bullet,
    no section-swallowing giant bullet, roles > 0). If ANY check fails we
    return None and the caller keeps the heuristic result / routes to rebuild.
    A bad LLM call can never corrupt output — worst case it is ignored.
  • Default OFF. Only the rebuild path, only when the heuristic parse already
    failed, only when the env flag is set. One Groq call per failing CV.
  • Groq (llama-3.3-70b, free tier) only — no paid-tier dependency.

PUBLIC API
----------
  read_outline_llm(pdf_path) -> Optional[dict]
      Returns an outline in the SAME shape as `pdf_editor.build_outline`
      ({summary, roles:[{header, section, bullets:[{text,length}]}], skills})
      or None on any failure.
"""

import os
import re
import json
from typing import Any, Dict, List, Optional, Tuple

# ── Tunables (mirror validate_parse_integrity so the two agree on what a
# "clean" parse looks like). Kept module-level for easy calibration. ──
_LLM_READER_MODEL_TOKENS = int(os.getenv("CV_READER_MAX_TOKENS", "3000"))
_MAX_LINES_TO_LLM        = int(os.getenv("CV_READER_MAX_LINES", "240"))
_MIN_HEADER_LEN          = 3
_GIANT_BULLET_RATIO      = 0.6
_GIANT_BULLET_MIN_CHARS  = 350


def llm_reader_enabled() -> bool:
    """True when the Tier-2 LLM reader is allowed to run. Default OFF so it
    never changes behaviour until explicitly switched on for calibration."""
    return os.getenv("REPLICA_LLM_READER", "0").strip().lower() not in (
        "0", "false", "no", "off", ""
    )


# ─────────────────────────────────────────────────────────────
# 1. Collect raw lines with stable ids
# ─────────────────────────────────────────────────────────────

def _collect_lines_with_ids(pdf_path: str) -> List[Dict[str, Any]]:
    """
    Flatten every non-empty text line across all pages into one list, each
    annotated with a stable integer `id` (= index in this list), `bold`,
    `x0` and `page`. Reuses pdf_editor's per-page collector so the line dicts
    are identical in shape to what the heuristic parser sees.
    """
    import fitz  # local import — only paid for when the reader actually runs
    from agents.pdf_editor import _collect_page_lines, _line_is_bold

    lines: List[Dict[str, Any]] = []
    doc = fitz.open(pdf_path)
    try:
        for pi in range(doc.page_count):
            for ln in _collect_page_lines(doc[pi], pi):
                txt = (ln.get("text") or "").strip()
                if not txt:
                    continue  # markers / blank lines — never referenced
                try:
                    bold = bool(_line_is_bold(ln))
                except Exception:
                    bold = False
                bbox = ln.get("bbox") or [0, 0, 0, 0]
                lines.append({
                    "id":   len(lines),
                    "text": txt,
                    "bold": bold,
                    "x0":   float(bbox[0]),
                    "page": int(ln.get("page", pi) or pi),
                    # Geometry bridge (May 2026): keep the FULL source line
                    # dict ({text, bbox, spans, page, is_marker}) so the
                    # LLM-recovered groupings can be re-assembled into the
                    # `_role_blocks` shape `apply_edits` consumes — letting a
                    # collapse CV be edited IN PLACE (replica) instead of
                    # rebuilt. The previous version kept only `x0` and threw
                    # y0/x1/y1/spans away, which forced every collapse CV to
                    # the rebuild path.
                    "_src": ln,
                })
    finally:
        try:
            doc.close()
        except Exception:
            pass
    return lines


# ─────────────────────────────────────────────────────────────
# 2. Prompt
# ─────────────────────────────────────────────────────────────

_READER_PROMPT = """You are a precise CV STRUCTURE parser. You are given the \
numbered text lines of ONE candidate's CV, in reading order. Each line shows \
its id, a bold flag, and the exact text:

    [id] (B) text         <- (B) means the line is rendered BOLD

Group the lines into the CV's logical structure. Return STRICT JSON only — no \
prose, no markdown fences.

RULES (follow exactly):
1. Output line IDS ONLY. Never copy, rewrite, translate or invent any text.
2. Assign each line id to AT MOST ONE place. Do not reuse an id.
3. "summary_line_ids": the professional summary / profile / "about me" lines \
(may be empty []). The candidate's NAME, contact details, photo captions and \
section HEADINGS ("Professional Experience", "Skills", etc.) are NOT summary — \
leave them out entirely.
4. "roles": one object per JOB / position in the work-experience (and, if \
present, projects) section, in order. For each role:
     - "header_line_ids": the line(s) that name the company and/or job title \
and/or dates for THAT role (usually 1, sometimes 2 if split across lines).
     - "bullets": a list where each element is a list of line ids forming ONE \
bullet point. A bullet that WRAPS across several visual lines = one inner list \
with several ids. Do NOT merge two different bullets into one list.
5. Only real WORK / PROJECT positions become roles. Education entries, skills, \
certifications, awards and the summary are NOT roles.
6. If a line is a section heading, the name, or contact info, simply omit it.

REQUIRED JSON SHAPE:
{{
  "summary_line_ids": [int, ...],
  "roles": [
    {{"header_line_ids": [int, ...], "bullets": [[int, ...], [int, ...]]}}
  ]
}}

CV LINES:
{lines_block}

Return ONLY the JSON object."""


def _build_reader_prompt(lines: List[Dict[str, Any]]) -> str:
    rows = []
    for ln in lines[:_MAX_LINES_TO_LLM]:
        b = "(B) " if ln["bold"] else "    "
        rows.append(f"[{ln['id']}] {b}{ln['text']}")
    return _READER_PROMPT.format(lines_block="\n".join(rows))


# ─────────────────────────────────────────────────────────────
# 3. Robust JSON extraction
# ─────────────────────────────────────────────────────────────

def _extract_json(raw: str) -> Optional[Dict[str, Any]]:
    """Pull the first balanced JSON object out of an LLM response, tolerating
    ```json fences and leading/trailing prose. Returns None if unparseable."""
    if not raw:
        return None
    s = raw.strip()
    # Strip code fences if present.
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()
    # Fast path.
    try:
        return json.loads(s)
    except Exception:
        pass
    # Balanced-brace scan for the first {...} block.
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(s)):
        c = s[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start:i + 1])
                except Exception:
                    return None
    return None


# ─────────────────────────────────────────────────────────────
# 4. Assemble outline from id-groupings (text comes from OUR lines)
# ─────────────────────────────────────────────────────────────

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _join_ids(ids: Any, by_id: Dict[int, str], used: set) -> Tuple[str, int]:
    """Join the text of `ids` (in order), skipping invalid / already-used ids.
    Returns (joined_text, n_valid_ids_consumed). Marks consumed ids in `used`
    to enforce single-assignment across the whole document."""
    parts: List[str] = []
    n = 0
    if not isinstance(ids, list):
        return "", 0
    for raw_id in ids:
        try:
            i = int(raw_id)
        except Exception:
            continue
        if i in by_id and i not in used:
            used.add(i)
            parts.append(by_id[i])
            n += 1
    return _norm(" ".join(parts)), n


def _union_bbox(bboxes: List[List[float]]) -> List[float]:
    """Smallest rectangle covering every bbox. Caller guarantees non-empty."""
    return [
        min(b[0] for b in bboxes),
        min(b[1] for b in bboxes),
        max(b[2] for b in bboxes),
        max(b[3] for b in bboxes),
    ]


def _synth_header_line(text: str, srcs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Build a `_role_blocks`-shaped header line dict from the source lines the
    LLM grouped as this role's header. None when the role had no header line."""
    if not srcs:
        return None
    return {
        "text":  text,
        "bbox":  _union_bbox([s["bbox"] for s in srcs]),
        "spans": srcs[0].get("spans", []),
        "page":  int(srcs[0].get("page", 0) or 0),
    }


def _synth_summary_section(srcs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Build an `extract_structure`-shaped summary section from the source
    lines the LLM grouped as the summary. `apply_edits` feeds this straight to
    `_apply_summary_edit`, which only needs `lines` (each with bbox/spans/page).
    """
    if not srcs:
        return None
    return {
        "type":         "summary",
        "heading":      "",
        "heading_bbox": None,
        "page":         int(srcs[0].get("page", 0) or 0),
        "lines":        srcs,
        "synthetic":    True,
    }


def _assemble_outline(
    data: Dict[str, Any], lines: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Assemble BOTH the text outline (what the tailor LLM sees) AND a geometry
    structure (what `apply_edits` needs to edit in place) from the SAME id
    groupings in ONE pass, sharing one `used` set. Building them together is
    load-bearing: the diff is keyed by role-header text and bullet INDEX, so
    the geometry roles MUST be the same set, in the same order, with the same
    per-role bullet count as the text roles — otherwise a rewrite lands on the
    wrong bullet. The geometry is attached as `_geometry` and validated /
    dropped by the caller; consumers that only want text ignore the key.
    """
    by_text = {ln["id"]: ln["text"] for ln in lines}
    by_src  = {ln["id"]: ln.get("_src") for ln in lines}
    used: set = set()

    def consume(ids: Any) -> Tuple[str, List[Dict[str, Any]]]:
        """Join text + collect source line dicts for `ids` in order, skipping
        invalid / already-used ids and marking consumed ids in `used`."""
        parts: List[str] = []
        srcs:  List[Dict[str, Any]] = []
        if not isinstance(ids, list):
            return "", srcs
        for raw_id in ids:
            try:
                i = int(raw_id)
            except Exception:
                continue
            if i in by_text and i not in used:
                used.add(i)
                parts.append(by_text[i])
                s = by_src.get(i)
                if isinstance(s, dict) and s.get("bbox"):
                    srcs.append(s)
        return _norm(" ".join(parts)), srcs

    summary, summary_srcs = consume(data.get("summary_line_ids"))

    roles_out: List[Dict[str, Any]] = []
    geo_roles: List[Dict[str, Any]] = []
    for role in (data.get("roles") or []):
        if not isinstance(role, dict):
            continue
        header, header_srcs = consume(role.get("header_line_ids"))
        bullets:     List[Dict[str, Any]] = []
        geo_bullets: List[Dict[str, Any]] = []
        for grp in (role.get("bullets") or []):
            btext, bsrcs = consume(grp)
            # Require BOTH text and geometry so the text-outline bullet list
            # and the geometry bullet_groups stay index-for-index identical.
            if btext and bsrcs:
                bullets.append({"text": btext, "length": len(btext)})
                geo_bullets.append({
                    "lines":             bsrcs,
                    "text":              btext,
                    "total_char_length": len(btext),
                })
        if header or bullets:
            roles_out.append({
                "header":  header,
                "section": "experience",
                "bullets": bullets,
            })
            geo_roles.append({
                "header_text":   header,
                "header_line":   _synth_header_line(header, header_srcs),
                "bullet_groups": geo_bullets,
                "sub_lines":     [],
            })

    return {
        "summary": summary,
        "roles":   roles_out,
        "skills":  [],
        "_geometry": {
            "roles":           geo_roles,
            "summary_section": _synth_summary_section(summary_srcs),
        },
    }


# ─────────────────────────────────────────────────────────────
# 5. Validate the assembled outline (deterministic gate)
# ─────────────────────────────────────────────────────────────

def validate_llm_outline(outline: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deterministic health check on the LLM-built outline, mirroring the
    catastrophe signatures in pdf_editor.validate_parse_integrity but on the
    outline shape. Returns {ok, score, issues, n_roles, n_bullets}.
    """
    issues: List[str] = []
    roles = outline.get("roles") or []
    usable = [r for r in roles if (r.get("bullets") or [])]
    n_roles = len(usable)
    n_bullets = sum(len(r.get("bullets") or []) for r in usable)

    score = 100

    # Hard: a CV with no parsed roles is the very failure we are trying to fix.
    if n_roles == 0:
        issues.append("LLM outline has 0 usable roles")
        score -= 60

    total_bullet_chars = 0
    for r in usable:
        header = _norm(r.get("header") or "")
        bts = r.get("bullets") or []

        # Empty / fragment / bare-date header on a role that has bullets.
        if not header:
            issues.append("LLM role has an empty header")
            score -= 30
        elif len(header) < _MIN_HEADER_LEN:
            issues.append(f"LLM role header is a fragment: {header!r}")
            score -= 20
        else:
            try:
                from agents.pdf_editor import _is_bare_date_line
                if _is_bare_date_line(header):
                    issues.append(f"LLM role header is a bare date: {header!r}")
                    score -= 30
            except Exception:
                pass

        for b in bts:
            total_bullet_chars += int(b.get("length", len(b.get("text", ""))))

    # Section-swallowing giant bullet across the whole outline.
    if total_bullet_chars > 300:
        all_b = [b for r in usable for b in (r.get("bullets") or [])]
        if all_b:
            biggest = max(all_b, key=lambda b: int(b.get("length", 0)))
            blen = int(biggest.get("length", 0))
            if blen >= _GIANT_BULLET_RATIO * total_bullet_chars and (
                (len(all_b) >= 2 and blen > _GIANT_BULLET_MIN_CHARS)
                or (len(all_b) == 1 and blen > 550)
            ):
                issues.append(f"LLM outline has a section-swallowing bullet "
                              f"({blen} chars)")
                score -= 45

    score = max(0, score)
    return {
        "ok":        score >= 60 and n_roles >= 1,
        "score":     score,
        "issues":    issues,
        "n_roles":   n_roles,
        "n_bullets": n_bullets,
    }


def validate_geometry_blocks(geometry: Optional[Dict[str, Any]]) -> bool:
    """
    Deterministic safety gate on the LLM-recovered GEOMETRY before it is used
    to edit a PDF in place. The text outline already passed
    `validate_llm_outline`; this checks the *coordinates* are sane enough to
    redact-and-redraw without corrupting the page. ANY failure returns False,
    which makes the caller drop the geometry and fall back to rebuild — a bad
    grouping can never corrupt output, only forfeit the in-place path.

    Checks (all must hold):
      • ≥1 role with ≥1 bullet, ≥2 bullets total (a real experience section).
      • Every bullet line has a non-degenerate bbox (x1>x0, y1>y0).
      • Every bullet group lies on a SINGLE page (apply_edits cannot redact a
        bullet across a page break).
      • Each role's header (when present) sits at or above its first bullet —
        a header BELOW its bullets signals a mis-grouped block.
      • No two bullet lines share an (almost) identical bbox — a duplicate
        signals two bullets pointing at the same glyphs (double-redaction).
    """
    if not geometry:
        return False
    roles = geometry.get("roles") or []
    usable = [r for r in roles if (r.get("bullet_groups") or [])]
    if not usable:
        return False

    total_bullets = 0
    seen_keys: set = set()
    for r in usable:
        groups = r.get("bullet_groups") or []
        first_bullet_top: Optional[float] = None
        for b in groups:
            blines = b.get("lines") or []
            if not blines:
                return False
            if len({int(ln.get("page", -1)) for ln in blines}) != 1:
                return False  # cross-page bullet
            for ln in blines:
                bb = ln.get("bbox")
                if not bb or len(bb) != 4:
                    return False
                if not (bb[2] > bb[0] and bb[3] > bb[1]):
                    return False  # degenerate / zero-area
                key = (
                    int(ln.get("page", -1)),
                    round(float(bb[0]), 1), round(float(bb[1]), 1),
                    round(float(bb[2]), 1), round(float(bb[3]), 1),
                )
                if key in seen_keys:
                    return False  # same glyph box used by two bullets
                seen_keys.add(key)
                if first_bullet_top is None or bb[1] < first_bullet_top:
                    first_bullet_top = bb[1]
            total_bullets += 1
        hl = r.get("header_line") or {}
        hbb = hl.get("bbox")
        # Header must not sit below its own bullets (2pt slack for baseline).
        if hbb and first_bullet_top is not None and hbb[1] > first_bullet_top + 2.0:
            return False

    return total_bullets >= 2


# ─────────────────────────────────────────────────────────────
# 6. Public entry point
# ─────────────────────────────────────────────────────────────

def read_outline_llm(
    pdf_path: str, verbose: bool = False
) -> Optional[Dict[str, Any]]:
    """
    Re-derive the CV outline with one Groq call. Returns a build_outline-shaped
    dict on success, or None on ANY failure (no lines, LLM error, unparseable
    JSON, or the assembled outline fails validation). Never raises.
    """
    try:
        lines = _collect_lines_with_ids(pdf_path)
    except Exception as e:
        if verbose:
            print(f"   [llm-reader] line collection failed: {type(e).__name__}: {e}")
        return None
    if len(lines) < 5:
        if verbose:
            print(f"   [llm-reader] too few lines ({len(lines)}) — skipping")
        return None

    prompt = _build_reader_prompt(lines)
    try:
        from agents.llm_client import chat_fast
        raw = chat_fast(prompt, max_tokens=_LLM_READER_MODEL_TOKENS, temperature=0.0)
    except Exception as e:
        if verbose:
            print(f"   [llm-reader] Groq call failed: {type(e).__name__}: {e}")
        return None

    data = _extract_json(raw or "")
    if not isinstance(data, dict):
        if verbose:
            print("   [llm-reader] could not parse JSON from LLM response")
        return None

    outline = _assemble_outline(data, lines)
    report = validate_llm_outline(outline)
    if verbose:
        print(f"   [llm-reader] outline: roles={report['n_roles']} "
              f"bullets={report['n_bullets']} score={report['score']} "
              f"ok={report['ok']}")
        for iss in report["issues"]:
            print(f"       ! {iss}")
    if not report["ok"]:
        return None

    # ── Geometry bridge gate ──────────────────────────────────────────
    # The text outline is good. Now decide whether the recovered GEOMETRY is
    # safe to edit in place. Keep `_geometry` only if it passes the
    # deterministic coordinate gate; otherwise drop it so the caller routes
    # to rebuild instead of risking an in-place corruption. Either way the
    # text outline is returned, so a failed geometry check never loses the
    # role recovery — it only forfeits the replica path for this CV.
    geometry = outline.pop("_geometry", None)
    geo_ok = validate_geometry_blocks(geometry)
    if geo_ok:
        outline["_geometry"] = geometry
    if verbose:
        if geo_ok:
            n_geo_bullets = sum(
                len(r.get("bullet_groups") or [])
                for r in (geometry.get("roles") or [])
            )
            print(f"   [llm-reader] geometry bridge: VALID "
                  f"({len(geometry.get('roles') or [])} roles, "
                  f"{n_geo_bullets} bullets) — replica in-place available")
        else:
            print("   [llm-reader] geometry bridge: unavailable "
                  "(failed coordinate gate) — caller will route to rebuild")

    outline["_source"] = "llm_reader"
    outline["_validation"] = report
    return outline


# ─────────────────────────────────────────────────────────────
# Manual test:  python -m agents.cv_structure_reader <pdf> [<pdf> ...]
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        print("=" * 78)
        print(os.path.basename(p))
        out = read_outline_llm(p, verbose=True)
        if not out:
            print("  -> None (reader declined / validation failed)")
            continue
        print(f"  summary: {out['summary'][:120]!r}")
        for r in out["roles"]:
            print(f"  role: {r['header'][:60]!r} ({len(r['bullets'])} bullets)")
            for b in r["bullets"][:3]:
                print(f"       - {b['text'][:80]!r}")
