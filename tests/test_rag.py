"""Unit tests for the RAG pipeline (chunker, endpoints).

All Qdrant and Ollama calls are mocked — no live services required.
"""
from __future__ import annotations

import io
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Chunker unit tests — no network, no mocking needed
# ---------------------------------------------------------------------------


class TestChunkText:
    def test_chunk_text_basic(self):
        from gateway.rag.chunker import chunk_text

        text = "word " * 600  # 600 words
        chunks = chunk_text(text.strip(), chunk_size=512, overlap=64)
        # First chunk should be exactly 512 words
        assert len(chunks[0].split()) == 512
        # All chunks are non-empty
        assert all(c.strip() for c in chunks)

    def test_chunk_text_overlap(self):
        from gateway.rag.chunker import chunk_text

        words = list(range(100))
        text = " ".join(str(w) for w in words)
        chunks = chunk_text(text, chunk_size=20, overlap=5)
        # Second chunk should start with the last 5 words of the first chunk
        first_end = chunks[0].split()[-5:]
        second_start = chunks[1].split()[:5]
        assert first_end == second_start

    def test_chunk_text_empty(self):
        from gateway.rag.chunker import chunk_text

        assert chunk_text("") == []
        assert chunk_text("   ") == []

    def test_chunk_text_shorter_than_chunk_size(self):
        from gateway.rag.chunker import chunk_text

        text = "only a few words"
        chunks = chunk_text(text, chunk_size=512, overlap=64)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_chunk_text_exact_chunk_size(self):
        from gateway.rag.chunker import chunk_text

        text = " ".join(["word"] * 512)
        chunks = chunk_text(text, chunk_size=512, overlap=64)
        assert len(chunks) == 1


class TestExtractText:
    def test_extract_text_utf8(self):
        from gateway.rag.chunker import extract_text_from_bytes

        content = "Hello, world!\nSecond line.".encode("utf-8")
        result = extract_text_from_bytes(content, "test.txt")
        assert result == "Hello, world!\nSecond line."

    def test_extract_text_markdown(self):
        from gateway.rag.chunker import extract_text_from_bytes

        content = "# Heading\n\nParagraph text.".encode("utf-8")
        result = extract_text_from_bytes(content, "doc.md")
        assert "Heading" in result

    def test_extract_text_lossy_fallback(self):
        from gateway.rag.chunker import extract_text_from_bytes

        # Byte sequence that is not valid UTF-8
        content = b"Hello \x80\x81 World"
        result = extract_text_from_bytes(content, "binary.bin")
        assert "Hello" in result
        assert "World" in result

    def test_extract_text_pdf_missing_pypdf(self, monkeypatch: pytest.MonkeyPatch):
        """When pypdf is not installed, raise ValueError with helpful message."""
        from gateway.rag import chunker

        # Simulate pypdf not being importable
        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "pypdf":
                raise ImportError("No module named 'pypdf'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)
        with pytest.raises(ValueError, match="pypdf"):
            chunker.extract_text_from_bytes(b"%PDF-1.4", "doc.pdf")


# ---------------------------------------------------------------------------
# Document endpoint tests — RAG_ENABLED=false guard (503 responses)
# ---------------------------------------------------------------------------

def _make_app_with_rag_disabled():
    """Create a test app instance where RAG_ENABLED is False."""
    import gateway.config as cfg_module
    import gateway.routes.health as health_module
    import gateway.routes.chat as chat_module
    import gateway.routes.audio as audio_module
    import gateway.routes.status as status_module
    import gateway.client as client_module
    from gateway import main as main_module
    from gateway.config import Settings
    from gateway.main import create_app

    test_settings = Settings(
        ollama_base_url="http://127.0.0.1:11434",
        host="127.0.0.1",
        port=8080,
        default_model_profile="main",
        enable_api_key_auth=False,
        api_key="",
        request_timeout_seconds=10,
        max_request_body_bytes=10_485_760,
        enable_arbitrary_models=False,
    )
    return create_app(), test_settings, client_module


class TestIngestEndpointRagDisabled:
    async def test_ingest_endpoint_rag_disabled(self):
        """POST /v1/documents/ingest returns 503 when RAG_ENABLED=false."""
        from gateway.main import create_app
        import gateway.client as client_module
        from gateway.config import Settings

        app = create_app()
        settings = Settings(
            ollama_base_url="http://127.0.0.1:11434",
            host="127.0.0.1",
            port=8080,
            default_model_profile="main",
            enable_api_key_auth=False,
            api_key="",
            request_timeout_seconds=10,
            max_request_body_bytes=10_485_760,
            enable_arbitrary_models=False,
        )
        client_module.init(settings)

        # Ensure RAG_ENABLED is False in the rag.config module
        with patch("gateway.rag.config.RAG_ENABLED", False):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                resp = await ac.post(
                    "/v1/documents/ingest",
                    files={"file": ("test.txt", b"some content", "text/plain")},
                )
            assert resp.status_code == 503
            assert "RAG is not enabled" in resp.json()["detail"]

        await client_module.close()


class TestSearchEndpointRagDisabled:
    async def test_search_endpoint_rag_disabled(self):
        """POST /v1/search returns 503 when RAG_ENABLED=false."""
        from gateway.main import create_app
        import gateway.client as client_module
        from gateway.config import Settings

        app = create_app()
        settings = Settings(
            ollama_base_url="http://127.0.0.1:11434",
            host="127.0.0.1",
            port=8080,
            default_model_profile="main",
            enable_api_key_auth=False,
            api_key="",
            request_timeout_seconds=10,
            max_request_body_bytes=10_485_760,
            enable_arbitrary_models=False,
        )
        client_module.init(settings)

        with patch("gateway.rag.config.RAG_ENABLED", False):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                resp = await ac.post(
                    "/v1/search",
                    json={"query": "hello"},
                )
            assert resp.status_code == 503

        await client_module.close()


class TestListDocumentsRagDisabled:
    async def test_list_documents_rag_disabled(self):
        """GET /v1/documents returns 503 when RAG_ENABLED=false."""
        from gateway.main import create_app
        import gateway.client as client_module
        from gateway.config import Settings

        app = create_app()
        settings = Settings(
            ollama_base_url="http://127.0.0.1:11434",
            host="127.0.0.1",
            port=8080,
            default_model_profile="main",
            enable_api_key_auth=False,
            api_key="",
            request_timeout_seconds=10,
            max_request_body_bytes=10_485_760,
            enable_arbitrary_models=False,
        )
        client_module.init(settings)

        with patch("gateway.rag.config.RAG_ENABLED", False):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                resp = await ac.get("/v1/documents")
            assert resp.status_code == 503

        await client_module.close()


# ---------------------------------------------------------------------------
# Embeddings unit test — mock httpx
# ---------------------------------------------------------------------------


class TestEmbedTexts:
    async def test_embed_texts_calls_ollama(self):
        """embed_texts calls /api/embed and returns the embeddings list."""
        from gateway.rag import embeddings

        fake_embeddings = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={"embeddings": fake_embeddings})

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await embeddings.embed_texts(["hello", "world"])

        assert result == fake_embeddings
        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        assert "/api/embed" in call_kwargs[0][0]

    async def test_embed_text_returns_single_vector(self):
        """embed_text is a convenience wrapper that returns one vector."""
        from gateway.rag import embeddings

        fake_vector = [0.1, 0.2, 0.3]
        with patch.object(embeddings, "embed_texts", AsyncMock(return_value=[fake_vector])):
            result = await embeddings.embed_text("hello")

        assert result == fake_vector


# ---------------------------------------------------------------------------
# Store unit tests — mock AsyncQdrantClient
# ---------------------------------------------------------------------------


class TestQdrantStore:
    async def test_ensure_collection_creates_if_missing(self):
        """ensure_collection calls create_collection when the collection is absent."""
        from gateway.rag import store

        mock_collections = MagicMock()
        mock_collections.collections = []  # empty — collection doesn't exist

        mock_client = AsyncMock()
        mock_client.get_collections = AsyncMock(return_value=mock_collections)
        mock_client.create_collection = AsyncMock()

        # Replace the module-level singleton
        original_client = store._client
        store._client = mock_client
        try:
            await store.ensure_collection()
            mock_client.create_collection.assert_called_once()
        finally:
            store._client = original_client

    async def test_qdrant_healthy_returns_true_on_success(self):
        """qdrant_healthy returns True when get_collections succeeds."""
        from gateway.rag import store

        mock_client = AsyncMock()
        mock_client.get_collections = AsyncMock(return_value=MagicMock())

        original_client = store._client
        store._client = mock_client
        try:
            result = await store.qdrant_healthy()
            assert result is True
        finally:
            store._client = original_client

    async def test_qdrant_healthy_returns_false_on_exception(self):
        """qdrant_healthy returns False when Qdrant is unreachable."""
        from gateway.rag import store

        mock_client = AsyncMock()
        mock_client.get_collections = AsyncMock(side_effect=ConnectionRefusedError("refused"))

        original_client = store._client
        store._client = mock_client
        try:
            result = await store.qdrant_healthy()
            assert result is False
        finally:
            store._client = original_client
