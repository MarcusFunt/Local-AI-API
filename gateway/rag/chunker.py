"""Split raw text into overlapping chunks for indexing."""
from __future__ import annotations
from .config import CHUNK_SIZE, CHUNK_OVERLAP


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """
    Split text into overlapping word-based chunks.
    Each chunk is at most chunk_size words. Adjacent chunks share `overlap` words.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero.")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be non-negative and smaller than chunk_size.")
    words = text.split()
    if not words:
        return []
    chunks = []
    start = 0
    while start < len(words):
        end = min(start + chunk_size, len(words))
        chunks.append(" ".join(words[start:end]))
        if end == len(words):
            break
        start += chunk_size - overlap
    return chunks


def extract_text_from_bytes(content: bytes, filename: str) -> str:
    """
    Extract plain text from uploaded file bytes.
    Handles: .txt, .md, .py, .json, .yaml, .csv (UTF-8)
    Falls back to lossy UTF-8 decode for unknown types.
    PDF support requires pypdf (imported lazily).
    """
    name = filename.lower()

    if name.endswith(".pdf"):
        try:
            import io
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(content))
            return "\n\n".join(
                page.extract_text() or "" for page in reader.pages
            ).strip()
        except ImportError:
            raise ValueError("PDF support requires pypdf: pip install pypdf")
        except Exception as exc:
            raise ValueError(f"Could not parse PDF: {exc}") from exc

    # Plain text variants
    try:
        return content.decode("utf-8").strip()
    except UnicodeDecodeError:
        return content.decode("utf-8", errors="replace").strip()
