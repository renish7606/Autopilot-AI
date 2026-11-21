"""Persistent ChromaDB storage for approved-file text chunks and embeddings."""

from __future__ import annotations

from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "rag_store"
_COLLECTION_NAME = "workspace_chunks"


def _client(db_path: Path = DEFAULT_DB_PATH):
    try:
        import chromadb
        from chromadb.config import Settings
    except ImportError as exc:
        raise RuntimeError("Semantic search requires ChromaDB. Install project dependencies with: python -m pip install -r requirements.txt") from exc
    db_path.mkdir(parents=True, exist_ok=True)
    try:
        # AutoPilot is local-first; disable Chroma's optional telemetry and avoid
        # noisy telemetry-client compatibility warnings in the CLI.
        settings = Settings(anonymized_telemetry=False)
        return chromadb.PersistentClient(path=str(db_path), settings=settings)
    except Exception as exc:
        raise RuntimeError(f"Could not open the local semantic-search index at {db_path}: {exc}") from exc


def get_collection(db_path: Path = DEFAULT_DB_PATH):
    """Return the persistent collection, using cosine distance."""
    try:
        client = _client(db_path)
        return client.get_or_create_collection(
            name=_COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Could not load the local semantic-search collection: {exc}") from exc


def upsert_chunks(
    file_path: str,
    chunks: list[str],
    embeddings: list[list[float]],
    db_path: Path = DEFAULT_DB_PATH,
    root_path: str | None = None,
) -> None:
    """Upsert chunks for one file and remove obsolete chunks after the upsert."""
    if len(chunks) != len(embeddings):
        raise ValueError("Each stored text chunk must have exactly one embedding.")
    if not chunks:
        delete_file_chunks(file_path, db_path)
        return
    collection = get_collection(db_path)
    try:
        existing = collection.get(where={"file_path": file_path}, include=["metadatas"])
        old_ids = existing.get("ids") or []
        ids = [f"{file_path}::chunk::{index}" for index in range(len(chunks))]
        metadata = [
            {
                "file_path": file_path,
                "root_path": root_path or str(Path(file_path).parent),
                "chunk_index": index,
            }
            for index in range(len(chunks))
        ]
        collection.upsert(ids=ids, embeddings=embeddings, documents=chunks, metadatas=metadata)
        obsolete = [chunk_id for chunk_id in old_ids if chunk_id not in set(ids)]
        if obsolete:
            collection.delete(ids=obsolete)
    except Exception as exc:
        raise RuntimeError(f"Could not update semantic-search chunks for {Path(file_path).name}: {exc}") from exc


def query_chunks(
    query_embedding: list[float],
    n_results: int = 5,
    db_path: Path = DEFAULT_DB_PATH,
    root_path: str | None = None,
) -> list[dict[str, Any]]:
    """Return the closest chunks, optionally limited to one approved root."""
    if not isinstance(n_results, int) or isinstance(n_results, bool) or n_results < 1:
        raise ValueError("n_results must be a positive integer.")
    collection = get_collection(db_path)
    where = {"root_path": root_path} if root_path is not None else None
    try:
        # Collection.count() has no `where` parameter in ChromaDB 0.6.x.
        # Use the total as a safe upper bound; the query itself applies `where`.
        count = collection.count()
        if count == 0:
            return []
        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=min(n_results, count),
            include=["documents", "metadatas", "distances"],
            where=where,
        )
    except Exception as exc:
        raise RuntimeError(f"Could not query the local semantic-search index: {exc}") from exc

    ids = results.get("ids")
    documents = results.get("documents")
    metadatas = results.get("metadatas")
    distances = results.get("distances")
    if not all(isinstance(values, list) and values for values in (ids, documents, metadatas, distances)):
        return []
    rows = [ids[0], documents[0], metadatas[0], distances[0]]
    if len({len(row) for row in rows}) != 1:
        raise RuntimeError("ChromaDB returned inconsistent semantic-search results.")

    output: list[dict[str, Any]] = []
    for _, document, metadata, distance in zip(*rows):
        if not isinstance(document, str) or not isinstance(metadata, dict):
            continue
        try:
            score = max(-1.0, min(1.0, 1.0 - float(distance)))
            chunk_index = int(metadata.get("chunk_index", 0))
        except (TypeError, ValueError, OverflowError):
            continue
        output.append({
            "file_path": str(metadata.get("file_path") or ""),
            "chunk_index": chunk_index,
            "chunk_text": document,
            "score": round(score, 4),
        })
    return output


def delete_file_chunks(file_path: str, db_path: Path = DEFAULT_DB_PATH) -> None:
    """Delete every indexed chunk belonging to one path."""
    collection = get_collection(db_path)
    try:
        existing = collection.get(where={"file_path": file_path}, include=["metadatas"])
        ids = existing.get("ids") or []
        if ids:
            collection.delete(ids=ids)
    except Exception as exc:
        raise RuntimeError(f"Could not remove semantic-search chunks for {Path(file_path).name}: {exc}") from exc


def collection_stats(db_path: Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    """Return local index size and location."""
    collection = get_collection(db_path)
    try:
        count = collection.count()
    except Exception as exc:
        raise RuntimeError(f"Could not read semantic-search index statistics: {exc}") from exc
    return {"total_chunks": int(count), "db_path": str(db_path)}


def count_chunks(root_path: str, db_path: Path = DEFAULT_DB_PATH) -> int:
    """Count chunks belonging to a single approved root."""
    try:
        # ChromaDB's count() is unfiltered in supported 0.5/0.6 releases.
        # get() applies the metadata filter and always returns matching IDs.
        result = get_collection(db_path).get(
            where={"root_path": root_path},
            include=[],
        )
        return len(result.get("ids") or [])
    except Exception as exc:
        raise RuntimeError(f"Could not count indexed chunks for an approved directory: {exc}") from exc
