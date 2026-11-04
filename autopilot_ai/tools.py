"""Small deterministic tools exposed through a validated registry."""

from pathlib import Path
import os
import hashlib
from typing import Any, Callable, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .browser import BrowserPolicyError, BrowserToolError, OpenWebpageArgs, open_webpage
from .contracts import ToolResult
from .desktop import DesktopToolError, inspect_active_window
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


class InspectActiveWindowArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_controls: int = Field(default=60, ge=1, le=100)
    screenshot_fallback: bool = True


def _create_directory(policy: PathPolicy, path: str) -> dict[str, Any]:
    destination = policy.resolve_new_path(path)
    destination.mkdir()
    return {"path": str(destination), "created": destination.is_dir()}


def _copy_file(policy: PathPolicy, source_path: str, destination_path: str, source_root_index: int) -> dict[str, Any]:
    source = policy.resolve_path(source_path, source_root_index)
    if not source.is_file():
        raise ToolError("The source must be an existing regular file.")
    destination = policy.resolve_new_path(destination_path)
    # Stage beside the destination, then use a hard link as an atomic,
    # no-overwrite publish. Only the uniquely named staging file is cleaned
    # up on errors, so a file created concurrently at the requested path is
    # never removed by this operation.
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.autopilot-tmp")
    digest = hashlib.sha256()
    size = 0
    temporary_created = False
    cleanup_warning = None
    try:
        with source.open("rb") as source_stream:
            with temporary.open("xb") as destination_stream:
                temporary_created = True
                while chunk := source_stream.read(1024 * 1024):
                    destination_stream.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
        try:
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise ToolError("The destination already exists; overwrite is not allowed.") from exc
    except OSError as exc:
        raise ToolExecutionError(f"Could not copy file: {exc}") from exc
    finally:
        if temporary_created:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                # The published destination remains valid; surface cleanup
                # trouble without misreporting the copy operation itself.
                cleanup_warning = f"Temporary staging file could not be removed: {exc}"
    output = {"source": str(source), "destination": str(destination), "size_bytes": size, "sha256": digest.hexdigest()}
    if cleanup_warning:
        output["warning"] = cleanup_warning
    return output


class ToolError(ValueError):
    pass


class ToolExecutionError(RuntimeError):
    """Raised when a valid operation fails because of an operating-system error."""


class ToolArgumentsError(ValueError):
    """Raised when a proposed tool call does not match its registered schema."""

    def __init__(self, detail: list[dict[str, Any]]) -> None:
        super().__init__("Tool arguments failed schema validation.")
        self.detail = detail


def _list_directory(policy: PathPolicy, path: str) -> dict[str, Any]:
    directory = policy.resolve(path)
    items = []
    for child in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
        try:
            if child.is_symlink():
                kind = "symlink"
                size = child.lstat().st_size
            elif child.is_dir():
                kind = "directory"
                size = None
            else:
                kind = "file"
                size = child.stat().st_size
        except OSError as exc:
            raise ToolExecutionError(f"Could not inspect {child.name}: {exc}") from exc
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
            "inspect_active_window": (InspectActiveWindowArgs, lambda policy, max_controls, screenshot_fallback: inspect_active_window(max_controls, screenshot_fallback)),
        }

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def validate_arguments(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Validate and normalize arguments before policy checks or side effects."""
        entry = self._tools.get(name)
        if entry is None:
            raise ToolArgumentsError([{"type": "unknown_tool", "loc": ["tool_name"], "msg": "Tool is not registered."}])
        schema, _ = entry
        try:
            return schema.model_validate(arguments).model_dump()
        except ValidationError as exc:
            raise ToolArgumentsError(exc.errors(include_url=False)) from exc

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
        except DesktopToolError as exc:
            return ToolResult(status="error", error_code="desktop_error", output={"detail": str(exc)})
        except ToolExecutionError as exc:
            return ToolResult(status="error", error_code="filesystem_error", output={"detail": str(exc)})
        except (ToolError, ValueError, OSError) as exc:
            return ToolResult(status="denied", error_code="policy_or_tool_error", output={"detail": str(exc)})
