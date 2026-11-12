"""Small deterministic tools exposed through a validated registry."""

from pathlib import Path
import os
import hashlib
import re
import shutil
import subprocess
from typing import Any, Callable, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .browser import BrowserPolicyError, BrowserToolError, OpenWebpageArgs, open_webpage
from .contracts import ToolResult
from .desktop import DesktopActionPolicyError, DesktopToolError, desktop_action, inspect_active_window
from .filename_matching import matches_filename
from .policy import PathPolicy, command_risk
from .web_search import SearchWebArgs, WebSearchError, search_web


class ListDirectoryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(default=".", min_length=1, max_length=300)


class SearchFilesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(default=".", min_length=1, max_length=300)
    root_index: int | None = Field(default=None, ge=0)
    query: str = Field(default="", max_length=200)
    match_mode: Literal["exact", "contains"] = "exact"
    extensions: list[str] = Field(default_factory=list, max_length=16)
    max_results: int = Field(default=100, ge=1, le=500)

    @field_validator("extensions")
    @classmethod
    def normalize_extensions(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for extension in value:
            item = extension.strip().casefold()
            if not item.startswith("."):
                item = f".{item}"
            if not re.fullmatch(r"\.[a-z0-9]{1,10}", item):
                raise ValueError("Extensions must be simple suffixes such as .pdf or .docx.")
            if item not in normalized:
                normalized.append(item)
        return normalized


class CreateDirectoryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=300)


class CopyFileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_path: str = Field(min_length=1, max_length=300)
    destination_path: str = Field(min_length=1, max_length=300)
    source_root_index: int = Field(default=0, ge=0)


class WriteFileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str = Field(min_length=1, max_length=300)
    content: str = Field(max_length=100000)
    mode: Literal["create_new", "overwrite", "append"] = "create_new"
    root_index: int = Field(default=0, ge=0)


class InspectActiveWindowArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_controls: int = Field(default=60, ge=1, le=100)
    screenshot_fallback: bool = True


_DESKTOP_KEYS = frozenset(
    {"enter", "esc", "tab", "space", "backspace", "delete", "insert", "home", "end", "pageup", "pagedown",
     "up", "down", "left", "right", "ctrl", "alt", "shift", "win", "command", "capslock", "numlock",
     "scrolllock", "printscreen", "pause", "clear", "help", "apps", "decimal", "add", "subtract", "multiply", "divide",
     *{f"f{index}" for index in range(1, 13)}, *{str(index) for index in range(10)},
     *{chr(code) for code in range(ord("a"), ord("z") + 1)},
     "-", "=", "[", "]", "\\", ";", "'", ",", ".", "/", "`"}
)


class DesktopActionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action_type: Literal["click", "double_click", "right_click", "type_text", "press_key", "hotkey"]
    x: int | None = Field(default=None, ge=0)
    y: int | None = Field(default=None, ge=0)
    text: str | None = Field(default=None, max_length=500)
    keys: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("keys")
    @classmethod
    def validate_keys(cls, values: list[str]) -> list[str]:
        normalized = [key.strip().casefold() for key in values]
        if any(key not in _DESKTOP_KEYS for key in normalized):
            raise ValueError("One or more keys are not supported.")
        if len(set(normalized)) != len(normalized):
            raise ValueError("A key may appear only once in a hotkey.")
        return normalized

    @model_validator(mode="after")
    def validate_action_fields(self) -> "DesktopActionArgs":
        if self.action_type in {"click", "double_click", "right_click"}:
            if self.x is None or self.y is None or self.text is not None or self.keys:
                raise ValueError("Pointer actions require x and y and cannot include text or keys.")
        elif self.action_type == "type_text":
            if not self.text or self.x is not None or self.y is not None or self.keys:
                raise ValueError("type_text requires non-empty text and cannot include coordinates or keys.")
        elif self.action_type == "press_key":
            if len(self.keys) != 1 or self.x is not None or self.y is not None or self.text is not None:
                raise ValueError("press_key requires exactly one key and cannot include coordinates or text.")
        elif self.action_type == "hotkey":
            if not 2 <= len(self.keys) <= 5 or self.x is not None or self.y is not None or self.text is not None:
                raise ValueError("hotkey requires two to five keys and cannot include coordinates or text.")
            if not any(key in {"ctrl", "alt", "shift", "win", "command"} for key in self.keys):
                raise ValueError("hotkey must include a modifier key.")
        return self


class RunCommandArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    command: str = Field(min_length=1, max_length=1000)
    timeout_seconds: int = Field(default=30, ge=1, le=120)
    working_dir: str = Field(default=".", min_length=1, max_length=300)
    root_index: int = Field(default=0, ge=0)


class LaunchAppArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    app_name: str = Field(min_length=1, max_length=100)
    arguments: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("app_name")
    @classmethod
    def validate_app_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or not re.fullmatch(r"[A-Za-z0-9_. -]+", normalized):
            raise ValueError("Application names may contain only letters, numbers, spaces, dots, underscores, and hyphens.")
        return normalized


APP_ALIASES = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "calc": "calc.exe",
    "chrome": "chrome.exe",
    "google chrome": "chrome.exe",
    "edge": "msedge.exe",
    "explorer": "explorer.exe",
    "cmd": "cmd.exe",
    "powershell": "powershell.exe",
    "code": "code.cmd",
    "vscode": "code.cmd",
    "paint": "mspaint.exe",
}


class ReadFileContentsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str = Field(min_length=1, max_length=300)
    root_index: int = Field(default=0, ge=0)
    max_characters: int = Field(default=8000, ge=100, le=50000)
    page_number: int | None = Field(default=None, ge=1)


_TEXT_FILE_SUFFIXES = frozenset({
    ".txt", ".log", ".md", ".markdown", ".rst", ".json", ".csv", ".tsv", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".py", ".pyi", ".js", ".ts", ".tsx", ".jsx", ".html", ".htm", ".css", ".xml", ".sql", ".sh",
    ".ps1", ".bat", ".cmd", ".c", ".h", ".cpp", ".hpp", ".cs", ".java", ".go", ".rs", ".rb", ".php",
})


def _create_directory(policy: PathPolicy, path: str) -> dict[str, Any]:
    destination = policy.resolve_new_path(path)
    destination.mkdir()
    return {"path": str(destination), "created": destination.is_dir()}


def _publish_new_file(temporary: Path, destination: Path, exists_message: str) -> None:
    """Publish a staged file without replacing an existing destination.

    Windows rename is atomic on the same volume and refuses to overwrite an
    existing destination. Use it only when hard-link creation is unavailable
    due to Windows permissions; on POSIX, rename could replace an existing file.
    """
    try:
        os.link(temporary, destination)
    except FileExistsError as exc:
        raise ToolError(exists_message) from exc
    except PermissionError:
        if os.name != "nt":
            raise
        try:
            os.rename(temporary, destination)
        except FileExistsError as exc:
            raise ToolError(exists_message) from exc


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
        _publish_new_file(temporary, destination, "The destination already exists; overwrite is not allowed.")
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


def _write_file(policy: PathPolicy, path: str, content: str, mode: str, root_index: int) -> dict[str, Any]:
    destination = policy.resolve_path(path, root_index)
    if not destination.parent.is_dir():
        raise ToolError("The destination parent must already exist inside an approved directory.")
    exists = destination.exists()
    if mode == "create_new" and exists:
        raise ToolError("The destination already exists; create_new does not overwrite files.")
    if mode == "append" and (not exists or not destination.is_file()):
        raise ToolError("Append requires an existing regular file.")
    if mode == "overwrite" and exists and not destination.is_file():
        raise ToolError("Overwrite requires a regular file destination.")

    content_bytes = content.encode("utf-8")
    try:
        temporary = destination.with_name(f".{destination.name}.tmp.{uuid4().hex}")
        temporary_created = False
        published = False
        cleanup_warning = None
        digest = hashlib.sha256()
        size_bytes = 0
        try:
            with temporary.open("xb") as stream:
                temporary_created = True
                if mode == "append":
                    with destination.open("rb") as source_stream:
                        while chunk := source_stream.read(1024 * 1024):
                            stream.write(chunk)
                            digest.update(chunk)
                            size_bytes += len(chunk)
                stream.write(content_bytes)
                digest.update(content_bytes)
                size_bytes += len(content_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            if mode == "create_new":
                _publish_new_file(temporary, destination, "The destination already exists; create_new does not overwrite files.")
                published = True
            else:
                os.replace(temporary, destination)
                temporary_created = False
                published = True
        finally:
            if temporary_created:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError as exc:
                    if published:
                        cleanup_warning = f"Temporary staging file could not be removed: {exc}"
                    else:
                        raise ToolExecutionError(f"Could not remove temporary staging file: {exc}") from exc
    except ToolError:
        raise
    except OSError as exc:
        raise ToolExecutionError(f"Could not write file: {exc}") from exc

    output = {
        "path": str(destination),
        "mode": mode,
        "bytes_written": len(content_bytes),
        "size_bytes": size_bytes,
        "sha256": digest.hexdigest(),
    }
    if cleanup_warning:
        output["warning"] = cleanup_warning
    return output


def _run_command(
    policy: PathPolicy,
    command: str,
    timeout_seconds: int,
    working_dir: str,
    root_index: int,
) -> dict[str, Any]:
    command_risk(command)
    cwd = policy.resolve(working_dir, root_index)
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            encoding="utf-8",
            errors="replace",
        )
        return {
            "command": command,
            "exit_code": proc.returncode,
            "stdout": proc.stdout[:8000],
            "stderr": proc.stderr[:4000],
            "timed_out": False,
            "cwd": str(cwd),
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "Command timed out.")
        return {
            "command": command,
            "exit_code": -1,
            "stdout": stdout[:4000],
            "stderr": stderr[:4000],
            "timed_out": True,
            "cwd": str(cwd),
        }
    except OSError as exc:
        raise ToolExecutionError(f"Command execution failed: {exc}") from exc


def _resolve_app_executable(app_name: str) -> str:
    target = shutil.which(app_name) or APP_ALIASES.get(app_name.casefold())
    if not target:
        raise ToolError(f"Application is not available on PATH: {app_name}")
    executable = shutil.which(target)
    if executable:
        return executable
    for candidate in _windows_app_candidates(target):
        if candidate.is_file():
            return str(candidate)
    raise ToolError(f"Application is not available on PATH or in standard Windows locations: {app_name}")


def _windows_app_candidates(target: str) -> list[Path]:
    """Return safe Windows registry and standard-install candidates for an executable."""
    candidates: list[Path] = []
    try:
        import winreg

        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for key_path in (rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{target}", rf"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\{target}"):
                try:
                    with winreg.OpenKey(hive, key_path) as key:
                        value, _ = winreg.QueryValueEx(key, None)
                    path = Path(str(value).strip('"'))
                    if path.is_file():
                        candidates.append(path)
                except OSError:
                    continue
    except ImportError:
        return candidates
    if target.casefold() == "chrome.exe":
        for variable in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(variable)
            if base:
                candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
    return candidates


def _launch_app(policy: PathPolicy, app_name: str, arguments: list[str]) -> dict[str, Any]:
    executable = _resolve_app_executable(app_name)
    try:
        process = subprocess.Popen([executable, *arguments])
    except OSError as exc:
        raise ToolExecutionError(f"Could not launch {app_name}: {exc}") from exc
    return {"pid": process.pid, "app_name": app_name, "executable_path": executable}


def _read_file_contents(
    policy: PathPolicy,
    path: str,
    root_index: int,
    max_characters: int,
    page_number: int | None,
) -> dict[str, Any]:
    file_path = policy.resolve_path(path, root_index)
    if not file_path.is_file():
        raise ToolError("The requested path must be an existing regular file.")
    try:
        size_bytes = file_path.stat().st_size
        digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        if file_path.suffix.casefold() == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(file_path)
            if reader.is_encrypted:
                raise ToolError("Encrypted PDF files are not supported.")
            if page_number is not None:
                if page_number > len(reader.pages):
                    raise ToolError("The requested PDF page does not exist.")
                content = reader.pages[page_number - 1].extract_text() or ""
            else:
                content = "\n".join(page.extract_text() or "" for page in reader.pages)
            file_type = "pdf"
        else:
            if page_number is not None:
                raise ToolError("page_number is supported only for PDF files.")
            if file_path.suffix.casefold() not in _TEXT_FILE_SUFFIXES:
                raise ToolError("Only supported text, code, data, Markdown, and PDF files can be read.")
            content = file_path.read_text(encoding="utf-8", errors="replace")
            file_type = "text"
    except ToolError:
        raise
    except Exception as exc:
        raise ToolExecutionError(f"Could not read file contents: {exc}") from exc
    return {
        "path": str(file_path),
        "file_type": file_type,
        "page_number": page_number,
        "size_bytes": size_bytes,
        "sha256": digest,
        "content": content[:max_characters],
        "total_characters": len(content),
        "truncated": len(content) > max_characters,
    }


def _desktop_action_tool(
    policy: PathPolicy,
    action_type: str,
    x: int | None,
    y: int | None,
    text: str | None,
    keys: list[str],
) -> dict[str, Any]:
    del policy  # Desktop actions are bounded against the primary display, not filesystem roots.
    return desktop_action(action_type, x, y, text, keys)


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
    extensions: list[str],
    root_index: int | None,
) -> dict[str, Any]:
    needle = query.casefold().strip()
    extensions = [extension.casefold() for extension in extensions]
    if not needle and not extensions:
        raise ToolError("Search query must contain at least one non-space character.")

    matches: list[str] = []
    seen_matches: set[str] = set()
    roots: list[str] = []
    errors: list[str] = []
    visited = 0
    truncated = False
    root_indexes = range(len(policy.approved_roots)) if root_index is None else (root_index,)
    for selected_root_index in root_indexes:
        root = policy.resolve(path, selected_root_index)
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
                matched = matches_filename(name, needle, match_mode, extensions)
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
        "extensions": extensions,
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
            "write_file": (WriteFileArgs, _write_file),
            "open_webpage": (OpenWebpageArgs, lambda policy, url: open_webpage(url)),
            "search_web": (SearchWebArgs, lambda policy, query, max_results: search_web(query, max_results)),
            "inspect_active_window": (InspectActiveWindowArgs, lambda policy, max_controls, screenshot_fallback: inspect_active_window(max_controls, screenshot_fallback)),
            "desktop_action": (DesktopActionArgs, _desktop_action_tool),
            "run_command": (RunCommandArgs, _run_command),
            "launch_app": (LaunchAppArgs, _launch_app),
            "read_file_contents": (ReadFileContentsArgs, _read_file_contents),
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
        except WebSearchError as exc:
            return ToolResult(status="error", error_code="web_search_error", output={"detail": str(exc)})
        except DesktopActionPolicyError as exc:
            return ToolResult(status="denied", error_code="desktop_policy_denied", output={"detail": str(exc)})
        except DesktopToolError as exc:
            return ToolResult(status="error", error_code="desktop_error", output={"detail": str(exc)})
        except ToolExecutionError as exc:
            return ToolResult(status="error", error_code="filesystem_error", output={"detail": str(exc)})
        except (ToolError, ValueError, OSError) as exc:
            return ToolResult(status="denied", error_code="policy_or_tool_error", output={"detail": str(exc)})
