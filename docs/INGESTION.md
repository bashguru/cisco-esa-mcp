# Ingestion

Ingestion turns a folder of files into the searchable backend. Run it once to
build the index, and re-run it any time to pick up new or changed files.

```bash
# Ingest the folder mounted at /data/input (set INPUT_DIR in .env)
docker compose run --rm mcp python scripts/ingest.py

# Or a single file / a different path
docker compose run --rm mcp python scripts/ingest.py /data/input/some-guide.pdf
```

## What happens per file

1. **Fingerprint.** A sha256 of the file is compared to what is already indexed.
   Unchanged files are skipped, so re-runs are cheap. A changed file is
   re-ingested cleanly (its old rows are removed first).
2. **Classify.** Product, version, and doc type are derived from the filename,
   with the version confirmed from the first page of text when needed. See
   `src/cisco_mcp/classify.py`. The rules ship tuned for Cisco Secure Email
   filenames and are a plain table you can edit for another corpus.
3. **Parse.** Docling reads the PDF layout, keeping tables and extracting figure
   images with the page each came from.
4. **Chunk.** Structure-aware chunks are produced for text and tables, each with
   a section path and page range.
5. **Figures.** Each image is saved as a PNG, described and transcribed by the
   vision model, and turned into a `figure_caption` chunk linked to the stored
   image.
6. **Embed and store.** Every chunk is embedded and written to ParadeDB along
   with the document and image rows.

Slow work (vision captioning and embedding) happens before the database
transaction opens, so writes are short and the database connection is not held
open across model or network calls.

## Supported inputs

PDF is the primary format. Plain text and Markdown (`.md`, `.txt`) are also
ingested as text (no figures). Other types are skipped.

## Figure captioning

Captioning uses a local vision model through Ollama by default
(`VLM_MODEL`, `OLLAMA_BASE_URL`). It is optional. With `ENABLE_VLM_CAPTIONS=false`
the pipeline still runs and keeps any native PDF caption, you just lose the
richer description and in-image text. On Linux, the compose file maps
`host.docker.internal` so the container can reach an Ollama running on the host.
Point `OLLAMA_BASE_URL` at another machine (for example a homelab GPU host) to
use a bigger model.

## Re-ingesting and updating

Run the same command again. Only changed or new files are processed. To force a
full rebuild, drop the database volume (`docker compose down -v`) and ingest
again. Remember that changing the embedding model changes the vector dimension,
so set `EMBEDDING_DIM` to match and rebuild.

## Adapting to another corpus

This is a general pipeline, not a Cisco-only one. To point it at different
documents, edit `PRODUCT_RULES` and `DOCTYPE_RULES` in `classify.py` and the
version regex if your naming differs. Everything else (parsing, chunking, figure
handling, retrieval) is corpus-agnostic.
