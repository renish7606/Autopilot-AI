"""One bounded receive-plan-policy-execute-verify cycle."""

from pathlib import Path
from typing import Any

from .planner import ModelPlannerError, PlanningError, plan_goal_steps, plan_with_ollama, plan_with_ollama_steps
from .policy import PathPolicy, PolicyError
from .tools import ToolRegistry
from .verifier import verify_copied_file, verify_created_directory, verify_directory_listing, verify_file_search, verify_webpage_open


def run_goal(
    goal: str,
    workspace: Path,
    planner: str = "heuristic",
    approved_dirs: tuple[Path, ...] | list[Path] = (),
    approved: bool = False,
) -> dict[str, Any]:
    try:
        policy = PathPolicy(workspace, approved_dirs)
    except (PolicyError, OSError, ValueError) as exc:
        return {"status": "denied", "goal": goal, "message": f"Approved directory rejected: {exc}"}
    registry = ToolRegistry(policy)
    planner_used = planner
    planner_note: str | None = None
    try:
        if planner == "heuristic":
            steps = plan_goal_steps(goal)
        elif planner == "ollama":
            try:
                expected_steps = plan_goal_steps(goal)
                if len(expected_steps) > 1:
                    proposed_steps = plan_with_ollama_steps(goal, ("list_directory", "search_files"))
                    if len(proposed_steps) == len(expected_steps) and all(_matches_intent(proposed, expected) for proposed, expected in zip(proposed_steps, expected_steps)):
                        steps = proposed_steps
                    else:
                        steps = expected_steps
                        planner_used = "heuristic_fallback"
                        if len(proposed_steps) != len(expected_steps):
                            mismatch = f"The local model returned {len(proposed_steps)} step(s), but the request needs {len(expected_steps)}."
                        else:
                            mismatch_index = next(index for index, pair in enumerate(zip(proposed_steps, expected_steps), start=1) if not _matches_intent(*pair))
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
                    matches_intent = (
                        proposed.tool_name == expected.tool_name
                        and proposed.arguments.get("path", ".") == expected.arguments.get("path", ".")
                    )
                    if proposed.tool_name == "search_files":
                        matches_intent = matches_intent and (
                            str(proposed.arguments.get("query", "")).casefold().strip()
                            == str(expected.arguments.get("query", "")).casefold().strip()
                            and proposed.arguments.get("match_mode", "exact") == expected.arguments.get("match_mode", "exact")
                            and proposed.arguments.get("max_results", 100) == expected.arguments.get("max_results", 100)
                        )
                    elif proposed.tool_name == "open_webpage":
                        matches_intent = matches_intent and proposed.arguments.get("url", "").rstrip("/" ) == expected.arguments.get("url", "").rstrip("/" )
                    if matches_intent:
                        steps = [proposed]
                    else:
                        steps = [expected]
                        planner_used = "heuristic_fallback"
                        planner_note = "The local model proposal did not match the bounded intent check; used the deterministic plan."
            except (ModelPlannerError, PlanningError) as model_error:
                try:
                    steps = plan_goal_steps(goal)
                except PlanningError as intent_error:
                    return {"status": "unsupported", "goal": goal, "message": str(intent_error)}
                planner_used = "heuristic_fallback"
                planner_note = f"The local model plan was unusable ({model_error}); used the deterministic plan for this supported request."
        else:
            return {"status": "error", "goal": goal, "message": f"Unknown planner: {planner}"}
    except ModelPlannerError as exc:
        return {"status": "error", "goal": goal, "message": str(exc)}
    except PlanningError as exc:
        return {"status": "unsupported", "goal": goal, "message": str(exc)}

    step_reports = []
    for step in steps:
        if step.tool_name not in registry.names:
            return {"status": "denied", "goal": goal, "message": "Plan selected a tool that is not registered."}
        try:
            if step.tool_name == "create_directory":
                target = policy.resolve_new_path(step.arguments["path"])
                source = None
            elif step.tool_name == "open_webpage":
                target = None
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
            else:
                search_targets = [policy.resolve(step.arguments.get("path", "."), i) for i in range(len(policy.approved_roots))]
                target = search_targets[0]
                source = None
        except (TypeError, ValueError) as exc:
            return {"status": "denied", "goal": goal, "message": f"Plan path rejected: {exc}"}
        if step.requires_approval and not approved:
            return {"status": "approval_required", "goal": goal, "planner": planner_used, "plan": step.model_dump(), "message": "This file operation will change the filesystem. Review the plan, then rerun with --approve to execute it."}
        result = registry.execute(step.tool_name, step.arguments)
        if step.tool_name == "create_directory":
            verification = verify_created_directory(result, target)
        elif step.tool_name == "copy_file":
            verification = verify_copied_file(result, source, target)
        elif step.tool_name == "open_webpage":
            verification = verify_webpage_open(result, step.arguments["url"])
        elif step.tool_name == "list_directory":
            verification = verify_directory_listing(result, target)
        elif step.tool_name == "search_files":
            verification = verify_file_search(result, search_targets or [target], step.arguments.get("query", ""), step.arguments.get("match_mode", "exact"), step.arguments.get("max_results", 100))
        else:
            return {"status": "denied", "goal": goal, "message": "No verifier is registered for the selected tool."}
        step_reports.append({"plan": step.model_dump(), "tool_result": result.model_dump(), "verification": verification.model_dump()})
        if result.status != "success" or not verification.passed:
            break
    status = "completed" if len(step_reports) == len(steps) and all(report["verification"]["passed"] for report in step_reports) else "failed"
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


def _matches_intent(proposed, expected) -> bool:
    """Require the model's ordered tool calls and meaningful arguments to match the user's parsed intent."""
    if proposed.tool_name != expected.tool_name or proposed.arguments.get("path", ".") != expected.arguments.get("path", "."):
        return False
    if proposed.tool_name == "search_files":
        return (
            str(proposed.arguments.get("query", "")).casefold().strip()
            == str(expected.arguments.get("query", "")).casefold().strip()
            and proposed.arguments.get("match_mode", "exact") == expected.arguments.get("match_mode", "exact")
            and proposed.arguments.get("max_results", 100) == expected.arguments.get("max_results", 100)
            and set(proposed.arguments).issubset({"path", "query", "match_mode", "max_results"})
        )
    return set(proposed.arguments).issubset({"path"})
