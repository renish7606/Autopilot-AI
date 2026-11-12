"""One bounded receive-plan-policy-execute-verify cycle."""

from pathlib import Path
import os
import stat
import time
from typing import Any

from .approved_apps import ApprovedAppError, approve_app, is_app_approved
from .planner import ClarificationRequired, ModelPlannerError, PlanningError, plan_goal_steps, plan_with_ollama, plan_with_ollama_steps
from .policy import PathPolicy, PolicyError, command_risk
from .recovery import MAX_ATTEMPTS, should_retry
from .tools import ToolArgumentsError, ToolRegistry
from .verifier import verify_app_launch, verify_command_execution, verify_copied_file, verify_created_directory, verify_directory_listing, verify_file_contents, verify_file_search, verify_webpage_open, verify_web_search, verify_desktop_action, verify_desktop_inspection, verify_written_file


class AmbiguousSearchScope(ValueError):
    def __init__(self, folder: str, candidates: list[Path]) -> None:
        super().__init__(folder)
        self.folder = folder
        self.candidates = candidates


class AmbiguousReadFile(ValueError):
    def __init__(self, requested: str, candidates: list[Path]) -> None:
        super().__init__(requested)
        self.requested = requested
        self.candidates = candidates


def _resolve_read_scope(policy: PathPolicy, arguments: dict[str, Any]) -> dict[str, Any]:
    """Resolve a read request to one regular file under an approved root."""
    requested_text = str(arguments["path"])
    requested = Path(requested_text).expanduser()
    matches: dict[str, tuple[Path, int, str]] = {}
    if requested.is_absolute() or requested.drive:
        try:
            absolute = requested.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise PolicyError("The requested file does not exist or cannot be resolved.") from exc
        for index, root in enumerate(policy.approved_roots):
            try:
                relative = absolute.relative_to(root)
            except ValueError:
                continue
            relative_text = str(relative) if relative.parts else "."
            candidate = policy.resolve_path(relative_text, index)
            if candidate == absolute and candidate.is_file():
                matches[str(candidate).casefold()] = (candidate, index, relative_text)
    else:
        for index, root in enumerate(policy.approved_roots):
            try:
                candidate = policy.resolve_path(requested_text, index)
            except (PolicyError, OSError):
                continue
            if candidate.is_file():
                matches[str(candidate).casefold()] = (candidate, index, requested_text)

        if len(requested.parts) == 1 and not matches:
            for index, root in enumerate(policy.approved_roots):
                for current, directories, files in os.walk(root, followlinks=False):
                    current_path = Path(current)
                    directories.sort(key=str.casefold)
                    files.sort(key=str.casefold)
                    directories[:] = [
                        name for name in directories
                        if not (current_path / name).is_symlink()
                    ]
                    for filename in files:
                        if filename.casefold() != requested.name.casefold():
                            continue
                        candidate = current_path / filename
                        if candidate.is_symlink() or not candidate.is_file():
                            continue
                        relative_text = str(candidate.relative_to(root))
                        try:
                            safe_candidate = policy.resolve_path(relative_text, index)
                        except (PolicyError, OSError):
                            continue
                        matches[str(safe_candidate).casefold()] = (safe_candidate, index, relative_text)

    candidates = list(matches.values())
    if not candidates:
        raise PolicyError(
            f"No file named '{requested.name}' was found inside the approved directories. "
            "Set --workspace or approve the directory containing the file."
        )
    if len(candidates) > 1:
        raise AmbiguousReadFile(requested_text, [candidate[0] for candidate in candidates])
    file_path, root_index, relative_path = candidates[0]
    return {**arguments, "path": relative_path, "root_index": root_index}


def _resolve_search_scope(policy: PathPolicy, arguments: dict[str, Any]) -> dict[str, Any]:
    """Resolve a named folder only when it identifies one approved location."""
    path = str(arguments.get("path", "."))
    if path in ("", ".") or arguments.get("root_index") is None:
        return arguments

    requested = Path(path).expanduser()
    if requested.is_absolute() or requested.drive:
        try:
            absolute = requested.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise PolicyError("The requested folder does not exist or cannot be resolved.") from exc
        scoped_locations: list[tuple[int, str]] = []
        for index, root in enumerate(policy.approved_roots):
            try:
                relative = absolute.relative_to(root)
            except ValueError:
                continue
            relative_text = str(relative) if relative.parts else "."
            scoped = policy.resolve_path(relative_text, index)
            if scoped == absolute and scoped.is_dir():
                scoped_locations.append((index, relative_text))
        unique_locations = list(dict.fromkeys(scoped_locations))
        if len(unique_locations) != 1:
            raise PolicyError("The absolute folder path must identify one location inside an approved directory.")
        root_index, relative_path = unique_locations[0]
        return {**arguments, "path": relative_path, "root_index": root_index}

    candidates: list[tuple[Path, int, str]] = []
    for index, root in enumerate(policy.approved_roots):
        # A request such as 'in ML folder' can refer to an approved root named
        # ML itself, or to a child folder ML beneath an approved root.
        if len(requested.parts) == 1 and requested.parts[0].casefold() == root.name.casefold():
            candidates.append((root, index, "."))
        try:
            scoped = policy.resolve_path(path, index)
        except OSError as exc:
            raise PolicyError(f"Could not inspect the requested folder under approved root {root}: {exc}") from exc
        try:
            scoped_info = scoped.stat()
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as exc:
            raise PolicyError(f"Could not inspect the requested folder under approved root {root}: {exc}") from exc
        if stat.S_ISDIR(scoped_info.st_mode):
            candidates.append((scoped, index, path))

    unique: dict[str, tuple[Path, int, str]] = {}
    for candidate in candidates:
        unique[str(candidate[0]).casefold()] = candidate
    candidates = list(unique.values())
    if not candidates:
        raise PolicyError(f"No folder named '{path}' was found in the approved directories.")
    if len(candidates) > 1:
        raise AmbiguousSearchScope(path, [candidate[0] for candidate in candidates])
    selected, root_index, relative_path = candidates[0]
    return {**arguments, "path": relative_path, "root_index": root_index}


def run_goal(
    goal: str,
    workspace: Path,
    planner: str = "heuristic",
    approved_dirs: tuple[Path, ...] | list[Path] = (),
    approved: bool = False,
    desktop_delay_seconds: int = 0,
) -> dict[str, Any]:
    try:
        policy = PathPolicy(workspace, approved_dirs)
    except (PolicyError, OSError, ValueError) as exc:
        return {"status": "denied", "goal": goal, "message": f"Approved directory rejected: {exc}"}
    registry = ToolRegistry(policy)
    # Dispatch the supported evidence-driven workflow before single-turn
    # planning can accidentally reduce it to only its first clause.
    if desktop_delay_seconds:
        from .react import parse_file_workflow, run_file_workflow

        if parse_file_workflow(goal) is not None:
            return {"status": "error", "goal": goal, "message": "--delay-seconds can only be used with desktop inspection."}
    else:
        from .react import run_file_workflow

    workflow_report = run_file_workflow(goal, policy, registry, approved)
    if workflow_report is not None:
        return workflow_report
    planner_used = planner
    planner_note: str | None = None
    try:
        if planner == "heuristic":
            steps = plan_goal_steps(goal)
        elif planner == "ollama":
            try:
                expected_steps = plan_goal_steps(goal)
                if len(expected_steps) > 1:
                    proposed_steps = plan_with_ollama_steps(goal, registry.names)
                    if len(proposed_steps) == len(expected_steps) and all(_matches_intent(proposed, expected, registry) for proposed, expected in zip(proposed_steps, expected_steps)):
                        steps = proposed_steps
                    else:
                        steps = expected_steps
                        planner_used = "heuristic_fallback"
                        if len(proposed_steps) != len(expected_steps):
                            mismatch = f"The local model returned {len(proposed_steps)} step(s), but the request needs {len(expected_steps)}."
                        else:
                            mismatch_index = next(index for index, pair in enumerate(zip(proposed_steps, expected_steps), start=1) if not _matches_intent(*pair, registry))
                            proposed, expected = proposed_steps[mismatch_index - 1], expected_steps[mismatch_index - 1]
                            if proposed.tool_name != expected.tool_name:
                                mismatch = f"The local model chose a different tool for step {mismatch_index}."
                            elif proposed.tool_name == "search_files" and str(proposed.arguments.get("query", "")).casefold().strip() != str(expected.arguments.get("query", "")).casefold().strip():
                                mismatch = f"The local model changed the filename for step {mismatch_index}."
                            else:
                                mismatch = f"The local model changed the arguments for step {mismatch_index}."
                        planner_note = f"{mismatch} Used the deterministic plan instead."
                else:
                    proposed = plan_with_ollama(goal, registry.names)
                    expected = expected_steps[0]
                    matches_intent = _matches_intent(proposed, expected, registry)
                    if matches_intent:
                        steps = [proposed]
                    else:
                        steps = [expected]
                        planner_used = "heuristic_fallback"
                        planner_note = "The local model proposal did not match the bounded intent check; used the deterministic plan."
            except (ModelPlannerError, PlanningError) as model_error:
                try:
                    steps = plan_goal_steps(goal)
                except ClarificationRequired as intent_error:
                    return {"status": "clarification_required", "goal": goal, "question": intent_error.question}
                except PlanningError as intent_error:
                    return {"status": "unsupported", "goal": goal, "message": str(intent_error)}
                planner_used = "heuristic_fallback"
                planner_note = f"The local model plan was unusable ({model_error}); used the deterministic plan for this supported request."
        else:
            return {"status": "error", "goal": goal, "message": f"Unknown planner: {planner}"}
    except ModelPlannerError as exc:
        return {"status": "error", "goal": goal, "message": str(exc)}
    except ClarificationRequired as exc:
        return {"status": "clarification_required", "goal": goal, "question": exc.question}
    except PlanningError as exc:
        return {"status": "unsupported", "goal": goal, "message": str(exc)}

    step_reports = []
    if not isinstance(desktop_delay_seconds, int) or isinstance(desktop_delay_seconds, bool) or not 0 <= desktop_delay_seconds <= 30:
        return {"status": "error", "goal": goal, "message": "Desktop delay must be an integer from 0 to 30 seconds."}
    if desktop_delay_seconds and any(step.tool_name != "inspect_active_window" for step in steps):
        return {"status": "error", "goal": goal, "message": "--delay-seconds can only be used with desktop inspection."}
    for step in steps:
        if step.tool_name not in registry.names:
            return {"status": "denied", "goal": goal, "message": "Plan selected a tool that is not registered."}
        if step.tool_name == "search_files":
            try:
                resolved_scope = _resolve_search_scope(policy, step.arguments)
                step = step.model_copy(update={"arguments": resolved_scope})
            except AmbiguousSearchScope as exc:
                return {
                    "status": "clarification_required",
                    "goal": goal,
                    "question": f"More than one approved folder matches '{exc.folder}'. Specify the full path of the folder to search.",
                    "candidates": [str(candidate) for candidate in exc.candidates],
                    "scope_kind": "folder",
                }
            except (TypeError, ValueError, OSError, RuntimeError) as exc:
                return {"status": "denied", "goal": goal, "message": f"Search folder rejected: {exc}"}
        elif step.tool_name == "read_file_contents":
            try:
                resolved_scope = _resolve_read_scope(policy, step.arguments)
                step = step.model_copy(update={"arguments": resolved_scope})
            except AmbiguousReadFile as exc:
                return {
                    "status": "clarification_required",
                    "goal": goal,
                    "question": f"More than one approved file matches '{exc.requested}'. Specify its relative path or full path.",
                    "candidates": [str(candidate) for candidate in exc.candidates],
                    "candidate_kind": "read_file",
                }
            except (TypeError, ValueError, OSError, RuntimeError) as exc:
                return {"status": "denied", "goal": goal, "message": f"File path rejected: {exc}"}
        try:
            validated_arguments = registry.validate_arguments(step.tool_name, step.arguments)
        except ToolArgumentsError as exc:
            return {
                "status": "denied",
                "goal": goal,
                "message": "The proposed plan contains invalid tool arguments; no operation was executed.",
                "plan": step.model_dump(),
                "argument_errors": exc.detail,
            }
        step = step.model_copy(update={"arguments": validated_arguments})
        if step.tool_name == "run_command":
            try:
                risk_level, requires_approval = command_risk(step.arguments["command"])
            except PolicyError as exc:
                return {"status": "denied", "goal": goal, "message": f"Command rejected: {exc}"}
            step = step.model_copy(update={"risk_level": risk_level, "requires_approval": requires_approval})
        elif step.tool_name == "launch_app":
            try:
                app_is_trusted = is_app_approved(step.arguments["app_name"])
            except ApprovedAppError as exc:
                return {"status": "error", "goal": goal, "message": f"Approved-application policy could not be read: {exc}"}
            if app_is_trusted:
                step = step.model_copy(update={"requires_approval": False})
        elif step.tool_name == "write_file":
            step = step.model_copy(update={"risk_level": "medium", "requires_approval": True})
        elif step.tool_name == "desktop_action":
            step = step.model_copy(update={"risk_level": "medium", "requires_approval": True})
        try:
            if step.tool_name == "write_file":
                mode = step.arguments["mode"]
                if mode == "create_new":
                    target = policy.resolve_path(step.arguments["path"], step.arguments["root_index"])
                    if target.exists():
                        return {
                            "status": "clarification_required",
                            "goal": goal,
                            "question": f"{target.name} already exists. Specify whether to append to it or overwrite it, and include the new content.",
                        }
                    if not target.parent.is_dir():
                        return {"status": "denied", "goal": goal, "message": "The destination parent must already exist inside an approved directory."}
                else:
                    target = policy.resolve_path(step.arguments["path"], step.arguments["root_index"])
                    if not target.parent.is_dir():
                        return {"status": "denied", "goal": goal, "message": "The destination parent must already exist inside an approved directory."}
                    if mode == "append" and (not target.is_file() or target.is_symlink()):
                        return {"status": "denied", "goal": goal, "message": "Append requires an existing regular file."}
                    if mode == "overwrite" and target.exists() and (not target.is_file() or target.is_symlink()):
                        return {"status": "denied", "goal": goal, "message": "Overwrite requires a regular file destination."}
                source = None
            elif step.tool_name == "create_directory":
                target = policy.resolve_new_path(step.arguments["path"])
                source = None
            elif step.tool_name == "open_webpage":
                target = None
                source = None
            elif step.tool_name == "search_web":
                target = None
                source = None
            elif step.tool_name == "inspect_active_window":
                target = None
                source = None
            elif step.tool_name == "run_command":
                target = policy.resolve(step.arguments["working_dir"], step.arguments["root_index"])
                source = None
            elif step.tool_name == "launch_app":
                target = None
                source = None
            elif step.tool_name == "read_file_contents":
                target = policy.resolve_path(step.arguments["path"], step.arguments["root_index"])
                if not target.is_file():
                    return {"status": "denied", "goal": goal, "message": "The requested path must be an existing regular file inside an approved directory."}
                source = None
            elif step.tool_name == "copy_file":
                source = policy.resolve_path(step.arguments["source_path"], step.arguments.get("source_root_index", 0))
                if not source.is_file():
                    return {"status": "denied", "goal": goal, "message": "The copy source must be an existing regular file inside an approved directory."}
                target = policy.resolve_new_path(step.arguments["destination_path"])
            elif step.tool_name == "list_directory":
                target = policy.resolve(step.arguments.get("path", "."))
                search_targets = None
                source = None
            elif step.tool_name == "search_files":
                root_index = step.arguments.get("root_index")
                indexes = range(len(policy.approved_roots)) if root_index is None else (root_index,)
                search_targets = [policy.resolve(step.arguments.get("path", "."), i) for i in indexes]
                target = search_targets[0]
                source = None
            else:
                target = None
                search_targets = None
                source = None
        except (TypeError, ValueError, OSError, RuntimeError) as exc:
            return {"status": "denied", "goal": goal, "message": f"Plan path rejected: {exc}"}
        if step.requires_approval and not approved:
            return {"status": "approval_required", "goal": goal, "planner": planner_used, "plan": step.model_dump(), "message": "This operation requires approval. Review the plan, then rerun with --approve to execute it."}
        if step.tool_name == "inspect_active_window" and desktop_delay_seconds:
            time.sleep(desktop_delay_seconds)
        attempt_history = []
        for attempt in range(1, MAX_ATTEMPTS + 1):
            result = registry.execute(step.tool_name, step.arguments)
            if step.tool_name == "write_file":
                verification = verify_written_file(result, target, step.arguments["content"], step.arguments["mode"])
            elif step.tool_name == "create_directory":
                verification = verify_created_directory(result, target)
            elif step.tool_name == "copy_file":
                verification = verify_copied_file(result, source, target)
            elif step.tool_name == "open_webpage":
                verification = verify_webpage_open(result, step.arguments["url"])
            elif step.tool_name == "search_web":
                verification = verify_web_search(result, step.arguments["query"], step.arguments["max_results"])
            elif step.tool_name == "inspect_active_window":
                verification = verify_desktop_inspection(result)
            elif step.tool_name == "desktop_action":
                verification = verify_desktop_action(result, step.arguments["action_type"], step.arguments["x"], step.arguments["y"])
            elif step.tool_name == "list_directory":
                verification = verify_directory_listing(result, target)
            elif step.tool_name == "search_files":
                verification = verify_file_search(
                    result,
                    search_targets or [target],
                    step.arguments.get("query", ""),
                    step.arguments.get("match_mode", "exact"),
                    step.arguments.get("max_results", 100),
                    step.arguments.get("extensions", []),
                )
            elif step.tool_name == "run_command":
                verification = verify_command_execution(result)
            elif step.tool_name == "launch_app":
                verification = verify_app_launch(result)
            elif step.tool_name == "read_file_contents":
                verification = verify_file_contents(result, target, step.arguments["max_characters"], step.arguments["page_number"])
            else:
                return {"status": "denied", "goal": goal, "message": "No verifier is registered for the selected tool."}
            attempt_history.append({
                "attempt": attempt,
                "tool_status": result.status,
                "error_code": result.error_code,
                "verification_passed": verification.passed,
                "summary": verification.summary,
            })
            if not should_retry(step.tool_name, result, verification, attempt):
                break
        if step.tool_name == "launch_app" and approved and result.status == "success" and verification.passed:
            try:
                approve_app(step.arguments["app_name"])
            except ApprovedAppError as exc:
                result.output["warning"] = f"Application launched but its approval could not be saved: {exc}"
        step_reports.append({"plan": step.model_dump(), "tool_result": result.model_dump(), "verification": verification.model_dump(), "attempts": len(attempt_history), "attempt_history": attempt_history})
        if result.status != "success" or not verification.passed:
            break
    all_verified = len(step_reports) == len(steps) and all(
        report["tool_result"]["status"] == "success" and report["verification"]["passed"]
        for report in step_reports
    )
    if all_verified:
        status = "completed"
    elif (any(
        report["tool_result"]["status"] == "success" and report["verification"]["passed"]
        for report in step_reports[:-1]
    ) or any(
        report["plan"]["tool_name"] == "search_files"
        and report["tool_result"]["status"] == "success"
        and report["tool_result"]["output"].get("complete") is False
        for report in step_reports
    )):
        status = "partially_completed"
    else:
        status = "failed"
    report = {
        "status": status,
        "goal": goal,
        "planner": planner_used,
        "approved_roots": [str(root) for root in policy.approved_roots],
    }
    if len(step_reports) == 1:
        report.update(step_reports[0])
    else:
        report["steps"] = step_reports
    if planner_note:
        report["planner_note"] = planner_note
    return report


def _matches_intent(proposed, expected, registry: ToolRegistry) -> bool:
    """Require every normalized model argument to match deterministic user-intent parsing."""
    if proposed.tool_name != expected.tool_name:
        return False
    try:
        proposed_arguments = registry.validate_arguments(proposed.tool_name, proposed.arguments)
        expected_arguments = registry.validate_arguments(expected.tool_name, expected.arguments)
    except ToolArgumentsError:
        return False
    return proposed_arguments == expected_arguments
