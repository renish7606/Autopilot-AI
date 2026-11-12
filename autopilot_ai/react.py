"""Bounded evidence-driven file workflow for iterative search, read, and copy."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from .agent import _resolve_read_scope, _resolve_search_scope
from .contracts import PlanStep
from .policy import PathPolicy, PolicyError
from .tools import ToolArgumentsError, ToolRegistry
from .verifier import verify_copied_file, verify_file_contents, verify_file_search


@dataclass(frozen=True)
class FileWorkflow:
    extension: str
    source_folder: str
    phrase: str
    destination_folder: str


_WORKFLOW = re.compile(
    r"^\s*(?:please\s+)?find\s+(?:all\s+)?(?P<kind>pdfs?|text\s+files?)\s+"
    r"in\s+(?P<source>.+?),\s*(?:check|see|read)\s+which\s+ones?\s+mention(?:s)?\s+"
    r"(?P<phrase>['\"]?[^,'\".!?]+?['\"]?),?\s+and\s+copy\s+them\s+to\s+(?P<destination>.+?)\s*[.!]?\s*$",
    re.IGNORECASE,
)


def parse_file_workflow(goal: str) -> FileWorkflow | None:
    """Recognize the supported iterative file-inspection workflow."""
    match = _WORKFLOW.fullmatch(goal)
    if not match:
        return None
    kind = " ".join(match.group("kind").casefold().split())
    extension = ".pdf" if kind in {"pdf", "pdfs"} else ".txt"
    source = match.group("source").strip().strip("\"'")
    phrase = match.group("phrase").strip().strip("\"'").strip()
    destination = match.group("destination").strip().strip("\"'").rstrip(".")
    if not source or not phrase or not destination:
        return None
    return FileWorkflow(extension, source, phrase, destination)


def run_file_workflow(
    goal: str,
    policy: PathPolicy,
    registry: ToolRegistry,
    approved: bool,
) -> dict[str, Any] | None:
    """Run a bounded search/read/filter/copy loop; return None for other requests."""
    workflow = parse_file_workflow(goal)
    if workflow is None:
        return None

    observations: list[dict[str, Any]] = []
    try:
        search_arguments = _resolve_search_scope(
            policy,
            {"path": workflow.source_folder, "root_index": 0, "query": "", "match_mode": "exact", "extensions": [workflow.extension], "max_results": 100},
        )
        search_arguments = registry.validate_arguments("search_files", search_arguments)
    except (PolicyError, ToolArgumentsError, OSError, RuntimeError, ValueError) as exc:
        return {"status": "denied", "goal": goal, "message": f"Search folder rejected: {exc}", "workflow": "react_file_filter_copy"}

    roots = [policy.approved_roots[search_arguments["root_index"]]]
    search_result = registry.execute("search_files", search_arguments)
    search_verification = verify_file_search(
        search_result, roots, "", "exact", search_arguments["max_results"], search_arguments["extensions"]
    )
    observations.append({
        "action": "search_files",
        "tool_result": search_result.model_dump(),
        "verification": search_verification.model_dump(),
    })
    if search_result.status != "success":
        return _report(goal, "failed", observations, "The file search failed or could not be independently verified.")
    if search_result.output.get("complete") is not True:
        return _report(goal, "partially_completed", observations, "The search or its independent verification was incomplete, so no files were copied.")
    if not search_verification.passed:
        return _report(goal, "failed", observations, "The file search failed or could not be independently verified.")

    selected: list[tuple[Path, int]] = []
    for match_path in search_result.output.get("matches", []):
        try:
            read_arguments = _resolve_read_scope(policy, {"path": match_path, "root_index": 0, "max_characters": 50000, "page_number": None})
            read_arguments = registry.validate_arguments("read_file_contents", read_arguments)
            file_path = policy.resolve_path(read_arguments["path"], read_arguments["root_index"])
        except (PolicyError, ToolArgumentsError, OSError, RuntimeError, ValueError) as exc:
            return _report(goal, "failed", observations, f"A search result could not be safely resolved for reading: {exc}")
        read_result = registry.execute("read_file_contents", read_arguments)
        read_verification = verify_file_contents(read_result, file_path, read_arguments["max_characters"], None)
        observations.append({
            "action": "read_file_contents",
            "path": str(file_path),
            "tool_result": read_result.model_dump(),
            "verification": read_verification.model_dump(),
        })
        if read_result.status != "success" or not read_verification.passed:
            return _report(goal, "partially_completed" if selected else "failed", observations, f"Could not independently verify reading {file_path}; stopped the workflow.")
        if read_result.output.get("truncated"):
            return _report(goal, "partially_completed", observations, f"The contents of {file_path} exceeded the read limit; no files were copied because its full contents could not be checked.")
        if workflow.phrase.casefold() in read_result.output.get("content", "").casefold():
            selected.append((file_path, read_arguments["root_index"]))

    destination_text = workflow.destination_folder.replace("\\", "/")
    primary_name = policy.approved_root.name
    if destination_text.casefold().startswith(primary_name.casefold() + "/"):
        destination_text = destination_text[len(primary_name) + 1:]
    destination_relative = Path(destination_text)
    try:
        if destination_relative.is_absolute() or destination_relative.drive or ".." in destination_relative.parts:
            raise PolicyError("The destination must be a relative folder inside the primary approved workspace.")
        destination = policy.resolve_path(str(destination_relative), 0)
        if destination == policy.approved_root:
            raise PolicyError("Choose a destination subfolder inside the workspace.")
        if destination.exists() and not destination.is_dir():
            raise PolicyError("The destination exists but is not a directory.")
        if not destination.exists() and destination.parent != policy.approved_root and not destination.parent.is_dir():
            raise PolicyError("Only one new destination folder level can be created at a time.")
    except (OSError, RuntimeError, ValueError) as exc:
        return _report(goal, "denied", observations, f"Destination folder rejected: {exc}")

    filenames = [source.name.casefold() for source, _ in selected]
    if len(filenames) != len(set(filenames)):
        return _report(goal, "clarification_required", observations, "Matching files share a filename, so copying all of them would collide. Search a narrower source folder or resolve the duplicates first.")
    if destination.is_dir():
        existing_targets = [destination / source.name for source, _ in selected if (destination / source.name).exists()]
        if existing_targets:
            return _report(goal, "denied", observations, f"Copy would overwrite an existing destination ({existing_targets[0]}). No files were copied.")

    planned_actions: list[dict[str, str]] = []
    if selected and not destination.exists():
        planned_actions.append({"action": "create_directory", "path": str(destination)})
    for source, _ in selected:
        planned_actions.append({"action": "copy_file", "source": str(source), "destination": str(destination / source.name)})
    if not approved and planned_actions:
        report = _report(goal, "approval_required", observations, "The matching files are ready to copy. Review the planned changes, then rerun with --approve.")
        report["planned_actions"] = planned_actions
        report["plan"] = {
            "step_id": "react-mutation",
            "description": f"Create the destination if needed and copy {len(selected)} matching file(s).",
            "tool_name": "copy_file",
            "arguments": {"destination_folder": str(destination), "file_count": len(selected)},
            "risk_level": "medium",
            "requires_approval": True,
        }
        return report

    if selected and planned_actions and not destination.exists():
        try:
            destination.mkdir()
        except OSError as exc:
            return _report(goal, "failed", observations, f"Could not create destination folder: {exc}")
        observations.append({"action": "create_directory", "path": str(destination), "verified": destination.is_dir()})
        if not destination.is_dir():
            return _report(goal, "failed", observations, "Destination folder creation could not be verified.")

    for source, source_root_index in selected:
        destination_file = destination / source.name
        try:
            source_relative = str(source.relative_to(policy.approved_roots[source_root_index]))
            destination_relative_text = str(destination_file.relative_to(policy.approved_root))
            copy_arguments = registry.validate_arguments("copy_file", {
                "source_path": source_relative,
                "destination_path": destination_relative_text,
                "source_root_index": source_root_index,
            })
            source_path = policy.resolve_path(copy_arguments["source_path"], source_root_index)
            target_path = policy.resolve_new_path(copy_arguments["destination_path"])
        except (PolicyError, ToolArgumentsError, OSError, RuntimeError, ValueError) as exc:
            prior_mutation = any(item.get("action") in {"create_directory", "copy_file"} for item in observations)
            return _report(goal, "partially_completed" if prior_mutation else "denied", observations, f"Copy path rejected: {exc}")
        copy_result = registry.execute("copy_file", copy_arguments)
        copy_verification = verify_copied_file(copy_result, source_path, target_path)
        observations.append({
            "action": "copy_file",
            "source": str(source_path),
            "destination": str(target_path),
            "tool_result": copy_result.model_dump(),
            "verification": copy_verification.model_dump(),
        })
        if copy_result.status != "success" or not copy_verification.passed:
            prior_mutation = any(item.get("action") in {"create_directory", "copy_file"} for item in observations[:-1])
            return _report(goal, "partially_completed" if prior_mutation else "failed", observations, f"Copying {source_path.name} failed or did not verify; stopped the workflow.")

    return _report(goal, "completed", observations, f"Inspected {len(search_result.output.get('matches', []))} file(s); copied {len(selected)} file(s) containing '{workflow.phrase}'.")


def _report(goal: str, status: str, observations: list[dict[str, Any]], summary: str) -> dict[str, Any]:
    return {
        "status": status,
        "goal": goal,
        "planner": "react_bounded",
        "workflow": "react_file_filter_copy",
        "summary": summary,
        "observations": observations,
    }
