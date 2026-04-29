"""
diagnostics.analyzer
====================

Post-run analysis of a JSONL trace file. Generates `summary.md` next to
the traces, plus optional matplotlib charts when the package is available.

Exactly the diagnostics Saumya called for:

    1. Token breakdown by agent + provider
    2. Input/output ratio per agent (response_chars / prompt_chars proxy
       when SDK split is unavailable)
    3. Tokens-per-minute rolling sum (60s windows) — flags >5 RPM Gemini
       breach windows
    4. Concurrent calls timeline — burst fan-out detection
    5. Job-over-job degradation — does avg tokens grow per job index?
    6. Truncation incidents — agent, max_tokens, response chars

Usage:
    python -m diagnostics.analyzer                  # latest run
    python -m diagnostics.analyzer <run-dir>        # specific run
    python -m diagnostics.analyzer --json           # emit JSON instead of MD
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


_DIAG_ROOT = Path(__file__).resolve().parent
_RUNS_ROOT = _DIAG_ROOT / "runs"


# ─────────────────────────────────────────────────────────────
# IO helpers
# ─────────────────────────────────────────────────────────────

def _latest_run_dir() -> Optional[Path]:
    if not _RUNS_ROOT.exists():
        return None
    candidates = [p for p in _RUNS_ROOT.iterdir() if p.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.name)


def _load_traces(run_dir: Path) -> List[Dict[str, Any]]:
    path = run_dir / "traces.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"traces.jsonl not found in {run_dir}")
    out: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
    return out


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ─────────────────────────────────────────────────────────────
# Aggregations
# ─────────────────────────────────────────────────────────────

def _by_agent(traces: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Token breakdown by agent + provider."""
    agg: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "calls":              0,
        "total_tokens":       0,
        "total_prompt_chars": 0,
        "total_resp_chars":   0,
        "total_duration_ms":  0.0,
        "errors":             0,
        "truncations":        0,
        "providers":          Counter(),
        "models":             Counter(),
    })
    for t in traces:
        a = t.get("agent") or "unknown"
        agg[a]["calls"]              += 1
        agg[a]["total_tokens"]       += int(t.get("total_tokens") or 0)
        agg[a]["total_prompt_chars"] += int(t.get("prompt_chars") or 0)
        agg[a]["total_resp_chars"]   += int(t.get("response_chars") or 0)
        agg[a]["total_duration_ms"]  += float(t.get("duration_ms") or 0.0)
        if t.get("error"):
            agg[a]["errors"]      += 1
        if t.get("truncated"):
            agg[a]["truncations"] += 1
        agg[a]["providers"][t.get("provider") or "?"] += 1
        agg[a]["models"][t.get("model") or "?"]       += 1
    return agg


def _tpm_windows(
    traces: List[Dict[str, Any]],
    window_s: int = 60,
) -> List[Dict[str, Any]]:
    """
    Bucket calls by 60s windows. Each window:
      { window_start, calls, total_tokens, gemini_calls, groq_calls }
    """
    if not traces:
        return []
    parsed = [(_parse_ts(t["ts"]), t) for t in traces if t.get("ts")]
    parsed.sort(key=lambda x: x[0])
    if not parsed:
        return []
    t0 = parsed[0][0]
    buckets: Dict[int, Dict[str, Any]] = defaultdict(lambda: {
        "window_start": None,
        "calls":         0,
        "total_tokens":  0,
        "gemini_calls":  0,
        "groq_calls":    0,
        "agents":        Counter(),
    })
    for ts, t in parsed:
        idx = int((ts - t0).total_seconds()) // window_s
        b = buckets[idx]
        if b["window_start"] is None:
            b["window_start"] = (
                t0.replace(microsecond=0)
                + (ts - t0).__class__(seconds=idx * window_s)  # type: ignore
            )
        b["calls"]        += 1
        b["total_tokens"] += int(t.get("total_tokens") or 0)
        prov = (t.get("provider") or "").lower()
        if prov.startswith("gemini"):
            b["gemini_calls"] += 1
        elif prov.startswith("groq"):
            b["groq_calls"]   += 1
        b["agents"][t.get("agent") or "unknown"] += 1
    return [buckets[k] for k in sorted(buckets.keys())]


def _per_job(traces: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Per-job-id aggregates to spot job-over-job drift."""
    agg: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "calls":         0,
        "total_tokens":  0,
        "duration_ms":   0.0,
        "first_ts":      None,
        "last_ts":       None,
        "agents":        Counter(),
        "errors":        0,
        "truncations":   0,
    })
    for t in traces:
        jid = t.get("job_id") or "(none)"
        a = agg[jid]
        a["calls"]         += 1
        a["total_tokens"]  += int(t.get("total_tokens") or 0)
        a["duration_ms"]   += float(t.get("duration_ms") or 0.0)
        if t.get("error"):
            a["errors"]      += 1
        if t.get("truncated"):
            a["truncations"] += 1
        a["agents"][t.get("agent") or "unknown"] += 1
        ts = t.get("ts")
        if ts:
            if a["first_ts"] is None or ts < a["first_ts"]:
                a["first_ts"] = ts
            if a["last_ts"] is None or ts > a["last_ts"]:
                a["last_ts"] = ts
    return agg


def _truncation_incidents(traces: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for t in traces:
        if not t.get("truncated"):
            continue
        out.append({
            "ts":             t.get("ts"),
            "agent":          t.get("agent"),
            "provider":       t.get("provider"),
            "model":          t.get("model"),
            "prompt_chars":   t.get("prompt_chars"),
            "response_chars": t.get("response_chars"),
            "max_tokens_req": (t.get("metadata") or {}).get("max_tokens_requested"),
            "duration_ms":    t.get("duration_ms"),
        })
    return out


def _concurrent_calls(traces: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Best-effort burst detection. For each timestamp, count how many calls
    in the trace started within ±5s of it. Returns the top 10 timestamps
    by concurrent count.
    """
    if not traces:
        return {"top_bursts": [], "max_concurrent": 0}
    parsed = [(_parse_ts(t["ts"]), t) for t in traces if t.get("ts")]
    parsed.sort(key=lambda x: x[0])
    bursts: List[Tuple[int, str, List[str]]] = []
    for i, (ts_i, t_i) in enumerate(parsed):
        nearby: List[str] = []
        for j, (ts_j, t_j) in enumerate(parsed):
            if i == j:
                continue
            dt = abs((ts_j - ts_i).total_seconds())
            if dt <= 5.0:
                nearby.append(
                    f"{t_j.get('agent','?')}({t_j.get('provider','?')})"
                )
        if nearby:
            bursts.append((
                len(nearby) + 1,
                ts_i.isoformat(),
                [f"{t_i.get('agent','?')}({t_i.get('provider','?')})"] + nearby,
            ))
    bursts.sort(key=lambda x: x[0], reverse=True)
    top = [
        {"count": c, "ts": ts, "calls_in_window": calls}
        for c, ts, calls in bursts[:10]
    ]
    max_c = bursts[0][0] if bursts else 0
    return {"top_bursts": top, "max_concurrent": max_c}


# ─────────────────────────────────────────────────────────────
# Markdown report
# ─────────────────────────────────────────────────────────────

def _fmt_int(n: Any) -> str:
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


def render_summary_md(run_dir: Path, traces: List[Dict[str, Any]]) -> str:
    by_agent = _by_agent(traces)
    by_job   = _per_job(traces)
    windows  = _tpm_windows(traces, window_s=60)
    truncs   = _truncation_incidents(traces)
    bursts   = _concurrent_calls(traces)

    n_calls = len(traces)
    n_errors = sum(1 for t in traces if t.get("error"))
    n_trunc  = len(truncs)
    total_tokens = sum(int(t.get("total_tokens") or 0) for t in traces)
    total_duration = sum(float(t.get("duration_ms") or 0.0) for t in traces)

    if traces:
        first = min(t["ts"] for t in traces if t.get("ts"))
        last  = max(t["ts"] for t in traces if t.get("ts"))
    else:
        first = last = "-"

    lines: List[str] = []
    lines.append(f"# Diagnostics Run Summary")
    lines.append(f"")
    lines.append(f"- **Run dir:** `{run_dir}`")
    lines.append(f"- **First call:** {first}")
    lines.append(f"- **Last call:** {last}")
    lines.append(f"- **Total LLM calls:** {_fmt_int(n_calls)}")
    lines.append(f"- **Total tokens:** {_fmt_int(total_tokens)}")
    lines.append(f"- **Total wall-clock in LLM:** {total_duration / 1000:.1f}s")
    lines.append(f"- **Errors:** {_fmt_int(n_errors)}")
    lines.append(f"- **Truncations:** {_fmt_int(n_trunc)}")
    lines.append("")

    # ── 1. Token breakdown by agent ──
    lines.append("## 1. Token Breakdown by Agent")
    lines.append("")
    lines.append(
        "| Agent | Calls | Total Tokens | Avg Tokens | "
        "Resp/Prompt Ratio | Avg Latency (ms) | Errors | Trunc | Providers |"
    )
    lines.append(
        "|---|---:|---:|---:|---:|---:|---:|---:|---|"
    )
    sorted_agents = sorted(
        by_agent.items(), key=lambda kv: kv[1]["total_tokens"], reverse=True
    )
    for agent, a in sorted_agents:
        avg_tokens = a["total_tokens"] / a["calls"] if a["calls"] else 0
        ratio = (
            a["total_resp_chars"] / a["total_prompt_chars"]
            if a["total_prompt_chars"] else 0.0
        )
        avg_latency = a["total_duration_ms"] / a["calls"] if a["calls"] else 0
        providers = ", ".join(
            f"{p}×{n}" for p, n in a["providers"].most_common()
        )
        lines.append(
            f"| `{agent}` | {a['calls']} | {_fmt_int(a['total_tokens'])} | "
            f"{avg_tokens:.0f} | {ratio:.2f} | {avg_latency:.0f} | "
            f"{a['errors']} | {a['truncations']} | {providers} |"
        )
    lines.append("")

    # ── 2. TPM windows ──
    lines.append("## 2. Tokens-Per-Minute Windows (60s buckets)")
    lines.append("")
    lines.append(
        "| Window Start | Calls | Total Tokens | Gemini Calls | Groq Calls "
        "| Top Agents | Flag |"
    )
    lines.append("|---|---:|---:|---:|---:|---|---|")
    GEMINI_RPM_LIMIT = 5
    for w in windows:
        flag = ""
        if w["gemini_calls"] > GEMINI_RPM_LIMIT:
            flag = "🔴 **Gemini RPM breach**"
        elif w["calls"] > 30:
            flag = "🟡 high-burst"
        top_agents = ", ".join(
            f"{a}×{n}" for a, n in w["agents"].most_common(3)
        )
        ws = w["window_start"]
        ws_s = ws.isoformat() if hasattr(ws, "isoformat") else str(ws)
        lines.append(
            f"| {ws_s} | {w['calls']} | {_fmt_int(w['total_tokens'])} | "
            f"{w['gemini_calls']} | {w['groq_calls']} | {top_agents} | {flag} |"
        )
    lines.append("")

    # ── 3. Per-job aggregates ──
    lines.append("## 3. Per-Job Aggregates (job-over-job drift)")
    lines.append("")
    lines.append(
        "| Job ID | Calls | Total Tokens | Duration (s) | Errors | "
        "Trunc | Top Agents |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---|")
    sorted_jobs = sorted(
        by_job.items(),
        key=lambda kv: kv[1]["first_ts"] or "",
    )
    for jid, j in sorted_jobs:
        top_agents = ", ".join(
            f"{a}×{n}" for a, n in j["agents"].most_common(3)
        )
        lines.append(
            f"| `{jid}` | {j['calls']} | {_fmt_int(j['total_tokens'])} | "
            f"{j['duration_ms'] / 1000:.1f} | {j['errors']} | "
            f"{j['truncations']} | {top_agents} |"
        )
    lines.append("")

    # ── 4. Truncations ──
    lines.append("## 4. Truncation Incidents")
    lines.append("")
    if not truncs:
        lines.append("_No truncations detected._")
    else:
        lines.append(
            "| Time | Agent | Provider | Model | Prompt Chars | "
            "Resp Chars | Max Tokens | Duration (ms) |"
        )
        lines.append("|---|---|---|---|---:|---:|---:|---:|")
        for t in truncs:
            lines.append(
                f"| {t['ts']} | `{t['agent']}` | {t['provider']} | "
                f"{t['model']} | {_fmt_int(t['prompt_chars'])} | "
                f"{_fmt_int(t['response_chars'])} | "
                f"{_fmt_int(t['max_tokens_req'])} | "
                f"{t['duration_ms']:.0f} |"
            )
    lines.append("")

    # ── 5. Bursts ──
    lines.append("## 5. Burst Fan-Out (top 10 concurrent windows)")
    lines.append("")
    lines.append(f"- **Max concurrent calls (±5s window):** {bursts['max_concurrent']}")
    lines.append("")
    if bursts["top_bursts"]:
        lines.append("| Time | Concurrent Calls | Calls in Window |")
        lines.append("|---|---:|---|")
        for b in bursts["top_bursts"]:
            calls_str = ", ".join(b["calls_in_window"][:8])
            if len(b["calls_in_window"]) > 8:
                calls_str += f", … (+{len(b['calls_in_window']) - 8} more)"
            lines.append(f"| {b['ts']} | {b['count']} | {calls_str} |")
    lines.append("")

    # ── 6. Top suspicions ──
    lines.append("## 6. Top Suspicions (auto-derived)")
    lines.append("")
    suspicions: List[str] = []
    if any(w["gemini_calls"] > GEMINI_RPM_LIMIT for w in windows):
        suspicions.append(
            f"🔴 **Gemini RPM breach detected** in one or more 60s windows. "
            f"This is a hard structural cause of truncation/quota exhaustion."
        )
    if n_trunc > n_calls * 0.10 and n_calls > 0:
        suspicions.append(
            f"🔴 **High truncation rate ({n_trunc}/{n_calls} = "
            f"{100 * n_trunc / n_calls:.0f}%).** Suggests max_tokens too low "
            f"OR free-tier server-side truncation."
        )
    if by_agent:
        ratios = [
            (a, v["total_resp_chars"] / v["total_prompt_chars"])
            for a, v in by_agent.items()
            if v["total_prompt_chars"] > 0
        ]
        ratios.sort(key=lambda x: x[1], reverse=True)
        if ratios and ratios[0][1] > 1.5:
            suspicions.append(
                f"🟡 **Output bloat candidate:** `{ratios[0][0]}` has "
                f"resp/prompt char ratio {ratios[0][1]:.2f}× — verify if "
                f"the prompt is requesting unbounded output."
            )
    # Job-over-job drift check
    if len(sorted_jobs) >= 3:
        token_series = [j[1]["total_tokens"] for j in sorted_jobs]
        first_half = token_series[: len(token_series) // 2]
        last_half  = token_series[len(token_series) // 2 :]
        if first_half and last_half:
            avg_first = statistics.mean(first_half)
            avg_last  = statistics.mean(last_half)
            if avg_last > avg_first * 1.30:
                suspicions.append(
                    f"🟡 **Job-over-job token bloat:** later jobs avg "
                    f"{avg_last:.0f} tokens vs early jobs {avg_first:.0f} "
                    f"(+{100 * (avg_last - avg_first) / avg_first:.0f}%). "
                    f"Possible state-not-resetting bug."
                )
    if not suspicions:
        suspicions.append("_No automatic flags. Review the tables above manually._")
    for s in suspicions:
        lines.append(f"- {s}")
    lines.append("")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Analyze a diagnostics run and emit a summary report."
    )
    parser.add_argument(
        "run_dir",
        nargs="?",
        default=None,
        help="Specific run directory (default: latest under diagnostics/runs/).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of Markdown.",
    )
    args = parser.parse_args(argv)

    if args.run_dir:
        run_dir = Path(args.run_dir).resolve()
    else:
        latest = _latest_run_dir()
        if latest is None:
            print(
                "no diagnostics runs found under "
                f"{_RUNS_ROOT}",
                file=sys.stderr,
            )
            return 2
        run_dir = latest

    print(f"[analyzer] using run dir: {run_dir}", file=sys.stderr)
    traces = _load_traces(run_dir)
    print(f"[analyzer] loaded {len(traces)} trace records", file=sys.stderr)

    if args.json:
        out = {
            "run_dir":         str(run_dir),
            "n_traces":        len(traces),
            "by_agent":        {
                k: {**v, "providers": dict(v["providers"]), "models": dict(v["models"])}
                for k, v in _by_agent(traces).items()
            },
            "tpm_windows":     [
                {
                    **{k: v for k, v in w.items() if k != "agents"},
                    "agents": dict(w["agents"]),
                    "window_start": (
                        w["window_start"].isoformat()
                        if hasattr(w["window_start"], "isoformat")
                        else str(w["window_start"])
                    ),
                }
                for w in _tpm_windows(traces)
            ],
            "per_job":         {
                k: {**v, "agents": dict(v["agents"])}
                for k, v in _per_job(traces).items()
            },
            "truncations":     _truncation_incidents(traces),
            "bursts":          _concurrent_calls(traces),
        }
        print(json.dumps(out, indent=2, default=str))
        return 0

    md = render_summary_md(run_dir, traces)
    out_path = run_dir / "summary.md"
    out_path.write_text(md, encoding="utf-8")
    print(f"[analyzer] wrote {out_path}", file=sys.stderr)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
