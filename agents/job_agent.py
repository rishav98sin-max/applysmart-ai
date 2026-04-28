# agents/job_agent.py

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple, TypedDict

from groq import Groq, RateLimitError
from langgraph.graph import END, StateGraph
from dotenv import load_dotenv

from agents.application_tracker import (
    applied_urls  as _applied_urls,
    filter_out_applied,
    mark_shown    as _mark_shown,
)
from agents.cv_parser import parse_cv
from agents.planner import build_plan
from agents.job_scraper import boards_fallback_sequence, scrape_jobs
from agents.job_matcher import match_cv_to_job
from agents.cv_tailor import tailor_cv
from agents.cv_diff_tailor import tailor_cv_diff
from agents.pdf_editor import (
    apply_edits as apply_pdf_edits,
    build_outline as _build_outline,
    detect_replica_compatibility as _detect_replica_compatibility,
)
from agents.reviewer    import review_tailored_cv, ACCEPT_THRESHOLD as REVIEWER_ACCEPT_THRESHOLD
from agents.cover_letter_generator import generate_cover_letter
from agents.cv_style_agent import build_style_profile
from agents.pdf_formatter import (
    extract_cv_style,
    generate_cv_pdf_styled,
    generate_cover_letter_pdf_styled,
)
from agents.email_agent import send_email
from agents.runtime import OUTPUT_DIR
from agents.llm_client import chat_quality

load_dotenv(override=True)

MAX_SUPERVISOR_CYCLES = 32

LLM_SUPERVISOR_ENABLED = os.getenv("LLM_SUPERVISOR", "1").strip().lower() not in (
    "0", "false", "no", "off",
)

LLM_SUPERVISOR_SKIP_SINGLE = os.getenv("LLM_SUPERVISOR_SKIP_SINGLE", "1").strip().lower() not in (
    "0", "false", "no", "off",
)

MAX_TAILOR_RETRIES = int(os.getenv("MAX_TAILOR_RETRIES", "1"))

HARD_TERMINAL_STATUSES = frozenset({
    "validation_failed",
    "parse_failed",
    "scrape_failed",
    "budget_exceeded",
    "email_failed",
    "completed",
    "awaiting_send",
    "halted_max_steps",
    "halted_user_stop",
})

TERMINAL_STATUSES = HARD_TERMINAL_STATUSES | frozenset({
    "no_jobs_found", "no_matches",
})

SETUP_ROUTES: Dict[str, str] = {
    "starting":        "validate_inputs",
    "validated":       "parse_cv",
    "cv_parsed":       "extract_cv_style",
    "style_extracted": "planner",
    "plan_ready":      "scrape_jobs",
    "documents_ready": "send_email",
}

ROUTE_END = "__END__"



def _extract_json_object(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return {}


def _groq_supervisor_completion(prompt: str, max_tokens: int = 220) -> str:
    # C1: removed 3-retry exception loop. chat_quality already rotates Groq
    # keys internally; module-level retries on top just multiply token waste
    # on real exhaustion. On exception, return "" — the supervisor's caller
    # treats empty as "use deterministic fallback plan".
    from agents.runtime import track_llm_call
    track_llm_call(agent="supervisor")
    try:
        return chat_quality(prompt, max_tokens=max_tokens, temperature=0.1)
    except Exception as e:
        print(f"   ❌ Supervisor LLM error: {type(e).__name__}: {e}")
        return ""


# ─────────────────────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────────────────────

class AgentState(TypedDict):
    cv_path:             str
    cv_text:             str
    job_title:           str
    location:            str
    num_jobs:            int
    match_threshold:     int
    user_email:          str
    candidate_name:      str
    source:              str
    jobs_found:          List[Any]
    matched_jobs:        List[Any]
    skipped_jobs:        List[Any]
    errors:              List[str]
    status:              str
    steps_taken:         int
    messages:            List[Dict[str, Any]]
    supervisor_cycles:   int
    routing_decision:    str
    scrape_retry_count:  int
    preferred_job_board: str
    scrape_boards_tried: List[str]
    style_profile:       Dict[str, Any]
    # ── Agentic additions (Phase 1) ──────────────────────
    plan:                Dict[str, Any]
    scrape_round:        int
    current_bundle:      Dict[str, Any]
    supervisor_trace:    List[Dict[str, Any]]
    # ── Phase 2 placeholders ────────────────────────────
    review_results:      Dict[str, Any]
    tailor_attempts:     Dict[str, int]
    # ── Deploy-hardening additions (Phase 3) ─────────────
    output_dir:          str
    session_id:          str
    llm_budget:          Dict[str, Any]
    preview_mode:        bool
    cv_collection:       str
    experience_level:    str              # ✅ NEW
    progress_callback:   Any              # ✅ NEW


def _append_handoff(state: AgentState, entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = list(state.get("messages", []))
    out.append(entry)
    return out


def _report_progress(state: AgentState, stage: str, detail: str = "") -> None:
    """Safely call progress callback if available."""
    callback = state.get("progress_callback")
    if callback and callable(callback):
        try:
            callback(stage, detail)
        except Exception as e:
            print(f"   ⚠️  Progress callback error: {e}")


def _check_stop_requested(state: AgentState) -> bool:
    """Check if stop was requested via session state."""
    try:
        import streamlit as st
        return st.session_state.get("_stop_requested", False)
    except Exception:
        return False


def _resolve_output_dir(state: AgentState) -> str:
    d = state.get("output_dir")
    if d:
        os.makedirs(d, exist_ok=True)
        return d
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    return OUTPUT_DIR


def _allowed_next_workers(state: AgentState) -> List[str]:
    status = state.get("status", "starting")

    if status in HARD_TERMINAL_STATUSES:
        return []

    nxt = SETUP_ROUTES.get(status)
    if nxt is not None:
        return [nxt]

    plan = state.get("plan") or {}
    qb   = plan.get("quality_bar") or {}
    bundles       = plan.get("keyword_bundles") or []
    scrape_round  = state.get("scrape_round", 0)
    max_rounds    = int(qb.get("max_scrape_rounds", min(2, len(bundles) or 1)))
    matched_count = len(state.get("matched_jobs") or [])
    min_matches   = int(qb.get("min_matches", 1))
    can_rescrape  = scrape_round < max_rounds and scrape_round < len(bundles)

    if status == "jobs_scraped":
        opts = ["match_jobs"]
        if can_rescrape:
            opts.append("scrape_jobs")
        return opts

    if status == "no_jobs_found":
        opts: List[str] = []
        if can_rescrape:
            opts.append("scrape_jobs")
        opts.append(ROUTE_END)
        return opts

    if status == "jobs_matched":
        opts = ["tailor_and_generate"]
        if can_rescrape and matched_count < min_matches:
            opts.append("scrape_jobs")
        return opts

    if status == "no_matches":
        opts = []
        if can_rescrape:
            opts.append("scrape_jobs")
        opts.append(ROUTE_END)
        return opts

    return []


def _normalize_llm_next(raw: str) -> str:
    t = (raw or "").strip().lower()
    if t in ("end", "stop", "finish", "done", ROUTE_END.lower()):
        return ROUTE_END
    return raw.strip()


def _state_summary_for_supervisor(state: AgentState) -> str:
    plan = state.get("plan") or {}
    bundles = plan.get("keyword_bundles") or []
    qb = plan.get("quality_bar") or {}
    rd = state.get("scrape_round", 0)
    cur_bundle = state.get("current_bundle") or {}
    matched = state.get("matched_jobs") or []
    skipped = state.get("skipped_jobs") or []
    best_score = max((j.get("match_score", 0) for j in matched + skipped), default=0)
    errs = state.get("errors") or []
    next_idx = rd
    next_bundle = bundles[next_idx] if 0 <= next_idx < len(bundles) else None

    lines = [
        f"status               : {state.get('status')}",
        f"scrape_round         : {rd} / max {qb.get('max_scrape_rounds', '?')}",
        f"total_keyword_bundles: {len(bundles)}",
    ]
    if cur_bundle:
        lines.append(
            f"current_bundle       : {cur_bundle.get('title')!r} @ {cur_bundle.get('location')!r}"
        )
    if next_bundle:
        lines.append(
            f"next_bundle_if_rescr : {next_bundle.get('title')!r} @ {next_bundle.get('location')!r} "
            f"(reason: {next_bundle.get('reason','')})"
        )
    lines += [
        f"jobs_found_last_scrape: {len(state.get('jobs_found') or [])}",
        f"matched_total        : {len(matched)}  (min_matches_target={qb.get('min_matches','?')})",
        f"skipped_total        : {len(skipped)}",
        f"best_match_score     : {best_score}/100  (min_score={qb.get('min_score','?')})",
    ]
    if errs:
        lines.append(f"recent_errors        : {'; '.join(errs[-2:])}")
    return "\n".join(lines)


def _pick_route_supervisor(
    state: AgentState,
    allowed: List[str],
) -> Tuple[str, str, str]:
    if not allowed:
        return ROUTE_END, "no allowed transitions — stopping", "rule"

    if len(allowed) == 1:
        only = allowed[0]
        if only == ROUTE_END:
            return ROUTE_END, "pipeline stop (only option)", "shortcut"
        if (not LLM_SUPERVISOR_ENABLED) or LLM_SUPERVISOR_SKIP_SINGLE:
            return only, "single allowed transition", "shortcut"

    if not LLM_SUPERVISOR_ENABLED:
        return allowed[0], "LLM supervisor disabled — first allowlist option", "rule"

    plan = state.get("plan") or {}
    summary = _state_summary_for_supervisor(state)
    trace = state.get("supervisor_trace") or []
    trace_tail = trace[-3:]
    trace_str = (
        "\n".join(
            f"  [{i}] → {t.get('action')}: {t.get('reasoning','')}"
            for i, t in enumerate(trace_tail)
        )
        if trace_tail else "  (none)"
    )

    allowed_labels = []
    for a in allowed:
        if a == ROUTE_END:
            allowed_labels.append('"end" — stop the pipeline')
        elif a == "scrape_jobs":
            allowed_labels.append('"scrape_jobs" — run ANOTHER scrape round with the NEXT keyword bundle')
        elif a == "match_jobs":
            allowed_labels.append('"match_jobs" — score the jobs from the last scrape against the CV')
        elif a == "tailor_and_generate":
            allowed_labels.append('"tailor_and_generate" — tailor CV + cover letter for every matched job')
        elif a == "send_email":
            allowed_labels.append('"send_email" — email the user the tailored documents')
        else:
            allowed_labels.append(f'"{a}" — run that worker node')

    prompt = f"""You are the SUPERVISOR of an agentic job-application pipeline.

Your job: look at the current world state below and pick the BEST next action
from the allowed options. You MUST pick exactly one allowed option.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PLAN (drafted at the start by the planner agent)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{json.dumps(plan, indent=2)[:1200]}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CURRENT STATE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{summary}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RECENT SUPERVISOR DECISIONS (most recent last)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{trace_str}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ALLOWED NEXT ACTIONS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{chr(10).join(f"- {x}" for x in allowed_labels)}

Decision guidance:
- If we already met the plan's quality bar (matched_total >= min_matches AND
  best_match_score >= min_score), proceed toward tailoring/email. Do NOT
  keep scraping for the sake of it.
- If matched_total is short of min_matches AND we still have scrape rounds
  left, prefer 'scrape_jobs' to try the next keyword bundle.
- Never loop endlessly: respect max_scrape_rounds.

Reply with ONLY valid JSON (no markdown):
{{"next": "<one of {json.dumps(allowed)}>", "rationale": "<one concise sentence>"}}

Use "{ROUTE_END}" as the value when you choose end.
"""

    text = _groq_supervisor_completion(prompt)
    data = _extract_json_object(text)
    nxt = _normalize_llm_next(str(data.get("next", "")))
    rationale = str(data.get("rationale", "")).strip() or "(no rationale)"

    if nxt not in allowed:
        fix = _groq_supervisor_completion(
            prompt
            + f"\n\nYour previous answer {nxt!r} was invalid. 'next' MUST be one of {allowed}.",
            max_tokens=160,
        )
        data2 = _extract_json_object(fix)
        nxt = _normalize_llm_next(str(data2.get("next", "")))
        rationale = str(data2.get("rationale", rationale)).strip()
        if nxt not in allowed:
            fallback = allowed[0]
            return (
                fallback,
                f"LLM returned invalid next — defaulting to {fallback}. Raw: {text[:200]!r}",
                "rule",
            )

    return nxt, rationale, "llm"


def supervisor_node(state: AgentState) -> AgentState:
    cycle = state.get("supervisor_cycles", 0) + 1
    status = state.get("status", "starting")
    base = {**state, "supervisor_cycles": cycle}

    # Check for stop request
    if _check_stop_requested(state):
        print(f"   ⏹️  Stop requested - cancelling agent execution")
        return {
            **base,
            "status": "halted_user_stop",
            "routing_decision": ROUTE_END,
            "errors": state["errors"] + ["Execution stopped by user"],
            "messages": _append_handoff(
                state,
                {
                    "from":   "supervisor",
                    "action": "halt",
                    "reason": "user_stop",
                    "cycle":  cycle,
                },
            ),
        }

    if cycle > MAX_SUPERVISOR_CYCLES:
        return {
            **base,
            "status": "halted_max_steps",
            "routing_decision": ROUTE_END,
            "errors": state["errors"] + [
                "Supervisor: max coordination cycles exceeded — stopping."
            ],
            "messages": _append_handoff(
                state,
                {
                    "from":   "supervisor",
                    "action": "halt",
                    "reason": "max_supervisor_cycles",
                    "cycle":  cycle,
                },
            ),
        }

    if status in HARD_TERMINAL_STATUSES:
        return {
            **base,
            "routing_decision": ROUTE_END,
            "messages": _append_handoff(
                state,
                {
                    "from":             "supervisor",
                    "observed":         status,
                    "dispatch_to":      "end",
                    "cycle":            cycle,
                    "llm_supervisor":   False,
                },
            ),
        }

    allowed = _allowed_next_workers(base)
    if not allowed:
        return {
            **base,
            "routing_decision": ROUTE_END,
            "messages": _append_handoff(
                state,
                {
                    "from":        "supervisor",
                    "observed":    status,
                    "dispatch_to": "end",
                    "cycle":       cycle,
                    "note":        "no allowed workers",
                },
            ),
        }

    decision, rationale, mode = _pick_route_supervisor(base, allowed)

    patched: Dict[str, Any] = {}
    if decision == "scrape_jobs":
        plan = base.get("plan") or {}
        bundles = plan.get("keyword_bundles") or []
        rd = base.get("scrape_round", 0)
        if 0 <= rd < len(bundles):
            patched["current_bundle"] = bundles[rd]
        patched["status"]      = "dispatching_scrape"
        patched["jobs_found"]  = []

    entry = {
        "from":           "supervisor",
        "observed":       status,
        "dispatch_to":    "end" if decision == ROUTE_END else decision,
        "routing":        decision,
        "rationale":      rationale,
        "mode":           mode,
        "cycle":          cycle,
        "llm_supervisor": mode == "llm",
    }

    trace = list(base.get("supervisor_trace") or [])
    trace.append({
        "cycle":        cycle,
        "observed":     status,
        "action":       "end" if decision == ROUTE_END else decision,
        "reasoning":    rationale,
        "mode":         mode,
        "allowed":      allowed,
    })

    return {
        **base,
        **patched,
        "routing_decision":  decision,
        "supervisor_trace":  trace,
        "messages":          _append_handoff(state, entry),
    }


def route_from_supervisor(state: AgentState) -> str:
    rd = state.get("routing_decision", "")
    if rd == ROUTE_END or rd == "":
        return END
    return rd


# ─────────────────────────────────────────────────────────────
# NODE 1 — VALIDATE INPUTS
# ─────────────────────────────────────────────────────────────

def validate_inputs_node(state: AgentState) -> AgentState:
    print("\n✅ WORKER validate_inputs: Validating inputs...")
    _report_progress(state, "Validating inputs")
    errors = []

    if not state.get("cv_path") or not os.path.exists(state["cv_path"]):
        errors.append("CV file not found.")
    if not state.get("job_title", "").strip():
        errors.append("Job title is required.")
    if not state.get("user_email", "").strip() or "@" not in state.get("user_email", ""):
        errors.append("Valid email address is required.")
    if not state.get("candidate_name", "").strip():
        errors.append("Candidate name is required.")

    if errors:
        return {
            **state,
            "errors":   errors,
            "status":   "validation_failed",
            "messages": _append_handoff(
                state,
                {"from": "validate_inputs", "ok": False, "errors": errors},
            ),
        }

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    return {
        **state,
        "status":      "validated",
        "steps_taken": state["steps_taken"] + 1,
        "messages":    _append_handoff(
            state,
            {"from": "validate_inputs", "ok": True, "output_status": "validated"},
        ),
    }


# ─────────────────────────────────────────────────────────────
# NODE 2 — PARSE CV
# ─────────────────────────────────────────────────────────────

def parse_cv_node(state: AgentState) -> AgentState:
    print("\n📄 WORKER parse_cv: Parsing CV...")
    _report_progress(state, "Parsing CV")
    try:
        cv_text = parse_cv(state["cv_path"])
        if not cv_text or len(cv_text.strip()) < 50:
            return {
                **state,
                "errors": state["errors"] + ["CV parsing returned empty text."],
                "status": "parse_failed",
                "messages": _append_handoff(
                    state,
                    {"from": "parse_cv", "ok": False, "reason": "empty_or_short"},
                ),
            }
        print(f"   CV parsed: {len(cv_text)} characters")
        return {
            **state,
            "cv_text":     cv_text,
            "status":      "cv_parsed",
            "steps_taken": state["steps_taken"] + 1,
            "messages":    _append_handoff(
                state,
                {
                    "from":          "parse_cv",
                    "ok":            True,
                    "chars":         len(cv_text),
                    "output_status": "cv_parsed",
                },
            ),
        }
    except Exception as e:
        return {
            **state,
            "errors": state["errors"] + [f"CV parse error: {str(e)}"],
            "status": "parse_failed",
            "messages": _append_handoff(
                state,
                {"from": "parse_cv", "ok": False, "error": str(e)},
            ),
        }


# ─────────────────────────────────────────────────────────────
# NODE 3 — EXTRACT CV TEMPLATE / FONTS (style agent)
# ─────────────────────────────────────────────────────────────

def extract_cv_style_node(state: AgentState) -> AgentState:
    print(
        "\n📐 WORKER extract_cv_style: "
        "Analysing uploaded CV for fonts, margins, and colour template..."
    )
    _report_progress(state, "Extracting CV style")
    try:
        profile = build_style_profile(state["cv_path"])
        return {
            **state,
            "style_profile": profile,
            "status":        "style_extracted",
            "steps_taken":   state["steps_taken"] + 1,
            "messages":      _append_handoff(
                state,
                {
                    "from":          "extract_cv_style",
                    "ok":            True,
                    "font_body":     profile.get("font_body"),
                    "font_size":     profile.get("font_size_body"),
                    "page_size":     profile.get("page_size"),
                    "left_margin_mm": profile.get("left_margin_mm"),
                    "output_status": "style_extracted",
                },
            ),
        }
    except Exception as e:
        print(f"   ⚠️  extract_cv_style failed ({e}) — using basic colour extraction")
        profile = extract_cv_style(state["cv_path"])
        return {
            **state,
            "style_profile": profile,
            "status":        "style_extracted",
            "steps_taken":   state["steps_taken"] + 1,
            "messages":      _append_handoff(
                state,
                {
                    "from":          "extract_cv_style",
                    "ok":            False,
                    "error":         str(e),
                    "output_status": "style_extracted",
                },
            ),
        }


# ─────────────────────────────────────────────────────────────
# NODE 3.5 — PLANNER
# ─────────────────────────────────────────────────────────────

def planner_node(state: AgentState) -> AgentState:
    print("\n🗺  WORKER planner: Drafting search + tailoring plan...")
    _report_progress(state, "Planning search strategy")
    plan = build_plan(
        cv_text=state.get("cv_text", ""),
        user_inputs={
            "job_title":       state.get("job_title", ""),
            "location":        state.get("location", ""),
            "num_jobs":        state.get("num_jobs", 5),
            "match_threshold": state.get("match_threshold", 60),
            "source":          state.get("source", "LinkedIn"),
        },
    )

    first_bundle = plan["keyword_bundles"][0]
    return {
        **state,
        "plan":           plan,
        "current_bundle": first_bundle,
        "scrape_round":   0,
        "status":         "plan_ready",
        "steps_taken":    state["steps_taken"] + 1,
        "messages":       _append_handoff(
            state,
            {
                "from":            "planner",
                "ok":              True,
                "bundles":         len(plan["keyword_bundles"]),
                "min_matches":     plan["quality_bar"]["min_matches"],
                "min_score":       plan["quality_bar"]["min_score"],
                "max_rounds":      plan["quality_bar"]["max_scrape_rounds"],
                "emphasis_skills": plan.get("emphasis_skills", []),
                "output_status":   "plan_ready",
            },
        ),
    }


# ─────────────────────────────────────────────────────────────
# NODE 4 — SCRAPE JOBS
# ─────────────────────────────────────────────────────────────

def _scrape_one_board_with_broadening(
    full_job_title: str,
    location: str,
    num_jobs: int,
    board: str,
) -> list:
    search_title = full_job_title.split(",")[0].strip()
    jobs = scrape_jobs(search_title, location, num_jobs, board)
    if not jobs and search_title.split():
        print(f"   ⚠️  No jobs on {board} — retrying first word only...")
        jobs = scrape_jobs(search_title.split()[0], location, num_jobs, board)
    if not jobs:
        print(f"   ⚠️  Still none on {board} — retrying without location...")
        jobs = scrape_jobs(search_title, "", num_jobs, board)
    return jobs


def scrape_jobs_node(state: AgentState) -> AgentState:
    pref = state.get("preferred_job_board") or state.get("source", "LinkedIn")
    sequence = boards_fallback_sequence(pref)

    bundle = state.get("current_bundle") or {
        "title":    state.get("job_title", ""),
        "keywords": state.get("job_title", ""),
        "location": state.get("location", ""),
        "reason":   "(no bundle set — using raw user inputs)",
    }
    round_idx = state.get("scrape_round", 0)
    q_title   = bundle.get("keywords") or bundle.get("title") or state["job_title"]
    q_loc     = bundle.get("location") or state.get("location", "")

    print(
        f"\n🔍 WORKER scrape_jobs (round {round_idx + 1}): "
        f"bundle={bundle.get('title')!r} @ {q_loc!r} — reason: {bundle.get('reason','')}"
    )
    print(f"   Board order: {sequence}")
    _report_progress(state, f"Scraping job boards (round {round_idx + 1})")

    boards_tried: List[str] = []
    jobs: List[Any] = []
    last_error: Optional[str] = None

    try:
        for board in sequence:
            boards_tried.append(board)
            print(f"   ▶ Trying job board: {board}...")
            try:
                batch = _scrape_one_board_with_broadening(
                    q_title,
                    q_loc,
                    state["num_jobs"],
                    board,
                )
            except Exception as ex:
                last_error = f"{board}: {ex}"
                print(f"   ⚠️  {board} raised error — {ex} — trying next board...")
                continue

            if batch:
                jobs = batch
                print(
                    f"   ✅ Found {len(jobs)} job(s) via {board} "
                    f"(after trying: {', '.join(boards_tried)})"
                )
                break
            print(f"   ⚠️  0 jobs from {board} — falling back to next source...")

        new_round = round_idx + 1

        if not jobs:
            err_tail = (
                f"No jobs found for bundle {bundle.get('title')!r} @ {q_loc!r} "
                f"after trying: {', '.join(boards_tried)}."
            )
            if last_error:
                err_tail += f" Last error: {last_error}"
            return {
                **state,
                "errors":             state["errors"] + [err_tail],
                "status":             "no_jobs_found",
                "scrape_round":       new_round,
                "scrape_boards_tried": boards_tried,
                "messages":           _append_handoff(
                    state,
                    {
                        "from":         "scrape_jobs",
                        "ok":           False,
                        "round":        new_round,
                        "bundle_title": bundle.get("title"),
                        "count":        0,
                        "boards_tried": boards_tried,
                        "primary":      pref,
                    },
                ),
            }

        jobs, already_applied = filter_out_applied(state.get("user_email", ""), jobs)
        if already_applied:
            print(
                f"   🧹 Filtered out {len(already_applied)} job(s) the user has already "
                f"marked as applied (skipping these)."
            )

        existing = state.get("jobs_found") or []
        seen_urls = {j.get("url") for j in existing if j.get("url")}
        merged = list(existing)
        for j in jobs:
            u = j.get("url")
            if u and u in seen_urls:
                continue
            seen_urls.add(u)
            merged.append(j)

        if not merged:
            return {
                **state,
                "errors":             state["errors"] + [
                    f"All {len(already_applied)} result(s) for {bundle.get('title')!r} "
                    f"were already applied to — nothing new this round."
                ],
                "status":             "no_jobs_found",
                "scrape_round":       new_round,
                "scrape_boards_tried": boards_tried,
                "messages":           _append_handoff(
                    state,
                    {
                        "from":              "scrape_jobs",
                        "ok":                False,
                        "round":             new_round,
                        "bundle_title":      bundle.get("title"),
                        "count":             0,
                        "already_applied":   len(already_applied),
                        "boards_tried":      boards_tried,
                        "primary":           pref,
                    },
                ),
            }

        return {
            **state,
            "jobs_found":          merged,
            "status":              "jobs_scraped",
            "scrape_round":        new_round,
            "steps_taken":         state["steps_taken"] + 1,
            "scrape_boards_tried": boards_tried,
            "messages":            _append_handoff(
                state,
                {
                    "from":            "scrape_jobs",
                    "ok":              True,
                    "round":           new_round,
                    "bundle_title":    bundle.get("title"),
                    "count":           len(jobs),
                    "already_applied": len(already_applied),
                    "total_pool":      len(merged),
                    "boards_tried":    boards_tried,
                    "primary":         pref,
                    "output_status":   "jobs_scraped",
                },
            ),
        }

    except Exception as e:
        return {
            **state,
            "errors": state["errors"] + [f"Scrape error: {str(e)}"],
            "status": "scrape_failed",
            "scrape_boards_tried": boards_tried,
            "messages": _append_handoff(
                state,
                {"from": "scrape_jobs", "ok": False, "error": str(e)},
            ),
        }


# ─────────────────────────────────────────────────────────────
# NODE 5 — MATCH JOBS
# ─────────────────────────────────────────────────────────────

def match_jobs_node(state: AgentState) -> AgentState:
    prev_matched = list(state.get("matched_jobs") or [])
    prev_skipped = list(state.get("skipped_jobs") or [])
    scored_urls  = {
        j.get("url") for j in (prev_matched + prev_skipped) if j.get("url")
    }
    plan = state.get("plan") or {}
    threshold = int(
        (plan.get("quality_bar") or {}).get("min_score", state["match_threshold"])
    )
    new_jobs = [j for j in state["jobs_found"] if j.get("url") not in scored_urls]
    print(
        f"\n🎯 WORKER match_jobs: pool={len(state['jobs_found'])} "
        f"(already scored={len(scored_urls)}, new={len(new_jobs)}) "
        f"threshold={threshold}"
    )
    _report_progress(state, "Matching jobs to CV")

    matched = list(prev_matched)
    skipped = list(prev_skipped)

    cv_collection = state.get("cv_collection") or ""
    if not cv_collection:
        try:
            from agents.cv_embeddings import is_available, index_cv
            if is_available():
                outline = _build_outline(state["cv_path"])
                cv_collection = index_cv(state["cv_path"], outline) or ""
                if cv_collection:
                    print(f"   🧠 CV indexed as collection '{cv_collection}'")
        except Exception as e:
            print(f"   ⚠️  CV indexing skipped ({type(e).__name__}: {e})")
            cv_collection = ""

    for job in new_jobs:
        try:
            result = match_cv_to_job(
                cv_text          = state["cv_text"],
                job_description  = job.get("description", ""),
                job_title        = job.get("title",       ""),
                company          = job.get("company",     ""),
                cv_collection    = cv_collection,
                experience_level = state.get("experience_level", ""),  # ✅ NEW
            )

            score = result.get("match_score", 0)
            job_with_score = {
                **job,
                "match_score":     score,
                "match_result":    result,
                "matching_skills": result.get("matched_skills", []),
                "missing_skills":  result.get("missing_skills", []),
                "strengths":       result.get("strengths",      []),
                "improvements":    result.get("improvements",   []),
            }

            if score >= threshold:
                matched.append(job_with_score)
                print(f"   ✅ MATCH  {score}/100 — {job.get('title')} at {job.get('company')}")
            else:
                skipped.append(job_with_score)
                print(f"   ⏭  SKIP   {score}/100 — {job.get('title')} at {job.get('company')}")

        except Exception as e:
            print(f"   ❌ Match error for {job.get('title')}: {e}")
            skipped.append({**job, "match_score": 0})
            continue

    if not matched:
        return {
            **state,
            "matched_jobs":  [],
            "skipped_jobs":  skipped,
            "cv_collection": cv_collection,
            "status":        "no_matches",
            "steps_taken":  state["steps_taken"] + 1,
            "messages":     _append_handoff(
                state,
                {
                    "from":          "match_jobs",
                    "ok":            False,
                    "matched":       0,
                    "skipped":       len(skipped),
                    "output_status": "no_matches",
                },
            ),
        }

    return {
        **state,
        "matched_jobs":  matched,
        "skipped_jobs":  skipped,
        "cv_collection": cv_collection,
        "status":        "jobs_matched",
        "steps_taken":  state["steps_taken"] + 1,
        "messages":     _append_handoff(
            state,
            {
                "from":          "match_jobs",
                "ok":            True,
                "matched":       len(matched),
                "skipped":       len(skipped),
                "output_status": "jobs_matched",
            },
        ),
    }


# ─────────────────────────────────────────────────────────────
# NODE 6 — TAILOR CV + COVER LETTER + GENERATE PDFs
# ─────────────────────────────────────────────────────────────

def tailor_and_generate_node(state: AgentState) -> AgentState:
    print(
        f"\n✍️  WORKER tailor_and_generate: "
        f"Tailoring for {len(state['matched_jobs'])} jobs..."
    )
    _report_progress(state, f"Tailoring CVs and generating cover letters ({len(state['matched_jobs'])} jobs)")

    style_profile = state.get("style_profile") or extract_cv_style(state["cv_path"])
    updated_jobs  = []
    ok_count      = 0
    out_dir       = _resolve_output_dir(state)

    # Build the CV outline ONCE up front — same CV for every job, saves a
    # PyMuPDF parse per iteration (v1.1 perf fix).
    try:
        shared_outline: Optional[Dict[str, Any]] = _build_outline(state["cv_path"])
    except Exception as e:
        print(f"   ⚠️  Pre-build of CV outline failed ({e}); will build per-job.")
        shared_outline = None

    def _process_single_job(job: Dict[str, Any]) -> Dict[str, Any]:
        company = job.get("company", "Unknown")
        title   = job.get("title",   "Unknown")
        jd      = job.get("description", "")
        tag     = f"[{title[:20]}@{company[:15]}]"

        # ── Closure 1: cover-letter path (Gemma → review → PDF) ───────
        def _do_cover_letter():
            cl_review_local: Optional[Dict[str, Any]] = None
            print(f"   ✉️  {tag} Writing cover letter...")
            cl_text = generate_cover_letter(
                cv_text         = state["cv_text"],
                job_description = jd,
                job_title       = title,
                company         = company,
                candidate_name  = state["candidate_name"],
            )
            try:
                from agents.cover_letter_reviewer import review_cover_letter
                cl_review_local = review_cover_letter(
                    cv_text         = state["cv_text"],
                    cover_letter    = cl_text,
                    job_description = jd,
                    job_title       = title,
                    company         = company,
                )
                print(
                    f"   🧐 {tag} CL review: score={cl_review_local['score']}/100, "
                    f"verdict={cl_review_local['verdict']}, "
                    f"fabrications={len(cl_review_local['fabrications'])}"
                )
                # Only retry if the first attempt looks clearly weak.
                # Skip the retry when the score is already decent to avoid
                # the 40→40 feedback loop seen in the Cormac run (reviewer
                # gave the same feedback both times, wasting Groq quota).
                _CL_RETRY_THRESHOLD = 55
                if (
                    cl_review_local["verdict"] == "retry"
                    and int(cl_review_local.get("score", 0) or 0) < _CL_RETRY_THRESHOLD
                ):
                    try:
                        from agents.analytics import track_event
                        track_event(
                            "review_retry_triggered",
                            "system_infra",
                            {
                                "artifact": "cover_letter",
                                "score": int(cl_review_local.get("score", 0) or 0),
                                "fabrications": len(cl_review_local.get("fabrications", []) or []),
                            },
                        )
                    except Exception:
                        pass
                    print(f"   ↻  {tag} retry CL — "
                          f"{cl_review_local['feedback'][:100]}")
                    # Preserve the original so we can revert if the retry is worse.
                    cl_text_orig   = cl_text
                    cl_review_orig = cl_review_local
                    cl_text_retry = generate_cover_letter(
                        cv_text         = state["cv_text"],
                        job_description = jd + "\n\nREVIEWER FEEDBACK "
                                               "(address on this attempt): "
                                               + cl_review_local["feedback"],
                        job_title       = title,
                        company         = company,
                        candidate_name  = state["candidate_name"],
                    )
                    cl_review_2 = review_cover_letter(
                        cv_text         = state["cv_text"],
                        cover_letter    = cl_text_retry,
                        job_description = jd,
                        job_title       = title,
                        company         = company,
                    )
                    print(f"   🧐 {tag} CL re-review: "
                          f"score={cl_review_2['score']}/100, "
                          f"verdict={cl_review_2['verdict']}")
                    if cl_review_2["score"] > cl_review_orig["score"]:
                        # Retry is strictly better — adopt it.
                        cl_text         = cl_text_retry
                        cl_review_local = cl_review_2
                    else:
                        # Retry is not better — keep the original letter and its review.
                        print(f"   ↺  {tag} retry not better — keeping original letter")
                        cl_text         = cl_text_orig
                        cl_review_local = cl_review_orig
            except Exception as rev_err:
                print(f"   ⚠️  {tag} CL reviewer error (non-fatal): "
                      f"{type(rev_err).__name__}: {rev_err}")

            cl_pdf = generate_cover_letter_pdf_styled(
                cover_letter   = cl_text,
                job_title      = title,
                company        = company,
                output_dir     = out_dir,
                style_profile  = style_profile,
                candidate_name = state["candidate_name"],
            )
            return cl_text, cl_pdf, cl_review_local

        # ── Closure 2: CV-tailor path (diff → review → apply → PDF) ───
        def _do_cv_tailor():
            cv_pdf: Optional[str] = None
            tcv_text: str = state["cv_text"]
            rmode: str = "failed"
            safe_co    = company.replace(" ", "_").replace("/", "-")
            safe_title = title.replace(" ", "_").replace("/", "-")
            replica_path = os.path.join(out_dir, f"CV_{safe_co}_{safe_title}.pdf")

            best_diff:   Optional[Dict[str, Any]] = None
            best_review: Optional[Dict[str, Any]] = None

            try:
                outline_cache = shared_outline or _build_outline(state["cv_path"])
                feedback_in = ""
                prev_diff: Optional[Dict[str, Any]] = None

                # Apr 28 follow-up: sniff layout BEFORE attempting replica.
                # Two-column / image-heavy / scanned CVs cannot be edited
                # in-place without visible corruption — short-circuit straight
                # to the rebuild path so we don't ship a broken replica.
                # Using a `replica_skipped` flag (instead of raising) keeps
                # the broader try/except block from catching the signal as
                # an error and printing a misleading "Replica+review failed"
                # log line.
                replica_check = _detect_replica_compatibility(state["cv_path"])
                replica_skipped = not replica_check.get("compatible", True)
                if replica_skipped:
                    reason = replica_check.get("reason", "unknown")
                    print(
                        f"   ℹ️  {tag} Skipping replica path — CV layout "
                        f"detected as '{reason}' (n_columns="
                        f"{replica_check.get('n_columns')}, "
                        f"image_ratio={replica_check.get('image_ratio')}). "
                        f"Routing directly to rebuild."
                    )
                    best_diff = {}
                    best_review = {
                        "score": 60,
                        "verdict": "rebuild_fallback",
                        "feedback": (
                            f"In-place tailoring skipped: source PDF layout is "
                            f"'{reason}' which the replica path cannot edit "
                            f"safely. Rebuilt from scratch with your style profile."
                        ),
                        "strengths": [], "weaknesses": [],
                    }

                # Skip the diff/review/apply loop entirely when replica is
                # not viable. The downstream `if best_diff and (...)` check
                # will be False (best_diff is {}) so apply_pdf_edits is
                # never called, and the outer code falls cleanly through
                # to the rebuild path.
                attempts_range = (
                    range(0)
                    if replica_skipped
                    else range(MAX_TAILOR_RETRIES + 1)
                )
                for attempt in attempts_range:
                    attempt_label = "first attempt" if attempt == 0 else f"retry #{attempt}"
                    print(f"   Replica-tailor CV ({attempt_label})...")
                    diff = tailor_cv_diff(
                        cv_pdf_path     = state["cv_path"],
                        job_description = jd,
                        job_title       = title,
                        company         = company,
                        feedback        = feedback_in,
                        previous_diff   = prev_diff,
                        outline         = outline_cache,
                    )
                    if not (diff.get("summary") or diff.get("bullets") or diff.get("skills_order")):
                        # B1: Empty diff is NOT a 100/100 success. It means
                        # Gemini SAFETY-blocked, returned malformed JSON, or
                        # the prompt failed to engage. Two cases:
                        #   1) We have retries left → mark for retry with
                        #      a clear directive to actually produce edits.
                        #   2) Out of retries → mark as 'rebuild_fallback'
                        #      with score=55 so the UI shows the truth
                        #      (we'll fall through to the rebuild path).
                        if attempt < MAX_TAILOR_RETRIES:
                            print(
                                f"   ⚠️  {tag} Empty diff on attempt {attempt+1} — "
                                f"forcing retry (likely Gemini SAFETY-block or "
                                f"quota-exhausted JSON parse failure)."
                            )
                            feedback_in = (
                                "[empty-diff] Your previous response produced no "
                                "summary, no bullet edits, and no skills order. "
                                "This is a failure. You MUST return a non-empty "
                                "diff with: a rewritten summary that leads with "
                                "JD verbs, AT LEAST 2 bullet rewrites on the most "
                                "JD-relevant role, and a skills_order if reordering "
                                "would help. Use ONLY facts from the CV — no "
                                "fabrication — but DO produce edits."
                            )
                            prev_diff = diff
                            try:
                                from agents.analytics import track_event
                                track_event("cv_tailor_empty_diff", "system_infra", {
                                    "company": company, "title": title,
                                    "attempt": attempt,
                                })
                            except Exception:
                                pass
                            continue
                        # Out of retries — accept honestly as rebuild fallback.
                        print(
                            f"   ⚠️  {tag} Empty diff after {attempt+1} attempts — "
                            f"falling through to rebuild path (no in-place tailor)."
                        )
                        best_diff = diff
                        best_review = {
                            "score": 55,
                            "verdict": "rebuild_fallback",
                            "feedback": (
                                "In-place tailoring failed (empty diff after retries) — "
                                "rebuilt CV from scratch using your style profile. "
                                "Layout fidelity may differ from your original PDF."
                            ),
                            "strengths": [], "weaknesses": [],
                        }
                        break

                    review = review_tailored_cv(
                        outline         = outline_cache,
                        diff            = diff,
                        job_description = jd,
                        job_title       = title,
                        company         = company,
                    )

                    if best_review is None or review["score"] > best_review["score"]:
                        best_diff, best_review = diff, review
                        # Piggyback the diff's _debug counters onto best_review
                        # so the job-card UI can surface them without a new
                        # plumbing path through the closure's return signature.
                        _dbg = diff.get("_debug") or {}
                        best_review["_bullet_reverts"] = int(
                            _dbg.get("bullet_reverts_count", 0) or 0
                        )
                        best_review["_summary_reverts"] = list(
                            _dbg.get("summary_reverts") or []
                        )
                        best_review["_all_reverted"] = bool(_dbg.get("all_reverted"))

                    # C3: total-revert override. If every change was reverted
                    # by fabrication guards, the reviewer is scoring the
                    # ORIGINAL CV (not a tailored version) and will accept it
                    # at high score. Force a retry with stricter prompting
                    # rather than shipping an unchanged CV under a deceptive
                    # accept verdict.
                    all_reverted = bool((diff.get("_debug") or {}).get("all_reverted"))
                    if all_reverted and attempt < MAX_TAILOR_RETRIES:
                        print(
                            f"   🛡️  {tag} all-revert detected — "
                            f"forcing retry with stricter prompt"
                        )
                        feedback_in = (
                            (review.get("feedback") or "")
                            + " [all-reverted] Your previous diff had every "
                              "rewrite blocked by the fabrication guard "
                              "(introduced terms not in CV, or wrong "
                              "professional identity). Rewrite using ONLY "
                              "verbatim phrases and facts from the CV — no "
                              "new acronyms, technologies, employers, or "
                              "credentials. Lead with JD-relevant verbs but "
                              "every noun must come from the CV."
                        ).strip()
                        prev_diff = diff
                        try:
                            from agents.analytics import track_event
                            track_event("cv_tailor_all_reverted", "system_infra", {
                                "company": company, "title": title,
                                "attempt": attempt,
                            })
                        except Exception:
                            pass
                        continue

                    # Count rewrites before the accept-check so the rewrite-
                    # floor can override a too-conservative reviewer accept.
                    n_rewrites = sum(
                        1
                        for entries in (diff.get("bullets") or {}).values()
                        if isinstance(entries, list)
                        for e in entries
                        if isinstance(e, dict) and e.get("text")
                    )
                    n_bullets_total = sum(
                        len(role.get("bullets") or [])
                        for role in (outline_cache.get("roles") or [])
                    )
                    # Minimum-rewrite floor (fix #3): a diff with 0-1 rewrites
                    # on a CV with 15+ bullets is indistinguishable from a
                    # no-op from the user's POV. Force a retry rather than
                    # accepting a cosmetic diff even when the reviewer is OK.
                    rewrite_floor = max(2, n_bullets_total // 5)
                    too_few_rewrites = (
                        n_rewrites < rewrite_floor
                        and n_bullets_total >= 5
                        and attempt < MAX_TAILOR_RETRIES
                    )

                    fb = (review.get("feedback") or "").lower()
                    wk = " ".join(review.get("weaknesses") or []).lower()
                    fab_flag = "fabricat" in fb or "fabricat" in wk or "invent" in fb

                    if not too_few_rewrites and not fab_flag and (
                        review["verdict"] == "accept"
                        or review["score"] >= REVIEWER_ACCEPT_THRESHOLD
                    ):
                        print(
                            f"   ✓  {tag} accepting "
                            f"(score={review['score']}, {n_rewrites}/{n_bullets_total} "
                            f"rewrites, no fabrications) — skipping retry."
                        )
                        break

                    if attempt >= MAX_TAILOR_RETRIES:
                        print(
                            f"   ⚠️  {tag} reviewer still unhappy after "
                            f"{attempt+1} attempt(s) (best={best_review['score']}) — "
                            f"keeping best."
                        )
                        break

                    # Build retry feedback. When the rewrite floor wasn't met,
                    # augment with an explicit directive so the LLM knows to
                    # rewrite more, not just address the reviewer's critique.
                    reviewer_fb = review.get("feedback", "") or ""
                    if too_few_rewrites:
                        extra = (
                            f" [rewrite floor] Your previous diff rewrote only "
                            f"{n_rewrites} of {n_bullets_total} bullets — the "
                            f"minimum is {rewrite_floor}. You MUST rewrite at "
                            f"least {rewrite_floor} bullets on this retry, "
                            f"leading with JD-relevant language while staying "
                            f"factual to the CV."
                        )
                        feedback_in = (reviewer_fb + extra).strip()
                        print(
                            f"   ↻  {tag} rewrite-floor retry "
                            f"({n_rewrites}/{n_bullets_total}, need "
                            f"{rewrite_floor})"
                        )
                    else:
                        feedback_in = reviewer_fb
                    prev_diff   = diff

                if best_diff and (best_diff.get("summary") or best_diff.get("bullets") or best_diff.get("skills_order")):
                    report = apply_pdf_edits(state["cv_path"], best_diff, replica_path)
                    # Stash table-protection stats onto best_review so the UI
                    # job-card can show "N tables protected" in the insight
                    # expander — same plumbing-free strategy as _bullet_reverts.
                    if best_review is not None:
                        _tbl_ui = report.get("tables") or {}
                        best_review["_tables_detected"] = int(
                            _tbl_ui.get("detected", 0) or 0
                        )
                        best_review["_table_lines_filtered"] = int(
                            _tbl_ui.get("lines_filtered", 0) or 0
                        )
                    # ── Silent-failure observability (P3) ──
                    # Mixpanel event so we can correlate "user says summary
                    # didn't change" with guard firings in production.
                    try:
                        from agents.analytics import track_event
                        _tbl = report.get("tables") or {}
                        _dbg = (best_diff or {}).get("_debug") or {}
                        track_event(
                            "cv_tailor_applied",
                            state.get("user_email") or "system_infra",
                            {
                                "company":               company,
                                "title":                 title,
                                "review_score":          int((best_review or {}).get("score", 0) or 0),
                                "tables_protected":      int(_tbl.get("detected", 0) or 0),
                                "table_lines_filtered":  int(_tbl.get("lines_filtered", 0) or 0),
                                "bullet_reverts_count":  int(_dbg.get("bullet_reverts_count", 0) or 0),
                                "summary_reverts_count": len(_dbg.get("summary_reverts") or []),
                            },
                        )
                    except Exception:
                        pass
                    if os.path.exists(replica_path) and os.path.getsize(replica_path) > 0:
                        cv_pdf = replica_path
                        rmode = "in_place"
                        print(
                            f"   ✅ {tag} Replica CV written "
                            f"(applied={list(report.get('applied', {}).keys())}, "
                            f"review={best_review['score']}/100, "
                            f"tables_protected={(report.get('tables') or {}).get('detected', 0)})"
                        )
                    else:
                        print(f"   ⚠️  {tag} Replica output missing — falling back.")
                else:
                    print(f"   ℹ️  {tag} No effective diff — skipping replica path.")
            except Exception as e:
                print(f"   ⚠️  {tag} Replica+review failed ({e}); falling back to ReportLab.")

            if not cv_pdf:
                print(f"   📝 {tag} Tailoring CV (rebuild)...")
                tcv_text = tailor_cv(
                    cv_text         = state["cv_text"],
                    job_description = jd,
                    job_title       = title,
                    company         = company,
                )
                cv_pdf = generate_cv_pdf_styled(
                    cv_text       = tcv_text,
                    job_title     = title,
                    company       = company,
                    output_dir    = out_dir,
                    style_profile = style_profile,
                )
                if cv_pdf and os.path.exists(cv_pdf):
                    rmode = "rebuilt"
                    # B1: when we end up on rebuild path AND best_review
                    # carried over the optimistic empty-diff stub from an
                    # earlier branch, override it so the UI shows truth.
                    # The replica path was attempted but failed; the user is
                    # getting a brand-new ReportLab PDF, not their original
                    # layout with surgical edits.
                    if best_review is None or best_review.get("verdict") in (
                        "accept", None,
                    ) and best_review.get("score", 0) >= 90 and not (
                        best_diff and (
                            best_diff.get("summary")
                            or best_diff.get("bullets")
                            or best_diff.get("skills_order")
                        )
                    ):
                        best_review = {
                            "score": 65,
                            "verdict": "rebuild_fallback",
                            "feedback": (
                                "Used rebuild path (LLM-rewritten text + ReportLab "
                                "PDF) — your original layout was not preserved. "
                                "Content IS tailored but visual fidelity differs "
                                "from your uploaded CV."
                            ),
                            "strengths": [], "weaknesses": [],
                            "_rebuild_mode": True,
                        }
                    elif best_review is not None:
                        best_review["_rebuild_mode"] = True
            return cv_pdf, tcv_text, best_review, rmode

        # ── Sequential execution: cover-letter THEN CV-tailor ────────
        # Apr 28 follow-up (Strategy B + sequential): the previous parallel
        # implementation fired both Gemini calls simultaneously, busting
        # the 5 RPM-per-key free-tier ceiling and triggering cooldown
        # cascades across all 3 keys within the first job. With Gemini's
        # global gap rate-limiter set to 7s, sequential execution costs
        # ~10-15s of extra wall-clock per job but lets each Gemini call
        # land in its own RPM window — first key gets the cover letter,
        # second key (or first after cooldown) gets the CV tailor.
        # Combined with Strategy B's instant-Groq fallback on Gemini
        # failure, this maximises Gemini hit-rate while preventing
        # quota-burn spirals.
        print(f"\n   {tag} sequential: cover-letter → CV-tailor")
        cover_letter_text: str = ""
        cl_pdf_path: Optional[str] = None
        cl_review: Optional[Dict[str, Any]] = None
        cv_pdf_path: Optional[str] = None
        tailored_cv_text: str = state["cv_text"]
        best_review: Optional[Dict[str, Any]] = None
        render_mode: str = "failed"

        try:
            # Cover letter first — Gemini gets first shot, instant Groq
            # fallback on truncation. Typically 5-15s.
            try:
                cover_letter_text, cl_pdf_path, cl_review = _do_cover_letter()
            except Exception as cle:
                print(f"   ❌ {tag} cover-letter path failed: {cle}")

            # CV tailor second — by now the Gemini key used for cover
            # letter has had 5-15s of cooldown progress, so this call
            # lands in a fresh RPM window. Strategy B fallback applies
            # the same way: Gemini once, Groq instant on failure.
            try:
                cv_pdf_path, tailored_cv_text, best_review, render_mode = _do_cv_tailor()
            except Exception as cve:
                print(f"   ❌ {tag} CV-tailor path failed: {cve}")

            if cv_pdf_path:
                print(f"   ✅ {tag} Done: {os.path.basename(cv_pdf_path)}")
            if cl_pdf_path:
                print(f"   ✅ {tag} Done: {os.path.basename(cl_pdf_path)}")
            return {
                **job,
                "tailored_cv":       tailored_cv_text,
                "cover_letter":      cover_letter_text,
                "cv_pdf_path":       cv_pdf_path,
                "cover_letter_path": cl_pdf_path,
                "review":            best_review,
                "render_mode":       render_mode,
                "cover_letter_review": cl_review,
            }

        except Exception as e:
            print(f"   ❌ Tailor error for {title} at {company}: {e}")
            return {**job, "_tailor_error": str(e)}

    # ── Fork across jobs: bounded concurrency ────────────────────────
    # B2 (Apr 27): default lowered 2 → 1. Gemini 2.5 Flash free tier caps at
    # 5 RPM PER GOOGLE PROJECT (not per key) — see header
    # `GenerateRequestsPerMinutePerProjectPerModel-FreeTier`. With 2 parallel
    # jobs each making 4-6 Gemini calls (tailor + cover letter + retries),
    # we hit the cap in <60s and the rest of the run cascades through
    # Groq fallback, losing tailoring quality. Serial = slightly slower
    # wall-time but reliable replica path completion. Power users with a
    # paid Gemini key or a separate project per key can override via env.
    job_concurrency = max(1, int(os.getenv("TAILOR_JOB_CONCURRENCY", "1")))
    jobs_to_tailor = state["matched_jobs"]
    print(
        f"   ⚡ tailor across-jobs concurrency={job_concurrency} "
        f"(of {len(jobs_to_tailor)} job(s))"
    )
    if job_concurrency <= 1 or len(jobs_to_tailor) <= 1:
        updated_jobs = []
        for j in jobs_to_tailor:
            if _check_stop_requested(state):
                print("   ⏹️  Stop requested — aborting remaining jobs in tailor phase")
                break
            updated_jobs.append(_process_single_job(j))
    else:
        with ThreadPoolExecutor(max_workers=job_concurrency,
                                thread_name_prefix="tailor-job") as outer_ex:
            updated_jobs = list(outer_ex.map(_process_single_job, jobs_to_tailor))

    ok_count = sum(
        1 for j in updated_jobs
        if j.get("cv_pdf_path") or j.get("cover_letter_path")
    )

    review_results = dict(state.get("review_results") or {})
    tailor_attempts = dict(state.get("tailor_attempts") or {})
    review_scores: List[int] = []
    for j in updated_jobs:
        key = j.get("url") or f"{j.get('company','?')}:{j.get('title','?')}"
        rev = j.get("review")
        if rev:
            review_results[key] = rev
            review_scores.append(int(rev.get("score", 0)))
        tailor_attempts[key] = tailor_attempts.get(key, 0) + 1

    try:
        shown = _mark_shown(state.get("user_email", ""), updated_jobs)
        if shown:
            print(f"   📝 Recorded {shown} shown job(s) in application history.")
    except Exception as e:
        print(f"   ⚠️  application_tracker.mark_shown failed (non-fatal): {e}")

    avg_score = round(sum(review_scores) / len(review_scores), 1) if review_scores else None
    return {
        **state,
        "matched_jobs":    updated_jobs,
        "review_results":  review_results,
        "tailor_attempts": tailor_attempts,
        "status":          "documents_ready",
        "steps_taken":     state["steps_taken"] + 1,
        "messages":        _append_handoff(
            state,
            {
                "from":              "tailor_and_generate",
                "pdfs_ok":           ok_count,
                "jobs":              len(updated_jobs),
                "avg_review_score":  avg_score,
                "output_status":     "documents_ready",
            },
        ),
    }


# ─────────────────────────────────────────────────────────────
# NODE 7 — SEND EMAIL
# ─────────────────────────────────────────────────────────────

def send_email_node(state: AgentState) -> AgentState:
    if state.get("preview_mode"):
        pdf_count = sum(
            1 for j in state["matched_jobs"]
            if j.get("cv_pdf_path") or j.get("cover_letter_path")
        )
        print(f"\n🛈  send_email SKIPPED (preview mode): "
              f"{pdf_count} job(s) ready for manual review.")
        return {
            **state,
            "status":      "awaiting_send",
            "steps_taken": state["steps_taken"] + 1,
            "messages":    _append_handoff(
                state,
                {
                    "from":          "send_email",
                    "ok":            True,
                    "preview_mode":  True,
                    "output_status": "awaiting_send",
                },
            ),
        }

    print(f"\n📧 WORKER send_email: Sending email to {state['user_email']}...")
    _report_progress(state, "Sending email with documents")

    try:
        pdf_paths = []
        for job in state["matched_jobs"]:
            if job.get("cv_pdf_path") and os.path.exists(job["cv_pdf_path"]):
                pdf_paths.append(job["cv_pdf_path"])
            if job.get("cover_letter_path") and os.path.exists(job["cover_letter_path"]):
                pdf_paths.append(job["cover_letter_path"])

        job_summary = "\n".join([
            f"• {j.get('title')} at {j.get('company')} "
            f"— Score: {j.get('match_score', 0)}/100 "
            f"| Posted: {j.get('posted_label', 'N/A')} "
            f"| Source: {j.get('source', 'N/A')} "
            f"| {j.get('url', '#')}"
            for j in state["matched_jobs"]
        ])

        tried = state.get("scrape_boards_tried") or []
        pref  = state.get("preferred_job_board") or state.get("source", "LinkedIn")
        board_line = (
            f"Primary job board preference: {pref}."
            if len(tried) <= 1
            else (
                f"Primary job board preference: {pref}. "
                f"Boards searched (in order): {', '.join(tried)}."
            )
        )

        email_body = f"""
Hi {state['candidate_name']},

Your Job Application Agent found {len(state['matched_jobs'])} matched role(s)
for "{state['job_title']}" in {state['location']}.
{board_line}

MATCHED JOBS:
{job_summary}

Your tailored CVs and cover letters are attached.

Good luck!
Job Application Agent
        """.strip()

        send_email(
            to_email    = state["user_email"],
            subject     = (
                f"Job Agent: {len(state['matched_jobs'])} match(es) "
                f"for {state['job_title']}"
            ),
            body        = email_body,
            attachments = pdf_paths,
        )

        print(f"   ✅ Email sent with {len(pdf_paths)} attachments")
        return {
            **state,
            "status":      "completed",
            "steps_taken": state["steps_taken"] + 1,
            "messages":    _append_handoff(
                state,
                {
                    "from":          "send_email",
                    "ok":            True,
                    "attachments":   len(pdf_paths),
                    "output_status": "completed",
                },
            ),
        }

    except Exception as e:
        print(f"   ❌ Email error: {e}")
        return {
            **state,
            "errors": state["errors"] + [f"Email error: {str(e)}"],
            "status": "email_failed",
            "messages": _append_handoff(
                state,
                {"from": "send_email", "ok": False, "error": str(e)},
            ),
        }


# ─────────────────────────────────────────────────────────────
# BUILD GRAPH
# ─────────────────────────────────────────────────────────────

workflow = StateGraph(AgentState)

workflow.add_node("supervisor",            supervisor_node)
workflow.add_node("validate_inputs",       validate_inputs_node)
workflow.add_node("parse_cv",              parse_cv_node)
workflow.add_node("extract_cv_style",      extract_cv_style_node)
workflow.add_node("planner",               planner_node)
workflow.add_node("scrape_jobs",           scrape_jobs_node)
workflow.add_node("match_jobs",            match_jobs_node)
workflow.add_node("tailor_and_generate",   tailor_and_generate_node)
workflow.add_node("send_email",            send_email_node)

workflow.set_entry_point("supervisor")

workflow.add_conditional_edges(
    "supervisor",
    route_from_supervisor,
    {
        "validate_inputs":     "validate_inputs",
        "parse_cv":            "parse_cv",
        "extract_cv_style":    "extract_cv_style",
        "planner":             "planner",
        "scrape_jobs":         "scrape_jobs",
        "match_jobs":          "match_jobs",
        "tailor_and_generate": "tailor_and_generate",
        "send_email":          "send_email",
        END:                   END,
    },
)

for w in (
    "validate_inputs",
    "parse_cv",
    "extract_cv_style",
    "planner",
    "scrape_jobs",
    "match_jobs",
    "tailor_and_generate",
    "send_email",
):
    workflow.add_edge(w, "supervisor")

app = workflow.compile()


def build_agent_graph():
    return app


# ─────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────

def run_agent(
    cv_path:          str,
    job_title:        str,
    location:         str,
    num_jobs:         int,
    match_threshold:  int,
    user_email:       str,
    candidate_name:   str,
    source:           str  = "LinkedIn",
    output_dir:       str  = "",
    session_id:       str  = "",
    llm_budget:       Any  = None,
    preview_mode:     bool = False,
    experience_level: str  = "",   # ✅ NEW
    progress_callback: Any = None,  # ✅ NEW
) -> dict:
    from agents.runtime import (
        LLMBudget, llm_budget_scope, BudgetExceeded, save_run_snapshot,
    )
    import datetime as _dt

    _started_at = _dt.datetime.utcnow().isoformat() + "Z"

    print(f"\n{'='*60}")
    print(f"🤖 JOB APPLICATION AGENT (supervisor + workers)")
    print(f"   Role     : {job_title}")
    print(f"   Location : {location}")
    print(f"   Source   : {source}")
    print(f"   Jobs     : {num_jobs}")
    print(f"   Threshold: {match_threshold}%")
    print(f"   Level    : {experience_level if experience_level else 'Not specified'}")
    print(f"   Session  : {session_id[:10]}…")
    print(f"   LLM cap  : {llm_budget.limit if llm_budget else 'N/A'}")
    print(f"{'='*60}\n")

    # Early quota exhaustion check - use conservative threshold
    from agents.llm_client import get_quota_summary
    quota = get_quota_summary()
    # If we have less than 1 estimated run left, show error message
    # This is conservative to prevent wasting time on runs that will likely fail
    if quota.get("est_runs_left", 0) <= 1:
        raise RuntimeError(
            "Both Gemini and Groq quotas exhausted. "
            "Please try again tomorrow after the daily quota reset. "
            "Your CV and cover letter have not been generated to avoid producing degraded output."
        )

    initial_state: AgentState = {
        "cv_path":             cv_path,
        "cv_text":             "",
        "job_title":           job_title,
        "location":            location,
        "num_jobs":            num_jobs,
        "match_threshold":     match_threshold,
        "user_email":          user_email,
        "candidate_name":      candidate_name,
        "source":              source,
        "jobs_found":          [],
        "matched_jobs":        [],
        "skipped_jobs":        [],
        "errors":              [],
        "status":              "starting",
        "steps_taken":         0,
        "messages":            [],
        "supervisor_cycles":   0,
        "routing_decision":    "",
        "scrape_retry_count":  0,
        "preferred_job_board": source,
        "scrape_boards_tried": [],
        "style_profile":       {},
        "plan":                {},
        "scrape_round":        0,
        "current_bundle":      {},
        "supervisor_trace":    [],
        "review_results":      {},
        "tailor_attempts":     {},
        "output_dir":          output_dir,
        "session_id":          session_id,
        "llm_budget":          {},
        "preview_mode":        bool(preview_mode),
        "cv_collection":       "",
        "experience_level":    experience_level,   # ✅ NEW
        "progress_callback":   progress_callback,  # ✅ NEW
    }

    _snapshot_inputs = {
        "started_at":      _started_at,
        "job_title":       job_title,
        "location":        location,
        "num_jobs":        num_jobs,
        "match_threshold": match_threshold,
        "candidate_name":  candidate_name,
        "source":          source,
        "cv_path":         cv_path,
        "user_email":      (user_email[:3] + "***" if user_email else ""),
    }

    result: Dict[str, Any] = dict(initial_state)
    _exception: Optional[BaseException] = None

    try:
        with llm_budget_scope(llm_budget):
            try:
                result = app.invoke(initial_state)
            except BudgetExceeded as be:
                snap = llm_budget.snapshot() if llm_budget else {}
                print(f"   🛑 LLM budget exceeded — {be}")
                result = {
                    **initial_state,
                    "status":     "budget_exceeded",
                    "errors":     list(initial_state.get("errors", [])) + [str(be)],
                    "llm_budget": snap,
                }
                _exception = be
    except Exception as unexpected:
        _exception = unexpected
        result = {
            **initial_state,
            "status": "crashed",
            "errors": list(initial_state.get("errors", [])) + [
                f"{type(unexpected).__name__}: {unexpected}"
            ],
        }
        snapshot_path = save_run_snapshot(
            output_dir = _resolve_output_dir(initial_state),
            session_id = session_id,
            inputs     = _snapshot_inputs,
            state      = result,
            exception  = unexpected,
            budget     = llm_budget,
        )
        if snapshot_path:
            print(f"   💾 crash snapshot: {snapshot_path}")
            result["snapshot_path"] = snapshot_path
        if llm_budget is not None:
            result["llm_budget"] = llm_budget.snapshot()
        return result

    if llm_budget is not None:
        result["llm_budget"] = llm_budget.snapshot()

    snapshot_path = save_run_snapshot(
        output_dir = _resolve_output_dir(result),
        session_id = session_id,
        inputs     = _snapshot_inputs,
        state      = result,
        exception  = _exception,
        budget     = llm_budget,
    )
    if snapshot_path:
        print(f"   💾 run snapshot: {snapshot_path}")
        result["snapshot_path"] = snapshot_path

    print(f"\n{'='*60}")
    print(f"🏁 AGENT FINISHED — Status: {result.get('status')}")
    print(f"   Matched : {len(result.get('matched_jobs', []))}")
    print(f"   Skipped : {len(result.get('skipped_jobs', []))}")
    print(f"   Steps   : {result.get('steps_taken', 0)}")
    print(f"   Supervisor cycles: {result.get('supervisor_cycles', 0)}")
    print(f"   Scrape rounds    : {result.get('scrape_round', 0)}")

    reviews = (result.get("review_results") or {}).values()
    review_scores = [int(r.get("score", 0)) for r in reviews if r]
    if review_scores:
        avg = round(sum(review_scores) / len(review_scores), 1)
        print(f"   Reviewer avg     : {avg}/100  (n={len(review_scores)}, "
              f"min={min(review_scores)}, max={max(review_scores)})")
    retry_total = sum(max(0, v - 1) for v in (result.get("tailor_attempts") or {}).values())
    if retry_total:
        print(f"   Tailor retries   : {retry_total}")

    trace = result.get("supervisor_trace") or []
    llm_decisions = [t for t in trace if t.get("mode") == "llm"]
    print(f"   LLM decisions    : {len(llm_decisions)}")
    for t in llm_decisions:
        print(f"     • cycle {t['cycle']:>2} @ {t['observed']:<18} → "
              f"{t['action']:<22}  — {t.get('reasoning','')[:90]}")
    print(f"{'='*60}\n")

    return result