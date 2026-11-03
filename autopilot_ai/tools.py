"""Small deterministic tools exposed through a validated registry."""

from pathlib import Path
import os
import hashlib
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .browser import BrowserPolicyError, BrowserToolError, OpenWebpageArgs, open_webpage
from .contracts import ToolResult
from .policy import PathPolicy


class ListDirectoryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(default=".", min_length=1, max_length=300)


class SearchFilesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(default=".", min_length=1, max_length=300)
    query: str = Field(min_length=1, max_length=200)
    match_mode: Literal["exact", "contains"] = "exact"
    max_results: int = Field(default=100, ge=1, le=500)


class CreateDirectoryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=300)


class CopyFileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_path: str = Field(min_length=1, max_length=300)
    destination_path: str = Field(min_length=1, max_length=300)
    source_root_index: int = Field(default=0, ge=0)


def _create_directory(policy: PathPolicy, path: str) -> dict[str, Any]:
    destination = policy.resolve_new_path(path)
    destination.mkdir()
    return {"path": str(destination), "created": destination.is_dir()}


def _copy_file(policy: PathPolicy, source_path: str, destination_path: str, source_root_index: int) -> dict[str, Any]:
    source = policy.resolve_path(source_path, source_root_index)
    if not source.is_file():
        raise ToolError("The source must be an existing regular file.")
    destination = policy.resolve_new_path(destination_path)
    digest = hashlib.sha256()
    size = 0
    try:
        with source.open("rb") as source_stream, destination.open("xb") as destination_stream:
            while chunk := source_stream.read(1024 * 1024):
                destination_stream.write(chunk)
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise ToolError(f"Could not copy file: {exc}") from exc
    return {"source": str(source), "destination": str(destination), "size_bytes": size, "sha256": digest.hexdigest()}


class ToolError(ValueError):
    pass


def _list_directory(policy: PathPolicy, path: str) -> dict[str, Any]:
    directory = policy.resolve(path)
    items = []
    for child in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
        try:
            kind = "directory" if child.is_dir() else "file"
            size = child.stat().st_size if kind == "file" else None
        except OSError as exc:
            raise ToolError(f"Could not inspect {child.name}: {exc}") from exc
        items.append({"name": child.name, "type": kind, "size_bytes": size})
    return {"path": str(directory), "items": items, "count": len(items)}


def _search_files(
    policy: PathPolicy,
    path: str,
    query: str,
    match_mode: str,
    max_results: int,
) -> dict[str, Any]:
    needle = query.casefold().strip()
    if not needle:
        raise ToolError("Search query must contain at least one non-space character.")

    matches: list[str] = []
    seen_matches: set[str] = set()
    roots: list[str] = []
    errors: list[str] = []
    visited = 0
    truncated = False
    for root_index in range(len(policy.approved_roots)):
        root = policy.resolve(path, root_index)
        roots.append(str(root))

        def record_walk_error(error: OSError) -> None:
            errors.append(str(error))

        for current, directories, files in os.walk(root, topdown=True, onerror=record_walk_error, followlinks=False):
            visited += len(directories) + len(files)
            if visited > 100_000:
                truncated = True
                break
            safe_directories = []
            for name in directories:
                directory = Path(current) / name
                if directory.is_symlink():
                    errors.append(f"Skipped symlink directory: {directory}")
                else:
                    safe_directories.append(name)
            directories[:] = sorted(safe_directories, key=str.casefold)
            for name in sorted(files, key=str.casefold):
                candidate = Path(current) / name
                if candidate.is_symlink():
                    errors.append(f"Skipped symlink file: {candidate}")
                    continue
                folded_name = name.casefold()
                matched = folded_name == needle if match_mode == "exact" else needle in folded_name
                if not matched:
                    continue
                full_path = str(candidate.resolve())
                match_key = os.path.normcase(full_path)
                if match_key in seen_matches:
                    continue
                seen_matches.add(match_key)
                matches.append(full_path)
                if len(matches) > max_results:
                    truncated = True
                    break
            if truncated:
                break
        if truncated:
            break
    matches = sorted(matches[:max_results], key=str.casefold)
    return {
        "query": query,
        "match_mode": match_mode,
        "roots": roots,
        "matches": matches,
        "count": len(matches),
        "truncated": truncated,
        "complete": not truncated and not errors,
        "errors": errors,
        "entries_scanned": min(visited, 100_000),
    }


class ToolRegistry:
    """Allowlisted tool dispatch; callers cannot supply executable code."""

    def __init__(self, policy: PathPolicy) -> None:
        self._policy = policy
        self._tools: dict[str, tuple[type[BaseModel], Callable[..., dict[str, Any]]]] = {
            "list_directory": (ListDirectoryArgs, _list_directory),
            "search_files": (SearchFilesArgs, _search_files),
            "create_directory": (CreateDirectoryArgs, _create_directory),
            "copy_file": (CopyFileArgs, _copy_file),
            "open_webpage": (OpenWebpageArgs, lambda policy, url: open_webpage(url)),
        }

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        entry = self._tools.get(name)
        if entry is None:
            return ToolResult(status="denied", error_code="unknown_tool")
        schema, function = entry
        try:
            validated = schema.model_validate(arguments)
            output = function(self._policy, **validated.model_dump())
            return ToolResult(status="success", output=output)
        except ValidationError as exc:
            return ToolResult(status="denied", error_code="invalid_arguments", output={"detail": exc.errors(include_url=False)})
        except BrowserPolicyError as exc:
            return ToolResult(status="denied", error_code="browser_policy_denied", output={"detail": str(exc)})
        except BrowserToolError as exc:
            return ToolResult(status="error", error_code="browser_error", output={"detail": str(exc)})
        except (ToolError, ValueError, OSError) as exc:
            return ToolResult(status="denied", error_code="policy_or_tool_error", output={"detail": str(exc)})


def independently_list_names(directory: Path) -> list[str]:
    """Fresh filesystem observation used by the verifier, separate from tool output."""
    return sorted((entry.name for entry in directory.iterdir()), key=str.casefold)
