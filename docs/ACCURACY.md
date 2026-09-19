# Accuracy

The goal is the most accurate answer this corpus can give while staying free and
self-hosted. Five techniques stack to get there, and each one is a knob you can
turn.

## 1. Version-aware metadata filtering

The corpus repeats the same guide across many AsyncOS versions, so returning the
right edition matters as much as returning the right passage. Every chunk carries
`product`, `version`, and `doc_type`, derived at ingest time (see
[INGESTION.md](INGESTION.md)). The `search_docs` tool takes those as filters, and
`list_versions` / `list_doc_types` let the calling model discover valid values
first. When a user names a version, filtering to it removes the single largest
source of wrong-but-plausible answers.

A `version` filter of `16.5` also matches `16.5.1`, `16.5.2`, and so on.

## 2. Hybrid retrieval (BM25 plus dense vectors)

Two retrievers run per query.

- **BM25** (`pg_search`) nails exact strings that embeddings blur, such as CLI
  commands, error codes, header names, and part numbers.
- **Dense vectors** (`pgvector`, BGE-M3) catch paraphrases and conceptual
  matches where the wording differs from the manual.

Their ranked lists are merged with Reciprocal Rank Fusion (`RRF_K` default 60).
Tune `RETRIEVE_BM25_K`, `RETRIEVE_VECTOR_K`, and the `RRF_TEXT_WEIGHT` /
`RRF_VECTOR_WEIGHT` weights if one signal should lead.

## 3. Cross-encoder reranking

The fused candidates (`RERANK_CANDIDATES`, default 30) are re-scored by a cross
encoder (`bge-reranker-v2-m3`) that reads the query and passage together. This is
the biggest precision gain in the pipeline. It reorders the shortlist and the top
`RESULT_TOP_K` are returned. Turn it off with `RERANK_ENABLED=false` if you need
lower latency and can accept fewer correct top hits.

## 4. Structure-aware chunking

Docling's HybridChunker splits on document structure, so tables and sections stay
intact instead of being cut mid-row. Tables are tagged `content_type='table'` and
section headings travel with each chunk, which both improves retrieval and gives
better citations. Adjust `CHUNK_MAX_TOKENS` to trade granularity against context.

## 5. Figures become searchable text

Each extracted figure produces a `figure_caption` chunk built from the native PDF
caption plus a vision-model description and a verbatim transcription of visible
text (labels, CLI, port and LED markings). That makes diagrams and screenshots
findable, and the original PNG is kept so `get_image` can return it.

## Turning accuracy up on a GPU

Ingestion is where the heavy models run, so point it at a GPU box (for example a
DGX) for a max-accuracy index build, then serve from the result anywhere.

| Component | Free CPU default | Higher-accuracy option |
|---|---|---|
| Embeddings | `BAAI/bge-m3` (1024-dim) | `Qwen/Qwen3-Embedding-4B` or larger (set `EMBEDDING_DIM` to match, then re-ingest) |
| Reranker | `BAAI/bge-reranker-v2-m3` | `BAAI/bge-reranker-v2-gemma` or a Qwen3 reranker |
| Figure captions | `qwen2.5vl:7b` via Ollama | a larger vision model on the GPU host |

Set `EMBEDDING_DEVICE=cuda` and `RERANK_DEVICE=cuda` when a GPU is present.
Changing the embedding model usually changes the vector dimension, so update
`EMBEDDING_DIM` and re-ingest (the schema is rebuilt from that value).

## Measuring it

Do not guess at accuracy. Build a small set of real questions with known correct
pages, run them through `search_docs`, and track how often the correct passage is
in the top result and the top five. Re-run it after any model or tuning change.
An evaluation harness is on the roadmap.
