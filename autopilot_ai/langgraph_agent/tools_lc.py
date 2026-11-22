"""LangChain wrappers around AutoPilot's validated and verified tool registry."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from autopilot_ai.approved_dirs import load_approved_dirs
from autopilot_ai.contracts import ToolResult, Verification
from autopilot_ai.policy import PathPolicy, PolicyError, command_risk
from autopilot_ai.tools import ToolArgumentsError, ToolRegistry
from autopilot_ai.verifier import (
    verify_app_launch,
    verify_command_execution,
    verify_copied_file,
    verify_created_directory,
    verify_directory_listing,
    verify_desktop_action,
    verify_desktop_inspection,
    verify_file_contents,
    verify_file_search,
    verify_semantic_search,
    verify_web_search,
    verify_webpage_open,
    verify_written_file,
)


_APPROVAL_TOOLS = frozenset({
    "write_file", "copy_file", "create_directory", "desktop_action", "launch_app", "run_command",
})


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def _approval_description(name: str, arguments: dict[str, Any]) -> str:
    visible = dict(arguments)
    if isinstance(visible.get("content"), str) and len(visible["content"]) > 240:
        visible["content"] = visible["content"][:240] + "… (truncated)"
    return f"Approve {name}: {json.dumps(visible, ensure_ascii=False, default=str)}"


def _registry(workspace: str) -> tuple[ToolRegistry, PathPolicy]:
    policy = PathPolicy(Path(workspace), load_approved_dirs())
    return ToolRegistry(policy), policy


def _relative_approved_path(path: str, policy: PathPolicy) -> tuple[str, int]:
    """Convert an exact observed absolute path into an approved-root-relative path."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() and not candidate.drive:
        if ".." in candidate.parts:
            raise PolicyError("Parent-directory traversal is not allowed in approved paths.")
        parts = candidate.parts
        if len(parts) > 1:
            alias_index = next(
                (index for index, root in enumerate(policy.approved_roots) if parts[0].casefold() == root.name.casefold()),
                None,
            )
            if alias_index is not None:
                # Prefer a real workspace-relative path if one exists; otherwise
                # interpret a configured root name (for example Downloads) as
                # an approved-root alias.
                workspace_candidate = policy.resolve_path(path, 0)
                if not workspace_candidate.exists():
                    root_relative = str(Path(*parts[1:]))
                    policy.resolve_path(root_relative, alias_index)
                    return root_relative, alias_index
        return path, 0
    resolved = candidate.resolve(strict=True)
    for root_index, root in enumerate(policy.approved_roots):
        try:
            return str(resolved.relative_to(root)), root_index
        except ValueError:
            continue
    raise PolicyError("The requested path is outside the approved directories.")


def _call_tool(
    registry: ToolRegistry,
    policy: PathPolicy,
    name: str,
    arguments: dict[str, Any],
    approved: bool,
    approved_action: dict[str, Any] | None = None,
) -> str:
    """Validate, execute and independently verify one registered operation."""
    try:
        validated = registry.validate_arguments(name, arguments)
    except ToolArgumentsError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error.get('loc', [])) or 'arguments'}: {error.get('msg', 'invalid value')}"
            for error in exc.detail
        )
        return _json({"status": "denied", "message": f"Invalid {name} arguments: {details}", "tool_name": name})
    except (TypeError, ValueError) as exc:
        return _json({"status": "denied", "message": f"Invalid {name} arguments: {exc}", "tool_name": name})

    if name in _APPROVAL_TOOLS and not approved:
        plan = {
            "tool_name": name,
            "description": _approval_description(name, validated),
            "arguments": validated,
            "risk_level": "medium",
            "requires_approval": True,
        }
        return _json({
            "status": "denied",
            "requires_approval": True,
            "message": f"{name} requires explicit user approval before execution.",
            "plan": plan,
            "tool_name": name,
            "arguments": validated,
        })

    if name in _APPROVAL_TOOLS and approved_action is not None:
        expected_name = approved_action.get("tool_name")
        expected_arguments = approved_action.get("arguments")
        if expected_name != name or expected_arguments != validated:
            return _json({
                "status": "denied",
                "message": "This action differs from the operation shown for approval; no operation was performed.",
                "tool_name": name,
                "arguments": validated,
            })

    if name == "run_command":
        try:
            risk, _ = command_risk(validated["command"])
        except PolicyError as exc:
            return _json({"status": "denied", "message": str(exc), "tool_name": name, "arguments": validated})
        # LangGraph must not turn a general-purpose language model into an
        # unrestricted shell. Permit only the deterministic inspection allowlist.
        if risk != "low":
            return _json({
                "status": "denied",
                "message": "This command is outside AutoPilot's safe inspection-command allowlist.",
                "tool_name": name,
                "arguments": validated,
            })

    result = registry.execute(name, validated)
    verification = _verify(registry, policy, name, validated, result)
    return _json({
        "status": result.status if verification.passed else ("failed" if result.status == "success" else result.status),
        "tool_name": name,
        "arguments": validated,
        "tool_result": result.model_dump(mode="json"),
        "verification": verification.model_dump(mode="json"),
    })


def _verify(
    registry: ToolRegistry,
    policy: PathPolicy,
    name: str,
    arguments: dict[str, Any],
    result: ToolResult,
) -> Verification:
    """Dispatch existing independent verifiers for each supported LangGraph tool."""
    try:
        if name == "list_directory":
            directory = policy.resolve(arguments["path"], 0)
            return verify_directory_listing(result, directory)
        if name == "search_files":
            indexes = range(len(policy.approved_roots)) if arguments["root_index"] is None else (arguments["root_index"],)
            roots = [policy.resolve(arguments["path"], index) for index in indexes]
            return verify_file_search(result, roots, arguments["query"], arguments["match_mode"], arguments["max_results"], arguments["extensions"])
        if name == "semantic_search":
            return verify_semantic_search(result, arguments["query"], policy.approved_roots)
        if name == "read_file_contents":
            target = policy.resolve_path(arguments["path"], 0)
            return verify_file_contents(result, target, arguments["max_characters"], arguments["page_number"])
        if name == "search_web":
            return verify_web_search(result, arguments["query"], arguments["max_results"])
        if name == "open_webpage":
            return verify_webpage_open(result, arguments["url"])
        if name == "inspect_active_window":
            return verify_desktop_inspection(result)
        if name == "write_file":
            target = policy.resolve_path(arguments["path"], arguments["root_index"])
            return verify_written_file(result, target, arguments["content"], arguments["mode"])
        if name == "create_directory":
            target = policy.resolve_path(arguments["path"], 0)
            return verify_created_directory(result, target)
        if name == "copy_file":
            source = policy.resolve_path(arguments["source_path"], arguments["source_root_index"])
            target = policy.resolve_path(arguments["destination_path"], 0)
            return verify_copied_file(result, source, target)
        if name == "run_command":
            return verify_command_execution(result)
        if name == "launch_app":
            return verify_app_launch(result)
        if name == "desktop_action":
            return verify_desktop_action(result, arguments["action_type"], arguments["x"], arguments["y"])
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        return Verification(passed=False, summary=f"Could not independently verify {name}.", evidence={"error": str(exc)})
    return Verification(passed=False, summary=f"No independent verifier is registered for {name}.")


def make_tools(
    workspace: str,
    approved: bool = False,
    approved_action: dict[str, Any] | None = None,
    goal: str = "",
):
    """Create per-run tools bound to the validated workspace and approval state."""
    registry, policy = _registry(workspace)
    recent_search_matches: list[str] = []
    goal_requests_first_match = bool(
        re.search(r"\b(?:read|open|inspect)\b.{0,50}\bfirst(?:\s+(?:one|file|result|match))?\b", goal, re.IGNORECASE)
    )

    def execute(name: str, arguments: dict[str, Any]) -> str:
        return _call_tool(registry, policy, name, arguments, approved, approved_action)

    def readable_result_path(requested_path: str) -> tuple[str, int] | None:
        """Resolve follow-up reads only to real approved files or verified search results."""
        normalized: tuple[str, int] | None = None
        try:
            candidate_path, candidate_root = _relative_approved_path(requested_path, policy)
            candidate = policy.resolve_path(candidate_path, candidate_root)
            if candidate.is_file() and not candidate.is_symlink():
                normalized = (candidate_path, candidate_root)
        except (OSError, RuntimeError, ValueError):
            pass
        if normalized is not None:
            return normalized

        if not recent_search_matches:
            return None
        requested_name = Path(requested_path.replace("\\", "/")).name.casefold()
        exact_matches = [
            result_path for result_path in recent_search_matches
            if Path(result_path).name.casefold() == requested_name
        ]
        if len(exact_matches) == 1:
            selected = exact_matches[0]
        elif goal_requests_first_match:
            selected = recent_search_matches[0]
        elif len(exact_matches) > 1:
            return None
        else:
            return None
        try:
            relative_path, root_index = _relative_approved_path(selected, policy)
            resolved = policy.resolve_path(relative_path, root_index)
        except (OSError, RuntimeError, ValueError):
            return None
        return (relative_path, root_index) if resolved.is_file() and not resolved.is_symlink() else None

    @tool
    def list_directory(path: str | None = None) -> str:
        """List entries immediately inside the primary approved workspace. Leave path blank for its root."""
        safe_path = path.strip() if isinstance(path, str) and path.strip() else "."
        return execute("list_directory", {"path": safe_path})

    @tool
    def search_files(query: str | None = None, path: str | None = None, match_mode: str | None = None, extensions: str | None = None, max_results: int | None = None) -> str:
        """Find filenames in approved folders. Use query for a name, or extensions as comma-separated suffixes like '.pdf,.docx'. Use list_directory to list files without a search filter."""
        query = query if isinstance(query, str) else ""
        selected_path = path.strip() if isinstance(path, str) and path.strip() else "."
        match_mode = match_mode if match_mode in {"exact", "contains"} else "contains"
        extensions = extensions if isinstance(extensions, str) else ""
        max_results = max_results if isinstance(max_results, int) and not isinstance(max_results, bool) else 50
        selected_root = None
        if selected_path != "." and not Path(selected_path).is_absolute():
            root_alias = next((i for i, root in enumerate(policy.approved_roots) if Path(selected_path).name.casefold() == root.name.casefold()), None)
            if root_alias is not None:
                selected_path, selected_root = ".", root_alias
        extension_values = [item.strip() for item in extensions.split(",") if item.strip()]
        response = execute("search_files", {
            "path": selected_path, "root_index": selected_root, "query": query, "match_mode": match_mode,
            "extensions": extension_values, "max_results": max_results,
        })
        recent_search_matches.clear()
        try:
            payload = json.loads(response)
            result = payload.get("tool_result", {})
            verification = payload.get("verification", {})
            matches = result.get("output", {}).get("matches", [])
            if (
                result.get("status") == "success"
                and verification.get("passed") is True
                and isinstance(matches, list)
            ):
                recent_search_matches.extend(path for path in matches if isinstance(path, str))
        except (AttributeError, json.JSONDecodeError, TypeError):
            pass
        return response

    @tool
    def semantic_search(query: str, n_results: int = 5) -> str:
        """Find approved files by the meaning of their indexed contents, returning snippets."""
        return execute("semantic_search", {"query": query, "n_results": n_results, "min_score": 0.30})

    @tool
    def read_file_contents(path: str, max_characters: int = 8000) -> str:
        """Read supported text or PDF content from a relative path or exact path returned by search."""
        resolved = readable_result_path(path)
        if resolved is None:
            return _json({
                "status": "denied",
                "message": "The requested read path is not an existing approved file or a verified recent search result.",
                "tool_name": "read_file_contents",
            })
        relative_path, root_index = resolved
        return execute("read_file_contents", {
            "path": relative_path, "root_index": root_index, "max_characters": max_characters, "page_number": None,
        })

    @tool
    def search_web(query: str, max_results: int = 5) -> str:
        """Search public web results and return titles, snippets and URLs."""
        return execute("search_web", {"query": query, "max_results": max_results})

    @tool
    def open_webpage(url: str) -> str:
        """Read visible text from a public HTTP or HTTPS page without clicking or submitting forms."""
        return execute("open_webpage", {"url": url})

    @tool
    def inspect_active_window(max_controls: int = 60) -> str:
        """Inspect the active Windows window using supported UI Automation or screenshot fallback."""
        return execute("inspect_active_window", {"max_controls": max_controls, "screenshot_fallback": True})

    @tool
    def write_file(path: str, content: str, mode: str = "create_new") -> str:
        """Create or update a text file in the workspace. Requires explicit approval."""
        return execute("write_file", {"path": path, "content": content, "mode": mode, "root_index": 0})

    @tool
    def copy_file(source_path: str, destination_path: str) -> str:
        """Copy a file returned by search to a new workspace path. Requires approval."""
        try:
            relative_source, source_root_index = _relative_approved_path(source_path, policy)
        except (OSError, RuntimeError, ValueError) as exc:
            return _json({"status": "denied", "message": str(exc), "tool_name": "copy_file"})
        return execute("copy_file", {
            "source_path": relative_source, "destination_path": destination_path, "source_root_index": source_root_index,
        })

    @tool
    def create_directory(path: str) -> str:
        """Create a directory in the workspace. Requires explicit approval."""
        return execute("create_directory", {"path": path})

    @tool
    def desktop_action(action_type: str, x: int | None = None, y: int | None = None, text: str | None = None, keys: list[str] | None = None) -> str:
        """Perform one bounded desktop click or keyboard action. Requires approval."""
        return execute("desktop_action", {
            "action_type": action_type, "x": x, "y": y, "text": text, "keys": keys or [],
        })

    @tool
    def launch_app(app_name: str, arguments: list[str] | None = None) -> str:
        """Launch an installed Windows application. Requires explicit approval."""
        return execute("launch_app", {"app_name": app_name, "arguments": arguments or []})

    @tool
    def run_command(command: str, working_dir: str = ".", timeout_seconds: int = 30) -> str:
        """Run only a safe allowlisted inspection command in the workspace. Requires approval."""
        return execute("run_command", {
            "command": command, "working_dir": working_dir, "root_index": 0, "timeout_seconds": timeout_seconds,
        })

    @tool
    def finish(answer: str) -> str:
        """Finish the task with a summary grounded in tool observations."""
        return answer

    available = [
        list_directory, search_files, semantic_search, read_file_contents, search_web,
        open_webpage, inspect_active_window, write_file, copy_file, create_directory,
        desktop_action, launch_app, run_command, finish,
    ]
    goal_lower = goal.casefold()
    web_search_requested = bool(re.search(
        r"\b(?:search|look\s+up|google)\b.{0,60}\b(?:the\s+)?(?:web|internet|online|google)\b|\bweb\s+search\b",
        goal_lower,
    ))
    webpage_requested = bool(
        re.search(r"https?://", goal_lower)
        or re.search(r"\b(?:open|visit|read|summarize)\b.{0,40}\b(?:website|webpage|web\s+page|site|url)\b", goal_lower)
    )
    if not web_search_requested:
        available.remove(search_web)
    if not webpage_requested:
        available.remove(open_webpage)
    return available
