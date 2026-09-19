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

import numpy as np

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
        if s.embedding_backend == "openai":
            return self._encode_openai(list(texts))
        model = self._load()
        return model.encode(
            list(texts),
            batch_size=s.embedding_batch,
            normalize_embeddings=True,   # cosine distance via pgvector <=>
            convert_to_numpy=True,
            show_progress_bar=False,
        )

    def _encode_openai(self, texts: list[str]) -> Any:
        """Embed via an OpenAI-compatible endpoint (LM Studio / MLX, vLLM, ...).

        Vectors are Matryoshka-truncated to EMBEDDING_DIM (so a 7168-dim model
        like Qwen3-Embedding fits a Postgres HNSW index) and L2-normalized for
        cosine distance.
        """
        import httpx

        s = get_settings()
        base = s.api_base.rstrip("/")
        headers = {"Authorization": f"Bearer {s.api_key}"} if s.api_key else {}
        vectors: list[list[float]] = []
        for i in range(0, len(texts), s.embedding_batch):
            batch = texts[i : i + s.embedding_batch]
            resp = httpx.post(
                f"{base}/embeddings",
                json={"model": s.embed_api_model, "input": batch},
                headers=headers,
                timeout=180,
            )
            resp.raise_for_status()
            data = sorted(resp.json()["data"], key=lambda d: d.get("index", 0))
            vectors.extend(d["embedding"] for d in data)
        arr = np.asarray(vectors, dtype=np.float32)
        if arr.ndim == 2 and arr.shape[1] > s.embedding_dim:
            arr = arr[:, : s.embedding_dim]          # MRL truncation
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        return arr / np.clip(norms, 1e-12, None)

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
                    if s.torch_threads > 0:
                        try:
                            import torch

                            torch.set_num_threads(s.torch_threads)
                        except Exception:  # noqa: BLE001
                            pass
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
        if s.vlm_backend == "openai":
            return self._caption_openai(png_bytes, s)
        if s.vlm_backend == "ollama":
            return self._caption_ollama(png_bytes, s)
        log.warning("Unknown VLM backend %s; skipping caption", s.vlm_backend)
        return None

    def _caption_ollama(self, png_bytes: bytes, s) -> Optional[str]:
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
            return (resp.json().get("response") or "").strip() or None
        except Exception as exc:  # noqa: BLE001 - captioning is best-effort
            log.warning("VLM caption failed (%s); continuing without it", exc)
            return None

    def _caption_openai(self, png_bytes: bytes, s) -> Optional[str]:
        """Caption via an OpenAI-compatible vision chat endpoint (LM Studio / MLX)."""
        try:
            import httpx

            b64 = base64.b64encode(png_bytes).decode("ascii")
            headers = {"Authorization": f"Bearer {s.api_key}"} if s.api_key else {}
            resp = httpx.post(
                f"{s.api_base.rstrip('/')}/chat/completions",
                json={
                    "model": s.vlm_model,
                    "temperature": 0.0,
                    "max_tokens": 512,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": _CAPTION_PROMPT},
                                {"type": "image_url",
                                 "image_url": {"url": f"data:image/png;base64,{b64}"}},
                            ],
                        }
                    ],
                },
                headers=headers,
                timeout=s.vlm_timeout,
            )
            resp.raise_for_status()
            return (resp.json()["choices"][0]["message"]["content"] or "").strip() or None
        except Exception as exc:  # noqa: BLE001 - captioning is best-effort
            log.warning("VLM (openai) caption failed (%s); continuing without it", exc)
            return None


embedder = _Embedder()
reranker = _Reranker()
captioner = _Captioner()
