"""Conversation-turn glue for numbered search candidates (no persistent memory)."""

from collections.abc import Callable
from typing import Any

from .session_state import SessionState


class ConversationSession:
    """Keep one turn of search context so users can refer to a numbered result."""

    def __init__(self) -> None:
        self.state = SessionState()
        self._pending_kind: str | None = None
        self._pending_goal: str | None = None

    def handle(self, message: str, run_task: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
        """Resolve a candidate reply, or process it as a new task."""
        was_pending = self.state.pending_question is not None
        selected = self.state.select_candidate(message)
        if selected is not None:
            pending_kind, pending_goal = self._pending_kind, self._pending_goal
            self._pending_kind = None
            self._pending_goal = None
            if pending_kind == "folder_scope" and pending_goal:
                resolved_goal = _replace_folder_scope(pending_goal, selected)
                if resolved_goal is None:
                    return {"status": "error", "goal": pending_goal, "message": "Could not safely apply the selected folder to the original request."}
                report = run_task(resolved_goal)
                report["goal"] = pending_goal
                report["selected_folder"] = selected
                return report
            return {
                "status": "candidate_selected",
                "goal": message,
                "selected_path": selected,
                "message": "Selected the search result. No file was opened or changed.",
            }

        if was_pending and self.state.is_candidate_reply(message):
            return {
                "status": "clarification_required",
                "goal": message,
                "question": self.state.pending_question,
                "follow_up_question": self.state.pending_question,
                "candidates": list(self.state.candidates),
                "candidate_kind": self._pending_kind,
            }

        # A non-selection starts a new request, so stale candidates cannot leak
        # into an unrelated future turn.
        self.state.clear()
        self._pending_kind = None
        self._pending_goal = None
        report = run_task(message)
        clarification_candidates = report.get("candidates", [])
        if report.get("status") == "clarification_required" and report.get("scope_kind") == "folder" and clarification_candidates:
            self.state.set_pending(report.get("question", "Which approved folder should I use?"), clarification_candidates)
            self._pending_kind = "folder_scope"
            self._pending_goal = message
            report["candidate_kind"] = "folder_scope"
            report["follow_up_question"] = self.state.pending_question
            return report
        candidates = _search_matches(report)
        if len(candidates) > 1:
            self.state.set_pending("Which search result do you mean?", candidates)
            self._pending_kind = "search_result"
            report["follow_up_question"] = self.state.pending_question
        return report


def _replace_folder_scope(goal: str, selected_path: str) -> str | None:
    """Replace only the folder part of a pending request with a chosen candidate."""
    import re

    match = re.search(
        r"\b(in|inside|under|within)\s+(?:the\s+)?(.+?)\s+folder\b",
        goal,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    replacement = f'{match.group(1)} "{selected_path}" folder'
    return goal[:match.start()] + replacement + goal[match.end():]


def _search_matches(report: dict[str, Any]) -> list[str]:
    reports = report.get("steps")
    items = reports if isinstance(reports, list) else [report]
    for item in reversed(items):
        if item.get("plan", {}).get("tool_name") != "search_files":
            continue
        if item.get("tool_result", {}).get("status") != "success":
            return []
        if item.get("verification", {}).get("passed") is not True:
            return []
        output = item.get("tool_result", {}).get("output", {})
        if output.get("complete") is not True:
            return []
        matches = output.get("matches", [])
        return [match for match in matches if isinstance(match, str)] if len(matches) > 1 else []
    return []
