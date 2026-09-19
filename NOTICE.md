# Notices and credits

This project's own code is MIT licensed (see LICENSE). It depends on the
following free software, each under its own license. Keep these notices when you
redistribute.

| Component | Role | License |
|---|---|---|
| PostgreSQL | Database engine | PostgreSQL License |
| ParadeDB `pg_search` | BM25 full-text search in Postgres | AGPL-3.0 |
| pgvector | Vector search in Postgres | PostgreSQL License |
| Docling | Layout-aware PDF parsing, figures, tables | MIT |
| FastMCP | MCP server framework | Apache-2.0 |
| Sentence-Transformers | Embedding + cross-encoder runtime | Apache-2.0 |
| BAAI BGE-M3 | Default embedding model | MIT |
| BAAI bge-reranker-v2-m3 | Default reranker model | MIT |
| PyTorch | Model runtime | BSD-3-Clause |
| Uvicorn / Starlette | ASGI server and toolkit | BSD-3-Clause |
| PyJWT | Access JWT verification | MIT |
| cloudflared | Cloudflare Tunnel connector | Apache-2.0 |
| Ollama (optional) | Local vision-model runtime | MIT |
| Qwen2.5-VL 7B (optional, default VLM) | Figure captioning | Apache-2.0 |

## A note on the ParadeDB (AGPL) dependency

`pg_search` is AGPL-3.0. This project runs the official, unmodified ParadeDB
container as a separate networked service and does not distribute a modified
version of it, so it can be used freely in a self-hosted deployment and does not
change the MIT license of this project's own code. If you fork and modify
ParadeDB itself and offer it to others over a network, the AGPL's terms apply to
that modified database. This paragraph is a summary, not legal advice.

## Trademarks

Cisco, AsyncOS, and IronPort are trademarks of Cisco Systems, Inc. This project
is independent and not affiliated with or endorsed by Cisco. You are responsible
for your rights to any documents you ingest and index.
