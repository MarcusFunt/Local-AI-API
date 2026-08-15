"""Qdrant vector store operations for RAG."""
from __future__ import annotations
import hashlib
import logging
import math
import re
import time
import uuid
from typing import Any

try:
    from qdrant_client import AsyncQdrantClient
    from qdrant_client.models import (
        Distance,
        FieldCondition,
        Filter,
        MatchValue,
        PointStruct,
        VectorParams,
    )
except ImportError as exc:  # pragma: no cover - exercised by tests without RAG deps
    AsyncQdrantClient = None  # type: ignore[assignment]
    _QDRANT_IMPORT_ERROR: ImportError | None = exc

    class Distance:  # type: ignore[no-redef]
        COSINE = "Cosine"

    def VectorParams(**kwargs: Any) -> dict[str, Any]:  # type: ignore[no-redef]
        return kwargs

    def PointStruct(**kwargs: Any) -> dict[str, Any]:  # type: ignore[no-redef]
        return kwargs

    def Filter(**kwargs: Any) -> dict[str, Any]:  # type: ignore[no-redef]
        return kwargs

    def FieldCondition(**kwargs: Any) -> dict[str, Any]:  # type: ignore[no-redef]
        return kwargs

    def MatchValue(**kwargs: Any) -> dict[str, Any]:  # type: ignore[no-redef]
        return kwargs
else:
    _QDRANT_IMPORT_ERROR = None

from .config import (
    EMBED_DIM,
    HYBRID_CANDIDATES,
    LEXICAL_SCAN_LIMIT,
    QDRANT_COLLECTION,
    QDRANT_URL,
    RERANK_CANDIDATES,
    TOP_K,
)
from .embeddings import embed_text, embed_texts

_client: AsyncQdrantClient | None = None
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_RRF_K = 60
logger = logging.getLogger(__name__)


def _missing_qdrant_dependency_error() -> RuntimeError:
    return RuntimeError(
        "RAG support requires qdrant-client. "
        "Install the RAG dependencies with: pip install -r requirements-rag.txt"
    )


def get_client() -> AsyncQdrantClient:
    global _client
    if _client is None:
        if AsyncQdrantClient is None:
            raise _missing_qdrant_dependency_error() from _QDRANT_IMPORT_ERROR
        _client = AsyncQdrantClient(url=QDRANT_URL)
    return _client


async def ensure_collection() -> None:
    """Create collection if it doesn't exist."""
    client = get_client()
    collections = await client.get_collections()
    names = [c.name for c in collections.collections]
    if QDRANT_COLLECTION not in names:
        await client.create_collection(
            collection_name=QDRANT_COLLECTION,
            vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
        )


async def ingest_chunks(
    chunks: list[str],
    document_id: str,
    filename: str,
    metadata: dict[str, Any] | None = None,
) -> int:
    """Embed and store chunks. Returns number of points upserted."""
    await ensure_collection()
    client = get_client()
    embeddings = await embed_texts(chunks)
    points = [
        PointStruct(
            id=str(uuid.uuid4()),
            vector=embedding,
            payload={
                "text": chunk,
                "document_id": document_id,
                "filename": filename,
                "chunk_index": i,
                "ingested_at": time.time(),
                **(metadata or {}),
            },
        )
        for i, (chunk, embedding) in enumerate(zip(chunks, embeddings))
    ]
    await client.upsert(collection_name=QDRANT_COLLECTION, points=points)
    return len(points)


def _tokens(value: str) -> list[str]:
    return _TOKEN_RE.findall(value.lower())


def _candidate_from_result(result: Any, *, score: float) -> dict[str, Any]:
    payload = result.payload if isinstance(result.payload, dict) else {}
    return {
        "source_id": str(result.id),
        "text": str(payload.get("text") or ""),
        "score": score,
        "document_id": payload.get("document_id"),
        "filename": payload.get("filename"),
        "chunk_index": payload.get("chunk_index"),
    }


def _bm25_candidates(query: str, points: list[Any], limit: int) -> list[dict[str, Any]]:
    """Return lexical candidates using a transparent Unicode-aware BM25 scorer."""
    query_tokens = _tokens(query)
    if not query_tokens or not points:
        return []

    documents: list[tuple[Any, list[str]]] = []
    document_frequency: dict[str, int] = {}
    for point in points:
        payload = point.payload if isinstance(point.payload, dict) else {}
        tokens = _tokens(str(payload.get("text") or ""))
        if not tokens:
            continue
        documents.append((point, tokens))
        for token in set(tokens):
            document_frequency[token] = document_frequency.get(token, 0) + 1

    if not documents:
        return []
    average_length = sum(len(tokens) for _, tokens in documents) / len(documents)
    scored: list[dict[str, Any]] = []
    for point, tokens in documents:
        term_frequency: dict[str, int] = {}
        for token in tokens:
            term_frequency[token] = term_frequency.get(token, 0) + 1
        score = 0.0
        for token in set(query_tokens):
            frequency = term_frequency.get(token, 0)
            if not frequency:
                continue
            idf = math.log(1 + (len(documents) - document_frequency.get(token, 0) + 0.5) /
                           (document_frequency.get(token, 0) + 0.5))
            denominator = frequency + 1.5 * (1 - 0.75 + 0.75 * len(tokens) / average_length)
            score += idf * (frequency * 2.5 / denominator)
        if score > 0:
            scored.append(_candidate_from_result(point, score=score))
    return sorted(scored, key=lambda candidate: float(candidate["score"]), reverse=True)[:limit]


async def _lexical_points(client: AsyncQdrantClient, search_filter: Any) -> list[Any]:
    """Read a bounded local corpus for quality-first lexical retrieval."""
    points: list[Any] = []
    offset = None
    while len(points) < LEXICAL_SCAN_LIMIT:
        batch_limit = min(256, LEXICAL_SCAN_LIMIT - len(points))
        result, next_offset = await client.scroll(
            collection_name=QDRANT_COLLECTION,
            offset=offset,
            limit=batch_limit,
            scroll_filter=search_filter,
            with_payload=True,
            with_vectors=False,
        )
        points.extend(result)
        if next_offset is None:
            break
        offset = next_offset
    return points


def _rrf_fuse(*ranked_lists: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fuse dense and lexical rankings without assuming comparable score scales."""
    fused: dict[str, dict[str, Any]] = {}
    for ranked in ranked_lists:
        for rank, candidate in enumerate(ranked, start=1):
            source_id = str(candidate["source_id"])
            current = fused.setdefault(source_id, {**candidate, "fusion_score": 0.0})
            current["fusion_score"] = float(current["fusion_score"]) + 1 / (_RRF_K + rank)
    return sorted(fused.values(), key=lambda candidate: float(candidate["fusion_score"]), reverse=True)


async def search(query: str, top_k: int = TOP_K, document_id: str | None = None) -> list[dict]:
    """Hybrid dense+lexical retrieval followed by local ColBERT reranking."""
    await ensure_collection()
    client = get_client()
    query_vector = await embed_text(query)
    search_filter = None
    if document_id:
        search_filter = Filter(
            must=[FieldCondition(key="document_id", match=MatchValue(value=document_id))]
        )
    candidate_limit = max(top_k, HYBRID_CANDIDATES)
    results = await client.search(
        collection_name=QDRANT_COLLECTION,
        query_vector=query_vector,
        limit=candidate_limit,
        query_filter=search_filter,
        with_payload=True,
    )
    dense = [_candidate_from_result(result, score=float(result.score)) for result in results]
    lexical = _bm25_candidates(
        query,
        await _lexical_points(client, search_filter),
        candidate_limit,
    )
    fused = _rrf_fuse(dense, lexical)
    try:
        from .reranker import rerank

        reranked = await rerank(query, fused[:RERANK_CANDIDATES])
    except Exception as exc:
        # Retrieval remains available if a first-time model download fails; the
        # caller can retry and diagnostics retain the reason for the fallback.
        logger.warning("Local ColBERT reranking failed; returning fused candidates: %s", exc)
        reranked = fused[:RERANK_CANDIDATES]
        for candidate in reranked:
            candidate["rerank_error"] = type(exc).__name__
    return reranked[:top_k]


async def list_documents() -> list[dict]:
    """Return unique documents stored in the collection."""
    await ensure_collection()
    client = get_client()
    # Scroll all points and deduplicate by document_id
    seen: dict[str, dict] = {}
    offset = None
    while True:
        result, next_offset = await client.scroll(
            collection_name=QDRANT_COLLECTION,
            offset=offset,
            limit=256,
            with_payload=True,
            with_vectors=False,
        )
        for point in result:
            doc_id = point.payload.get("document_id", "")
            if doc_id not in seen:
                seen[doc_id] = {
                    "document_id": doc_id,
                    "filename": point.payload.get("filename", ""),
                    "ingested_at": point.payload.get("ingested_at"),
                }
        if next_offset is None:
            break
        offset = next_offset
    return list(seen.values())


async def delete_document(document_id: str) -> int:
    """Delete all chunks for a document. Returns count deleted."""
    await ensure_collection()
    client = get_client()
    result = await client.delete(
        collection_name=QDRANT_COLLECTION,
        points_selector=Filter(
            must=[FieldCondition(key="document_id", match=MatchValue(value=document_id))]
        ),
    )
    return result.operation_id  # approximation — Qdrant doesn't return exact count here


async def qdrant_healthy() -> bool:
    """Return True if Qdrant is reachable."""
    try:
        client = get_client()
        await client.get_collections()
        return True
    except Exception:
        return False
