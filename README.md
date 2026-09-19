# Docs MCP — hybrid search over product PDFs

A local, free, Docker-deployable **MCP server** that turns a folder of product
PDFs into accurate, version-aware answers for any MCP client. It reads text,
tables, and figures, keeps the figures so it can hand them back, and runs either
privately on your machine or securely over the internet through a Cloudflare
Tunnel with per-user access tokens.

It ships tuned for Cisco Secure Email / Content Security documentation, but it is
a general pipeline. Point it at your own PDFs and edit one rules file.

Everything in the stack is free and self-hostable. No API keys, no paid services.

---

## What it does

- **Hybrid search for real accuracy.** Combines BM25 keyword search and dense
  vector search in one Postgres backend (ParadeDB), fuses the results, then
  reranks them with a cross-encoder. Exact strings like CLI commands and error
  codes land, and so do paraphrased questions.
- **Version-aware.** Every chunk is tagged with product, version, and doc type,
  so you can ask for "DKIM setup in AsyncOS 16.5" and get that edition, not a
  mix of twelve versions.
- **Keeps the pictures.** Diagrams, screenshots, rack and port photos, and LED
  tables are extracted, described and transcribed by a vision model, made
  searchable, and stored so the server can return the original image.
- **Two ways to run, one image.** Open on localhost for private use, or behind a
  Cloudflare Tunnel with Cloudflare Access service tokens for remote use.
- **Per-user access and full audit logging** in remote mode. Add a user, issue a
  token, log every call. Local mode needs no token.

## How it works

```
Ingest:  PDFs ─▶ Docling parse ─▶ chunks + tables + figure captions ─▶ embed ─▶ ParadeDB
Serve:   question ─▶ BM25 + vector ─▶ RRF fuse ─▶ cross-encoder rerank ─▶ answers + citations
                                                                      └▶ get_image returns the figure
```

Ingestion populates the backend (run it on a strong machine for a top-accuracy
index if you like). The MCP server is a thin, portable query layer over that
backend. Full design is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Requirements

- Docker and Docker Compose.
- About 8 GB of RAM for the default CPU models, and a few GB of disk for the
  models and index.
- Optional, for richer figure captions, an [Ollama](https://ollama.com) instance
  with a vision model (for example `ollama pull qwen2.5vl:7b`).

## Quick start (local, private)

```bash
git clone <your-repo-url> docs-mcp && cd docs-mcp
cp .env.example .env          # then edit POSTGRES_PASSWORD at least

# Put your PDFs somewhere and point INPUT_DIR at them in .env
mkdir -p docs_input           # or set INPUT_DIR=/path/to/your/pdfs

docker compose up -d --build  # starts ParadeDB + the MCP server (localhost only)
docker compose run --rm mcp python scripts/ingest.py   # build the index
docker compose run --rm mcp python -c "from cisco_mcp.db import corpus_stats; print(corpus_stats())"
```

The server is now at `http://127.0.0.1:8000/mcp` with no authentication, bound to
localhost. First ingest downloads the embedding, reranker, and parsing models
into a cached volume, so it takes a while. Later runs are fast and only process
new or changed files.

## Ingest your documents

```bash
docker compose run --rm mcp python scripts/ingest.py            # whole INPUT_DIR
docker compose run --rm mcp python scripts/ingest.py /data/input/one-file.pdf
```

Re-run any time. Unchanged files are skipped by content hash. Details and how to
adapt the classifier to your own filenames are in
[docs/INGESTION.md](docs/INGESTION.md).

## Connect an MCP client

Any client that speaks streamable HTTP works. Locally there are no headers.

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "docs": { "command": "npx", "args": ["mcp-remote", "http://127.0.0.1:8000/mcp"] }
  }
}
```

Claude Code:

```bash
claude mcp add --transport http docs http://127.0.0.1:8000/mcp
```

Then ask things like "list the doc versions you have", "how do I rotate a DKIM
key on ESA in 16.5", or "show me the C695 port diagram".

## Remote access (Cloudflare Tunnel + service tokens)

For access from anywhere, without opening a port, and with a token per user:

```bash
# In .env: AUTH_MODE=cloudflare, the CF_ACCESS_* values, and TUNNEL_TOKEN
docker compose --profile tunnel up -d --build
```

The full step-by-step (create the tunnel, the Access application, the Service
Auth policy, and the tokens) is in [docs/CLOUDFLARE.md](docs/CLOUDFLARE.md).

### Add a user, issue a token

```bash
# You created the token in the Cloudflare dashboard:
make add-user NAME="Alice" CLIENT_ID="<client-id>.access"

# Or let the helper create it via the Cloudflare API (needs CF_API_TOKEN + CF_ACCOUNT_ID):
make add-user NAME="Alice" CREATE=1

make list-users
```

Each user connects with two headers, `CF-Access-Client-Id` and
`CF-Access-Client-Secret`. Local mode needs none of this.

### Logging

Every remote request and tool call is written as a JSON line to
`data/logs/audit.log` and to the container log, with the user identity, tool,
filters, result count, source IP, status, and latency. Cloudflare Access keeps
its own log of every authentication as well.

## MCP tools

| Tool | What it does |
|---|---|
| `search_docs` | Hybrid search with optional `product`, `version`, `doc_type`, `content_type` filters. |
| `list_products` / `list_versions` / `list_doc_types` | Discover valid filter values. |
| `get_context` | A result chunk plus its neighbours for more context. |
| `get_image` | Return an extracted figure (PNG) for display or reuse. |
| `get_image_info` | A figure's caption, transcription, and citation. |
| `corpus_stats` | Document, chunk, and image counts. |

## Configuration

Everything is set in `.env` (see `.env.example` for the full list with comments).
The most useful knobs:

| Variable | Default | Purpose |
|---|---|---|
| `INPUT_DIR` | `./docs_input` | Folder of documents to ingest. |
| `AUTH_MODE` | `local` | `local` (no auth) or `cloudflare` (service-token auth + logging). |
| `EMBEDDING_MODEL` / `EMBEDDING_DIM` | `BAAI/bge-m3` / `1024` | Dense embedder. Change both together, then re-ingest. |
| `RERANK_ENABLED` / `RERANK_MODEL` | `true` / `bge-reranker-v2-m3` | Cross-encoder reranking. |
| `ENABLE_VLM_CAPTIONS` / `VLM_MODEL` | `true` / `qwen2.5vl:7b` | Figure captioning via Ollama. |
| `EMBEDDING_DEVICE` / `RERANK_DEVICE` | `cpu` | Set to `cuda` on a GPU box. |
| `RESULT_TOP_K`, `RRF_K`, `RERANK_CANDIDATES` | `8`, `60`, `30` | Retrieval tuning. |

Accuracy strategy and how to push it higher on a GPU are in
[docs/ACCURACY.md](docs/ACCURACY.md).

## Project layout

```
docker-compose.yml     db + mcp, plus an optional cloudflared tunnel profile
Dockerfile             the MCP/ingestion image (CPU by default)
.env.example           all configuration
sql/001_init.sql       reference schema (the app also builds it automatically)
config/users.example.yaml   remote-user allowlist template
src/cisco_mcp/
  config.py            environment-driven settings
  db.py                Postgres/ParadeDB access + schema
  classify.py          product / version / doc-type detection
  models.py            embedder, reranker, vision captioner
  ingest.py            parse -> chunk -> caption -> embed -> store
  search.py            BM25 + vector -> RRF -> rerank
  auth.py              Cloudflare Access verification + audit middleware
  server.py            the FastMCP server and tools
scripts/               ingest, add_user, list_users, healthcheck
docs/                  ARCHITECTURE, ACCURACY, INGESTION, CLOUDFLARE
```

## Adapting to your own documents

This is not Cisco-specific under the hood. Edit `PRODUCT_RULES`, `DOCTYPE_RULES`,
and the version regex in `src/cisco_mcp/classify.py` to match your filenames, and
ingest your folder. Parsing, chunking, figures, and retrieval are all
corpus-agnostic.

## License and credits

This project's code is MIT (see [LICENSE](LICENSE)). It stands on excellent free
software listed in [NOTICE.md](NOTICE.md), including ParadeDB, pgvector, Docling,
FastMCP, Sentence-Transformers, and the BGE models. Please keep their notices.

Cisco, AsyncOS, and IronPort are trademarks of Cisco Systems, Inc. This project
is not affiliated with or endorsed by Cisco. You are responsible for your rights
to any documents you ingest.
