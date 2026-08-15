"""Lazy local ColBERT reranking for the quality-first RAG pipeline."""
from __future__ import annotations

import asyncio
import threading
from typing import Any

from .config import RERANK_CACHE_DIR, RERANK_MODEL

_model: Any | None = None
_model_lock = threading.Lock()


def _get_model() -> Any:
    """Load the small multilingual ONNX model only when RAG is first queried."""
    global _model
    with _model_lock:
        if _model is None:
            from fastembed import LateInteractionTextEmbedding

            _model = LateInteractionTextEmbedding(
                model_name=RERANK_MODEL,
                cache_dir=RERANK_CACHE_DIR,
            )
    return _model


def _rerank_sync(query: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply ColBERT MaxSim scoring to a deliberately small fused candidate set."""
    if not candidates:
        return []

    model = _get_model()
    query_embedding = next(iter(model.query_embed(query)))
    document_embeddings = list(model.embed([str(candidate["text"]) for candidate in candidates]))
    rescored: list[dict[str, Any]] = []
    for candidate, document_embedding in zip(candidates, document_embeddings):
        # ColBERT's late interaction score: every query token contributes the
        # best matching document token. FastEmbed returns NumPy arrays here.
        score = float((query_embedding @ document_embedding.T).max(axis=1).sum())
        rescored.append({**candidate, "rerank_score": score})
    return sorted(rescored, key=lambda candidate: float(candidate["rerank_score"]), reverse=True)


async def rerank(query: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run CPU-backed reranking outside the FastAPI event loop."""
    return await asyncio.to_thread(_rerank_sync, query, candidates)
