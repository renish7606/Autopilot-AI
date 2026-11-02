"""One bounded receive-plan-policy-execute-verify cycle."""

from pathlib import Path
from typing import Any

from .planner import ModelPlannerError, PlanningError, plan_goal, plan_with_ollama
from .policy import PathPolicy, PolicyError
from .tools import ToolRegistry
from .verifier import verify_directory_listing, verify_file_search


def run_goal(
    goal: str,
    workspace: Path,
    planner: str = "heuristic",
    approved_dirs: tuple[Path, ...] | list[Path] = (),
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
            step = plan_goal(goal)
        elif planner == "ollama":
            try:
                proposed = plan_with_ollama(goal, registry.names)
                expected = plan_goal(goal)
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
                if matches_intent:
                    step = proposed
                else:
                    step = expected
                    planner_used = "heuristic_fallback"
                    planner_note = "The local model proposal did not match the bounded intent check; used the deterministic plan."
            except (ModelPlannerError, PlanningError) as model_error:
                try:
                    step = plan_goal(goal)
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

    if step.tool_name not in registry.names:
        return {"status": "denied", "goal": goal, "message": "Plan selected a tool that is not registered."}
    try:
        if step.tool_name == "list_directory":
            target = policy.resolve(step.arguments.get("path", "."))
            search_targets = None
        else:
            search_targets = [
                policy.resolve(step.arguments.get("path", "."), root_index)
                for root_index in range(len(policy.approved_roots))
            ]
            target = search_targets[0]
    except (TypeError, ValueError) as exc:
        return {"status": "denied", "goal": goal, "message": f"Plan path rejected: {exc}"}
    result = registry.execute(step.tool_name, step.arguments)
    if step.tool_name == "list_directory":
        verification = verify_directory_listing(result, target)
    elif step.tool_name == "search_files":
        verification = verify_file_search(
            result,
            search_targets or [target],
            query=step.arguments.get("query", ""),
            match_mode=step.arguments.get("match_mode", "exact"),
            max_results=step.arguments.get("max_results", 100),
        )
    else:
        return {"status": "denied", "goal": goal, "message": "No verifier is registered for the selected tool."}
    status = "completed" if verification.passed else "failed"
    report = {
        "status": status,
        "goal": goal,
        "planner": planner_used,
        "approved_roots": [str(root) for root in policy.approved_roots],
        "plan": step.model_dump(),
        "tool_result": result.model_dump(),
        "verification": verification.model_dump(),
    }
    if planner_note:
        report["planner_note"] = planner_note
    return report
