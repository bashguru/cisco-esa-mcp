"""Ingestion pipeline: PDFs (and text/markdown) -> ParadeDB.

For each source file:
  1. sha256 -> skip if unchanged (incremental re-runs are cheap).
  2. Classify product / version / doc type (see classify.py).
  3. Parse with Docling, keeping tables and figure images.
  4. Chunk text/tables structure-aware (Docling HybridChunker).
  5. For each figure: save the PNG, caption + OCR it with a vision model, and
     add a searchable "figure_caption" chunk linked to the stored image.
  6. Embed every chunk and upsert documents + chunks + images.

Run it with `python -m cisco_mcp.ingest /data/input` or via scripts/ingest.py.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import classify as clf
from .config import get_settings
from .db import connection, init_schema
from .logging_setup import audit_event, get_logger
from .models import captioner, embedder

log = get_logger(__name__)

PDF_EXTS = {".pdf"}
TEXT_EXTS = {".md", ".markdown", ".txt"}


@dataclass
class PendingChunk:
    content: str
    content_type: str = "text"
    section_path: Optional[str] = None
    page_from: Optional[int] = None
    page_to: Optional[int] = None
    image_id: Optional[int] = None


@dataclass
class PendingImage:
    page: Optional[int]
    image_index: int
    png_bytes: bytes
    width: Optional[int] = None
    height: Optional[int] = None
    native_caption: str = ""
    vlm_caption: Optional[str] = None
    file_path: str = ""
    sha256: str = ""


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _document_needs_ingest(cur, source_path: str, sha: str) -> Optional[int]:
    """Return existing document_id to delete (re-ingest), or None if unchanged.

    Raises _Unchanged sentinel via return value semantics: caller checks.
    """
    cur.execute("SELECT id, sha256 FROM documents WHERE source_path = %s", (source_path,))
    row = cur.fetchone()
    if row is None:
        return None
    doc_id, existing_sha = row
    if existing_sha == sha:
        return -1  # sentinel: unchanged, skip
    return doc_id  # changed: delete this id then re-insert


# --------------------------------------------------------------------------
# Docling parsing
# --------------------------------------------------------------------------
_converter = None


def _get_converter():
    global _converter
    if _converter is None:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        s = get_settings()
        opts = PdfPipelineOptions()
        opts.images_scale = s.images_scale
        opts.generate_picture_images = True
        opts.do_ocr = s.enable_ocr
        opts.do_table_structure = True
        _converter = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
        )
    return _converter


def _get_chunker():
    from docling.chunking import HybridChunker

    s = get_settings()
    try:
        return HybridChunker(max_tokens=s.chunk_max_tokens, merge_peers=True)
    except TypeError:
        return HybridChunker()


def _page_range(doc_items) -> tuple[Optional[int], Optional[int]]:
    pages: list[int] = []
    for it in doc_items or []:
        for prov in getattr(it, "prov", None) or []:
            if getattr(prov, "page_no", None) is not None:
                pages.append(prov.page_no)
    if not pages:
        return None, None
    return min(pages), max(pages)


def parse_pdf(path: str, meta: dict) -> tuple[list[PendingChunk], list[PendingImage], int]:
    from docling_core.types.doc import PictureItem, TableItem

    converter = _get_converter()
    chunker = _get_chunker()
    result = converter.convert(path)
    doc = result.document

    chunks: list[PendingChunk] = []
    for ch in chunker.chunk(doc):
        text = (chunker.contextualize(ch) or ch.text or "").strip()
        if not text:
            continue
        doc_items = getattr(ch.meta, "doc_items", None)
        headings = getattr(ch.meta, "headings", None) or []
        is_table = any(isinstance(it, TableItem) for it in (doc_items or []))
        pf, pt = _page_range(doc_items)
        chunks.append(
            PendingChunk(
                content=text,
                content_type="table" if is_table else "text",
                section_path=" > ".join(h for h in headings if h) or None,
                page_from=pf,
                page_to=pt,
            )
        )

    images: list[PendingImage] = []
    idx = 0
    for element, _level in doc.iterate_items():
        if not isinstance(element, PictureItem):
            continue
        pil = element.get_image(doc)
        if pil is None:
            continue
        idx += 1
        buf = io.BytesIO()
        pil.save(buf, "PNG")
        png = buf.getvalue()
        page = None
        prov = getattr(element, "prov", None) or []
        if prov and getattr(prov[0], "page_no", None) is not None:
            page = prov[0].page_no
        native_caption = ""
        try:
            native_caption = (element.caption_text(doc) or "").strip()
        except Exception:  # noqa: BLE001
            native_caption = ""
        images.append(
            PendingImage(
                page=page,
                image_index=idx,
                png_bytes=png,
                width=pil.width,
                height=pil.height,
                native_caption=native_caption,
                sha256=hashlib.sha256(png).hexdigest(),
            )
        )

    page_count = len(getattr(doc, "pages", {}) or {}) or 0
    return chunks, images, page_count


def parse_text(path: str) -> tuple[list[PendingChunk], list[PendingImage], int]:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    # Simple paragraph-pack splitter for plain text / markdown.
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks, buf, size = [], [], 0
    for p in paras:
        buf.append(p)
        size += len(p)
        if size > 1500:
            chunks.append(PendingChunk(content="\n\n".join(buf)))
            buf, size = [], 0
    if buf:
        chunks.append(PendingChunk(content="\n\n".join(buf)))
    return chunks, [], 0


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------
def _save_image_file(img: PendingImage, doc_sha: str) -> str:
    s = get_settings()
    out_dir = os.path.join(s.image_dir, doc_sha[:16])
    os.makedirs(out_dir, exist_ok=True)
    fname = f"p{img.page or 0:04d}_{img.image_index:03d}.png"
    fpath = os.path.join(out_dir, fname)
    with open(fpath, "wb") as fh:
        fh.write(img.png_bytes)
    return fpath


def _figure_content(img: PendingImage, meta: dict, vlm_caption: Optional[str]) -> str:
    parts = []
    label = f"Figure on page {img.page}" if img.page else "Figure"
    parts.append(f"{label} of {meta.get('title')}")
    if img.native_caption:
        parts.append(img.native_caption)
    if vlm_caption:
        parts.append(vlm_caption)
    return "\n".join(parts).strip()


def ingest_file(path: str) -> str:
    """Ingest one file. Returns one of: 'indexed', 'skipped', 'error'."""
    source_path = os.path.abspath(path)
    ext = os.path.splitext(source_path)[1].lower()
    filename = os.path.basename(source_path)
    sha = sha256_file(source_path)

    try:
        with connection() as conn:
            with conn.cursor() as cur:
                existing = _document_needs_ingest(cur, source_path, sha)
                if existing == -1:
                    log.info("Unchanged, skipping: %s", filename)
                    return "skipped"

            # Parse outside the DB transaction (parsing is the slow part).
            if ext in PDF_EXTS:
                meta = clf.classify(filename)
                chunks, images, page_count = parse_pdf(source_path, meta)
            elif ext in TEXT_EXTS:
                meta = clf.classify(filename)
                chunks, images, page_count = parse_text(source_path)
            else:
                log.warning("Unsupported file type, skipping: %s", filename)
                return "skipped"

            if not chunks and not images:
                log.warning("Nothing extracted from %s", filename)
                return "skipped"

            # --- Slow work FIRST, outside any DB transaction ---------------
            # Save + caption images, build figure chunks, then embed everything.
            figure_chunks: list[PendingChunk] = []
            for img in images:
                img.file_path = _save_image_file(img, sha)
                img.vlm_caption = captioner.caption(img.png_bytes)
                figure_chunks.append(
                    PendingChunk(
                        content=_figure_content(img, meta, img.vlm_caption),
                        content_type="figure_caption",
                        page_from=img.page,
                        page_to=img.page,
                    )
                )
            all_chunks = chunks + figure_chunks
            vectors = embedder.encode([c.content for c in all_chunks])

            # --- Fast inserts, inside a short transaction ------------------
            with conn.cursor() as cur:
                if existing and existing != -1:
                    cur.execute("DELETE FROM documents WHERE id = %s", (existing,))

                cur.execute(
                    """INSERT INTO documents
                       (source_path, filename, sha256, product, version, doc_type, title, page_count)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (source_path, filename, sha, meta["product"], meta["version"],
                     meta["doc_type"], meta["title"], page_count),
                )
                document_id = cur.fetchone()[0]

                # Insert image rows in order, then link them to figure chunks.
                for img, fchunk in zip(images, figure_chunks):
                    cur.execute(
                        """INSERT INTO images
                           (document_id, page, image_index, sha256, mime, width, height,
                            file_path, caption, ocr_text)
                           VALUES (%s,%s,%s,%s,'image/png',%s,%s,%s,%s,%s) RETURNING id""",
                        (document_id, img.page, img.image_index, img.sha256, img.width,
                         img.height, img.file_path, img.native_caption or None, img.vlm_caption),
                    )
                    fchunk.image_id = cur.fetchone()[0]

                for i, (c, vec) in enumerate(zip(all_chunks, vectors)):
                    cur.execute(
                        """INSERT INTO chunks
                           (document_id, chunk_index, content, content_type, section_path,
                            page_from, page_to, product, version, doc_type, image_id,
                            token_count, embedding)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (document_id, i, c.content, c.content_type, c.section_path,
                         c.page_from, c.page_to, meta["product"], meta["version"],
                         meta["doc_type"], c.image_id, len(c.content.split()), vec),
                    )
            conn.commit()
            chunks = all_chunks  # for the log line below
        log.info("Indexed %s (%s chunks, %s images, v=%s, type=%s)",
                 filename, len(chunks), len(images), meta["version"], meta["doc_type"])
        return "indexed"
    except Exception as exc:  # noqa: BLE001 - keep going through the corpus
        log.exception("Failed to ingest %s: %s", filename, exc)
        return "error"


def ingest_dir(directory: str) -> dict[str, int]:
    counts = {"indexed": 0, "skipped": 0, "error": 0}
    files = sorted(
        p for p in Path(directory).rglob("*")
        if p.is_file() and p.suffix.lower() in (PDF_EXTS | TEXT_EXTS)
    )
    log.info("Found %d candidate files under %s", len(files), directory)
    for p in files:
        counts[ingest_file(str(p))] += 1
    audit_event("ingest_complete", directory=directory, **counts)
    log.info("Ingest complete: %s", counts)
    return counts


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Ingest documents into the Cisco docs MCP backend.")
    ap.add_argument("path", nargs="?", default=get_settings().input_dir,
                    help="File or directory to ingest (default: $INPUT_DIR).")
    args = ap.parse_args(argv)
    init_schema()
    target = args.path
    if os.path.isdir(target):
        ingest_dir(target)
    elif os.path.isfile(target):
        print(ingest_file(target))
    else:
        print(f"No such path: {target}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
