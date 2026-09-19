"""Model wrappers: dense embedder, cross-encoder reranker, vision captioner.

All are lazily loaded singletons so importing this module is cheap and the MCP
server only pays for what it uses. Everything defaults to free, self-hostable
models. Point the env vars at bigger models on a GPU box for more accuracy; the
interfaces do not change.
"""

from __future__ import annotations

import base64
import threading
from typing import Any, Optional, Sequence

from .config import get_settings
from .logging_setup import get_logger

log = get_logger(__name__)

_CAPTION_PROMPT = (
    "You are indexing a figure from a Cisco technical manual so it can be found "
    "by search. In 2-5 sentences, state exactly what the image shows (network "
    "diagram, GUI screenshot, CLI output, rack/port photo, table, flowchart) and "
    "transcribe ALL visible text verbatim: labels, field names, menu paths, CLI "
    "commands, values, and port or LED markings. Be factual. Do not guess or add "
    "anything that is not visible."
)


# --------------------------------------------------------------------------
# Dense embeddings
# --------------------------------------------------------------------------
class _Embedder:
    def __init__(self) -> None:
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from sentence_transformers import SentenceTransformer

                    s = get_settings()
                    log.info("Loading embedding model %s on %s", s.embedding_model, s.embedding_device)
                    self._model = SentenceTransformer(s.embedding_model, device=s.embedding_device)
        return self._model

    def encode(self, texts: Sequence[str], is_query: bool = False) -> Any:
        """Return an (n, dim) float32 numpy array.

        numpy arrays (not plain lists) are what the pgvector psycopg adapter
        expects as query/insert parameters, so we keep them as arrays.
        """
        s = get_settings()
        model = self._load()
        return model.encode(
            list(texts),
            batch_size=s.embedding_batch,
            normalize_embeddings=True,   # cosine distance via pgvector <=>
            convert_to_numpy=True,
            show_progress_bar=False,
        )

    def encode_one(self, text: str, is_query: bool = False) -> Any:
        return self.encode([text], is_query=is_query)[0]


# --------------------------------------------------------------------------
# Cross-encoder reranker
# --------------------------------------------------------------------------
class _Reranker:
    def __init__(self) -> None:
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from sentence_transformers import CrossEncoder

                    s = get_settings()
                    log.info("Loading reranker %s on %s", s.rerank_model, s.rerank_device)
                    self._model = CrossEncoder(s.rerank_model, device=s.rerank_device)
        return self._model

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        if not passages:
            return []
        model = self._load()
        pairs = [[query, p] for p in passages]
        scores = model.predict(pairs, show_progress_bar=False)
        return [float(x) for x in scores]


# --------------------------------------------------------------------------
# Vision captioner (Ollama by default; degrades gracefully to no caption)
# --------------------------------------------------------------------------
class _Captioner:
    def caption(self, png_bytes: bytes) -> Optional[str]:
        s = get_settings()
        if not s.enable_vlm_captions:
            return None
        if s.vlm_backend != "ollama":
            log.warning("Unknown VLM backend %s; skipping caption", s.vlm_backend)
            return None
        try:
            import httpx

            b64 = base64.b64encode(png_bytes).decode("ascii")
            resp = httpx.post(
                f"{s.ollama_base_url.rstrip('/')}/api/generate",
                json={
                    "model": s.vlm_model,
                    "prompt": _CAPTION_PROMPT,
                    "images": [b64],
                    "stream": False,
                    "options": {"temperature": 0.0},
                },
                timeout=s.vlm_timeout,
            )
            resp.raise_for_status()
            text = (resp.json().get("response") or "").strip()
            return text or None
        except Exception as exc:  # noqa: BLE001 - captioning is best-effort
            log.warning("VLM caption failed (%s); continuing without it", exc)
            return None


embedder = _Embedder()
reranker = _Reranker()
captioner = _Captioner()
