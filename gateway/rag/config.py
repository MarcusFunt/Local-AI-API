"""RAG configuration loaded from environment."""
from __future__ import annotations
import os
from pathlib import Path

QDRANT_URL: str = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333")
QDRANT_COLLECTION: str = os.environ.get("QDRANT_COLLECTION", "local-ai-api-docs")
EMBED_MODEL: str = os.environ.get("RAG_EMBED_MODEL", "nomic-embed-text")
EMBED_DIM: int = int(os.environ.get("RAG_EMBED_DIM", "768"))
OLLAMA_BASE_URL: str = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
TOP_K: int = int(os.environ.get("RAG_TOP_K", "4"))
CHUNK_SIZE: int = int(os.environ.get("RAG_CHUNK_SIZE", "512"))
CHUNK_OVERLAP: int = int(os.environ.get("RAG_CHUNK_OVERLAP", "64"))
RAG_ENABLED: bool = os.environ.get("RAG_ENABLED", "false").lower() == "true"
