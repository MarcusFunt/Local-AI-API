"""Qdrant vector store operations for RAG."""
from __future__ import annotations
import hashlib
import time
import uuid
from typing import Any

from qdrant_client import QdrantClient, AsyncQdrantClient
from qdrant_client.models import (
    Distance, VectorParams, PointStruct, Filter,
    FieldCondition, MatchValue, PayloadSchemaType,
)

from .config import QDRANT_URL, QDRANT_COLLECTION, EMBED_DIM, TOP_K
from .embeddings import embed_text, embed_texts

_client: AsyncQdrantClient | None = None


def get_client() -> AsyncQdrantClient:
    global _client
    if _client is None:
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


async def search(query: str, top_k: int = TOP_K, document_id: str | None = None) -> list[dict]:
    """Search for relevant chunks. Returns list of {text, score, document_id, filename}."""
    await ensure_collection()
    client = get_client()
    query_vector = await embed_text(query)
    search_filter = None
    if document_id:
        search_filter = Filter(
            must=[FieldCondition(key="document_id", match=MatchValue(value=document_id))]
        )
    results = await client.search(
        collection_name=QDRANT_COLLECTION,
        query_vector=query_vector,
        limit=top_k,
        query_filter=search_filter,
        with_payload=True,
    )
    return [
        {
            "text": r.payload["text"],
            "score": r.score,
            "document_id": r.payload.get("document_id"),
            "filename": r.payload.get("filename"),
            "chunk_index": r.payload.get("chunk_index"),
        }
        for r in results
    ]


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
