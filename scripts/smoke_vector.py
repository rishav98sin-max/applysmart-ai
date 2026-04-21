"""
Smoke test for e3 — vector retrieval.

Walks the full intended path:
  1. is_available() returns True (deps + env OK)
  2. build_outline(cv_path) produces a non-empty outline
  3. index_cv(cv_path, outline) returns a collection name
  4. retrieve(coll, jd, k) returns chunks sorted by relevance
  5. format_chunks_for_prompt(chunks) is non-empty

Run:  python scripts/smoke_vector.py [optional_cv_path]
"""
from __future__ import annotations

import os
import sys
import time

# Make sure project root is on sys.path when invoked from scripts/
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agents.cv_embeddings import (
    is_available, index_cv, retrieve, format_chunks_for_prompt,
)
from agents.pdf_editor import build_outline


DEFAULT_CV = os.path.join(
    ROOT, "sessions",
    "2497225b2e724c1a859dd316e9429688", "uploads",
    "1d177529_RishavSinghProductManager_CV.pdf",
)

SAMPLE_JD = """
Product Manager, Payments Infrastructure — Stripe, Dublin.
We need a PM with hands-on experience shipping API products, strong SQL
and data analysis skills, and a track record of stakeholder management
across cross-functional engineering and business teams. Agile delivery,
performance testing background a plus.
"""


def main() -> int:
    cv_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CV
    if not os.path.exists(cv_path):
        print(f"❌ CV not found: {cv_path}")
        return 2

    print(f"▶ CV: {cv_path}")

    t0 = time.perf_counter()
    if not is_available():
        print("❌ cv_embeddings.is_available() returned False "
              "(deps missing or USE_VECTOR_RETRIEVAL=0)")
        return 1
    print(f"✔ is_available() — {time.perf_counter()-t0:.2f}s  (first call loads model)")

    t1 = time.perf_counter()
    outline = build_outline(cv_path)
    n_bullets = sum(len(r.get("bullets") or []) for r in outline.get("roles") or [])
    print(f"✔ build_outline — {time.perf_counter()-t1:.2f}s  "
          f"(summary={bool(outline.get('summary'))}, "
          f"roles={len(outline.get('roles') or [])}, "
          f"bullets={n_bullets}, "
          f"skills={len(outline.get('skills') or [])})")

    t2 = time.perf_counter()
    coll = index_cv(cv_path, outline)
    print(f"✔ index_cv — {time.perf_counter()-t2:.2f}s  collection={coll!r}")
    if not coll:
        print("❌ index_cv returned None — nothing to retrieve against")
        return 1

    t3 = time.perf_counter()
    # Second index call should be near-instant (idempotent).
    coll2 = index_cv(cv_path, outline)
    assert coll == coll2, "collection name must be stable per CV"
    print(f"✔ index_cv (idempotent re-call) — {time.perf_counter()-t3:.2f}s")

    t4 = time.perf_counter()
    chunks = retrieve(coll, SAMPLE_JD, k=8)
    print(f"✔ retrieve — {time.perf_counter()-t4:.2f}s  got {len(chunks)} chunks")

    for i, c in enumerate(chunks, 1):
        print(f"  [{i}] section={c['section']:<12} "
              f"distance={c['distance']:.3f}  "
              f"text={c['text'][:80]!r}")

    formatted = format_chunks_for_prompt(chunks)
    print("\n── format_chunks_for_prompt output ──")
    print(formatted[:1200])
    print("...\n")
    print(f"✔ formatted length: {len(formatted)} chars")
    return 0


if __name__ == "__main__":
    sys.exit(main())
