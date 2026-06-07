"""Document ingestion and RAG search endpoints."""
from __future__ import annotations

import hashlib
import time
import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter()

_MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MiB


class SearchRequest(BaseModel):
    query: str
    top_k: int = 4
    document_id: str | None = None


@router.post("/v1/documents/ingest")
async def ingest_document(
    file: Annotated[UploadFile, File(description="Document to index")],
    document_id: Annotated[str | None, Form()] = None,
) -> JSONResponse:
    """Upload and index a document for RAG retrieval."""
    from ..rag.store import ingest_chunks
    from ..rag.chunker import chunk_text, extract_text_from_bytes
    from ..rag.config import RAG_ENABLED

    if not RAG_ENABLED:
        raise HTTPException(status_code=503, detail="RAG is not enabled. Set RAG_ENABLED=true.")

    content = await file.read()
    if len(content) > _MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 10 MiB).")
    if not content:
        raise HTTPException(status_code=422, detail="Empty file.")

    filename = file.filename or "unknown"
    try:
        text = extract_text_from_bytes(content, filename)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    if not text.strip():
        raise HTTPException(status_code=422, detail="No text could be extracted from the file.")

    doc_id = document_id or hashlib.sha256(content).hexdigest()[:16]
    chunks = chunk_text(text)

    try:
        count = await ingest_chunks(chunks, document_id=doc_id, filename=filename)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Vector store error: {exc}")

    return JSONResponse({
        "document_id": doc_id,
        "filename": filename,
        "chunks": count,
        "characters": len(text),
    })


@router.get("/v1/documents")
async def list_documents() -> JSONResponse:
    """List all indexed documents."""
    from ..rag.store import list_documents as _list
    from ..rag.config import RAG_ENABLED

    if not RAG_ENABLED:
        raise HTTPException(status_code=503, detail="RAG is not enabled.")
    try:
        docs = await _list()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Vector store error: {exc}")
    return JSONResponse({"documents": docs, "count": len(docs)})


@router.delete("/v1/documents/{document_id}")
async def delete_document(document_id: str) -> JSONResponse:
    """Remove a document and all its chunks from the vector store."""
    from ..rag.store import delete_document as _delete
    from ..rag.config import RAG_ENABLED

    if not RAG_ENABLED:
        raise HTTPException(status_code=503, detail="RAG is not enabled.")
    try:
        await _delete(document_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Vector store error: {exc}")
    return JSONResponse({"deleted": document_id})


@router.post("/v1/search")
async def search_documents(req: SearchRequest) -> JSONResponse:
    """Semantic search over indexed documents."""
    from ..rag.store import search
    from ..rag.config import RAG_ENABLED

    if not RAG_ENABLED:
        raise HTTPException(status_code=503, detail="RAG is not enabled.")
    if not req.query.strip():
        raise HTTPException(status_code=422, detail="Query must not be empty.")
    try:
        results = await search(req.query, top_k=req.top_k, document_id=req.document_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Vector store error: {exc}")
    return JSONResponse({"results": results, "count": len(results)})
