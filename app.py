"""
Job Application Agent — Streamlit UI (professional redesign).

Sections:
  • Custom CSS          : subtle indigo accent, card styles, chips.
  • Sidebar             : CV upload + preferences + run button.
  • Main area           : on first load a welcome panel; after a run four
    tabs — Matches / Below Threshold / Agent Insight / History.
  • Application tracker : every matched job has a "Mark applied" toggle that
    persists via agents.application_tracker.

Design principles:
  • Minimal emoji (only on action buttons / status chips).
  • Consistent vertical rhythm (cards spaced by 1.25rem).
  • One accent colour (#4F46E5 indigo). Status chips use red/amber/green
    only for the match/review score, not for decoration.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, List

import streamlit as st
from dotenv import load_dotenv

from agents.job_agent import run_agent
from agents.application_tracker import (
    history_summary,
    load_history,
    mark_applied,
    mark_not_applied,
)
from agents.preflight import run_preflight, PreflightError
from agents.runtime import (
    LLMBudget,
    cleanup_session,
    safe_upload_path,
    secret_or_env,
    session_dirs,
    session_id as new_session_id,
)
from agents.cv_validator import validate_cv
from agents.privacy import apply_tracing_consent, set_session_pii
from agents.analytics import analytics_enabled, distinct_id, track_event

load_dotenv()

# Per-run LLM call cap — hard stop if a rogue loop tries to burn through
# the Groq budget. Tunable via env / st.secrets.
_MAX_LLM_CALLS_PER_RUN = int(secret_or_env("MAX_LLM_CALLS_PER_RUN", "20") or "20")


# ═════════════════════════════════════════════════════════════════════════
# PAGE CONFIG + CSS
# ═════════════════════════════════════════════════════════════════════════

st.set_page_config(
    page_title = "ApplySmart AI — Job Application Agent",
    page_icon  = "◈",
    layout     = "wide",
    initial_sidebar_state = "expanded",
)

_CUSTOM_CSS = """
<style>
    :root {
        /* Deep-teal accent + warm stone neutrals. Distinct from career-tech
           sites that use cyan-mint (Teal), violet (Huntr), or corporate
           blue (Jobscan) — feels professional without copying anyone. */
        --accent:         #0F766E;   /* teal-700, confident + restrained */
        --accent-hover:   #115E59;   /* teal-800 */
        --accent-soft:    #F0FDFA;   /* teal-50, barely-there fill */
        --accent-ring:    #99F6E4;   /* teal-200, for hover rings */
        --text-strong:    #1C1917;   /* stone-900 (warm near-black) */
        --text-muted:     #57534E;   /* stone-600 */
        --text-faint:     #A8A29E;   /* stone-400 */
        --border:         #E7E5E4;   /* stone-200, warm border */
        --border-strong:  #D6D3D1;   /* stone-300 */
        --bg-card:        #FFFFFF;
        --bg-soft:        #F5F5F4;   /* stone-100 for sidebar / subtle fills */
        --bg-page:        #FAFAF9;   /* stone-50, softer than pure white */
        /* Status palette — muted tints + deep-text for AA contrast. */
        --green-bg:       #ECFDF5;
        --green-text:     #065F46;
        --amber-bg:       #FFFBEB;
        --amber-text:     #92400E;
        --red-bg:         #FEF2F2;
        --red-text:       #991B1B;
    }

    /* Force the light palette regardless of the visitor's OS dark-mode. */
    html, body, .stApp, [data-testid="stAppViewContainer"], .main, .block-container {
        background-color: var(--bg-page) !important;
        color: var(--text-strong) !important;
    }

    /* Hide default Streamlit chrome */
    #MainMenu, footer, header { visibility: hidden; }
    .block-container { padding-top: 1.25rem; padding-bottom: 3rem; max-width: 1240px; }

    /* Typography */
    html, body, [class*="css"] {
        font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI",
                     Roboto, Helvetica, Arial, sans-serif;
        color: var(--text-strong);
    }
    h1, h2, h3, h4, p, span, div, label { color: var(--text-strong); }
    h1, h2, h3 { letter-spacing: -0.015em; }

    /* Every Streamlit widget label explicitly dark so dark mode can't whiten them. */
    [data-testid="stWidgetLabel"], [data-testid="stMarkdownContainer"] p,
    .stSelectbox label, .stSlider label, .stTextInput label, .stFileUploader label {
        color: var(--text-strong) !important;
    }
    .stCaption, .stCaption p, [data-testid="stCaptionContainer"] {
        color: var(--text-muted) !important;
    }

    /* Input fields: white background, dark text, visible border */
    .stTextInput input, .stTextArea textarea, .stSelectbox [data-baseweb="select"] > div,
    .stFileUploader [data-testid="stFileUploaderDropzone"] {
        background-color: #FFFFFF !important;
        color: var(--text-strong) !important;
        border: 1px solid var(--border) !important;
    }
    .stTextInput input::placeholder, .stTextArea textarea::placeholder {
        color: #94A3B8 !important;
    }

    /* ──────────────────────────────────────────────────────────────
       BRAND — product-level masthead.
       Professional hierarchy: eyebrow → name → descriptor.
       ────────────────────────────────────────────────────────────── */
    .brand-header {
        display: flex; flex-direction: column; align-items: center;
        gap: 0.3rem;
        padding: 0.2rem 0 2rem 0;
        margin: 0 auto 2.1rem auto;
        border-bottom: 1px solid var(--border);
        max-width: 920px;
    }
    .brand-header .eyebrow {
        font-size: 0.72rem;
        text-transform: uppercase;
        letter-spacing: 0.12em;
        color: var(--text-muted);
        font-weight: 600;
    }
    .brand-header .mark {
        width: 60px; height: 60px;
        border-radius: 15px;
        background: linear-gradient(145deg, var(--accent) 0%, var(--accent-hover) 100%);
        color: #fff;
        display: flex; align-items: center; justify-content: center;
        font-weight: 600; font-size: 1.95rem;
        box-shadow:
            0 1px 2px rgba(15, 118, 110, 0.08),
            0 8px 20px -6px rgba(15, 118, 110, 0.22),
            inset 0 1px 0 rgba(255, 255, 255, 0.22);
        letter-spacing: -0.02em;
    }
    .brand-header .name {
        font-size: 2.2rem; font-weight: 650;
        letter-spacing: -0.035em; color: var(--text-strong);
        line-height: 1.1; margin-top: 0.95rem;
    }
    .brand-header .tag {
        font-size: 0.95rem; color: var(--text-muted);
        font-weight: 400; letter-spacing: -0.005em;
    }

    /* Legacy sidebar brand block (kept for backwards-compat; now unused
       since brand moved to main area — no-op if function removed). */
    .app-brand { display: none; }

    /* KPI cards (top of Matches tab) */
    .kpi-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.85rem; margin: 0.25rem 0 1.25rem 0; }
    .kpi {
        background: var(--bg-card); border: 1px solid var(--border);
        border-radius: 10px; padding: 0.85rem 1rem;
    }
    .kpi .label { font-size: 0.72rem; text-transform: uppercase;
                   letter-spacing: 0.06em; color: var(--text-muted); margin-bottom: 0.3rem; }
    .kpi .value { font-size: 1.65rem; font-weight: 600; color: var(--text-strong); line-height: 1; }
    .kpi .hint  { font-size: 0.75rem; color: var(--text-muted); margin-top: 0.35rem; }

    /* Job card */
    .job-card {
        background: var(--bg-card); border: 1px solid var(--border);
        border-radius: 12px; padding: 1.05rem 1.2rem; margin-bottom: 1rem;
    }
    .job-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 1rem; }
    .job-title { font-size: 1.05rem; font-weight: 600; color: var(--text-strong); margin: 0; }
    .job-sub   { font-size: 0.88rem; color: var(--text-muted); margin-top: 0.15rem; }
    .job-meta  { font-size: 0.80rem; color: var(--text-muted); margin-top: 0.4rem; }
    .job-meta a { color: var(--accent); text-decoration: none; }
    .job-meta a:hover { text-decoration: underline; }

    /* Chips */
    .chip-row { display: flex; gap: 0.4rem; flex-wrap: wrap; }
    .chip {
        display: inline-flex; align-items: center;
        padding: 0.18rem 0.6rem; border-radius: 999px;
        font-size: 0.72rem; font-weight: 600; line-height: 1.35;
        border: 1px solid var(--border); background: var(--bg-soft);
        color: var(--text-strong);
    }
    .chip-primary { background: var(--accent); color: #FFFFFF;
                    border-color: transparent; }
    .chip-green   { background: var(--green-bg); color: var(--green-text); border-color: transparent; }
    .chip-amber   { background: var(--amber-bg); color: var(--amber-text); border-color: transparent; }
    .chip-red     { background: var(--red-bg);   color: var(--red-text);   border-color: transparent; }

    /* Skill pills in detail section */
    .pill-row  { display: flex; gap: 0.35rem; flex-wrap: wrap; margin-top: 0.35rem; }
    .pill      { font-size: 0.75rem; padding: 0.15rem 0.55rem; border-radius: 6px;
                 background: var(--bg-soft); color: var(--text-strong);
                 border: 1px solid var(--border); }
    .pill-miss { background: var(--red-bg);   color: var(--red-text);   border-color: transparent; }
    .pill-have { background: var(--green-bg); color: var(--green-text); border-color: transparent; }

    /* Section label */
    .section-label {
        font-size: 0.72rem; text-transform: uppercase;
        letter-spacing: 0.08em; color: var(--text-muted);
        margin: 0.9rem 0 0.3rem 0;
    }

    /* ──────────────────────────────────────────────────────────────
       HERO — editorial, centered, generous whitespace.
       No card/gradient around the hero text; this makes the page feel
       like a product landing, not a dashboard panel.
       ────────────────────────────────────────────────────────────── */
    .welcome {
        padding: 0.25rem 0 0 0; margin-top: 0.1rem;
    }
    .welcome h2 {
        margin: 0 auto 1.1rem auto;
        font-size: 2.95rem; font-weight: 650;
        letter-spacing: -0.04em; line-height: 1.06;
        color: var(--text-strong);
        max-width: 840px;
    }
    .welcome p {
        color: var(--text-muted); font-size: 1.05rem;
        line-height: 1.55; margin: 0 auto;
        max-width: 720px;
    }
    .welcome p b { color: var(--text-strong); font-weight: 600; }

    /* Feature cards — equal-height grid with hover lift. */
    .feature-grid {
        display: grid; grid-template-columns: repeat(3, 1fr);
        gap: 1.1rem; margin-top: 2.7rem;
    }
    .feature {
        background: var(--bg-card); border: 1px solid var(--border);
        border-radius: 12px; padding: 1.4rem 1.3rem;
        transition: transform 160ms ease, box-shadow 160ms ease,
                    border-color 160ms ease;
    }
    .feature:hover {
        transform: translateY(-2px);
        border-color: var(--border-strong);
        box-shadow:
            0 1px 2px rgba(0, 0, 0, 0.02),
            0 12px 28px -12px rgba(15, 118, 110, 0.14);
    }
    .feature .ft-icon {
        width: 36px; height: 36px; border-radius: 9px;
        background: var(--accent-soft); color: var(--accent);
        display: flex; align-items: center; justify-content: center;
        font-size: 1.05rem; font-weight: 600;
        margin-bottom: 1rem;
        border: 1px solid var(--accent-ring);
    }
    .feature .ft-label {
        font-size: 0.7rem; color: var(--text-muted);
        text-transform: uppercase; font-weight: 600;
        letter-spacing: 0.09em;
    }
    .feature .ft-title {
        font-size: 1.02rem; font-weight: 600;
        margin: 0.28rem 0 0.45rem 0;
        letter-spacing: -0.012em;
        color: var(--text-strong);
    }
    .feature .ft-body {
        font-size: 0.875rem; color: var(--text-muted);
        line-height: 1.5;
    }

    /* Sidebar tweaks */
    section[data-testid="stSidebar"] {
        background: var(--bg-soft) !important;
        border-right: 1px solid var(--border);
    }
    section[data-testid="stSidebar"] *,
    section[data-testid="stSidebar"] label,
    section[data-testid="stSidebar"] p,
    section[data-testid="stSidebar"] span,
    section[data-testid="stSidebar"] div {
        color: var(--text-strong);
    }
    section[data-testid="stSidebar"] .stCaption { color: var(--text-muted) !important; }
    section[data-testid="stSidebar"] .stButton > button {
        background: var(--accent) !important;
        color: #fff !important;
        border: none !important;
        font-weight: 500;
    }
    section[data-testid="stSidebar"] .stButton > button:hover {
        background: var(--accent-hover) !important;
    }
    section[data-testid="stSidebar"] .stButton > button * { color: #fff !important; }
    section[data-testid="stSidebar"] .stButton > button {
        border-radius: 10px !important;
        box-shadow: 0 6px 14px -10px rgba(15, 118, 110, 0.55);
    }

    /* Sidebar section labels */
    section[data-testid="stSidebar"] .sidebar-h {
        font-size: 0.72rem;
        text-transform: uppercase;
        letter-spacing: 0.1em;
        color: var(--text-muted);
        margin: 0.75rem 0 0.42rem 0;
        font-weight: 600;
    }

    /* Tab labels */
    .stTabs [data-baseweb="tab-list"] { gap: 0; border-bottom: 1px solid var(--border); }
    .stTabs [data-baseweb="tab"] {
        font-weight: 500; color: var(--text-muted);
        padding: 0.65rem 1rem;
    }
    .stTabs [aria-selected="true"] { color: var(--text-strong) !important; }
    .stTabs [aria-selected="true"]::after {
        background-color: var(--text-strong) !important;
    }

    /* Supervisor trace rows */
    .trace-row {
        display: grid; grid-template-columns: 50px 160px 180px 1fr;
        gap: 0.75rem; padding: 0.55rem 0.8rem;
        border-bottom: 1px solid var(--border);
        font-size: 0.85rem;
    }
    .trace-row:last-child { border-bottom: none; }
    .trace-row .cyc  { color: var(--text-muted); font-variant-numeric: tabular-nums; }
    .trace-row .from { color: var(--text-muted); }
    .trace-row .to   { font-weight: 500; color: var(--text-strong); }

    /* Plan bundle rows */
    .plan-bundle {
        border: 1px solid var(--border); border-radius: 8px;
        padding: 0.7rem 0.9rem; margin-bottom: 0.55rem; background: var(--bg-card);
    }
    .plan-bundle .b-title { font-weight: 600; font-size: 0.9rem; }
    .plan-bundle .b-meta  { font-size: 0.78rem; color: var(--text-muted); margin-top: 2px; }

    /* Subtle tweaks */
    .stAlert { border-radius: 8px; }
    div[data-testid="stFileUploader"] > section { border-radius: 8px; }
    hr { margin: 1rem 0; border-color: var(--border); }

    /* Sidebar footer credit — theme-aware so it stays legible on both bgs. */
    .sidebar-footer {
        margin-top: 1.5rem;
        padding-top: 1rem;
        border-top: 1px solid var(--border);
        text-align: center;
        font-size: 0.78rem;
        color: var(--text-muted);
    }
    .sidebar-footer b { color: var(--text-strong); }

    /* Inline code chips inside captions (e.g. `docs/PRIVACY.md`). Streamlit's
       default styling is a light grey box that vanishes on the charcoal bg;
       use theme variables so both modes render a readable chip. */
    [data-testid="stCaptionContainer"] code,
    .stCaption code,
    .sidebar .stCaption code {
        background: var(--border) !important;
        color: var(--text-strong) !important;
        padding: 1px 6px;
        border-radius: 4px;
        font-size: 0.82em;
    }

    @media (max-width: 980px) {
        .brand-header .name { font-size: 1.85rem; }
        .welcome h2 { font-size: 2.15rem; line-height: 1.1; }
        .feature-grid { grid-template-columns: 1fr; gap: 0.85rem; }
        .kpi-grid { grid-template-columns: repeat(2, 1fr); }
    }
</style>
"""

st.markdown(_CUSTOM_CSS, unsafe_allow_html=True)


# ─── Dark mode override ─────────────────────────────────────────────
# Activated by the sidebar theme toggle. Redefines the CSS variables so
# the whole app re-themes without touching any widget code. Palette is
# "charcoal teal" — inspired by Linear / Vercel dark + our brand teal.
_DARK_CSS = """
<style>
    :root {
        /* Brand stays teal but brighter for contrast on dark bg. */
        --accent:         #14B8A6;   /* teal-500 */
        --accent-hover:   #0D9488;   /* teal-600 */
        --accent-soft:    #042F2E;   /* teal-950 for subtle fills */
        --accent-ring:    #2DD4BF;   /* teal-400, hover rings */
        --text-strong:    #E6E8EB;   /* soft white */
        --text-muted:     #8B949E;   /* muted slate */
        --text-faint:     #6E7681;   /* faint slate */
        --border:         #30363D;   /* subtle slate divider */
        --border-strong:  #484F58;
        --bg-card:        #1C2128;   /* elevated surface (cards, inputs) */
        --bg-soft:        #1C2128;   /* sidebar / subtle fills */
        --bg-page:        #0F1419;   /* deep charcoal page */
        /* Status palette — dark-mode variants with AA contrast. */
        --green-bg:       #022C22;
        --green-text:     #6EE7B7;
        --amber-bg:       #3D2B11;
        --amber-text:     #FCD34D;
        --red-bg:         #450A0A;
        --red-text:       #FCA5A5;
    }
    /* Override hardcoded input whites from base CSS. */
    .stTextInput input, .stTextArea textarea,
    .stSelectbox [data-baseweb="select"] > div,
    .stFileUploader [data-testid="stFileUploaderDropzone"] {
        background-color: #1C2128 !important;
        color: var(--text-strong) !important;
        border: 1px solid var(--border) !important;
    }
    .stTextInput input::placeholder, .stTextArea textarea::placeholder {
        color: #6E7681 !important;
    }

    /* File uploader — the "Browse files" button ships with a hardcoded
       white background from Streamlit's base CSS, which disappears on
       our charcoal dropzone. Repaint it so it reads against the dark bg.
       We target every known selector Streamlit uses across versions. */
    .stFileUploader button,
    .stFileUploader [data-testid="stBaseButton-secondary"],
    [data-testid="stFileUploaderDropzone"] button {
        background-color: #30363D !important;  /* var(--border), one step up from dropzone */
        color: var(--text-strong) !important;
        border: 1px solid #484F58 !important;
    }
    .stFileUploader button:hover,
    [data-testid="stFileUploaderDropzone"] button:hover {
        background-color: #3D444D !important;
        border-color: var(--accent) !important;
        color: var(--accent) !important;
    }
    /* Dropzone helper text ("Drag and drop…", "Limit 7MB per file"). */
    .stFileUploader [data-testid="stFileUploaderDropzoneInstructions"],
    .stFileUploader [data-testid="stFileUploaderDropzoneInstructions"] span,
    .stFileUploader [data-testid="stFileUploaderDropzoneInstructions"] small {
        color: var(--text-muted) !important;
    }
    /* Uploaded-file chip background. */
    .stFileUploader [data-testid="stFileUploaderFile"] {
        background-color: #1C2128 !important;
        color: var(--text-strong) !important;
        border: 1px solid var(--border) !important;
    }
    /* Job card + panel surfaces: lift onto the elevated charcoal. */
    .job-card, .stTabs [data-baseweb="tab-panel"] {
        background: var(--bg-card) !important;
        border-color: var(--border) !important;
    }
    /* Slider track + thumb glow warmer against dark. */
    .stSlider [data-baseweb="slider"] > div > div {
        background: var(--accent) !important;
    }
    /* Brand mark retains gradient but glow tweaked for dark. */
    .brand-header .mark {
        box-shadow:
            0 1px 2px rgba(20, 184, 166, 0.15),
            0 8px 24px -6px rgba(20, 184, 166, 0.35),
            inset 0 1px 0 rgba(255, 255, 255, 0.15) !important;
    }
    /* Streamlit-generated alerts: darken bg, keep coloured text. */
    div[data-testid="stAlert"] {
        background-color: var(--bg-card) !important;
        border: 1px solid var(--border) !important;
    }
</style>
"""

# Initialise theme state once per session.
if "theme" not in st.session_state:
    st.session_state["theme"] = "light"

if st.session_state["theme"] == "dark":
    st.markdown(_DARK_CSS, unsafe_allow_html=True)


# ═════════════════════════════════════════════════════════════════════════
# BOOT GATE — preflight checks, optional password, session init
# ═════════════════════════════════════════════════════════════════════════
# Preflight runs ONCE per process. If GROQ_API_KEY is missing the app is
# unusable — halt with a friendly explanation instead of letting the user
# upload a CV only to see a runtime error later.
try:
    _preflight = run_preflight(strict=True)
except PreflightError as e:
    st.error(
        "**ApplySmart AI can't start yet.** Required configuration is missing:\n\n"
        f"{e}\n\nPlease set the key(s) above and reload."
    )
    st.stop()

# Optional password gate. Set APP_PASSWORD in .env or st.secrets to enable.
_APP_PASSWORD = secret_or_env("APP_PASSWORD")
if _APP_PASSWORD:
    if not st.session_state.get("_authed"):
        st.markdown(
            '<div class="welcome" style="max-width:520px;margin:3rem auto 0;">'
            '<h2>Sign in</h2>'
            '<p>Enter the access password to continue.</p></div>',
            unsafe_allow_html=True,
        )
        pw = st.text_input("Password", type="password", label_visibility="collapsed")
        if st.button("Enter", type="primary", use_container_width=False):
            if pw == _APP_PASSWORD:
                st.session_state["_authed"] = True
                st.rerun()
            else:
                st.error("Incorrect password.")
        st.stop()

# Per-session state: UUID + isolated upload/output directories. Created
# exactly once per browser session (persists across reruns inside the tab).
if "session_id" not in st.session_state:
    st.session_state["session_id"] = new_session_id()
_SESSION_ID = st.session_state["session_id"]
_UPLOADS_DIR, _OUTPUTS_DIR = session_dirs(_SESSION_ID)
_ANON_DISTINCT_ID = distinct_id(_SESSION_ID)

if analytics_enabled() and not st.session_state.get("_tracked_session_open"):
    track_event(
        "session_opened",
        _ANON_DISTINCT_ID,
        {
            "app": "ApplySmart AI",
            "surface": "streamlit",
        },
    )
    st.session_state["_tracked_session_open"] = True

# Privacy consent (GDPR baseline): tracing is OFF by default.
# Users can opt in to anonymized tracing for observability.
if "trace_consent" not in st.session_state:
    st.session_state["trace_consent"] = False

if "trace_consent_prompted" not in st.session_state:
    st.session_state["trace_consent_prompted"] = False

if not st.session_state["trace_consent_prompted"]:
    st.warning(
        "**Privacy choice required (this session):**\n\n"
        "- ApplySmart uses Groq/Google LLM APIs to generate results.\n"
        "- Optional LangSmith tracing helps debugging.\n"
        "- Tracing is **off by default** unless you explicitly allow it."
    )
    c1, c2, c3 = st.columns(3)
    if c1.button("Allow anonymized tracing", key="consent_allow", use_container_width=True):
        st.session_state["trace_consent"] = True
        st.session_state["trace_consent_prompted"] = True
        apply_tracing_consent(True)
        track_event("privacy_tracing_consent_updated", _ANON_DISTINCT_ID, {"enabled": True, "source": "first_prompt"})
        st.rerun()
    if c2.button("Disable all tracing", key="consent_deny", use_container_width=True):
        st.session_state["trace_consent"] = False
        st.session_state["trace_consent_prompted"] = True
        apply_tracing_consent(False)
        track_event("privacy_tracing_consent_updated", _ANON_DISTINCT_ID, {"enabled": False, "source": "first_prompt"})
        st.rerun()
    if c3.button("Cancel", key="consent_cancel", use_container_width=True):
        st.stop()
    st.stop()

# Keep process-level tracing flag in sync with this session's consent.
apply_tracing_consent(bool(st.session_state.get("trace_consent")))


# ═════════════════════════════════════════════════════════════════════════
# HELPERS
# ═════════════════════════════════════════════════════════════════════════

def _score_chip_class(score: int) -> str:
    if score >= 80:
        return "chip-green"
    if score >= 60:
        return "chip-amber"
    return "chip-red"


def _chip(label: str, cls: str = "") -> str:
    return f'<span class="chip {cls}">{label}</span>'


def _render_top_brand() -> None:
    """Render the brand mark + wordmark at the top-center of main content.

    Design intent: establish ApplySmart AI as the anchor of the page, like
    Linear / Stripe / Vercel product pages do with their logo. Persists
    across welcome + matches views so users always know where they are.
    """
    st.markdown(
        '<div class="brand-header">'
        '<div class="eyebrow">AI Job Application Studio</div>'
        '<div class="mark">◈</div>'
        '<div class="name">ApplySmart AI</div>'
        '<div class="tag">Find, tailor, review, and ship applications with confidence</div>'
        '</div>',
        unsafe_allow_html=True,
    )


def _render_welcome() -> None:
    st.markdown(
        """
        <div class="welcome">
          <h2>From job search to tailored application, in one focused workflow.</h2>
          <p>For every live job it finds, ApplySmart AI produces <b>a
             role-aligned CV and a matching cover letter</b>, each one
             tuned to the job description so you can review and apply in minutes
             instead of rewriting documents for hours.</p>
          <div class="feature-grid">
            <div class="feature">
              <div class="ft-icon">⌕</div>
              <div class="ft-label">Search</div>
              <div class="ft-title">Adaptive keyword search</div>
              <div class="ft-body">Planner agent drafts up to 8 keyword
                bundles (title variants, adjacent roles, broader locations).
                Supervisor broadens automatically if matches are thin.</div>
            </div>
            <div class="feature">
              <div class="ft-icon">✎</div>
              <div class="ft-label">Tailor</div>
              <div class="ft-title">CV + cover letter per role</div>
              <div class="ft-body">Every matched job gets its own pair:
                a replica-edit PDF of your CV (keeping your original fonts
                and layout) plus a bespoke cover letter written from the JD.</div>
            </div>
            <div class="feature">
              <div class="ft-icon">✓</div>
              <div class="ft-label">Review</div>
              <div class="ft-title">Self-critique loop</div>
              <div class="ft-body">A reviewer agent scores each tailored
                CV against the JD; below threshold it's re-tailored with
                the reviewer's own feedback before you see it.</div>
            </div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ═════════════════════════════════════════════════════════════════════════
# TOP BRAND — rendered at the top-center of main content, always visible.
# Replaces the old sidebar-corner brand so ApplySmart AI becomes the
# page anchor (Linear / Stripe pattern).
# ═════════════════════════════════════════════════════════════════════════

_render_top_brand()


# ═════════════════════════════════════════════════════════════════════════
# SIDEBAR — inputs + run
# ═════════════════════════════════════════════════════════════════════════

with st.sidebar:
    # ─── Theme toggle ────────────────────────────────────────────────
    # Segmented control at the very top of the sidebar. Writes to
    # `st.session_state["theme"]`; the conditional dark CSS block above
    # picks this up on the next rerun.
    _theme_choice = st.radio(
        "Theme",
        options=["☀️ Light", "🌙 Dark"],
        index=0 if st.session_state.get("theme", "light") == "light" else 1,
        horizontal=True,
        label_visibility="collapsed",
        key="_theme_radio",
    )
    _new_theme = "light" if _theme_choice.endswith("Light") else "dark"
    if _new_theme != st.session_state.get("theme", "light"):
        st.session_state["theme"] = _new_theme
        track_event(
            "theme_changed",
            _ANON_DISTINCT_ID,
            {"theme": _new_theme, "source": "sidebar_toggle"},
        )
        st.rerun()

    st.markdown('<div class="sidebar-h">Upload</div>', unsafe_allow_html=True)
    uploaded_cv = st.file_uploader(
        "Your CV (PDF)", type=["pdf"], label_visibility="collapsed",
    )
    # Add file info below uploader in a single line
    st.caption("PDF • Max 7 MB")
    # Hide Streamlit's default 200MB text and reduce spacing
    st.markdown("""
    <style>
    div[data-testid="stFileUploaderDropzoneInstructions"] {
        display: none !important;
    }
    /* Reduce vertical spacing in sidebar */
    .sidebar-h {
        margin-top: 0.5rem !important;
        margin-bottom: 0.5rem !important;
    }
    [data-testid="stSidebar"] > div {
        padding-top: 1rem !important;
    }
    [data-testid="stSidebar"] > div > div {
        gap: 0.5rem !important;
    }
    </style>
    """, unsafe_allow_html=True)
    # Activation-funnel top step: track the first CV upload per session.
    if uploaded_cv is not None and not st.session_state.get("_tracked_cv_upload"):
        track_event(
            "cv_uploaded",
            _ANON_DISTINCT_ID,
            {"file_size_bytes": getattr(uploaded_cv, "size", None)},
        )
        st.session_state["_tracked_cv_upload"] = True

    st.markdown('<div class="sidebar-h">You</div>', unsafe_allow_html=True)
    candidate_name = st.text_input(
        "Full name", placeholder="Your full name", label_visibility="collapsed",
    )
    user_email = st.text_input(
        "Email", placeholder="name@example.com", label_visibility="collapsed",
    )
    # Register current user's PII with the LangSmith anonymizer so any
    # traced state (CV text, JD text, prompts) has name/email masked
    # before upload. No-op if tracing consent is disabled. Idempotent.
    set_session_pii(name=candidate_name, email=user_email)

    st.markdown('<div class="sidebar-h">Target Role</div>', unsafe_allow_html=True)
    job_title = st.text_input(
        "Job title", placeholder="Desired job title", label_visibility="collapsed",
    )
    location = st.text_input(
        "Location", placeholder="City or country", label_visibility="collapsed",
    )

    selected_source = st.selectbox(
        "Primary job board",
        options = ["LinkedIn", "Indeed", "Glassdoor", "Jobs.ie", "Builtin", "All"],
        index   = 0,
        help    = (
            "Tried first. If empty, other boards are searched in order. "
            "Pick 'All' to query every board at once."
        ),
    )

    experience_level = st.selectbox(
        "Your experience level",
        options = [
            "Fresher (0-1 yrs)",
            "Entry / Associate (1-3 yrs)",
            "Mid-level (3-6 yrs)",
            "Senior (6-10 yrs)",
            "Lead / Manager (8+ yrs)",
            "Director / VP+ (12+ yrs)",
        ],
        index   = 2,  # Mid-level by default
        help    = (
            "Matcher uses this to filter out roles that are a bad seniority fit. "
            "For example, a Fresher applying to a VP role gets a heavy score "
            "penalty even if the JD keywords match."
        ),
    )

    st.markdown('<div class="sidebar-h">Run Settings</div>', unsafe_allow_html=True)
    num_jobs = st.slider("Jobs to scrape", 1, 20, 3)
    if num_jobs > 3:
        st.warning(
            "For a trial run, we recommend **3 jobs**. Higher counts may "
            "exhaust the daily LLM quota — if that happens, please try again "
            "tomorrow or reduce the job count.",
            icon=None,
        )
    match_threshold = st.slider(
        "Minimum match score (JD vs CV %)",
        10, 95, 60, step=5,
        help="Only show jobs with at least this score. Lower = more jobs, higher = better matches."
    )

    preview_mode = st.checkbox(
        "Preview before sending",
        value = True,
        help  = "When on, the agent generates the tailored CV + cover letter "
                "PDFs but does NOT send them automatically. You'll get a "
                "'Send now' button on each matched role.",
    )

    # ─── Primary CTA: Run button (pinned above the fold) ───────────────
    # Moved here from below the collapsed sections so it's always visible
    # without scrolling. Per-session run count hint sets expectations.
    st.markdown(" ")
    _max_runs_hint = int(secret_or_env("APPLYSMART_MAX_RUNS_PER_SESSION", "3") or "3")
    _runs_used_hint = int(st.session_state.get("_runs_used", 0))
    _runs_left_hint = max(0, _max_runs_hint - _runs_used_hint)
    _at_cap = _max_runs_hint > 0 and _runs_used_hint >= _max_runs_hint

    run_button = st.button(
        "Run agent" if not _at_cap else "Runs exhausted for this session",
        use_container_width=True,
        type="primary",
        disabled=_at_cap,
    )
    if _max_runs_hint > 0:
        st.caption(
            f"{_runs_used_hint}/{_max_runs_hint} runs used this session"
            + (" — cap reached, refresh tomorrow" if _at_cap else "")
        )

    st.caption(
        "Your CV is processed locally. Nothing leaves your machine except "
        "the anonymised prompts sent to the language model. "
        "See `docs/PRIVACY.md` for full details."
    )

    # Footer uses theme CSS variables so both light and dark modes are legible.
    # (Previously hardcoded rgba(0,0,0,*) which disappeared on charcoal bg.)
    st.markdown(
        """
        <div class="sidebar-footer">
            Built by <b>Rishav Singh</b>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ─── Daily budget panel ─────────────────────────────────────────
    # Live Groq quota read from the x-ratelimit-* response headers on
    # every LLM call (see agents.llm_client.get_quota_summary). Shows
    # tokens remaining + an estimate of how many more full runs fit
    # into today's daily window. Aggregates across all rotated keys.
    # Collapsed by default to reduce sidebar scroll height.
    with st.expander("Daily Budget", expanded=False):
        try:
            from agents.llm_client import get_quota_summary
            _q = get_quota_summary()
        except Exception:
            _q = {"ready": False}

        if not _q.get("ready"):
            st.caption(
                "💡 No Groq API keys configured — daily-budget meter unavailable."
            )
        else:
            _total    = _q.get("total_budget", 0)
            _used     = _q.get("used", 0)
            _rem      = _q.get("remaining", 0)
            _pct_used = _q.get("pct_used", 0)
            _runs     = _q.get("est_runs_left", 0)
            _per_key  = _q.get("tokens_per_key", 0)
            _keys_t   = _q.get("keys_total", 0)
            _per_run  = _q.get("tokens_per_run", 0)
            _reset    = _q.get("reset_tokens", "")

            def _fmt_tokens(n: int) -> str:
                if n >= 1_000_000:
                    return f"{n/1_000_000:.1f}M"
                if n >= 1_000:
                    return f"{n/1_000:.0f}K"
                return str(n)

            # Two-metric display: how much of the pool is left + how many more
            # full runs that buys us. Numbers are deployment-wide, not per-user.
            col1, col2 = st.columns(2)
            with col1:
                st.metric(
                    "Tokens left today",
                    _fmt_tokens(_rem),
                    delta=f"of {_fmt_tokens(_total)}",
                    delta_color="off",
                )
            with col2:
                st.metric(
                    "Runs left (est.)",
                    f"~{max(0, _runs)}",
                    delta=f"{_keys_t} keys × {_fmt_tokens(_per_key)}",
                    delta_color="off",
                )

            # Live progress of today's pool usage, 0-100%.
            st.progress(
                min(1.0, _pct_used / 100.0) if _pct_used else 0.0,
                text=f"{_pct_used}% of daily pool used",
            )

            pct_remaining = 100 - _pct_used
            if _total and pct_remaining < 20:
                st.warning(
                    f"Only ~{pct_remaining}% of today's Groq budget left. "
                    f"Quota resets every 24h" +
                    (f" ({_reset})" if _reset else "") +
                    " — consider waiting before the next large run.",
                    icon="⚠️",
                )
            elif _reset:
                st.caption(f"Resets in {_reset} (Groq daily window)")

    st.markdown('<div class="sidebar-h">Privacy</div>', unsafe_allow_html=True)
    with st.expander("Privacy & data", expanded=False):
        trace_opt_in = st.toggle(
            "Allow anonymized tracing",
            value=bool(st.session_state.get("trace_consent", False)),
            help="When enabled, LangSmith tracing is turned on for debugging. "
                 "Tracing stays off by default for this session.",
        )
        if trace_opt_in != bool(st.session_state.get("trace_consent", False)):
            st.session_state["trace_consent"] = trace_opt_in
            apply_tracing_consent(trace_opt_in)
            track_event("privacy_tracing_consent_updated", _ANON_DISTINCT_ID, {"enabled": bool(trace_opt_in), "source": "sidebar_toggle"})
            st.toast(
                "Tracing enabled for this session." if trace_opt_in else "Tracing disabled for this session.",
                icon="✅" if trace_opt_in else "🛡️",
            )

        if st.button("Delete my session data", use_container_width=True):
            track_event("session_data_deleted", _ANON_DISTINCT_ID, {"had_trace_consent": bool(st.session_state.get("trace_consent"))})
            cleanup_session(_SESSION_ID)
            for k in list(st.session_state.keys()):
                del st.session_state[k]
            st.success("Session data deleted.")
            st.rerun()


# ═════════════════════════════════════════════════════════════════════════
# MAIN AREA
# ═════════════════════════════════════════════════════════════════════════

if not run_button:
    # If a completed run is sitting in session state (user clicked a
    # per-card Send button which triggers st.rerun, or simply navigated
    # back into the tab), restore it so the match cards + KPIs stay
    # visible instead of falling back to the welcome screen.
    if "_last_final_state" in st.session_state:
        final_state = st.session_state["_last_final_state"]
        # Fall through to post-run rendering below; the validation + agent
        # block is gated on `run_button` so it won't re-fire.
    else:
        _render_welcome()

        # If the user has prior history, preview it (even before a new run).
        if user_email and user_email.strip():
            summary = history_summary(user_email.strip())
            if summary["total"] > 0:
                st.markdown(
                    f'<div class="section-label">Your history</div>'
                    f'<div class="job-sub">'
                    f'<b>{summary["total"]}</b> previously shown &middot; '
                    f'<b>{summary["applied"]}</b> marked applied &middot; '
                    f'<b>{summary["pending"]}</b> pending'
                    f'</div>',
                    unsafe_allow_html=True,
                )

        st.stop()


# ── Fresh-run-only block ────────────────────────────────────────────────
# Everything in this `if run_button:` scope only runs when the user just
# clicked "Run agent". On subsequent reruns (e.g. when a per-card Send
# button fires st.rerun), the previous run's `final_state` is restored
# from session state at the top of MAIN AREA — and we skip straight to
# the post-run rendering below. This avoids re-validating, re-saving the
# CV, or (worst of all) re-running the agent and burning LLM budget.
if run_button:
    # ── Validation ──────────────────────────────────────────────────────
    errors: List[str] = []
    if not uploaded_cv:                           errors.append("Please upload your CV.")
    if not candidate_name.strip():                errors.append("Please enter your full name.")
    if not user_email.strip() or "@" not in user_email:
        errors.append("Please enter a valid email address.")
    if not job_title.strip():                     errors.append("Please enter a job title.")

    if errors:
        for e in errors:
            st.error(e)
        st.stop()

    # ── Per-session rate limit ──────────────────────────────────────────
    # Prevent a single user / bot from draining the deployment-wide Groq
    # pool in one sitting. Counted in st.session_state (per browser tab,
    # not per user), which is the simplest unit of abuse we can see without
    # auth. Users hitting this are NOT angry — they've had 3 free tries;
    # the soft-stop preserves the service for everyone else.
    # Bypass with APPLYSMART_MAX_RUNS_PER_SESSION=0 (unlimited) for admins.
    _MAX_RUNS_PER_SESSION = int(
        secret_or_env("APPLYSMART_MAX_RUNS_PER_SESSION", "3") or "3"
    )
    
    # Daily reset logic for session run counter
    _today = datetime.now().date().isoformat()
    _last_run_date = st.session_state.get("_runs_used_date")
    if _last_run_date != _today:
        # New day, reset the counter
        st.session_state["_runs_used"] = 0
        st.session_state["_runs_used_date"] = _today
    
    _runs_used_this_session = int(st.session_state.get("_runs_used", 0))
    if _MAX_RUNS_PER_SESSION > 0 and _runs_used_this_session >= _MAX_RUNS_PER_SESSION:
        st.error(
            f"**You've used all {_MAX_RUNS_PER_SESSION} free runs for this "
            f"browser session.**\n\n"
            f"This keeps the shared Groq quota available to other users. "
            f"Refresh the page tomorrow (or get in touch for higher-volume "
            f"access) to continue tailoring."
        )
        # Keep the post-run render visible if the user has a completed run.
        if "_last_final_state" not in st.session_state:
            st.stop()
        else:
            # Halt the fresh-run block but let the previous results keep rendering.
            st.stop()
    # Increment now — even failed runs count, so flood/retry loops can't bypass.
    st.session_state["_runs_used"] = _runs_used_this_session + 1

    _user_distinct_id = distinct_id(_SESSION_ID, user_email)
    _run_started_at = datetime.utcnow()
    track_event(
        "run_started",
        _user_distinct_id,
        {
            "source": selected_source,
            "experience_level": experience_level,
            "num_jobs": num_jobs,
            "match_threshold": match_threshold,
            "preview_mode": bool(preview_mode),
            "has_cv_upload": bool(uploaded_cv),
        },
    )


    # ── Save CV to disk (session-scoped, sanitised filename) ────────────
    try:
        cv_path = safe_upload_path(_UPLOADS_DIR, uploaded_cv.name)
    except Exception as e:
        st.error(f"Rejected CV upload: {e}")
        st.stop()
    with open(cv_path, "wb") as f:
        f.write(uploaded_cv.getbuffer())

    # ── Pre-flight CV compatibility check ───────────────────────────────
    # Hard errors (scanned PDF, password-protected, corrupt file) block the
    # run. Warnings (non-English, too short, unusual format) are surfaced but
    # allow the user to proceed at their own risk.
    cv_report = validate_cv(cv_path)
    if not cv_report.ok:
        st.error(
            "**Your CV can't be processed by the agent.**\n\n"
            + "\n".join(f"- {e}" for e in cv_report.errors)
        )
        if cv_report.warnings:
            with st.expander("Additional warnings"):
                for w in cv_report.warnings:
                    st.write(f"• {w}")
        st.stop()
    if cv_report.warnings:
        with st.expander(
            f"⚠ CV compatibility: {cv_report.score}/100 — "
            f"{len(cv_report.warnings)} warning(s). Click to review.",
            expanded=False,
        ):
            for w in cv_report.warnings:
                st.write(f"• {w}")
            st.caption(
                "The agent will still run, but output quality may be reduced. "
                "You can stop the run if needed."
            )


    # ── Run agent with live progress ────────────────────────────────────
    _run_budget = LLMBudget(limit=_MAX_LLM_CALLS_PER_RUN)
    with st.status("Running agent…", expanded=True) as status_box:
        st.write(f"CV saved to session `{_SESSION_ID[:8]}…`")
        st.write(
            f"Searching {num_jobs} {selected_source} listing(s) for "
            f"**{job_title}** in **{location or 'any location'}**"
        )
        st.write(f"LLM-call budget for this run: **{_run_budget.limit}**")
        try:
            final_state = run_agent(
                cv_path         = cv_path,
                job_title       = job_title,
                location        = location,
                num_jobs        = num_jobs,
                match_threshold = match_threshold,
                user_email      = user_email,
                candidate_name  = candidate_name,
                source          = selected_source,
                experience_level = experience_level,
                output_dir      = _OUTPUTS_DIR,
                session_id      = _SESSION_ID,
                llm_budget      = _run_budget,
                preview_mode    = preview_mode,
            )
        except Exception as e:
            # run_agent now returns a partial result on crash instead of
            # raising — this handler only fires for errors thrown OUTSIDE the
            # graph (e.g. import failures). Still coerce into a final_state
            # shape so the downstream error banner + snapshot UI can render.
            final_state = {
                "status": "crashed",
                "matched_jobs": [],
                "skipped_jobs": [],
                "errors":       [f"{type(e).__name__}: {e}"],
                "llm_budget":   _run_budget.snapshot(),
                "snapshot_path": None,
            }

        # Remember whether this run used preview mode so card renderers
        # (which don't have access to sidebar state directly) can decide
        # whether to show a "Send now" button.
        st.session_state["_last_run_preview_mode"] = preview_mode
        # Persist the completed run so reruns (e.g. from per-card Send
        # buttons) don't lose the match cards and fall back to the
        # welcome screen.
        st.session_state["_last_final_state"] = final_state
        final_status = final_state.get("status")
        if final_status == "budget_exceeded":
            status_box.update(label="Halted — LLM budget exhausted.", state="error")
        elif final_status == "crashed":
            status_box.update(label="Agent stopped with an error.", state="error")
        elif final_status == "awaiting_send":
            status_box.update(
                label="Ready for review — use 'Send' on each card below.",
                state="complete", expanded=False,
            )
        else:
            status_box.update(label="Agent finished.", state="complete", expanded=False)

    _duration_sec = max(0, int((datetime.utcnow() - _run_started_at).total_seconds()))
    _budget_snap = final_state.get("llm_budget") or _run_budget.snapshot()
    _matched = final_state.get("matched_jobs", []) or []
    _match_scores = [
        int(m.get("match_score", 0) or 0) for m in _matched
        if isinstance(m, dict)
    ]
    _best_match_score   = max(_match_scores) if _match_scores else 0
    _median_match_score = (
        sorted(_match_scores)[len(_match_scores) // 2] if _match_scores else 0
    )
    track_event(
        "run_completed",
        _user_distinct_id,
        {
            "status": final_status,
            "duration_sec": _duration_sec,
            "jobs_scraped": len(final_state.get("jobs_found", []) or []),
            "matched_jobs": len(_matched),
            "matches_above_threshold_count": len(_matched),
            "skipped_jobs": len(final_state.get("skipped_jobs", []) or []),
            "best_match_score": _best_match_score,
            "median_match_score": _median_match_score,
            "match_threshold": int(match_threshold),
            "llm_calls_used": int((_budget_snap or {}).get("used", 0) or 0),
            "llm_calls_limit": int((_budget_snap or {}).get("limit", _MAX_LLM_CALLS_PER_RUN) or _MAX_LLM_CALLS_PER_RUN),
            "preview_mode": bool(preview_mode),
        },
    )


# ── Friendly error banner (budget exhausted / crash / rate limit) ──────
def _render_error_banner(state: dict) -> None:
    """Show a humane explanation + snapshot download + Retry button."""
    status  = state.get("status", "unknown")
    errors  = state.get("errors") or []
    err_msg = errors[-1] if errors else ""
    snap    = state.get("snapshot_path")
    budget  = state.get("llm_budget") or {}

    if status == "budget_exceeded":
        title   = "🛑 LLM budget exhausted for this run"
        cause   = (
            f"The agent made **{budget.get('used', '?')}** LLM calls and hit "
            f"the per-run cap of **{budget.get('limit', '?')}**. This is a "
            f"safety limit — not a Groq rate limit."
        )
        action  = (
            "Raise `MAX_LLM_CALLS_PER_RUN` in the environment (default 20) "
            "if you want more headroom, or reduce scrape rounds in the sidebar."
        )
    elif "rate-limited" in err_msg.lower() or "rate limit" in err_msg.lower():
        title  = "⏳ Groq rate limit hit"
        cause  = (
            "The Groq API returned a wait longer than the 60-second cap. "
            "On the free tier this usually means the per-minute or daily "
            "token budget is exhausted."
        )
        action = (
            "Wait **a few minutes** (or until the next daily reset), then "
            "click **Retry**. Using a shorter job title or lower `num_jobs` "
            "also helps stay under the limit."
        )
    elif status == "crashed":
        title  = "💥 Agent stopped with an unexpected error"
        cause  = f"`{err_msg}`" if err_msg else "No error message was captured."
        action = (
            "Download the run snapshot below for the full traceback, then "
            "click Retry. If it keeps failing, check the Streamlit terminal "
            "logs for the stack trace."
        )
    else:
        return  # nothing to warn about

    st.error(f"### {title}\n\n**What happened:** {cause}\n\n**What to do:** {action}")

    cols = st.columns([1, 1, 2])
    with cols[0]:
        if st.button("🔁 Retry", key="error_retry_btn", use_container_width=True):
            # Clear sent-email state + the restored-run cache so a retry
            # truly restarts the flow instead of resurrecting the failed run.
            for k in list(st.session_state.keys()):
                if k.startswith("sent_"):
                    del st.session_state[k]
            st.session_state.pop("_last_final_state", None)
            st.rerun()
    with cols[1]:
        if snap and os.path.exists(snap):
            with open(snap, "rb") as _f:
                st.download_button(
                    "📄 Download snapshot",
                    _f.read(),
                    file_name = os.path.basename(snap),
                    mime      = "application/json",
                    key       = "error_snapshot_dl",
                    use_container_width=True,
                )
        else:
            st.caption("No snapshot written.")


_render_error_banner(final_state)


# ═════════════════════════════════════════════════════════════════════════
# POST-RUN
# ═════════════════════════════════════════════════════════════════════════

status        = final_state.get("status", "unknown")
matched_jobs  = final_state.get("matched_jobs", [])
skipped_jobs  = final_state.get("skipped_jobs", [])
agent_errors  = final_state.get("errors", [])
plan          = final_state.get("plan") or {}
trace         = final_state.get("supervisor_trace") or []
reviews       = final_state.get("review_results") or {}
scrape_rounds = final_state.get("scrape_round", 0)

if agent_errors:
    with st.expander(f"{len(agent_errors)} warning(s) during run", expanded=False):
        for err in agent_errors:
            st.caption(err)


# ── KPI strip ───────────────────────────────────────────────────────────
review_scores = [int(r.get("score", 0)) for r in reviews.values() if r]
avg_review    = round(sum(review_scores) / len(review_scores), 0) if review_scores else None

st.markdown(
    f"""
    <div class="kpi-grid">
      <div class="kpi">
        <div class="label">Matched</div>
        <div class="value">{len(matched_jobs)}</div>
        <div class="hint">above {match_threshold}% threshold</div>
      </div>
      <div class="kpi">
        <div class="label">Below threshold</div>
        <div class="value">{len(skipped_jobs)}</div>
        <div class="hint">scored but skipped</div>
      </div>
      <div class="kpi">
        <div class="label">Scrape rounds</div>
        <div class="value">{scrape_rounds}</div>
        <div class="hint">across {len(plan.get("keyword_bundles", []))} bundle(s)</div>
      </div>
      <div class="kpi">
        <div class="label">Reviewer avg</div>
        <div class="value">{f"{int(avg_review)}" if avg_review else "—"}</div>
        <div class="hint">tailored-CV quality</div>
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)


# ── Tabs ────────────────────────────────────────────────────────────────
tab_match, tab_skip, tab_insight, tab_history = st.tabs([
    f"Matches ({len(matched_jobs)})",
    f"Below threshold ({len(skipped_jobs)})",
    "Agent insight",
    "History",
])


# ──────────────────────── TAB 1 — MATCHES ───────────────────────────────
def _render_match_card(job: Dict[str, Any]) -> None:
    score    = int(job.get("match_score", 0))
    company  = job.get("company", "Unknown")
    title    = job.get("title", "Unknown")
    location = job.get("location", "—")
    source   = job.get("source", "—")
    posted   = job.get("posted_label", "—")
    url      = job.get("url") or ""
    review   = job.get("review") or {}
    rev_score = int(review.get("score", 0)) if review else 0

    # Build chips
    chips: List[str] = [_chip(f"Match {score}/100", _score_chip_class(score))]
    if review:
        chips.append(_chip(f"Review {rev_score}/100", _score_chip_class(rev_score)))
    # "CV + Cover letter" badge when both tailored docs were generated.
    has_cv = bool(job.get("cv_pdf_path") and os.path.exists(job.get("cv_pdf_path") or ""))
    has_cl = bool(job.get("cover_letter_path") and os.path.exists(job.get("cover_letter_path") or ""))
    if has_cv and has_cl:
        chips.append(_chip("CV + Cover letter ready", "chip-primary"))
    # Render-mode transparency: tell the user when we fell back to a full
    # rebuild (visual fidelity lost) vs kept the original layout intact.
    render_mode = job.get("render_mode")
    if render_mode == "rebuilt":
        chips.append(_chip("CV rebuilt (layout differs from original)", "chip-amber"))
    elif render_mode == "in_place":
        chips.append(_chip("CV edited in place", "chip-green"))
    # Cover-letter fabrication warning (if the reviewer flagged any claims).
    cl_rev = job.get("cover_letter_review") or {}
    cl_fabs = cl_rev.get("fabrications") or []
    if cl_fabs:
        chips.append(_chip(
            f"Cover letter: {len(cl_fabs)} unverified claim(s)",
            "chip-red",
        ))
    chips.append(_chip(source))
    if posted and posted != "—":
        chips.append(_chip(posted))

    with st.container():
        st.markdown('<div class="job-card">', unsafe_allow_html=True)
        st.markdown(
            f"""
            <div class="job-head">
              <div>
                <div class="job-title">{title}</div>
                <div class="job-sub">{company} &middot; {location}</div>
              </div>
              <div class="chip-row">{''.join(chips)}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        if url:
            st.markdown(
                f'<div class="job-meta"><a href="{url}" target="_blank">View original listing ↗</a></div>',
                unsafe_allow_html=True,
            )

        # Skills row
        def _pills(items: List[str], css: str = "") -> str:
            return ''.join(f'<span class="pill {css}">{s}</span>' for s in items[:12])

        match_skills  = job.get("matching_skills") or []
        miss_skills   = job.get("missing_skills") or []
        if match_skills or miss_skills:
            st.markdown(
                '<div class="section-label">Skills</div>',
                unsafe_allow_html=True,
            )
            if match_skills:
                st.markdown(
                    f'<div class="pill-row">{_pills(match_skills, "pill-have")}</div>',
                    unsafe_allow_html=True,
                )
            if miss_skills:
                st.markdown(
                    f'<div class="pill-row" style="margin-top:0.4rem">'
                    f'{_pills(miss_skills, "pill-miss")}</div>',
                    unsafe_allow_html=True,
                )

        # Reviewer feedback
        if review and review.get("feedback"):
            st.markdown(
                '<div class="section-label">Reviewer feedback</div>',
                unsafe_allow_html=True,
            )
            st.caption(review["feedback"])

        # Downloads + apply toggle
        col_dl1, col_dl2, col_apply = st.columns([1, 1, 1])

        cv_pdf = job.get("cv_pdf_path")
        cl_pdf = job.get("cover_letter_path")
        safe_id = f"{company}_{title}".replace(" ", "_").replace("/", "-")[:60]

        if cv_pdf and os.path.exists(cv_pdf):
            with open(cv_pdf, "rb") as f:
                cv_clicked = col_dl1.download_button(
                    "Download tailored CV",
                    data=f, file_name=os.path.basename(cv_pdf),
                    mime="application/pdf",
                    key=f"cv_{safe_id}", use_container_width=True,
                )
                if cv_clicked:
                    track_event(
                        "cv_downloaded",
                        distinct_id(_SESSION_ID, user_email),
                        {
                            "company": company,
                            "source": source,
                            "match_score": score,
                        },
                    )
        if cl_pdf and os.path.exists(cl_pdf):
            with open(cl_pdf, "rb") as f:
                cl_clicked = col_dl2.download_button(
                    "Download cover letter",
                    data=f, file_name=os.path.basename(cl_pdf),
                    mime="application/pdf",
                    key=f"cl_{safe_id}", use_container_width=True,
                )
                if cl_clicked:
                    track_event(
                        "cover_letter_downloaded",
                        distinct_id(_SESSION_ID, user_email),
                        {
                            "company": company,
                            "source": source,
                            "match_score": score,
                        },
                    )

        # Note: Per-card Send button has been replaced by a single
        # bulk-send button at the bottom of the Matches tab (see below).
        # Users already download individual docs per card; batching the
        # email send matches how people actually use the tool.
        sent_key = f"sent_{safe_id}"
        if st.session_state.get("_last_run_preview_mode") and (cv_pdf or cl_pdf):
            if st.session_state.get(sent_key):
                st.caption("Emailed to you. Check your inbox.")

        # ── Fabrication + review details (expandable) ────────────
        if cl_fabs or (cl_rev and cl_rev.get("weaknesses")):
            with st.expander(
                f"Cover-letter review (score {cl_rev.get('score', '—')}/100)",
                expanded=bool(cl_fabs),
            ):
                if cl_fabs:
                    st.warning(
                        "**Unverified claims** — these are statements in the "
                        "cover letter that the reviewer could not ground in "
                        "your CV. Please edit the letter before sending:"
                    )
                    for f in cl_fabs:
                        st.write(f"• {f}")
                if cl_rev.get("weaknesses"):
                    st.caption("Reviewer weaknesses:")
                    for w in cl_rev["weaknesses"]:
                        st.write(f"• {w}")
                if cl_rev.get("feedback"):
                    st.caption("Reviewer feedback:")
                    st.write(cl_rev["feedback"])

        # Apply-status checkbox — persists to application_tracker.
        # Current state: load from history each render so a refresh reflects reality.
        user_hist = load_history(user_email)
        from agents.application_tracker import _normalize_url  # local import OK here
        applied_now = False
        if url:
            rec = user_hist.get(_normalize_url(url))
            if rec and rec.get("applied") is True:
                applied_now = True
        new_state = col_apply.checkbox(
            "I applied",
            value = applied_now,
            key   = f"applied_{safe_id}",
            help  = "Hides this job from future runs.",
        )
        if url and new_state != applied_now:
            if new_state:
                mark_applied(user_email, [url])
                st.toast(f"Marked applied: {title} at {company}", icon="✅")
                track_event(
                    "job_marked_applied",
                    distinct_id(_SESSION_ID, user_email),
                    {"company": company, "source": source, "match_score": score},
                )
            else:
                mark_not_applied(user_email, [url])
                st.toast(f"Un-marked: {title} at {company}", icon="🔄")
                track_event(
                    "job_unmarked_applied",
                    distinct_id(_SESSION_ID, user_email),
                    {"company": company, "source": source, "match_score": score},
                )

        st.markdown('</div>', unsafe_allow_html=True)


with tab_match:
    if not matched_jobs:
        st.info(
            f"No jobs met the {match_threshold}% threshold. "
            "Try lowering the threshold or picking a different board in the sidebar."
        )
    else:
        # Sort by match_score descending for visual hierarchy.
        sorted_matches = sorted(
            matched_jobs,
            key=lambda j: int(j.get("match_score", 0)),
            reverse=True,
        )
        for job in sorted_matches:
            _render_match_card(job)

        # ── Bulk "Send all to my email" action (preview mode only) ──
        # Replaces the per-card Send button. One click emails every
        # tailored CV + cover letter pair that the user has not already
        # received. Jobs already sent this session are skipped.
        if st.session_state.get("_last_run_preview_mode"):
            sendable = []
            for j in sorted_matches:
                url_j   = j.get("url") or j.get("title", "")
                safe_id = abs(hash(url_j)) % (10**10)
                sent_key = f"sent_{safe_id}"
                cv_p = j.get("cv_pdf_path")
                cl_p = j.get("cover_letter_path")
                if st.session_state.get(sent_key):
                    continue
                if not (cv_p and os.path.exists(cv_p)) and not (cl_p and os.path.exists(cl_p)):
                    continue
                sendable.append((j, sent_key, cv_p, cl_p))

            st.markdown("&nbsp;", unsafe_allow_html=True)
            if not sendable:
                if any(st.session_state.get(f"sent_{abs(hash((j.get('url') or j.get('title','')))) % (10**10)}") for j in sorted_matches):
                    st.success(
                        f"All {len(sorted_matches)} tailored application(s) emailed to "
                        f"**{user_email}**."
                    )
                    st.info(
                        "📬 Can't find the email? First-time senders often land "
                        "in **Spam / Junk / Promotions**. Mark as **Not spam** "
                        "to train your provider for future messages.",
                        icon="📬",
                    )
            else:
                label = (
                    f"📧 Send all {len(sendable)} tailored CV + cover letter(s) to my email"
                    if len(sendable) == len(sorted_matches)
                    else f"📧 Send remaining {len(sendable)} of {len(sorted_matches)} to my email"
                )
                if st.button(label, key="send_all_btn", type="primary", use_container_width=True):
                    track_event(
                        "send_attempted",
                        distinct_id(_SESSION_ID, user_email),
                        {"mode": "bulk", "jobs_count": len(sendable)},
                    )
                    from agents.email_agent import send_email
                    progress = st.progress(0.0, text="Preparing attachments …")
                    
                    # Collect all attachments for a single email
                    all_attachments = []
                    job_summaries = []
                    for j, sent_key, cv_p, cl_p in sendable:
                        t_title = j.get("title", "—")
                        t_company = j.get("company", "—")
                        for p in (cv_p, cl_p):
                            if p and os.path.exists(p):
                                all_attachments.append(p)
                        job_summaries.append(
                            f"• {t_title} at {t_company} — Score: {j.get('match_score', '?')}/100 | "
                            f"Source: {j.get('source', '—')} — {j.get('url', '')}"
                        )
                        st.session_state[sent_key] = True
                    
                    progress.progress(0.5, text="Sending email …")
                    
                    # Send one email with all attachments
                    try:
                        email_body = (
                            f"Hi {candidate_name},\n\n"
                            f"Your Job Application Agent found {len(sendable)} matched role(s) "
                            f"for \"{job_title}\" in {location}.\n\n"
                            f"MATCHED JOBS:\n" + "\n".join(job_summaries) + "\n\n"
                            f"Your tailored CVs and cover letters are attached.\n\n"
                            f"Good luck!\nJob Application Agent"
                        )
                        send_email(
                            to_email = user_email,
                            subject = f"Job Agent: {len(sendable)} match(es) for {job_title}",
                            body = email_body,
                            attachments = all_attachments,
                        )
                        n_ok = len(sendable)
                        n_fail = 0
                        progress.progress(1.0, text="Done.")
                        st.toast(f"Sent {n_ok} application(s) in one email to {user_email}", icon="✅")
                        # Persistent spam-folder reminder — stays visible after
                        # the toast auto-dismisses. First-time senders from a
                        # new domain are frequently filtered to Spam/Junk by
                        # Gmail/Outlook regardless of SPF/DKIM; this note
                        # sets expectations and gives users a clear next step.
                        st.info(
                            "📬 **Heads up:** first-time emails from a new "
                            "sender often land in **Spam / Junk / Promotions** "
                            "— check those folders if you don't see it in "
                            "your inbox within a minute. Marking the email "
                            "as **Not spam** (or adding the sender to your "
                            "contacts) trains your provider to deliver future "
                            "messages straight to the inbox.",
                            icon="📬",
                        )
                    except Exception as send_err:
                        n_ok = 0
                        n_fail = len(sendable)
                        progress.progress(1.0, text="Done.")
                        st.error(f"Failed to send email: {send_err}")
                        st.toast("No emails sent — see errors above", icon="❌")
                    track_event(
                        "send_completed",
                        distinct_id(_SESSION_ID, user_email),
                        {
                            "mode": "bulk",
                            "attempted_count": len(sendable),
                            "sent_count": n_ok,
                            "failed_count": n_fail,
                        },
                    )
                    st.rerun()


# ──────────────────────── TAB 2 — BELOW THRESHOLD ───────────────────────
with tab_skip:
    if not skipped_jobs:
        st.caption("Nothing below threshold this run.")
    else:
        st.caption(
            "Jobs scored below the match threshold. No tailored docs generated. "
            "Lower the threshold in the sidebar to include these next time."
        )
        for job in sorted(skipped_jobs, key=lambda j: int(j.get("match_score", 0)), reverse=True):
            score = int(job.get("match_score", 0))
            chip = _chip(f"Match {score}/100", _score_chip_class(score))
            url = job.get("url") or ""
            link = f' &middot; <a href="{url}" target="_blank">View</a>' if url else ""
            st.markdown(
                f"""
                <div class="job-card" style="padding: 0.65rem 1rem;">
                  <div class="job-head">
                    <div>
                      <div class="job-title" style="font-size:0.95rem">
                        {job.get('title', '—')} &middot; {job.get('company', '—')}
                      </div>
                      <div class="job-sub">
                        {job.get('source', '—')} &middot;
                        {job.get('posted_label', '—')} &middot;
                        {job.get('location', '—')}{link}
                      </div>
                    </div>
                    <div class="chip-row">{chip}</div>
                  </div>
                </div>
                """,
                unsafe_allow_html=True,
            )


# ──────────────────────── TAB 3 — AGENT INSIGHT ─────────────────────────
with tab_insight:
    # Plan
    st.markdown('<div class="section-label">Execution plan (planner agent)</div>',
                unsafe_allow_html=True)
    if not plan:
        st.caption("Planner did not run (legacy path).")
    else:
        qb = plan.get("quality_bar") or {}
        st.markdown(
            f'<div class="job-sub" style="margin-bottom:0.7rem">'
            f'Goal: at least <b>{qb.get("min_matches", "?")}</b> match(es) '
            f'at score <b>≥ {qb.get("min_score", "?")}</b>, '
            f'up to <b>{qb.get("max_scrape_rounds", "?")}</b> scrape round(s).'
            f'</div>',
            unsafe_allow_html=True,
        )
        for i, b in enumerate(plan.get("keyword_bundles") or []):
            st.markdown(
                f'<div class="plan-bundle">'
                f'<div class="b-title">Bundle {i+1}: {b.get("title","—")}</div>'
                f'<div class="b-meta">@ {b.get("location","—")} &mdash; {b.get("reason","")}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
        if plan.get("emphasis_skills"):
            st.markdown('<div class="section-label">Skills the planner wants to emphasise</div>',
                        unsafe_allow_html=True)
            st.markdown(
                f'<div class="pill-row">'
                f'{"".join(f"""<span class=pill>{s}</span>""" for s in plan["emphasis_skills"])}'
                f'</div>',
                unsafe_allow_html=True,
            )
        if plan.get("reasoning"):
            st.caption(plan["reasoning"])

    # Supervisor trace
    st.markdown('<div class="section-label">Supervisor decisions</div>',
                unsafe_allow_html=True)
    llm_decisions = [t for t in trace if t.get("mode") == "llm"]
    if not llm_decisions:
        st.caption(
            "Supervisor had only deterministic (single-option) transitions this run — "
            "no real agentic choices were required."
        )
    else:
        st.caption(
            f"{len(llm_decisions)} LLM-driven decision(s). Remaining cycles were "
            "short-circuited because only one next action was allowed."
        )
        rows = []
        for t in llm_decisions:
            rows.append(
                f'<div class="trace-row">'
                f'<div class="cyc">#{t["cycle"]}</div>'
                f'<div class="from">{t["observed"]}</div>'
                f'<div class="to">→ {t["action"]}</div>'
                f'<div>{t.get("reasoning","")}</div>'
                f'</div>'
            )
        st.markdown(
            f'<div class="job-card" style="padding:0">{"".join(rows)}</div>',
            unsafe_allow_html=True,
        )

    # Reviewer summary
    if reviews:
        st.markdown('<div class="section-label">Reviewer scores</div>',
                    unsafe_allow_html=True)
        st.caption(
            f"Each tailored CV is scored by a reviewer agent. "
            f"Scores < 72 trigger a retry with the reviewer's feedback."
        )
        for job_key, r in reviews.items():
            score = int(r.get("score", 0))
            st.markdown(
                f'<div class="job-card" style="padding:0.65rem 0.9rem; margin-bottom:0.5rem;">'
                f'<div class="job-head">'
                f'<div class="job-title" style="font-size:0.9rem">{job_key[:80]}</div>'
                f'<div class="chip-row">{_chip(f"{score}/100", _score_chip_class(score))}'
                f'{_chip(r.get("verdict","—").upper())}</div>'
                f'</div>'
                f'<div class="job-sub" style="margin-top:0.45rem">{r.get("feedback","")}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )


# ──────────────────────── TAB 4 — HISTORY ───────────────────────────────
with tab_history:
    hist = load_history(user_email)
    if not hist:
        st.caption("No job history yet. Records appear here after your first run.")
    else:
        applied_now  = [r for r in hist.values() if r.get("applied") is True]
        pending      = [r for r in hist.values() if not r.get("applied")]
        c1, c2, c3 = st.columns(3)
        c1.metric("Total seen",    len(hist))
        c2.metric("Marked applied", len(applied_now))
        c3.metric("Pending",        len(pending))

        st.markdown('<div class="section-label">All jobs you\'ve been shown</div>',
                    unsafe_allow_html=True)
        # Sort: most recent first
        rows = sorted(
            hist.items(),
            key=lambda kv: kv[1].get("shown_at", ""),
            reverse=True,
        )
        for url, rec in rows:
            status_chip = (
                _chip("Applied", "chip-green") if rec.get("applied") is True
                else _chip("Pending", "chip-amber") if rec.get("applied") is None
                else _chip("Not yet", "")
            )
            shown_at = (rec.get("shown_at") or "")[:19].replace("T", " ")
            st.markdown(
                f"""
                <div class="job-card" style="padding: 0.65rem 0.95rem; margin-bottom: 0.45rem;">
                  <div class="job-head">
                    <div>
                      <div class="job-title" style="font-size:0.93rem">
                        {rec.get('title','—')} &middot; {rec.get('company','—')}
                      </div>
                      <div class="job-sub">
                        Match {rec.get('match_score', 0)}/100 &middot;
                        seen {rec.get('seen_count', 1)}× &middot;
                        last: {shown_at}
                      </div>
                    </div>
                    <div class="chip-row">{status_chip}</div>
                  </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
