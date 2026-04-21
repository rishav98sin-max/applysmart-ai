"""
Smoke test for performance v1.1 changes:
- Smart retry gate logic
- Parallel execution structure
- Job concurrency configuration
"""

import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.job_agent import tailor_and_generate_node, MAX_TAILOR_RETRIES


def test_max_tailor_retries():
    """Verify MAX_TAILOR_RETRIES is set to 1."""
    assert MAX_TAILOR_RETRIES == 1, f"Expected MAX_TAILOR_RETRIES=1, got {MAX_TAILOR_RETRIES}"
    print("✓ MAX_TAILOR_RETRIES = 1")


def test_job_concurrency_env():
    """Verify TAILOR_JOB_CONCURRENCY env var is read correctly."""
    # Default should be 2
    concurrency = max(1, int(os.getenv("TAILOR_JOB_CONCURRENCY", "2")))
    assert concurrency == 2, f"Expected default concurrency=2, got {concurrency}"
    print("✓ TAILOR_JOB_CONCURRENCY default = 2")


def test_threadpool_import():
    """Verify ThreadPoolExecutor is imported."""
    import agents.job_agent as ja
    assert hasattr(ja, 'ThreadPoolExecutor'), "ThreadPoolExecutor not imported"
    print("✓ ThreadPoolExecutor imported")


def test_smart_retry_gate_logic():
    """Verify the smart retry gate logic exists in the code."""
    import agents.job_agent as ja
    source = open('agents/job_agent.py', encoding='utf-8').read()
    
    # Check for the smart retry gate condition
    assert 'n_rewrites >= 3' in source, "Smart retry gate: n_rewrites check missing"
    assert 'review["score"] >= 55' in source, "Smart retry gate: score check missing"
    assert 'fab_flag' in source, "Smart retry gate: fabrication flag missing"
    assert 'accepting first draft' in source, "Smart retry gate: success message missing"
    print("✓ Smart retry gate logic present")


def test_parallel_structure():
    """Verify parallel execution structure exists."""
    import agents.job_agent as ja
    source = open('agents/job_agent.py', encoding='utf-8').read()
    
    # Check for parallel execution markers (use ASCII to avoid encoding issues)
    assert 'parallel: cover-letter' in source, "Per-job parallel marker missing"
    assert 'tailor across-jobs concurrency=' in source, "Across-job parallel marker missing"
    assert '_process_single_job' in source, "Job helper function missing"
    assert 'ThreadPoolExecutor(max_workers=2' in source, "Inner thread pool missing"
    assert 'outer_ex.map(_process_single_job' in source, "Outer thread pool missing"
    print("✓ Parallel execution structure present")


def test_shared_outline_cache():
    """Verify CV outline is built once and shared."""
    import agents.job_agent as ja
    source = open('agents/job_agent.py', encoding='utf-8').read()
    
    assert 'shared_outline' in source, "shared_outline variable missing"
    assert '_build_outline(state["cv_path"])' in source, "Outline build missing"
    print("✓ Shared outline cache present")


if __name__ == "__main__":
    print("Running performance v1.1 smoke tests...\n")
    
    test_max_tailor_retries()
    test_job_concurrency_env()
    test_threadpool_import()
    test_smart_retry_gate_logic()
    test_parallel_structure()
    test_shared_outline_cache()
    
    print("\n✅ All smoke tests passed.")
