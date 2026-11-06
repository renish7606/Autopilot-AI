"""Conversation-turn glue for numbered search candidates (no persistent memory)."""

from collections.abc import Callable
from typing import Any

from .session_state import SessionState


class ConversationSession:
    """Keep one turn of search context so users can refer to a numbered result."""

    def __init__(self) -> None:
        self.state = SessionState()

    def handle(self, message: str, run_task: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
        """Resolve a candidate reply, or process it as a new task."""
        selected = self.state.select_candidate(message)
        if selected is not None:
            return {
                "status": "candidate_selected",
                "goal": message,
                "selected_path": selected,
                "message": "Selected the search result. No file was opened or changed.",
            }

        # A non-selection starts a new request, so stale candidates cannot leak
        # into an unrelated future turn.
        self.state.clear()
        report = run_task(message)
        candidates = _search_matches(report)
        if len(candidates) > 1:
            self.state.set_pending("Which search result do you mean?", candidates)
            report["follow_up_question"] = self.state.pending_question
        return report


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
