"""Semantic query engine that only returns files in the caller's approved roots."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .embedder import embed_query
from .store import collection_stats, count_chunks, query_chunks


def semantic_search(
    query: str,
    n_results: int = 5,
    min_score: float = 0.30,
    approved_roots: list[Path] | tuple[Path, ...] | None = None,
) -> dict[str, Any]:
    """Search indexed chunks and group the best matching snippets by approved file."""
    normalized_query = query.strip() if isinstance(query, str) else ""
    if not normalized_query:
        raise ValueError("Semantic search query cannot be empty.")
    if not isinstance(n_results, int) or isinstance(n_results, bool) or not 1 <= n_results <= 20:
        raise ValueError("n_results must be between 1 and 20.")
    if not isinstance(min_score, (int, float)) or isinstance(min_score, bool) or not 0 <= min_score <= 1:
        raise ValueError("min_score must be between 0 and 1.")

    roots: list[Path] = []
    for configured_root in approved_roots or []:
        try:
            root = Path(configured_root).resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if root.is_dir() and root not in roots:
            roots.append(root)

    if approved_roots is not None and not roots:
        return {
            "query": normalized_query,
            "results": [],
            "total_indexed_chunks": 0,
            "note": "No currently approved directories are available for semantic search.",
        }

    stats = collection_stats()
    if approved_roots is None:
        total_chunks = stats["total_chunks"]
        root_paths: list[Path | None] = [None]
    else:
        root_paths = roots
        total_chunks = sum(count_chunks(str(root)) for root in roots)

    if total_chunks == 0:
        return {
            "query": normalized_query,
            "results": [],
            "total_indexed_chunks": 0,
            "note": "No files are indexed yet. Use the dashboard's Index Workspace button first.",
        }

    query_embedding = embed_query(normalized_query)
    by_file: dict[str, dict[str, Any]] = {}
    per_root_limit = min(200, max(n_results * 10, n_results))
    for root in root_paths:
        root_chunks = query_chunks(
            query_embedding,
            n_results=per_root_limit,
            root_path=str(root) if root is not None else None,
        )
        for chunk in root_chunks:
            raw_path = chunk.get("file_path")
            if not isinstance(raw_path, str) or not raw_path:
                continue
            candidate = Path(raw_path)
            try:
                if candidate.is_symlink():
                    continue
                resolved = candidate.resolve(strict=True)
                if not resolved.is_file():
                    continue
                if root is not None:
                    resolved.relative_to(root)
            except (OSError, RuntimeError, ValueError):
                continue
            path_key = os.path.normcase(str(resolved))
            score = chunk.get("score")
            snippet = chunk.get("chunk_text")
            if not isinstance(score, (int, float)) or isinstance(score, bool) or not isinstance(snippet, str):
                continue
            if float(score) < min_score:
                continue
            existing = by_file.get(path_key)
            if existing is None or score > existing["best_score"]:
                by_file[path_key] = {
                    "file_path": str(resolved),
                    "file_name": resolved.name,
                    "best_score": round(float(score), 4),
                    "top_snippet": snippet[:400],
                }

    results = sorted(by_file.values(), key=lambda item: item["best_score"], reverse=True)[:n_results]
    return {
        "query": normalized_query,
        "results": results,
        "total_indexed_chunks": total_chunks,
        "note": None if results else "No results were above the similarity threshold. Try different keywords or re-index the approved folders.",
    }
