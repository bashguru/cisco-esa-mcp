"""Hybrid retrieval: BM25 + dense vectors, fused with RRF, then cross-encoder reranked.

Pipeline (all free, all local):

  query
    |-- BM25 (ParadeDB pg_search)  -- top bm25_k, lexical/exact matches
    |-- dense vector (pgvector)    -- top vector_k, semantic matches
    v
  Reciprocal Rank Fusion (rrf_k=60)  -- merge the two ranked lists
    v
  cross-encoder rerank (bge-reranker-v2-m3)  -- the accuracy multiplier
    v
  top result_top_k with citations

Metadata filters (product / version / doc_type) apply to BOTH legs and are the
main defense against the corpus's version sprawl.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from .config import get_settings
from .db import connection
from .logging_setup import get_logger
from .models import embedder, reranker

log = get_logger(__name__)

# Characters with special meaning to the pg_search query parser. We keep the
# raw query for the dense/vector leg but feed a sanitized version to BM25.
_BM25_SPECIAL = re.compile(r'[+\-!(){}\[\]^"~*?:\\/]|&&|\|\|')


def _sanitize_bm25(query: str) -> str:
    cleaned = _BM25_SPECIAL.sub(" ", query)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or query.strip()


def _filters(product, version, doc_type, content_type) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if product:
        clauses.append("product = %s")
        params.append(product)
    if version:
        # "16.5" should also match "16.5.1", "16.5.2", ...
        clauses.append("(version = %s OR version LIKE %s)")
        params.extend([version, f"{version}.%"])
    if doc_type:
        clauses.append("doc_type = %s")
        params.append(doc_type)
    if content_type:
        clauses.append("content_type = %s")
        params.append(content_type)
    sql = (" AND " + " AND ".join(clauses)) if clauses else ""
    return sql, params


def _bm25_leg(cur, query: str, filt_sql: str, filt_params: list[Any], k: int) -> list[int]:
    sql = f"""
        SELECT id, paradedb.score(id) AS score
        FROM chunks
        WHERE content @@@ %s{filt_sql}
        ORDER BY score DESC, id
        LIMIT %s
    """
    cur.execute(sql, [_sanitize_bm25(query), *filt_params, k])
    return [r[0] for r in cur.fetchall()]


def _vector_leg(cur, qvec: list[float], filt_sql: str, filt_params: list[Any], k: int) -> list[int]:
    sql = f"""
        SELECT id
        FROM chunks
        WHERE embedding IS NOT NULL{filt_sql}
        ORDER BY embedding <=> %s
        LIMIT %s
    """
    cur.execute(sql, [*filt_params, qvec, k])
    return [r[0] for r in cur.fetchall()]


def _rrf(bm25_ids: list[int], vector_ids: list[int]) -> list[tuple[int, float]]:
    s = get_settings()
    scores: dict[int, float] = {}
    for rank, cid in enumerate(bm25_ids, start=1):
        scores[cid] = scores.get(cid, 0.0) + s.rrf_text_weight / (s.rrf_k + rank)
    for rank, cid in enumerate(vector_ids, start=1):
        scores[cid] = scores.get(cid, 0.0) + s.rrf_vector_weight / (s.rrf_k + rank)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


def _load_chunks(cur, ids: list[int]) -> dict[int, dict[str, Any]]:
    if not ids:
        return {}
    cur.execute(
        """
        SELECT c.id, c.document_id, c.content, c.content_type, c.section_path,
               c.page_from, c.page_to, c.product, c.version, c.doc_type, c.image_id,
               d.filename, d.title, d.source_path
        FROM chunks c JOIN documents d ON d.id = c.document_id
        WHERE c.id = ANY(%s)
        """,
        (ids,),
    )
    cols = [c.name for c in cur.description]
    return {row[0]: dict(zip(cols, row)) for row in cur.fetchall()}


def hybrid_search(
    query: str,
    product: Optional[str] = None,
    version: Optional[str] = None,
    doc_type: Optional[str] = None,
    content_type: Optional[str] = None,
    top_k: Optional[int] = None,
) -> list[dict[str, Any]]:
    s = get_settings()
    top_k = top_k or s.result_top_k
    filt_sql, filt_params = _filters(product, version, doc_type, content_type)
    qvec = embedder.encode_one(query, is_query=True)

    with connection() as conn, conn.cursor() as cur:
        bm25_ids = _bm25_leg(cur, query, filt_sql, filt_params, s.bm25_k)
        vector_ids = _vector_leg(cur, qvec, filt_sql, filt_params, s.vector_k)
        fused = _rrf(bm25_ids, vector_ids)
        candidate_ids = [cid for cid, _ in fused[: s.rerank_candidates]]
        rows = _load_chunks(cur, candidate_ids)

    ordered = [rows[cid] for cid in candidate_ids if cid in rows]
    if not ordered:
        return []

    if s.rerank_enabled:
        scores = reranker.score(query, [r["content"] for r in ordered])
        for r, sc in zip(ordered, scores):
            r["score"] = round(float(sc), 4)
        ordered.sort(key=lambda r: r["score"], reverse=True)
    else:
        fused_map = dict(fused)
        for r in ordered:
            r["score"] = round(fused_map.get(r["id"], 0.0), 6)

    results = []
    for r in ordered[:top_k]:
        content = r["content"] or ""
        results.append(
            {
                "chunk_id": r["id"],
                "document_id": r["document_id"],
                "score": r["score"],
                "product": r["product"],
                "version": r["version"],
                "doc_type": r["doc_type"],
                "content_type": r["content_type"],
                "title": r["title"],
                "filename": r["filename"],
                "page_from": r["page_from"],
                "page_to": r["page_to"],
                "section": r["section_path"],
                "image_id": r["image_id"],
                "has_image": r["image_id"] is not None,
                "text": content[:4000],
                "truncated": len(content) > 4000,
                "citation": _citation(r),
            }
        )
    return results


def _citation(r: dict[str, Any]) -> str:
    bits = [r.get("title") or r.get("filename")]
    if r.get("version"):
        bits.append(f"v{r['version']}")
    if r.get("page_from"):
        pages = f"p.{r['page_from']}"
        if r.get("page_to") and r["page_to"] != r["page_from"]:
            pages += f"-{r['page_to']}"
        bits.append(pages)
    if r.get("section"):
        bits.append(r["section"])
    return " · ".join(str(b) for b in bits if b)
