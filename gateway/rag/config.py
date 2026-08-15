"""Validated RAG settings loaded safely from the environment."""
from __future__ import annotations

import os
from collections.abc import Mapping

from pydantic import BaseModel, Field, model_validator


MAX_TOP_K = 20
_DEFAULT_EMBED_DIM = 768
_DEFAULT_TOP_K = 4
_DEFAULT_CHUNK_SIZE = 512
_DEFAULT_CHUNK_OVERLAP = 64
_DEFAULT_HYBRID_CANDIDATES = 16
_DEFAULT_RERANK_CANDIDATES = 12
_DEFAULT_LEXICAL_SCAN_LIMIT = 5_000
_DEFAULT_RERANK_MODEL = "answerdotai/answerai-colbert-small-v1"
_DEFAULT_RERANK_CACHE_DIR = "/models/cache/fastembed"


def _environment_int(environment: Mapping[str, str], name: str, default: int) -> int:
    """Read one integer without letting a bad deployment variable break imports."""
    try:
        return int(environment.get(name, str(default)).strip())
    except (AttributeError, TypeError, ValueError):
        return default


class RagSettings(BaseModel):
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_collection: str = "local-ai-api-docs"
    embed_model: str = "nomic-embed-text"
    embed_dim: int = Field(default=_DEFAULT_EMBED_DIM, ge=1, le=16_384)
    ollama_base_url: str = "http://127.0.0.1:11434"
    top_k: int = Field(default=_DEFAULT_TOP_K, ge=1, le=MAX_TOP_K)
    chunk_size: int = Field(default=_DEFAULT_CHUNK_SIZE, ge=1, le=100_000)
    chunk_overlap: int = Field(default=_DEFAULT_CHUNK_OVERLAP, ge=0)
    hybrid_candidates: int = Field(default=_DEFAULT_HYBRID_CANDIDATES, ge=1, le=100)
    rerank_candidates: int = Field(default=_DEFAULT_RERANK_CANDIDATES, ge=1, le=100)
    lexical_scan_limit: int = Field(default=_DEFAULT_LEXICAL_SCAN_LIMIT, ge=1, le=100_000)
    rerank_model: str = _DEFAULT_RERANK_MODEL
    rerank_cache_dir: str = _DEFAULT_RERANK_CACHE_DIR
    enabled: bool = True

    @model_validator(mode="after")
    def validate_chunk_window(self) -> "RagSettings":
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("RAG_CHUNK_SIZE must be greater than RAG_CHUNK_OVERLAP.")
        if self.rerank_candidates > self.hybrid_candidates:
            self.rerank_candidates = self.hybrid_candidates
        return self

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> "RagSettings":
        values = os.environ if environment is None else environment
        embed_dim = _environment_int(values, "RAG_EMBED_DIM", _DEFAULT_EMBED_DIM)
        top_k = _environment_int(values, "RAG_TOP_K", _DEFAULT_TOP_K)
        chunk_size = _environment_int(values, "RAG_CHUNK_SIZE", _DEFAULT_CHUNK_SIZE)
        chunk_overlap = _environment_int(values, "RAG_CHUNK_OVERLAP", _DEFAULT_CHUNK_OVERLAP)
        hybrid_candidates = _environment_int(
            values, "RAG_HYBRID_CANDIDATES", _DEFAULT_HYBRID_CANDIDATES
        )
        rerank_candidates = _environment_int(
            values, "RAG_RERANK_CANDIDATES", _DEFAULT_RERANK_CANDIDATES
        )
        lexical_scan_limit = _environment_int(
            values, "RAG_LEXICAL_SCAN_LIMIT", _DEFAULT_LEXICAL_SCAN_LIMIT
        )

        if not 1 <= embed_dim <= 16_384:
            embed_dim = _DEFAULT_EMBED_DIM
        top_k = min(MAX_TOP_K, max(1, top_k))
        if not 1 <= chunk_size <= 100_000:
            chunk_size = _DEFAULT_CHUNK_SIZE
        if chunk_overlap < 0 or chunk_overlap >= chunk_size:
            chunk_overlap = min(_DEFAULT_CHUNK_OVERLAP, chunk_size - 1)
        hybrid_candidates = min(100, max(1, hybrid_candidates))
        rerank_candidates = min(hybrid_candidates, max(1, rerank_candidates))
        lexical_scan_limit = min(100_000, max(1, lexical_scan_limit))

        return cls(
            qdrant_url=str(values.get("QDRANT_URL", "http://127.0.0.1:6333")),
            qdrant_collection=str(values.get("QDRANT_COLLECTION", "local-ai-api-docs")),
            embed_model=str(values.get("RAG_EMBED_MODEL", "nomic-embed-text")),
            embed_dim=embed_dim,
            ollama_base_url=str(values.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")),
            top_k=top_k,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            hybrid_candidates=hybrid_candidates,
            rerank_candidates=rerank_candidates,
            lexical_scan_limit=lexical_scan_limit,
            rerank_model=str(values.get("RAG_RERANK_MODEL", _DEFAULT_RERANK_MODEL)).strip()
            or _DEFAULT_RERANK_MODEL,
            rerank_cache_dir=str(
                values.get("RAG_RERANK_CACHE_DIR", _DEFAULT_RERANK_CACHE_DIR)
            ).strip()
            or _DEFAULT_RERANK_CACHE_DIR,
            enabled=str(values.get("RAG_ENABLED", "false")).strip().lower() == "true",
        )


settings = RagSettings.from_environment()
QDRANT_URL = settings.qdrant_url
QDRANT_COLLECTION = settings.qdrant_collection
EMBED_MODEL = settings.embed_model
EMBED_DIM = settings.embed_dim
OLLAMA_BASE_URL = settings.ollama_base_url
TOP_K = settings.top_k
CHUNK_SIZE = settings.chunk_size
CHUNK_OVERLAP = settings.chunk_overlap
HYBRID_CANDIDATES = settings.hybrid_candidates
RERANK_CANDIDATES = settings.rerank_candidates
LEXICAL_SCAN_LIMIT = settings.lexical_scan_limit
RERANK_MODEL = settings.rerank_model
RERANK_CACHE_DIR = settings.rerank_cache_dir
RAG_ENABLED = settings.enabled
