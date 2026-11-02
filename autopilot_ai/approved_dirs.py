"""Persistent, inspectable list of directories explicitly approved for search."""

import json
import os
from pathlib import Path


DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "data" / "approved_directories.json"


class ApprovedDirectoryError(ValueError):
    pass


def _normalize_existing_directory(value: str | Path) -> Path:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ApprovedDirectoryError(f"Approved directory does not exist or cannot be resolved: {value}") from exc
    if not path.is_dir():
        raise ApprovedDirectoryError(f"Approved path is not a directory: {path}")
    return path


def _normalize_configured_path(value: str | Path) -> Path:
    try:
        path = Path(value).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise ApprovedDirectoryError(f"Approved directory cannot be resolved: {value}") from exc
    if path.exists() and not path.is_dir():
        raise ApprovedDirectoryError(f"Approved path is not a directory: {path}")
    return path


def _key(path: Path) -> str:
    return os.path.normcase(str(path))


def load_approved_dirs(config_path: Path = DEFAULT_CONFIG) -> list[Path]:
    if not config_path.exists():
        return []
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ApprovedDirectoryError(f"Could not read approved-directory list: {exc}") from exc
    if not isinstance(payload, list) or any(not isinstance(item, str) for item in payload):
        raise ApprovedDirectoryError("Approved-directory list must be a JSON array of path strings.")

    directories: list[Path] = []
    seen: set[str] = set()
    for item in payload:
        # Retain removed/missing roots in the listing so users can inspect and unapprove them.
        path = _normalize_configured_path(item)
        if _key(path) not in seen:
            directories.append(path)
            seen.add(_key(path))
    return directories


def save_approved_dirs(directories: list[Path], config_path: Path = DEFAULT_CONFIG) -> list[Path]:
    normalized: list[Path] = []
    seen: set[str] = set()
    for item in directories:
        path = _normalize_configured_path(item)
        if _key(path) not in seen:
            normalized.append(path)
            seen.add(_key(path))

    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = config_path.with_suffix(config_path.suffix + ".tmp")
    temporary.write_text(json.dumps([str(path) for path in normalized], indent=2), encoding="utf-8")
    temporary.replace(config_path)
    return normalized


def add_approved_dirs(directories: list[Path], config_path: Path = DEFAULT_CONFIG) -> list[Path]:
    current = load_approved_dirs(config_path)
    new_roots = [_normalize_existing_directory(item) for item in directories]
    return save_approved_dirs([*current, *new_roots], config_path)


def remove_approved_dirs(directories: list[Path], config_path: Path = DEFAULT_CONFIG) -> list[Path]:
    current = load_approved_dirs(config_path)
    remove_keys = {os.path.normcase(str(Path(item).expanduser().resolve())) for item in directories}
    return save_approved_dirs([path for path in current if _key(path) not in remove_keys], config_path)


def clear_approved_dirs(config_path: Path = DEFAULT_CONFIG) -> None:
    save_approved_dirs([], config_path)
