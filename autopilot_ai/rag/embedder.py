"""Chunk text and create embeddings with the local Ollama embedding API."""

from __future__ import annotations

import json
import math
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
EMBED_MODEL = os.environ.get("AUTOPILOT_EMBED_MODEL", "nomic-embed-text")
CHUNK_SIZE = 500
CHUNK_OVERLAP = 100


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into trimmed, overlapping character chunks."""
    if not isinstance(text, str):
        raise TypeError("Text to chunk must be a string.")
    if not isinstance(size, int) or isinstance(size, bool) or size < 1:
        raise ValueError("Chunk size must be a positive integer.")
    if not isinstance(overlap, int) or isinstance(overlap, bool) or overlap < 0 or overlap >= size:
        raise ValueError("Chunk overlap must be an integer between zero and size - 1.")
    if not text.strip():
        return []

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = end - overlap
    return chunks


def embed_texts(texts: list[str], timeout: float = 60.0) -> list[list[float]]:
    """Call Ollama's /api/embed endpoint and validate every returned vector."""
    if not texts:
        return []
    if any(not isinstance(text, str) or not text.strip() for text in texts):
        raise ValueError("Embedding inputs must be non-empty strings.")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("Embedding timeout must be positive.")

    payload = json.dumps({"model": EMBED_MODEL, "input": texts}).encode("utf-8")
    request = Request(
        f"{OLLAMA_HOST}/api/embed",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read())
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(
            f"Ollama embedding API returned HTTP {exc.code} for {EMBED_MODEL}: {detail}. "
            f"Install the local model with: ollama pull {EMBED_MODEL}"
        ) from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Could not reach the Ollama embedding API ({EMBED_MODEL} at {OLLAMA_HOST}): {exc}. "
            f"Ensure Ollama is running and install the local model with: ollama pull {EMBED_MODEL}"
        ) from exc

    embeddings = data.get("embeddings") if isinstance(data, dict) else None
    if not isinstance(embeddings, list) or len(embeddings) != len(texts):
        raise RuntimeError("Ollama embedding API returned an unexpected response shape.")
    if not embeddings:
        return []
    if any(not isinstance(vector, list) or not vector for vector in embeddings):
        raise RuntimeError("Ollama returned an empty or malformed embedding vector.")
    dimension = len(embeddings[0])
    if any(
        len(vector) != dimension
        or any(not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) for value in vector)
        for vector in embeddings
    ):
        raise RuntimeError("Ollama returned inconsistent or non-finite embedding vectors.")
    return [[float(value) for value in vector] for vector in embeddings]


def embed_query(query: str) -> list[float]:
    """Embed one non-empty search query."""
    normalized = query.strip() if isinstance(query, str) else ""
    if not normalized:
        raise ValueError("Semantic search query cannot be empty.")
    return embed_texts([normalized])[0]
