-- Reference schema (the app also creates this automatically from db.py, using
-- your configured EMBEDDING_DIM). This file assumes the default BGE-M3 dim=1024.
-- Requires the ParadeDB image, which bundles pg_search (BM25) and pgvector.

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
    embedding     VECTOR(1024),
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

CREATE INDEX IF NOT EXISTS chunks_meta_idx ON chunks (product, version, doc_type);
CREATE INDEX IF NOT EXISTS chunks_doc_idx ON chunks (document_id, chunk_index);
CREATE INDEX IF NOT EXISTS images_doc_idx ON images (document_id);

CREATE INDEX IF NOT EXISTS chunks_hnsw_idx
    ON chunks USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS chunks_bm25_idx ON chunks
    USING bm25 (id, content, section_path, product, version, doc_type, content_type)
    WITH (key_field = 'id');
