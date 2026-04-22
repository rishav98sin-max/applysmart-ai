import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from agents.llm_client import get_quota_summary

summary = get_quota_summary()
print("=== QUOTA SUMMARY ===")
print(f"Total Budget: {summary['total_budget']} tokens")
print(f"Used (Total): {summary['used']} tokens")
print(f"Used (Groq): {summary['groq_used']} tokens")
print(f"Used (Gemini): {summary['gemini_used']} tokens")
print(f"Remaining: {summary['remaining']} tokens")
print(f"Percentage Used: {summary['pct_used']}%")
print(f"Estimated Runs Left: {summary['est_runs_left']}")
print(f"Keys Total: {summary['keys_total']}")
print(f"Ready: {summary['ready']}")
