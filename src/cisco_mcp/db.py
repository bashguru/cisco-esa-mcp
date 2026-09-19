"""Postgres / ParadeDB access layer.

Uses psycopg 3 with a connection pool and the pgvector adapter. The schema is
created from code so that changing EMBEDDING_DIM (because you swapped the
embedding model) just works on the next ``init_schema()``.

Schema
------
documents  one row per source file (metadata + sha256 for incremental ingest)
chunks     retrievable text/table/figure units, with a dense embedding and
           denormalized product/version/doc_type for fast filtering
images     extracted figures/diagrams/photos, kept on disk with caption + OCR
           so the MCP can hand them back for inclusion in output
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Optional

import psycopg
from pgvector.psycopg import register_vector
from psycopg_pool import ConnectionPool

from .config import get_settings
from .logging_setup import get_logger

log = get_logger(__name__)

_pool: Optional[ConnectionPool] = None
_pool_lock = threading.Lock()


def _configure(conn: psycopg.Connection) -> None:
    # register_vector needs the `vector` type to already exist in the DB, which
    # init_schema() guarantees before the pool is used.
    try:
        register_vector(conn)
    except Exception as exc:  # noqa: BLE001 - first boot, extension not yet created
        log.warning("pgvector adapter not registered yet: %s", exc)


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                s = get_settings()
                _pool = ConnectionPool(
                    conninfo=s.database_url,
                    min_size=s.pool_min,
                    max_size=s.pool_max,
                    configure=_configure,
                    open=True,
                    kwargs={"autocommit": False},
                )
    return _pool


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    pool = get_pool()
    with pool.connection() as conn:
        yield conn


DDL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_search;

CREATE TABLE IF NOT EXISTS documents (
    id           BIGSERIAL PRIMARY KEY,
    source_path  TEXT UNIQUE NOT NULL,
    filename     TEXT NOT NULL,
    sha256       TEXT NOT NULL,
    product      TEXT,
    version      TEXT,
    doc_type     TEXT,
    title        TEXT,
    page_count   INT,
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chunks (
    id            BIGSERIAL PRIMARY KEY,
    document_id   BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index   INT NOT NULL,
    content       TEXT NOT NULL,
    content_type  TEXT NOT NULL DEFAULT 'text',   -- text | table | figure_caption
    section_path  TEXT,
    page_from     INT,
    page_to       INT,
    product       TEXT,
    version       TEXT,
    doc_type      TEXT,
    image_id      BIGINT,
    token_count   INT,
    embedding     VECTOR(__DIM__),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS images (
    id            BIGSERIAL PRIMARY KEY,
    document_id   BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page          INT,
    image_index   INT,
    sha256        TEXT,
    mime          TEXT NOT NULL DEFAULT 'image/png',
    width         INT,
    height        INT,
    file_path     TEXT NOT NULL,
    caption       TEXT,
    ocr_text      TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Metadata filter indexes (the version-sprawl accuracy lever).
CREATE INDEX IF NOT EXISTS chunks_meta_idx ON chunks (product, version, doc_type);
CREATE INDEX IF NOT EXISTS chunks_doc_idx ON chunks (document_id, chunk_index);
CREATE INDEX IF NOT EXISTS images_doc_idx ON images (document_id);

-- Dense vector index (cosine). Embeddings are L2-normalized at write time.
CREATE INDEX IF NOT EXISTS chunks_hnsw_idx
    ON chunks USING hnsw (embedding vector_cosine_ops);

-- BM25 lexical index over content + metadata (ParadeDB pg_search).
CREATE INDEX IF NOT EXISTS chunks_bm25_idx ON chunks
    USING bm25 (id, content, section_path, product, version, doc_type, content_type)
    WITH (key_field = 'id');
"""


def init_schema() -> None:
    """Create extensions, tables, and indexes. Safe to run repeatedly."""
    s = get_settings()
    ddl = DDL.replace("__DIM__", str(s.embedding_dim))
    # Use a dedicated autocommit connection so CREATE EXTENSION / INDEX run
    # cleanly before the pooled, vector-aware connections are opened.
    with psycopg.connect(s.database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            for statement in _split_sql(ddl):
                cur.execute(statement)
    log.info("Schema ready (embedding_dim=%s)", s.embedding_dim)


def _split_sql(sql: str) -> list[str]:
    return [stmt.strip() for stmt in sql.split(";") if stmt.strip()]


# --------------------------------------------------------------------------
# Small query helpers used by the MCP tools.
# --------------------------------------------------------------------------

def fetch_distinct(column: str, where_product: Optional[str] = None) -> list[str]:
    allowed = {"product", "version", "doc_type"}
    if column not in allowed:
        raise ValueError(f"column must be one of {allowed}")
    sql = f"SELECT DISTINCT {column} FROM documents WHERE {column} IS NOT NULL"
    params: list[Any] = []
    if where_product:
        sql += " AND product = %s"
        params.append(where_product)
    sql += f" ORDER BY {column}"
    with connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return [r[0] for r in cur.fetchall()]


def get_chunk(chunk_id: int) -> Optional[dict[str, Any]]:
    sql = """
        SELECT c.id, c.document_id, c.chunk_index, c.content, c.content_type,
               c.section_path, c.page_from, c.page_to, c.product, c.version,
               c.doc_type, c.image_id, d.filename, d.source_path, d.title
        FROM chunks c JOIN documents d ON d.id = c.document_id
        WHERE c.id = %s
    """
    with connection() as conn, conn.cursor() as cur:
        cur.execute(sql, (chunk_id,))
        row = cur.fetchone()
        return _chunk_row(row, cur) if row else None


def get_context(chunk_id: int, window: int = 1) -> list[dict[str, Any]]:
    """Return the chunk plus `window` neighbours on each side, same document."""
    with connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT document_id, chunk_index FROM chunks WHERE id = %s", (chunk_id,))
        base = cur.fetchone()
        if not base:
            return []
        document_id, idx = base
        cur.execute(
            """
            SELECT c.id, c.document_id, c.chunk_index, c.content, c.content_type,
                   c.section_path, c.page_from, c.page_to, c.product, c.version,
                   c.doc_type, c.image_id, d.filename, d.source_path, d.title
            FROM chunks c JOIN documents d ON d.id = c.document_id
            WHERE c.document_id = %s AND c.chunk_index BETWEEN %s AND %s
            ORDER BY c.chunk_index
            """,
            (document_id, idx - window, idx + window),
        )
        return [_chunk_row(r, cur) for r in cur.fetchall()]


def get_image(image_id: int) -> Optional[dict[str, Any]]:
    sql = """
        SELECT i.id, i.document_id, i.page, i.image_index, i.mime, i.width,
               i.height, i.file_path, i.caption, i.ocr_text,
               d.filename, d.product, d.version, d.doc_type, d.title
        FROM images i JOIN documents d ON d.id = i.document_id
        WHERE i.id = %s
    """
    with connection() as conn, conn.cursor() as cur:
        cur.execute(sql, (image_id,))
        row = cur.fetchone()
        if not row:
            return None
        cols = [c.name for c in cur.description]
        return dict(zip(cols, row))


def _chunk_row(row: Iterable[Any], cur: psycopg.Cursor) -> dict[str, Any]:
    cols = [c.name for c in cur.description]
    return dict(zip(cols, row))


def corpus_stats() -> dict[str, Any]:
    with connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM documents")
        docs = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM chunks")
        chunks = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM images")
        images = cur.fetchone()[0]
    return {"documents": docs, "chunks": chunks, "images": images}
