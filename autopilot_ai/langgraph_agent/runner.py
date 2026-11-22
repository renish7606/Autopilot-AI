"""Public entry point for LangGraph ReAct mode."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from autopilot_ai.approved_dirs import load_approved_dirs
from autopilot_ai.agent import AmbiguousReadFile, AmbiguousSearchScope, _resolve_read_scope, _resolve_search_scope
from autopilot_ai.policy import PathPolicy
from autopilot_ai.tools import ToolRegistry
from autopilot_ai.verifier import verify_file_contents, verify_file_search

_MUTATING_TOOLS = frozenset({
    "write_file", "copy_file", "create_directory", "desktop_action", "launch_app", "run_command",
})

_FIND_FIRST_PDF = re.compile(
    r"^\s*(?:please\s+)?(?:find|search(?:\s+for)?|locate)\s+(?:all\s+)?(?:pdfs?|pdf\s+files?)\s+"
    r"in\s+(?P<folder>.+?)\s+and\s+read\s+(?:the\s+)?first(?:\s+(?:one|file|result))?\s*[.!]?\s*$",
    re.IGNORECASE,
)


def _run_find_first_pdf(goal: str, workspace: Path) -> dict[str, Any] | None:
    """Deterministically search an approved folder, then read its first verified PDF."""
    match = _FIND_FIRST_PDF.fullmatch(goal)
    if match is None:
        return None

    try:
        policy = PathPolicy(workspace, load_approved_dirs())
        registry = ToolRegistry(policy)
        search_args = _resolve_search_scope(policy, {
            "path": match.group("folder").strip().strip("\"'"),
            "root_index": 0,
            "query": "",
            "match_mode": "exact",
            "extensions": [".pdf"],
            "max_results": 100,
        })
        search_args = registry.validate_arguments("search_files", search_args)
        search_root = policy.resolve(search_args["path"], search_args["root_index"])
        search_result = registry.execute("search_files", search_args)
        search_verification = verify_file_search(
            search_result,
            [search_root],
            "",
            "exact",
            search_args["max_results"],
            search_args["extensions"],
        )
    except AmbiguousSearchScope as exc:
        return {
            "status": "clarification_required",
            "goal": goal,
            "planner": "langgraph_react",
            "question": f"More than one approved folder matches '{exc.folder}'. Specify its full path.",
            "candidates": [str(path) for path in exc.candidates],
        }
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return {
            "status": "denied",
            "goal": goal,
            "planner": "langgraph_react",
            "message": f"Could not safely search the requested approved folder: {exc}",
        }

    search_step = {
        "plan": {
            "step_id": "step-1",
            "tool_name": "search_files",
            "description": "Find PDF files in the requested approved folder.",
            "arguments": search_args,
            "risk_level": "low",
            "requires_approval": False,
        },
        "tool_result": search_result.model_dump(mode="json"),
        "verification": search_verification.model_dump(mode="json"),
        "attempts": 1,
    }
    if search_result.status != "success" or not search_verification.passed:
        return {
            "status": "failed",
            "goal": goal,
            "planner": "langgraph_react",
            "approved_roots": [str(root) for root in policy.approved_roots],
            "message": "PDF search failed or could not be independently verified; no file was read.",
            "iterations": 0,
            "steps": [search_step],
        }

    matches = search_result.output.get("matches", [])
    if not matches:
        return {
            "status": "partially_completed",
            "goal": goal,
            "planner": "langgraph_react",
            "approved_roots": [str(root) for root in policy.approved_roots],
            "message": f"The approved folder {search_root} contains no PDF files, so there was no first file to read.",
            "iterations": 0,
            "steps": [search_step],
        }

    try:
        read_args = _resolve_read_scope(policy, {
            "path": matches[0],
            "root_index": 0,
            "max_characters": 8000,
            "page_number": None,
        })
        read_args = registry.validate_arguments("read_file_contents", read_args)
        read_path = policy.resolve_path(read_args["path"], read_args["root_index"])
        read_result = registry.execute("read_file_contents", read_args)
        read_verification = verify_file_contents(
            read_result, read_path, read_args["max_characters"], read_args["page_number"]
        )
    except AmbiguousReadFile as exc:
        return {
            "status": "clarification_required",
            "goal": goal,
            "planner": "langgraph_react",
            "question": f"More than one approved PDF matches '{exc.requested}'. Specify its full path.",
            "candidates": [str(path) for path in exc.candidates],
            "steps": [search_step],
        }
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return {
            "status": "partially_completed",
            "goal": goal,
            "planner": "langgraph_react",
            "approved_roots": [str(root) for root in policy.approved_roots],
            "message": f"PDFs were found, but the first file could not be read safely: {exc}",
            "iterations": 0,
            "steps": [search_step],
        }

    read_step = {
        "plan": {
            "step_id": "step-2",
            "tool_name": "read_file_contents",
            "description": "Read the first PDF returned by the verified search.",
            "arguments": read_args,
            "risk_level": "low",
            "requires_approval": False,
        },
        "tool_result": read_result.model_dump(mode="json"),
        "verification": read_verification.model_dump(mode="json"),
        "attempts": 1,
    }
    success = read_result.status == "success" and read_verification.passed
    return {
        "status": "completed" if success else "partially_completed",
        "goal": goal,
        "planner": "langgraph_react",
        "approved_roots": [str(root) for root in policy.approved_roots],
        "message": (
            f"Found and read {read_path.name}; search and file contents were independently verified."
            if success else f"Found {read_path.name}, but its contents could not be independently verified."
        ),
        "iterations": 0,
        "steps": [search_step, read_step],
    }


def _tool_payload(message: Any) -> dict[str, Any]:
    content = message.content
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
            return parsed if isinstance(parsed, dict) else {"status": "error", "message": "Tool returned malformed output."}
        except json.JSONDecodeError:
            return {"status": "error", "message": content[:2000]}
    return {"status": "error", "message": "Tool returned output in an unsupported format."}


def run_langgraph_goal(
    goal: str,
    workspace: Path,
    approved: bool = False,
    model_name: str | None = None,
    approved_action: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a ReAct task and return truthful, verifier-backed execution steps."""
    deterministic_file_report = _run_find_first_pdf(goal, workspace)
    if deterministic_file_report is not None:
        return deterministic_file_report

    try:
        from .graph import MAX_ITERATIONS, build_graph
        from langchain_core.messages import HumanMessage, ToolMessage
    except ImportError as exc:
        return {
            "status": "error",
            "goal": goal,
            "message": f"LangGraph dependencies are unavailable: {exc}. Install requirements.txt.",
        }

    resolved_model = model_name or os.environ.get("AUTOPILOT_MODEL", "qwen2.5:0.5b")
    try:
        graph = build_graph(str(workspace), approved, resolved_model, approved_action, goal)
    except Exception as exc:
        return {"status": "error", "goal": goal, "planner": "langgraph_react", "message": f"Could not build LangGraph: {exc}"}

    initial_state = {
        "messages": [HumanMessage(content=goal)],
        "goal": goal,
        "workspace": str(workspace),
        "approved": approved,
        "iterations": 0,
        "final_answer": None,
        "tool_calls_log": [],
    }
    try:
        final_state = graph.invoke(initial_state)
    except Exception as exc:
        return {"status": "error", "goal": goal, "planner": "langgraph_react", "message": f"LangGraph execution failed: {exc}"}

    messages = final_state.get("messages", [])
    tool_messages = {
        message.tool_call_id: message
        for message in messages
        if isinstance(message, ToolMessage) and getattr(message, "tool_call_id", None)
    }
    steps: list[dict[str, Any]] = []
    approvals: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for message in messages:
        for call in getattr(message, "tool_calls", []) or []:
            name = call.get("name", "unknown")
            arguments = call.get("args", {})
            if name == "finish":
                continue
            tool_message = tool_messages.get(call.get("id"))
            payload = _tool_payload(tool_message) if tool_message else {
                "status": "error", "message": "The tool call did not produce an observation."
            }
            plan = {
                "tool_name": name,
                "description": f"LangGraph selected {name}.",
                "arguments": arguments if isinstance(arguments, dict) else {},
                "risk_level": "medium" if name in _MUTATING_TOOLS else "low",
                "requires_approval": name in _MUTATING_TOOLS,
            }
            if payload.get("requires_approval") is True:
                approvals.append(payload.get("plan", plan))
                continue

            tool_result = payload.get("tool_result")
            verification = payload.get("verification")
            if not isinstance(tool_result, dict):
                tool_result = {
                    "status": "denied" if payload.get("status") == "denied" else "error",
                    "output": {"detail": payload.get("message", "Tool did not return a result.")},
                    "error_code": "approval_or_execution_error",
                }
            if not isinstance(verification, dict):
                verification = {
                    "passed": False,
                    "summary": payload.get("message", "Operation was not executed or verified."),
                    "evidence": {},
                }
            step = {
                "plan": plan,
                "tool_result": tool_result,
                "verification": verification,
                "attempts": 1,
            }
            steps.append(step)
            if tool_result.get("status") != "success" or verification.get("passed") is not True:
                failures.append(step)

    iterations = final_state.get("iterations", 0)
    final_answer = final_state.get("final_answer")
    last_message = messages[-1] if messages else None
    if not isinstance(final_answer, str) or not final_answer.strip():
        content = getattr(last_message, "content", "") if last_message else ""
        final_answer = content if isinstance(content, str) else ""

    verified_count = sum(
        step["tool_result"].get("status") == "success" and step["verification"].get("passed") is True
        for step in steps
    )
    if approvals:
        status = "approval_required"
        message = "Review and approve the requested operation(s), then confirm to rerun the task."
    elif iterations >= MAX_ITERATIONS and not final_state.get("final_answer"):
        status = "partially_completed"
        message = f"Reached the maximum of {MAX_ITERATIONS} planning iterations before receiving a final answer."
    elif failures:
        status = "partially_completed" if verified_count else "failed"
        message = final_answer.strip() or "One or more tool operations failed or could not be independently verified."
    elif final_answer.strip():
        status = "completed"
        message = final_answer.strip()
    else:
        status = "failed"
        message = "LangGraph did not return a final answer."

    report: dict[str, Any] = {
        "status": status,
        "goal": goal,
        "planner": "langgraph_react",
        "approved_roots": [str(root) for root in (Path(workspace).resolve(), *load_approved_dirs())],
        "message": message,
        "iterations": iterations,
        "steps": steps,
    }
    if approvals:
        report["plan"] = approvals[0]
        report["planned_actions"] = approvals
    if final_state.get("tool_calls_log"):
        report["tool_calls_log"] = final_state["tool_calls_log"]
    return report
