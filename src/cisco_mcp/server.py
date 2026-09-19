"""The MCP server: tools over the hybrid-search backend.

Transport is streamable HTTP so it works both locally and behind a Cloudflare
Tunnel. Authentication and audit logging are handled by AccessMiddleware
(see auth.py) according to AUTH_MODE.

Tools
-----
search_docs        hybrid search with optional product/version/doc_type filters
list_products      distinct product lines in the corpus
list_versions      distinct versions (optionally for one product)
list_doc_types     distinct document types
get_context        a chunk plus its neighbours (more surrounding text)
get_image          return an extracted figure/diagram/photo (PNG) for output
get_image_info     caption, OCR text, and citation for a figure
corpus_stats       document / chunk / image counts
"""

from __future__ import annotations

import os
import time
from typing import Annotated, Any, Optional

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.utilities.types import Image

from . import db
from .auth import AccessMiddleware, get_identity
from .config import get_settings
from .logging_setup import audit_event, get_logger, setup_logging
from .search import hybrid_search

log = get_logger(__name__)
settings = get_settings()

mcp: FastMCP = FastMCP(
    name=settings.server_name,
    instructions=(
        "Accurate, version-aware search over a corpus of Cisco product "
        "documentation (Secure Email Gateway / ESA and related). Always prefer "
        "filtering by `version` and `doc_type` when the user names one, because "
        "the same guide exists for many software versions. Cite the filename, "
        "version, and page from each result. Figures and diagrams are searchable "
        "and can be fetched with get_image for inclusion in output."
    ),
)


def _audit_tool(tool: str, **fields: Any) -> None:
    audit_event("tool_call", tool=tool, identity=get_identity(), **fields)


@mcp.tool
def search_docs(
    query: Annotated[str, "Natural-language question or keywords."],
    product: Annotated[Optional[str], "Filter to a product line (see list_products)."] = None,
    version: Annotated[Optional[str], "Filter to a version, e.g. '16.5' (also matches 16.5.x)."] = None,
    doc_type: Annotated[Optional[str], "Filter to a doc type (see list_doc_types)."] = None,
    content_type: Annotated[Optional[str], "One of 'text', 'table', 'figure_caption'."] = None,
    top_k: Annotated[int, "Number of results to return."] = 8,
) -> list[dict[str, Any]]:
    """Hybrid (BM25 + vector) search with cross-encoder reranking.

    Returns ranked passages with citations. Results whose `has_image` is true
    carry an `image_id` you can pass to get_image.
    """
    t0 = time.monotonic()
    results = hybrid_search(query, product, version, doc_type, content_type, top_k)
    _audit_tool(
        "search_docs",
        query=query[:200],
        filters={"product": product, "version": version, "doc_type": doc_type,
                 "content_type": content_type},
        results=len(results),
        latency_ms=round((time.monotonic() - t0) * 1000, 1),
    )
    return results


@mcp.tool
def list_products() -> list[str]:
    """List the distinct product lines available in the corpus."""
    _audit_tool("list_products")
    return db.fetch_distinct("product")


@mcp.tool
def list_versions(
    product: Annotated[Optional[str], "Restrict to one product line."] = None,
) -> list[str]:
    """List the distinct document versions available (optionally per product)."""
    _audit_tool("list_versions", product=product)
    return db.fetch_distinct("version", where_product=product)


@mcp.tool
def list_doc_types() -> list[str]:
    """List the distinct document types (Admin Guide, CLI Reference, etc.)."""
    _audit_tool("list_doc_types")
    return db.fetch_distinct("doc_type")


@mcp.tool
def get_context(
    chunk_id: Annotated[int, "A chunk_id from a search result."],
    window: Annotated[int, "How many neighbouring chunks each side."] = 1,
) -> list[dict[str, Any]]:
    """Return a chunk plus its neighbours for more surrounding context."""
    _audit_tool("get_context", chunk_id=chunk_id, window=window)
    return db.get_context(chunk_id, window=window)


@mcp.tool
def get_image_info(
    image_id: Annotated[int, "An image_id from a search result."],
) -> dict[str, Any]:
    """Return a figure's caption, OCR/transcription, and source citation."""
    info = db.get_image(image_id)
    if not info:
        raise ToolError(f"No image with id {image_id}")
    _audit_tool("get_image_info", image_id=image_id)
    info.pop("file_path", None)
    return info


@mcp.tool
def get_image(
    image_id: Annotated[int, "An image_id from a search result."],
) -> Image:
    """Return the extracted figure/diagram/photo (PNG) so it can be shown or reused."""
    info = db.get_image(image_id)
    if not info:
        raise ToolError(f"No image with id {image_id}")
    _audit_tool("get_image", image_id=image_id, page=info.get("page"),
                document_id=info.get("document_id"))
    # file_path is stored relative to IMAGE_DIR (absolute paths still resolve).
    return Image(path=os.path.join(settings.image_dir, info["file_path"]))


@mcp.tool
def corpus_stats() -> dict[str, Any]:
    """Return document, chunk, and image counts for the indexed corpus."""
    _audit_tool("corpus_stats")
    return db.corpus_stats()


# --------------------------------------------------------------------------
# App wiring
# --------------------------------------------------------------------------
def _wait_for_db(retries: int = 30, delay: float = 2.0) -> None:
    last = None
    for _ in range(retries):
        try:
            db.init_schema()
            return
        except Exception as exc:  # noqa: BLE001
            last = exc
            log.info("Waiting for database... (%s)", exc)
            time.sleep(delay)
    raise RuntimeError(f"Database not ready after {retries} tries: {last}")


def build_app():
    _wait_for_db()
    app = mcp.http_app(path=settings.mcp_path)
    app.add_middleware(AccessMiddleware)
    return app


def main() -> None:
    setup_logging()
    s = get_settings()
    log.info("Starting MCP server '%s' on %s:%s%s (auth=%s)",
             s.server_name, s.host, s.port, s.mcp_path, s.auth_mode)
    app = build_app()
    import uvicorn

    uvicorn.run(app, host=s.host, port=s.port, log_level=s.log_level.lower())


if __name__ == "__main__":
    main()
