from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from autopilot_ai.web import server


class _FakeWebSocket:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def send_text(self, value: str) -> None:
        self.events.append(json.loads(value))


class _FakeSession:
    session_id = "meta-query-test-session"

    def handle(self, goal: str, run_task: Any) -> dict[str, Any]:
        report = run_task(goal)
        report["session_id"] = self.session_id
        return report


@pytest.mark.parametrize(
    ("goal", "expected_message"),
    [
        ("What apps are approved?", "Approved apps: notepad"),
        ("List approved apps", "Approved apps: notepad"),
        ("What folders are approved?", "Approved dirs: D:\\Agent\\workspace"),
        ("Show approved dirs.", "Approved dirs: D:\\Agent\\workspace"),
    ],
)
def test_approved_list_meta_queries_skip_the_agent_planner(
    monkeypatch: pytest.MonkeyPatch,
    goal: str,
    expected_message: str,
) -> None:
    monkeypatch.setattr(server, "load_approved_apps", lambda: {"notepad"})
    monkeypatch.setattr(server, "load_approved_dirs", lambda: [Path(r"D:\Agent\workspace")])

    def unexpected_agent_call(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("Approved-list meta-queries must not call run_goal.")

    monkeypatch.setattr(server, "run_goal", unexpected_agent_call)
    websocket = _FakeWebSocket()
    asyncio.run(
        server._run_and_report(
            websocket,
            _FakeSession(),
            goal,
            Path(r"D:\Agent\workspace"),
            "heuristic",
            False,
        )
    )

    assert [event["event"] for event in websocket.events] == ["task_started", "planning", "task_completed"]
    report = websocket.events[-1]["report"]
    assert report["status"] == "completed"
    assert report["goal"] == goal
    assert report["message"] == expected_message
    assert report["session_id"] == _FakeSession.session_id
