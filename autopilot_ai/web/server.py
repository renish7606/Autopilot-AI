"""FastAPI server that wraps the existing agent behind WebSocket + REST."""

from __future__ import annotations

import asyncio
import json
import subprocess
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from autopilot_ai.agent import run_goal
from autopilot_ai.approved_apps import load_approved_apps
from autopilot_ai.approved_dirs import load_approved_dirs
from autopilot_ai.conversation import ConversationSession
from autopilot_ai.memory import TaskMemory


app = FastAPI(title="Autopilot AI", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DEFAULT_WORKSPACE = Path(__file__).resolve().parents[2] / "workspace"
_sessions: dict[str, ConversationSession] = {}
_pending_approvals: dict[str, dict[str, str]] = {}
_active_sessions: set[str] = set()
_memory = TaskMemory()
_ALLOWED_ORIGINS = frozenset({
    "http://localhost:5173",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
})
_MAX_GOAL_LENGTH = 2000
_MAX_MESSAGE_LENGTH = 16_384


@app.websocket("/ws/agent")
async def agent_ws(ws: WebSocket) -> None:
    """Receive bounded task requests and emit execution/verification events."""
    origin = ws.headers.get("origin")
    if origin and origin not in _ALLOWED_ORIGINS:
        await ws.close(code=1008, reason="Origin is not allowed.")
        return
    await ws.accept()
    try:
        while True:
            raw = await ws.receive_text()
            if len(raw) > _MAX_MESSAGE_LENGTH:
                await _send_event(ws, "error", message="WebSocket message is too large.")
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                await _send_event(ws, "error", message="Message must be valid JSON.")
                continue
            if not isinstance(message, dict):
                await _send_event(ws, "error", message="Message must be a JSON object.")
                continue

            message_type = message.get("type")
            if message_type == "run_goal":
                await _handle_run_goal(ws, message)
            elif message_type == "approve":
                await _handle_approval(ws, message)
            else:
                await _send_event(ws, "error", message="Unsupported WebSocket message type.")
    except WebSocketDisconnect:
        return


async def _handle_run_goal(ws: WebSocket, message: dict[str, Any]) -> None:
    goal = _validate_goal(message.get("goal"))
    if not goal:
        await _send_event(ws, "error", message="A non-empty goal of at most 2000 characters is required.")
        return
    planner = message.get("planner", "ollama")
    if planner not in {"ollama", "heuristic"}:
        await _send_event(ws, "error", message="Planner must be 'ollama' or 'heuristic'.")
        return
    approved = message.get("approved", False)
    if not isinstance(approved, bool):
        await _send_event(ws, "error", message="The approved field must be a boolean.")
        return
    try:
        workspace = _resolve_workspace(message.get("workspace"))
        session = _get_or_create_session(message.get("session_id"))
    except (ValueError, OSError) as exc:
        await _send_event(ws, "error", message=str(exc))
        return

    session_id = session.session_id
    if session_id in _active_sessions:
        await _send_event(ws, "error", message="This session already has a task running.", session_id=session_id)
        return
    _active_sessions.add(session_id)
    _pending_approvals.pop(session_id, None)
    await _send_event(ws, "session_created", session_id=session_id)
    try:
        await _run_and_report(ws, session, goal, workspace, planner, approved)
    finally:
        _active_sessions.discard(session_id)


async def _handle_approval(ws: WebSocket, message: dict[str, Any]) -> None:
    session_id = message.get("session_id")
    goal = _validate_goal(message.get("goal"))
    if not isinstance(session_id, str) or not goal:
        await _send_event(ws, "error", message="Approval requires the original goal and session ID.")
        return
    pending = _pending_approvals.get(session_id)
    if pending is None or pending["goal"] != goal:
        await _send_event(ws, "error", message="No matching operation is awaiting approval in this session.", session_id=session_id)
        return
    session = _sessions.get(session_id)
    if session is None:
        await _send_event(ws, "error", message="Unknown session.", session_id=session_id)
        return
    if session_id in _active_sessions:
        await _send_event(ws, "error", message="This session already has a task running.", session_id=session_id)
        return
    try:
        workspace = _resolve_workspace(pending["workspace"])
    except (ValueError, OSError) as exc:
        _pending_approvals.pop(session_id, None)
        await _send_event(ws, "error", message=f"The approved workspace is no longer available: {exc}", session_id=session_id)
        return

    _active_sessions.add(session_id)
    try:
        await _run_and_report(ws, session, goal, workspace, pending["planner"], approved=True)
    finally:
        _active_sessions.discard(session_id)


async def _run_and_report(
    ws: WebSocket,
    session: ConversationSession,
    goal: str,
    workspace: Path,
    planner: str,
    approved: bool,
) -> None:
    session_id = session.session_id
    await _send_event(ws, "task_started", goal=goal, session_id=session_id)
    await _send_event(ws, "planning", planner=planner)
    try:
        approved_dirs = load_approved_dirs()

        def run_task(task_goal: str) -> dict[str, Any]:
            return run_goal(
                task_goal,
                workspace,
                planner=planner,
                approved_dirs=approved_dirs,
                approved=approved,
            )

        report = await asyncio.to_thread(session.handle, goal, run_task)
    except Exception as exc:
        _pending_approvals.pop(session_id, None)
        await _send_event(ws, "error", message=str(exc), session_id=session_id)
        return

    await _send_step_events(ws, report)
    status = report.get("status", "error")
    if status == "approval_required":
        _pending_approvals[session_id] = {
            "goal": goal,
            "workspace": str(workspace),
            "planner": planner,
        }
        await _send_event(
            ws,
            "approval_required",
            goal=goal,
            plan=report.get("plan"),
            planned_actions=report.get("planned_actions", []),
            message=report.get("message", report.get("summary", "")),
            session_id=session_id,
        )
    elif status == "clarification_required":
        _pending_approvals.pop(session_id, None)
        await _send_event(
            ws,
            "clarification",
            goal=goal,
            question=report.get("question", ""),
            candidates=report.get("candidates", []),
            session_id=session_id,
        )
    elif status in {"completed", "partially_completed"}:
        _pending_approvals.pop(session_id, None)
        await _send_event(ws, "task_completed", report=_serialize(report))
    else:
        _pending_approvals.pop(session_id, None)
        await _send_event(ws, "task_failed", report=_serialize(report))


async def _send_step_events(ws: WebSocket, report: dict[str, Any]) -> None:
    """Translate the existing agent report into typed step events."""
    observations = report.get("observations")
    if isinstance(observations, list) and observations:
        for index, observation in enumerate(observations):
            plan = {
                "tool_name": observation.get("action", "unknown"),
                "description": observation.get("action", ""),
            }
            await _send_event(ws, "step_start", step_index=index, plan=plan)
            await _send_event(
                ws,
                "step_done",
                step_index=index,
                plan=plan,
                tool_result=observation.get("tool_result"),
                verification=observation.get("verification"),
                attempts=1,
            )
        return

    steps = report.get("steps")
    if isinstance(steps, list) and steps:
        for index, step in enumerate(steps):
            await _send_event(ws, "step_start", step_index=index, plan=step.get("plan"))
            if step.get("tool_result"):
                await _send_event(
                    ws,
                    "step_done",
                    step_index=index,
                    plan=step.get("plan"),
                    tool_result=step.get("tool_result"),
                    verification=step.get("verification"),
                    attempts=step.get("attempts", 1),
                )
    elif report.get("plan"):
        await _send_event(ws, "step_start", step_index=0, plan=report.get("plan"))
        if report.get("tool_result"):
            await _send_event(
                ws,
                "step_done",
                step_index=0,
                plan=report.get("plan"),
                tool_result=report.get("tool_result"),
                verification=report.get("verification"),
                attempts=report.get("attempts", 1),
            )


@app.get("/api/status")
async def get_status() -> dict[str, Any]:
    """Check whether the local Ollama executable responds."""
    ollama_available = False
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["ollama", "list"],
            capture_output=True,
            timeout=5,
            check=False,
        )
        ollama_available = result.returncode == 0
    except Exception:
        pass
    return {
        "status": "ok",
        "ollama_available": ollama_available,
        "workspace": str(DEFAULT_WORKSPACE),
        "version": "0.1.0",
    }


@app.get("/api/sessions")
async def list_sessions() -> dict[str, list[dict[str, Any]]]:
    """Summarize recently active task sessions."""
    history = await asyncio.to_thread(_memory.get_recent_sessions_history, 50)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in history:
        session_id = entry.get("session_id", "unknown")
        grouped.setdefault(session_id, []).append(entry)
    return {
        "sessions": [
            {
                "session_id": session_id,
                "task_count": len(entries),
                "last_goal": entries[-1].get("goal", ""),
            }
            for session_id, entries in grouped.items()
        ]
    }


@app.get("/api/sessions/{session_id}/history")
async def session_history(session_id: str) -> dict[str, Any]:
    """Return recent persisted actions for one session."""
    history = await asyncio.to_thread(_memory.get_recent_history, session_id, 50)
    return {"session_id": session_id, "history": history}


@app.get("/api/approved-dirs")
async def list_approved_directories() -> dict[str, list[str]]:
    directories = await asyncio.to_thread(load_approved_dirs)
    return {"approved_dirs": [str(directory) for directory in directories]}


@app.get("/api/approved-apps")
async def list_approved_applications() -> dict[str, list[str]]:
    applications = await asyncio.to_thread(load_approved_apps)
    return {"approved_apps": sorted(applications)}


def _validate_goal(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    goal = value.strip()
    if not goal or len(goal) > _MAX_GOAL_LENGTH:
        return None
    return goal


def _get_or_create_session(value: Any) -> ConversationSession:
    if value is None:
        session = ConversationSession(_memory)
        _sessions[session.session_id] = session
        return session
    if not isinstance(value, str):
        raise ValueError("Session ID must be a UUID string.")
    try:
        session_id = str(uuid.UUID(value))
    except ValueError as exc:
        raise ValueError("Session ID must be a valid UUID.") from exc
    if session_id in _sessions:
        return _sessions[session_id]
    if not _memory.has_session(session_id):
        raise ValueError("Unknown session ID.")
    session = ConversationSession(_memory, session_id)
    _sessions[session_id] = session
    return session


def _resolve_workspace(value: Any) -> Path:
    if value is None:
        value = str(DEFAULT_WORKSPACE)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Workspace must be a directory path.")
    try:
        workspace = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"Workspace does not exist or cannot be resolved: {value}") from exc
    if not workspace.is_dir():
        raise ValueError("Workspace must be an existing directory.")
    approved_roots = [DEFAULT_WORKSPACE, *load_approved_dirs()]
    for root in approved_roots:
        try:
            resolved_root = root.expanduser().resolve(strict=True)
            workspace.relative_to(resolved_root)
            return workspace
        except (ValueError, OSError, RuntimeError):
            continue
    raise ValueError("Workspace must be inside the default workspace or an approved directory.")


async def _send_event(ws: WebSocket, event: str, **payload: Any) -> None:
    await ws.send_text(json.dumps({"event": event, **payload}, ensure_ascii=False, default=str))


def _serialize(value: Any) -> Any:
    """Make reports JSON-safe, including any Path values from integrations."""
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))
