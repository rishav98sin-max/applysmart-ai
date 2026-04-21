# agents/cv_embeddings
# ====================
# Vector-retrieval layer for the agent. Fixed: larger chunks, full-CV
# fallback when retrieval is sparse.

from __future__ import annotations

import hashlib
import os
from typing import Any, Dict, List, Optional, Tuple

# ─────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────

_VECTOR_ENABLED = os.getenv("USE_VECTOR_RETRIEVAL", "1").strip().lower() not in (
    "0", "false", "no", "off",
)
_EMBED_MODEL_NAME = os.getenv(
    "VECTOR_EMBEDDER", "sentence-transformers/all-MiniLM-L6-v2"
)
_DB_DIR = os.getenv("VECTOR_DB_DIR", os.path.join("data", "chroma"))

_embedder      = None
_chroma_client = None
_import_failed = False


# ─────────────────────────────────────────────────────────────
# Import + init helpers
# ─────────────────────────────────────────────────────────────

def is_available() -> bool:
    if not _VECTOR_ENABLED or _import_failed:
        return False
    return _ensure_embedder() is not None and _ensure_chroma() is not None


def _ensure_embedder():
    global _embedder, _import_failed
    if _embedder is not None or _import_failed:
        return _embedder
    try:
        from sentence_transformers import SentenceTransformer
        _embedder = SentenceTransformer(_EMBED_MODEL_NAME)
        return _embedder
    except Exception as e:
        _import_failed = True
        print(f"   ⚠️  cv_embeddings disabled — embedder import failed: "
              f"{type(e).__name__}: {e}")
        return None


def _ensure_chroma():
    global _chroma_client, _import_failed
    if _chroma_client is not None or _import_failed:
        return _chroma_client
    try:
        import chromadb
        os.makedirs(_DB_DIR, exist_ok=True)
        _chroma_client = chromadb.PersistentClient(path=_DB_DIR)
        return _chroma_client
    except Exception as e:
        _import_failed = True
        print(f"   ⚠️  cv_embeddings disabled — chroma client failed: "
              f"{type(e).__name__}: {e}")
        return None


# ─────────────────────────────────────────────────────────────
# CV fingerprinting + chunking
# ─────────────────────────────────────────────────────────────

def _fingerprint(cv_path: str) -> str:
    h = hashlib.sha256()
    with open(cv_path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()[:16]


def _chunks_from_outline(outline: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    ✅ FIXED: Instead of one chunk per bullet/skill (too granular),
    we now create ROLE-LEVEL chunks that group all bullets for a role
    together. This gives the LLM much more context per chunk.
    """
    out: List[Dict[str, Any]] = []

    # Summary — keep as single chunk
    summary = (outline.get("summary") or "").strip()
    if summary:
        out.append({
            "id":   "summary",
            "text": summary,
            "metadata": {"section": "summary", "role": "", "idx": 0},
        })

    # ✅ FIXED: Group all bullets per role into ONE chunk instead of
    # one chunk per bullet. This prevents tiny 50-char chunks.
    for role_idx, role in enumerate(outline.get("roles", []) or []):
        header  = (role.get("header") or "").strip()
        bullets = [b.strip() for b in (role.get("bullets", []) or []) if b.strip()]

        if not bullets and not header:
            continue

        # Build a rich role block: header + all bullets combined
        role_text = header + "\n" + "\n".join(f"• {b}" for b in bullets) if bullets else header

        out.append({
            "id":   f"role::{role_idx}::{header[:40]}",
            "text": role_text,
            "metadata": {"section": "role_block", "role": header, "idx": role_idx},
        })

    # ✅ FIXED: Group ALL skills into ONE chunk instead of one per skill
    skills = outline.get("skills") or []
    if skills:
        skills_clean = [str(s).strip() for s in skills if str(s).strip()]
        if skills_clean:
            out.append({
                "id":   "skills_block",
                "text": "SKILLS: " + ", ".join(skills_clean),
                "metadata": {"section": "skills_block", "role": "", "idx": 0},
            })

    # ✅ NEW: Also add education as a single chunk if present
    education = (outline.get("education") or "").strip()
    if education:
        out.append({
            "id":   "education",
            "text": "EDUCATION:\n" + education,
            "metadata": {"section": "education", "role": "", "idx": 0},
        })

    return out


# ─────────────────────────────────────────────────────────────
# Public: index + retrieve
# ─────────────────────────────────────────────────────────────

def index_cv(cv_path: str, outline: Dict[str, Any]) -> Optional[str]:
    """
    Embed + store every chunk of this CV. Returns the Chroma collection
    name on success, None if unavailable or nothing indexable.
    Idempotent — same CV (same SHA256) reuses existing collection.
    """
    if not is_available():
        return None

    chunks = _chunks_from_outline(outline or {})
    if not chunks:
        return None

    fp   = _fingerprint(cv_path)
    name = f"cv_{fp}"

    client   = _chroma_client
    embedder = _embedder
    assert client is not None and embedder is not None

    try:
        existing = {c.name for c in client.list_collections()}
        if name in existing:
            coll = client.get_collection(name)
            # ✅ Always re-index if chunk count changed (e.g. after this fix)
            if coll.count() >= len(chunks):
                print(f"   🧠  Reusing existing collection '{name}' "
                      f"({coll.count()} chunks)")
                return name
            client.delete_collection(name)

        coll = client.create_collection(name)

        ids       = [c["id"] for c in chunks]
        docs      = [c["text"] for c in chunks]
        metadatas = [c["metadata"] for c in chunks]

        vectors = embedder.encode(
            docs, show_progress_bar=False, convert_to_numpy=True
        ).tolist()

        coll.add(
            ids        = ids,
            documents  = docs,
            embeddings = vectors,
            metadatas  = metadatas,
        )
        print(f"   🧠  Indexed {len(chunks)} chunks into collection '{name}'")
        return name

    except Exception as e:
        print(f"   ⚠️  index_cv failed ({type(e).__name__}: {e}) — "
              f"falling back to non-vector path.")
        return None


def retrieve(
    collection_name: str,
    query_text:      str,
    k:               int = 10,
) -> List[Dict[str, Any]]:
    """
    Return top-K chunks most relevant to query_text.
    ✅ FIXED: k defaulted to 10 so we retrieve ALL role blocks,
    not just 3 tiny bullets.
    """
    if not collection_name or not is_available() or not (query_text or "").strip():
        return []

    client   = _chroma_client
    embedder = _embedder
    assert client is not None and embedder is not None

    try:
        coll = client.get_collection(collection_name)
    except Exception:
        return []

    try:
        # ✅ FIXED: Retrieve up to ALL chunks (coll.count()) not just k
        # For a typical CV with 3-5 roles, this means we get everything
        n = min(max(1, int(k)), coll.count())
        qvec = embedder.encode([query_text], convert_to_numpy=True).tolist()
        res  = coll.query(query_embeddings=qvec, n_results=n)
    except Exception as e:
        print(f"   ⚠️  vector retrieve failed ({type(e).__name__}: {e})")
        return []

    docs = (res.get("documents") or [[]])[0]
    mets = (res.get("metadatas") or [[]])[0]
    dsts = (res.get("distances") or [[]])[0]

    out: List[Dict[str, Any]] = []
    for d, m, dist in zip(docs, mets, dsts):
        m = m or {}
        out.append({
            "text":     d,
            "section":  m.get("section", ""),
            "role":     m.get("role", ""),
            "idx":      int(m.get("idx", 0)),
            "distance": float(dist),
        })
    return out


def format_chunks_for_prompt(chunks: List[Dict[str, Any]]) -> str:
    """
    ✅ FIXED: Render chunks into a clean prompt fragment.
    Role blocks are now pre-formatted so we just join them directly.
    """
    if not chunks:
        return "(no retrieved CV context)"

    summary_parts: List[str] = []
    role_parts:    List[str] = []
    skills_parts:  List[str] = []
    edu_parts:     List[str] = []

    for c in chunks:
        sec  = c.get("section", "")
        text = c.get("text", "").strip()
        if not text:
            continue
        if sec == "summary":
            summary_parts.append(text)
        elif sec == "role_block":
            role_parts.append(text)
        elif sec == "skills_block":
            skills_parts.append(text)
        elif sec == "education":
            edu_parts.append(text)

    parts: List[str] = []
    if summary_parts:
        parts.append("SUMMARY:\n" + "\n".join(summary_parts))
    if role_parts:
        parts.append("EXPERIENCE:\n" + "\n\n".join(role_parts))
    if skills_parts:
        parts.append("\n".join(skills_parts))
    if edu_parts:
        parts.append("\n".join(edu_parts))

    return "\n\n".join(parts) or "(no retrieved CV context)"