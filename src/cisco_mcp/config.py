"""Central, environment-driven configuration.

Every knob is an environment variable so the same image runs three ways with no
code changes:

* Local, open, on a laptop (defaults below).
* Remote, behind a Cloudflare Tunnel, with per-user service-token auth.
* Accelerated ingestion on a GPU box (point the model vars at bigger models).

Nothing here imports heavy libraries, so it is safe to import anywhere.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


def _b(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _s(name: str, default: str) -> str:
    v = os.getenv(name)
    return default if v is None or v == "" else v


@dataclass(frozen=True)
class Settings:
    # ---- Database -------------------------------------------------------
    database_url: str = field(
        default_factory=lambda: _s(
            "DATABASE_URL",
            "postgresql://cisco:cisco@db:5432/ciscodocs",
        )
    )
    pool_min: int = field(default_factory=lambda: _i("DB_POOL_MIN", 1))
    pool_max: int = field(default_factory=lambda: _i("DB_POOL_MAX", 8))

    # ---- Embeddings -----------------------------------------------------
    # BGE-M3 is the default: 1024-dim dense vectors, strong accuracy, runs on
    # CPU for a bounded corpus and flies on a GPU. Swap for Qwen3-Embedding on
    # a GPU box for a bit more accuracy (remember to change EMBEDDING_DIM).
    # "local"  -> sentence-transformers in-process (EMBEDDING_MODEL).
    # "openai" -> an OpenAI-compatible server (LM Studio / MLX, vLLM, ...) via
    #             LLM_API_BASE; use EMBED_MODEL for the served model name.
    embedding_backend: str = field(default_factory=lambda: _s("EMBEDDING_BACKEND", "local"))
    embedding_model: str = field(default_factory=lambda: _s("EMBEDDING_MODEL", "BAAI/bge-m3"))
    embed_api_model: str = field(default_factory=lambda: _s("EMBED_MODEL", ""))
    embedding_dim: int = field(default_factory=lambda: _i("EMBEDDING_DIM", 1024))
    embedding_device: str = field(default_factory=lambda: _s("EMBEDDING_DEVICE", "cpu"))
    embedding_batch: int = field(default_factory=lambda: _i("EMBEDDING_BATCH", 16))

    # ---- Reranker (cross-encoder) --------------------------------------
    # The single biggest accuracy lever. bge-reranker-v2-m3 is CPU-friendly.
    rerank_enabled: bool = field(default_factory=lambda: _b("RERANK_ENABLED", True))
    rerank_model: str = field(default_factory=lambda: _s("RERANK_MODEL", "BAAI/bge-reranker-v2-m3"))
    rerank_device: str = field(default_factory=lambda: _s("RERANK_DEVICE", "cpu"))

    # ---- Vision captioning / OCR of figures ----------------------------
    # Optional. When on, each extracted figure is described and its embedded
    # text transcribed by a local vision model (Ollama by default), so diagrams
    # and screenshots become searchable. When off, ingestion still runs and
    # keeps any native PDF figure caption.
    enable_vlm_captions: bool = field(default_factory=lambda: _b("ENABLE_VLM_CAPTIONS", True))
    vlm_backend: str = field(default_factory=lambda: _s("VLM_BACKEND", "ollama"))
    ollama_base_url: str = field(
        default_factory=lambda: _s("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
    )
    vlm_model: str = field(default_factory=lambda: _s("VLM_MODEL", "qwen2.5vl:7b"))
    vlm_timeout: int = field(default_factory=lambda: _i("VLM_TIMEOUT", 120))

    # Shared OpenAI-compatible model server, used by the "openai" embedding
    # backend and the "openai" VLM backend (e.g. LM Studio on Apple Silicon).
    # api_base includes the /v1 suffix, e.g. http://host.docker.internal:1235/v1
    api_base: str = field(default_factory=lambda: _s("LLM_API_BASE", ""))
    api_key: str = field(default_factory=lambda: _s("LLM_API_KEY", ""))

    # ---- Ingestion / parsing -------------------------------------------
    input_dir: str = field(default_factory=lambda: _s("INPUT_DIR", "/data/input"))
    image_dir: str = field(default_factory=lambda: _s("IMAGE_DIR", "/data/images"))
    images_scale: float = field(default_factory=lambda: _f("IMAGES_SCALE", 2.0))
    # Docling compute device for layout/table models: auto|cpu|mps|cuda.
    # Use mps for native ingestion on Apple Silicon, cuda on an NVIDIA box.
    docling_device: str = field(default_factory=lambda: _s("DOCLING_DEVICE", "auto"))
    enable_ocr: bool = field(default_factory=lambda: _b("ENABLE_OCR", True))
    chunk_max_tokens: int = field(default_factory=lambda: _i("CHUNK_MAX_TOKENS", 512))

    # ---- Retrieval tuning ----------------------------------------------
    bm25_k: int = field(default_factory=lambda: _i("RETRIEVE_BM25_K", 40))
    vector_k: int = field(default_factory=lambda: _i("RETRIEVE_VECTOR_K", 40))
    rrf_k: int = field(default_factory=lambda: _i("RRF_K", 60))
    rrf_text_weight: float = field(default_factory=lambda: _f("RRF_TEXT_WEIGHT", 1.0))
    rrf_vector_weight: float = field(default_factory=lambda: _f("RRF_VECTOR_WEIGHT", 1.0))
    rerank_candidates: int = field(default_factory=lambda: _i("RERANK_CANDIDATES", 30))
    result_top_k: int = field(default_factory=lambda: _i("RESULT_TOP_K", 8))

    # ---- MCP server -----------------------------------------------------
    server_name: str = field(default_factory=lambda: _s("MCP_SERVER_NAME", "cisco-docs"))
    host: str = field(default_factory=lambda: _s("MCP_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _i("MCP_PORT", 8000))
    mcp_path: str = field(default_factory=lambda: _s("MCP_PATH", "/mcp"))

    # ---- Auth -----------------------------------------------------------
    # "local"      -> no authentication (bind to localhost only).
    # "cloudflare" -> require a valid Cloudflare Access service-token JWT,
    #                 map it to a named user, and log every remote call.
    auth_mode: str = field(default_factory=lambda: _s("AUTH_MODE", "local").lower())
    cf_team_domain: str = field(default_factory=lambda: _s("CF_ACCESS_TEAM_DOMAIN", ""))
    cf_aud: str = field(default_factory=lambda: _s("CF_ACCESS_AUD", ""))
    allow_any_token: bool = field(default_factory=lambda: _b("ALLOW_ANY_TOKEN", False))
    users_file: str = field(default_factory=lambda: _s("USERS_FILE", "/app/config/users.yaml"))

    # ---- Logging --------------------------------------------------------
    log_level: str = field(default_factory=lambda: _s("LOG_LEVEL", "INFO").upper())
    log_json: bool = field(default_factory=lambda: _b("LOG_JSON", True))
    audit_log_file: str = field(default_factory=lambda: _s("AUDIT_LOG_FILE", "/data/logs/audit.log"))

    @property
    def is_remote(self) -> bool:
        return self.auth_mode == "cloudflare"

    @property
    def cf_certs_url(self) -> str:
        return f"https://{self.cf_team_domain}/cdn-cgi/access/certs"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
