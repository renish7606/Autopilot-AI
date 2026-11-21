"""Extract, chunk, embed, and store text from approved workspace roots."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .embedder import CHUNK_OVERLAP, CHUNK_SIZE, chunk_text, embed_texts
from .store import collection_stats, delete_file_chunks, upsert_chunks

_INDEXABLE_SUFFIXES = frozenset({
    ".txt", ".md", ".markdown", ".rst", ".log", ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".csv", ".tsv", ".html", ".htm", ".css",
    ".xml", ".sql", ".sh", ".ps1", ".bat", ".cmd", ".c", ".h", ".cpp", ".hpp", ".cs", ".java",
    ".go", ".rs", ".rb", ".php",
})
_MAX_FILE_BYTES = 500_000
_SKIPPED_DIRECTORY_NAMES = frozenset({"__pycache__", "node_modules", ".venv", "venv", "env", ".tox"})


def _extract_text(file_path: Path) -> str | None:
    """Read supported UTF-8 text or extract PDF text; ignore encrypted/bad PDFs."""
    suffix = file_path.suffix.casefold()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(file_path)
            if reader.is_encrypted:
                return None
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception:
            return None
    if suffix not in _INDEXABLE_SUFFIXES:
        return None
    try:
        return file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _embed_chunks(chunks: list[str], batch_size: int) -> list[list[float]]:
    embeddings: list[list[float]] = []
    for start in range(0, len(chunks), batch_size):
        embeddings.extend(embed_texts(chunks[start:start + batch_size]))
    if len(embeddings) != len(chunks):
        raise RuntimeError("Ollama returned an unexpected number of embeddings.")
    return embeddings


def index_workspace(
    workspace: Path,
    extra_dirs: list[Path] | None = None,
    batch_size: int = 10,
) -> dict[str, Any]:
    """Index supported files from the workspace and explicitly approved roots only."""
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
        raise ValueError("batch_size must be a positive integer.")

    roots: list[Path] = []
    errors: list[str] = []
    seen_roots: set[str] = set()
    for configured_root in [workspace, *(extra_dirs or [])]:
        try:
            root = Path(configured_root).expanduser().resolve(strict=True)
            if not root.is_dir():
                errors.append(f"Approved indexing root is not a directory: {root}")
                continue
            key = os.path.normcase(str(root))
            if key not in seen_roots:
                roots.append(root)
                seen_roots.add(key)
        except (OSError, RuntimeError) as exc:
            errors.append(f"Could not open approved indexing root {configured_root}: {exc}")

    indexed = 0
    skipped = 0
    seen_files: set[str] = set()

    for root in roots:
        def record_walk_error(error: OSError) -> None:
            errors.append(f"Could not scan a folder under {root}: {error}")

        for current, directories, filenames in os.walk(root, topdown=True, followlinks=False, onerror=record_walk_error):
            current_path = Path(current)
            safe_directories = []
            for directory_name in sorted(directories, key=str.casefold):
                directory = current_path / directory_name
                if directory.is_symlink() or directory_name.casefold() in _SKIPPED_DIRECTORY_NAMES or directory_name.startswith("."):
                    skipped += 1
                    continue
                safe_directories.append(directory_name)
            directories[:] = safe_directories

            for filename in sorted(filenames, key=str.casefold):
                file_path = current_path / filename
                if file_path.is_symlink() or file_path.suffix.casefold() not in _INDEXABLE_SUFFIXES | {".pdf"}:
                    continue
                try:
                    canonical_path = file_path.resolve(strict=True)
                    canonical_path.relative_to(root)
                    size = canonical_path.stat().st_size
                except (OSError, RuntimeError, ValueError):
                    skipped += 1
                    continue
                if not canonical_path.is_file() or size > _MAX_FILE_BYTES:
                    skipped += 1
                    continue
                file_key = os.path.normcase(str(canonical_path))
                if file_key in seen_files:
                    continue
                seen_files.add(file_key)

                text = _extract_text(canonical_path)
                if not text or not text.strip():
                    skipped += 1
                    continue
                chunks = chunk_text(text, CHUNK_SIZE, CHUNK_OVERLAP)
                if not chunks:
                    skipped += 1
                    continue
                try:
                    embeddings = _embed_chunks(chunks, batch_size)
                    upsert_chunks(str(canonical_path), chunks, embeddings, root_path=str(root))
                    indexed += 1
                except Exception as exc:
                    errors.append(f"{canonical_path.name}: {exc}")
                    skipped += 1

    try:
        stats = collection_stats()
        total_chunks = stats["total_chunks"]
    except RuntimeError as exc:
        errors.append(str(exc))
        total_chunks = 0
    return {
        "indexed_files": indexed,
        "skipped_files": skipped,
        "total_chunks": total_chunks,
        "errors": errors[:20],
        "error_count": len(errors),
    }


def index_single_file(file_path: Path, root_path: Path | None = None) -> dict[str, Any]:
    """Index one regular supported file, optionally associating it with its approved root."""
    if not file_path.is_file() or file_path.is_symlink():
        return {"indexed": False, "reason": "Not a regular file."}
    try:
        canonical_path = file_path.resolve(strict=True)
        size = canonical_path.stat().st_size
    except (OSError, RuntimeError) as exc:
        return {"indexed": False, "reason": f"Could not inspect file: {exc}"}
    if size > _MAX_FILE_BYTES:
        return {"indexed": False, "reason": "File exceeds the 500 KB indexing limit."}
    text = _extract_text(canonical_path)
    if not text or not text.strip():
        delete_file_chunks(str(canonical_path))
        return {"indexed": False, "reason": "No extractable text."}
    chunks = chunk_text(text)
    embeddings = _embed_chunks(chunks, 10)
    selected_root = Path(root_path).resolve() if root_path is not None else canonical_path.parent
    try:
        canonical_path.relative_to(selected_root)
    except ValueError as exc:
        raise ValueError("File must be inside the selected indexing root.") from exc
    upsert_chunks(str(canonical_path), chunks, embeddings, root_path=str(selected_root))
    return {"indexed": True, "file": str(canonical_path), "chunks": len(chunks)}
