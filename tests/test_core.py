from pathlib import Path
from io import BytesIO
import json
import sys
import types
from urllib.error import HTTPError

import pytest

from autopilot_ai.approved_dirs import add_approved_dirs, clear_approved_dirs, load_approved_dirs, remove_approved_dirs
from autopilot_ai.approved_apps import approve_app, is_app_approved, load_approved_apps
from autopilot_ai.agent import _resolve_search_scope, run_goal
from autopilot_ai.browser import BrowserToolError, _http_status_error, open_webpage
from autopilot_ai.cli import format_report, render_report
from autopilot_ai.conversation import ConversationSession
from autopilot_ai.contracts import PlanStep, TaskPlan, ToolResult, Verification
from autopilot_ai.desktop import DesktopToolError, _capture_screenshot, _inspect_uia, inspect_active_window
from autopilot_ai.memory import TaskMemory
from autopilot_ai.planner import ModelPlannerError, PlanningError, plan_goal_steps, plan_with_ollama, plan_with_ollama_steps
from autopilot_ai.policy import PathPolicy, PolicyError
from autopilot_ai.recovery import should_retry
from autopilot_ai.session_state import SessionState
from autopilot_ai.tools import APP_ALIASES, ToolArgumentsError, ToolRegistry, _resolve_app_executable
from autopilot_ai.verifier import verify_app_launch, verify_command_execution, verify_desktop_action, verify_desktop_inspection, verify_directory_listing, verify_file_contents, verify_webpage_open, verify_written_file


def test_run_command_safe_execution_is_verified(tmp_path: Path) -> None:
    report = run_goal("Run command 'echo test'", tmp_path, planner="heuristic")

    assert report["status"] == "completed"
    assert report["plan"]["tool_name"] == "run_command"
    assert report["plan"]["requires_approval"] is False
    assert report["tool_result"]["output"]["exit_code"] == 0
    assert "test" in report["tool_result"]["output"]["stdout"]
    assert report["verification"]["passed"] is True


def test_run_command_working_directory_cannot_escape_approved_workspace(tmp_path: Path) -> None:
    result = ToolRegistry(PathPolicy(tmp_path)).execute(
        "run_command", {"command": "echo test", "working_dir": ".."}
    )

    assert result.status == "denied"
    assert result.error_code == "policy_or_tool_error"


def test_run_command_timeout_is_reported_and_fails_verification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    def timed_out(*args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired("echo test", 1, output=b"partial output")

    monkeypatch.setattr("autopilot_ai.tools.subprocess.run", timed_out)
    result = ToolRegistry(PathPolicy(tmp_path)).execute(
        "run_command", {"command": "echo test", "timeout_seconds": 1}
    )
    verification = verify_command_execution(result)

    assert result.status == "success"
    assert result.output["timed_out"] is True
    assert result.output["exit_code"] == -1
    assert result.output["stdout"] == "partial output"
    assert verification.passed is False


def test_run_command_non_whitelisted_command_requires_approval(tmp_path: Path) -> None:
    report = run_goal("Run command python --help", tmp_path, planner="heuristic")

    assert report["status"] == "approval_required"
    assert report["plan"]["tool_name"] == "run_command"
    assert report["plan"]["risk_level"] == "medium"
    assert report["plan"]["requires_approval"] is True


def test_run_command_destructive_command_is_denied_by_policy(tmp_path: Path) -> None:
    report = run_goal("Run in CMD command shutdown", tmp_path, planner="heuristic")

    assert report["status"] == "denied"
    assert "destructive-command policy" in report["message"]


def test_run_command_is_never_retried(tmp_path: Path) -> None:
    result = ToolResult(status="error", error_code="filesystem_error")
    verification = Verification(passed=False, summary="failed")

    assert should_retry("run_command", result, verification, attempt=1) is False


def test_launch_app_alias_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    looked_up: list[str] = []

    def fake_which(target: str) -> str | None:
        looked_up.append(target)
        return r"C:\Windows\System32\notepad.exe" if target == "notepad.exe" else None

    monkeypatch.setattr("autopilot_ai.tools.shutil.which", fake_which)

    executable = _resolve_app_executable("notepad")

    assert APP_ALIASES["notepad"] == "notepad.exe"
    assert executable.endswith("notepad.exe")
    assert looked_up == ["notepad", "notepad.exe"]


def test_launch_app_requires_approval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("autopilot_ai.agent.is_app_approved", lambda name: False)
    report = run_goal("Launch Notepad", tmp_path, planner="heuristic")

    assert report["status"] == "approval_required"
    assert report["plan"]["tool_name"] == "launch_app"
    assert report["plan"]["requires_approval"] is True


def test_launch_app_approval_persists_per_normalized_name(tmp_path: Path) -> None:
    config = tmp_path / "approved_apps.json"

    approve_app("  Notepad  ", config)

    assert load_approved_apps(config) == {"notepad"}
    assert is_app_approved("NOTEPAD", config) is True
    assert is_app_approved("Chrome", config) is False


def test_cli_can_approve_and_list_apps(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from autopilot_ai import cli

    approved: set[str] = set()
    monkeypatch.setattr(cli, "approve_app", lambda app: approved.add(app.casefold()))
    monkeypatch.setattr(cli, "load_approved_apps", lambda: approved)
    monkeypatch.setattr("sys.argv", ["autopilot", "--approve-app", "Notepad", "--approve-app", "Calculator"])

    cli.main()
    assert json.loads(capsys.readouterr().out) == {"status": "updated", "approved_apps": ["calculator", "notepad"]}

    monkeypatch.setattr("sys.argv", ["autopilot", "--list-approved-apps"])
    cli.main()
    assert json.loads(capsys.readouterr().out) == {"status": "ok", "approved_apps": ["calculator", "notepad"]}


def test_trusted_app_launch_does_not_require_approval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("autopilot_ai.agent.is_app_approved", lambda name: name == "Notepad")
    monkeypatch.setattr(
        ToolRegistry,
        "execute",
        lambda self, name, arguments: ToolResult(status="success", output={"pid": 4321, "app_name": "Notepad", "executable_path": r"C:\\Windows\\System32\\notepad.exe"}),
    )
    monkeypatch.setattr("autopilot_ai.agent.verify_app_launch", lambda result: Verification(passed=True, summary="verified"))

    report = run_goal("Launch Notepad", tmp_path, planner="heuristic")

    assert report["status"] == "completed"
    assert report["plan"]["requires_approval"] is False


def test_launch_app_uses_resolved_executable_and_returns_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    launched: list[list[str]] = []

    class FakeProcess:
        pid = 4321

    monkeypatch.setattr("autopilot_ai.tools.shutil.which", lambda target: r"C:\Windows\System32\notepad.exe")
    monkeypatch.setattr("autopilot_ai.tools.subprocess.Popen", lambda command: launched.append(command) or FakeProcess())

    result = ToolRegistry(PathPolicy(tmp_path)).execute("launch_app", {"app_name": "notepad", "arguments": ["example.txt"]})

    assert result.status == "success"
    assert result.output == {"pid": 4321, "app_name": "notepad", "executable_path": r"C:\Windows\System32\notepad.exe"}
    assert launched == [[r"C:\Windows\System32\notepad.exe", "example.txt"]]


def test_launch_app_verifier_checks_active_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    class TasklistResult:
        returncode = 0
        stdout = '"notepad.exe","4321","Console","1","10 K"\n'

    monkeypatch.setattr("autopilot_ai.verifier.time.sleep", lambda _: None)
    monkeypatch.setattr("autopilot_ai.verifier.subprocess.run", lambda *args, **kwargs: TasklistResult())
    verification = verify_app_launch(
        ToolResult(status="success", output={"pid": 4321, "app_name": "notepad", "executable_path": r"C:\Windows\System32\notepad.exe"})
    )

    assert verification.passed is True
    assert verification.evidence["pid_matches"] is True
    assert verification.evidence["executable_matches"] is True


def test_launch_app_is_never_retried() -> None:
    assert should_retry("launch_app", ToolResult(status="error", error_code="desktop_error"), Verification(passed=False, summary="failed"), attempt=1) is False


def test_read_file_contents_runs_and_is_independently_verified(tmp_path: Path) -> None:
    target = tmp_path / "notes.md"
    target.write_text("# Notes\nImportant content", encoding="utf-8")

    report = run_goal("Read file notes.md", tmp_path, planner="heuristic")

    assert report["status"] == "completed"
    assert report["plan"]["tool_name"] == "read_file_contents"
    assert report["tool_result"]["output"]["content"] == "# Notes\nImportant content"
    assert report["verification"]["passed"] is True


@pytest.mark.parametrize(
    ("goal", "expected"),
    [
        ("Click at (100, 200)", {"action_type": "click", "x": 100, "y": 200, "text": None, "keys": []}),
        ("Double-click at 40, 50", {"action_type": "double_click", "x": 40, "y": 50, "text": None, "keys": []}),
        ('Type text "hello"', {"action_type": "type_text", "x": None, "y": None, "text": "hello", "keys": []}),
        ("Press Enter", {"action_type": "press_key", "x": None, "y": None, "text": None, "keys": ["enter"]}),
        ("Press Ctrl+S", {"action_type": "hotkey", "x": None, "y": None, "text": None, "keys": ["ctrl", "s"]}),
    ],
)
def test_desktop_action_intents_are_explicit_and_approval_gated(goal: str, expected: dict) -> None:
    report = run_goal(goal, Path.cwd(), planner="heuristic")

    assert report["status"] == "approval_required"
    assert report["plan"]["tool_name"] == "desktop_action"
    assert report["plan"]["arguments"] == expected
    assert report["plan"]["requires_approval"] is True
    assert report["plan"]["risk_level"] == "medium"


def test_desktop_action_schema_rejects_extras_and_conflicting_fields(tmp_path: Path) -> None:
    registry = ToolRegistry(PathPolicy(tmp_path))

    with pytest.raises(ToolArgumentsError):
        registry.validate_arguments("desktop_action", {"action_type": "click", "x": 5, "y": 5, "shell": "bad"})
    with pytest.raises(ToolArgumentsError):
        registry.validate_arguments("desktop_action", {"action_type": "hotkey", "keys": ["ctrl"]})


def test_desktop_action_executes_with_bounds_and_fresh_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    cursor = [20, 30]
    calls: list[tuple[str, object]] = []
    pyautogui = SimpleNamespace(
        FAILSAFE=False,
        PAUSE=0,
        size=lambda: SimpleNamespace(width=800, height=600),
        position=lambda: SimpleNamespace(x=cursor[0], y=cursor[1]),
        click=lambda x, y: (calls.append(("click", (x, y))), cursor.__setitem__(slice(None), [x, y])),
        doubleClick=lambda **kwargs: calls.append(("double", kwargs)),
        rightClick=lambda **kwargs: calls.append(("right", kwargs)),
        write=lambda value, **kwargs: calls.append(("write", value)),
        press=lambda key: calls.append(("press", key)),
        hotkey=lambda *keys: calls.append(("hotkey", keys)),
    )
    win32gui = SimpleNamespace(GetForegroundWindow=lambda: 123)
    monkeypatch.setitem(sys.modules, "pyautogui", pyautogui)
    monkeypatch.setitem(sys.modules, "win32gui", win32gui)

    report = run_goal("Click at 100, 200", tmp_path, planner="heuristic", approved=True)

    assert report["status"] == "completed"
    assert calls == [("click", (100, 200))]
    assert pyautogui.FAILSAFE is True
    assert pyautogui.PAUSE == 0.25
    assert report["verification"]["passed"] is True
    assert report["verification"]["evidence"]["cursor_matches_requested_position"] is True


def test_desktop_action_rejects_edge_coordinates_and_is_never_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    pyautogui = SimpleNamespace(
        FAILSAFE=False, PAUSE=0,
        size=lambda: SimpleNamespace(width=800, height=600),
        position=lambda: SimpleNamespace(x=10, y=10),
    )
    monkeypatch.setitem(sys.modules, "pyautogui", pyautogui)
    monkeypatch.setitem(sys.modules, "win32gui", SimpleNamespace(GetForegroundWindow=lambda: 123))
    result = ToolRegistry(PathPolicy(tmp_path)).execute(
        "desktop_action", {"action_type": "click", "x": 0, "y": 20}
    )

    assert result.status == "denied"
    assert result.error_code == "desktop_policy_denied"
    assert should_retry("desktop_action", result, Verification(passed=False, summary="denied"), 1) is False


@pytest.mark.parametrize("goal", ["Open analysis.pdf", "Open file analysis.pdf", 'Please open the file "analysis.pdf"'])
def test_open_supported_document_is_planned_as_file_read(goal: str) -> None:
    plan = plan_goal_steps(goal)[0]

    assert plan.tool_name == "read_file_contents"
    assert plan.arguments["path"] == "analysis.pdf"
    assert plan.requires_approval is False


def test_open_file_name_reads_approved_text_file(tmp_path: Path) -> None:
    target = tmp_path / "notes.md"
    target.write_text("Document text", encoding="utf-8")

    report = run_goal("Open file notes.md", tmp_path, planner="heuristic")

    assert report["status"] == "completed"
    assert report["plan"]["tool_name"] == "read_file_contents"
    assert report["tool_result"]["output"]["content"] == "Document text"
    assert report["verification"]["passed"] is True


def test_read_file_contents_is_confined_to_approved_workspace(tmp_path: Path) -> None:
    result = ToolRegistry(PathPolicy(tmp_path)).execute("read_file_contents", {"path": "../outside.txt"})

    assert result.status == "denied"
    assert result.error_code == "policy_or_tool_error"


def test_read_file_contents_truncates_and_verifier_detects_tampering(tmp_path: Path) -> None:
    target = tmp_path / "large.txt"
    target.write_text("x" * 150, encoding="utf-8")
    result = ToolRegistry(PathPolicy(tmp_path)).execute(
        "read_file_contents", {"path": "large.txt", "max_characters": 100}
    )

    assert result.status == "success"
    assert result.output["content"] == "x" * 100
    assert result.output["truncated"] is True
    assert verify_file_contents(result, target, 100, None).passed is True
    result.output["content"] = "altered"
    assert verify_file_contents(result, target, 100, None).passed is False


def test_read_pdf_contents_and_a_selected_page(tmp_path: Path) -> None:
    from pypdf import PdfWriter

    target = tmp_path / "sample.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with target.open("wb") as stream:
        writer.write(stream)

    result = ToolRegistry(PathPolicy(tmp_path)).execute(
        "read_file_contents", {"path": "sample.pdf", "page_number": 1}
    )

    assert result.status == "success"
    assert result.output["file_type"] == "pdf"
    assert result.output["page_number"] == 1
    assert verify_file_contents(result, target, 8000, 1).passed is True


def test_read_file_contents_is_retryable_after_transient_read_error() -> None:
    assert should_retry(
        "read_file_contents", ToolResult(status="error", error_code="filesystem_error"), Verification(passed=False, summary="failed"), attempt=1
    ) is True


def test_write_file_requires_approval_then_writes_and_verifies(tmp_path: Path) -> None:
    preview = run_goal("Write file notes.txt with content Hello", tmp_path, planner="heuristic")
    assert preview["status"] == "approval_required"
    assert preview["plan"]["tool_name"] == "write_file"
    assert preview["plan"]["arguments"]["mode"] == "create_new"
    assert not (tmp_path / "notes.txt").exists()

    report = run_goal("Write file notes.txt with content Hello", tmp_path, planner="heuristic", approved=True)

    assert report["status"] == "completed"
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "Hello"
    assert report["verification"]["passed"] is True
    assert report["tool_result"]["output"]["bytes_written"] == 5


def test_write_file_overwrite_and_append_are_atomically_verified(tmp_path: Path) -> None:
    from hashlib import sha256

    target = tmp_path / "notes.txt"
    target.write_text("before", encoding="utf-8")
    registry = ToolRegistry(PathPolicy(tmp_path))

    overwritten = registry.execute("write_file", {"path": "notes.txt", "content": "replacement", "mode": "overwrite"})
    assert overwritten.status == "success"
    assert overwritten.output["sha256"] == sha256(b"replacement").hexdigest()
    assert verify_written_file(overwritten, target, "replacement", "overwrite").passed is True

    appended = registry.execute("write_file", {"path": "notes.txt", "content": " + more", "mode": "append"})
    assert appended.status == "success"
    assert target.read_text(encoding="utf-8") == "replacement + more"
    assert appended.output["sha256"] == sha256(b"replacement + more").hexdigest()
    assert verify_written_file(appended, target, " + more", "append").passed is True


def test_write_file_rejects_overwrite_create_and_path_escape(tmp_path: Path) -> None:
    (tmp_path / "existing.txt").write_text("keep", encoding="utf-8")
    registry = ToolRegistry(PathPolicy(tmp_path))

    existing = registry.execute("write_file", {"path": "existing.txt", "content": "bad"})
    escape = registry.execute("write_file", {"path": "../outside.txt", "content": "bad"})
    missing_append = registry.execute("write_file", {"path": "missing.txt", "content": "bad", "mode": "append"})

    assert existing.status == "denied"
    assert escape.status == "denied"
    assert missing_append.status == "denied"
    assert (tmp_path / "existing.txt").read_text(encoding="utf-8") == "keep"


def test_write_file_schema_is_strict_and_extra_forbidden(tmp_path: Path) -> None:
    registry = ToolRegistry(PathPolicy(tmp_path))

    extra = registry.execute("write_file", {"path": "new.txt", "content": "x", "unexpected": True})
    bad_root = registry.execute("write_file", {"path": "new.txt", "content": "x", "root_index": "0"})

    assert extra.status == "denied" and extra.error_code == "invalid_arguments"
    assert bad_root.status == "denied" and bad_root.error_code == "invalid_arguments"


def test_write_file_is_never_retried() -> None:
    assert should_retry(
        "write_file", ToolResult(status="error", error_code="filesystem_error"), Verification(passed=False, summary="failed"), attempt=1
    ) is False


def test_write_file_planner_requires_approval_and_preserves_content() -> None:
    step = plan_goal_steps("Write file notes.txt with content First line")[0]

    assert step.tool_name == "write_file"
    assert step.arguments == {"path": "notes.txt", "content": "First line", "mode": "create_new", "root_index": 0}
    assert step.risk_level == "medium"
    assert step.requires_approval is True


def test_run_command_schema_forbids_extra_or_coerced_arguments(tmp_path: Path) -> None:
    registry = ToolRegistry(PathPolicy(tmp_path))

    extra = registry.execute("run_command", {"command": "echo test", "unexpected": True})
    coerced_timeout = registry.execute("run_command", {"command": "echo test", "timeout_seconds": "1"})

    assert extra.status == "denied"
    assert extra.error_code == "invalid_arguments"
    assert coerced_timeout.status == "denied"
    assert coerced_timeout.error_code == "invalid_arguments"


def test_natural_language_listing_runs_and_verifies(tmp_path: Path) -> None:
    (tmp_path / "sample.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "subfolder").mkdir()
    report = run_goal("Show me the files in this folder", tmp_path)
    assert report["status"] == "completed"
    assert report["verification"]["passed"] is True
    assert report["verification"]["evidence"]["fresh_names"] == ["sample.txt", "subfolder"]


def test_search_output_numbers_matches_with_filename_and_full_path() -> None:
    report = {
        "status": "completed",
        "goal": "Find report.pdf",
        "plan": {"tool_name": "search_files", "description": "Search filenames."},
        "tool_result": {"status": "success", "output": {"matches": [r"D:\\Reports\\report.pdf", r"D:\\Archive\\report.pdf"], "complete": True}},
        "verification": {"passed": True, "summary": "verified"},
    }
    rendered = format_report(report)
    assert "1) report.pdf" in rendered
    assert r"D:\\Reports\\report.pdf" in rendered
    assert "2) report.pdf" in rendered
    assert r"D:\\Archive\\report.pdf" in rendered


def test_session_state_resolves_ordinal_and_clears_after_selection() -> None:
    state = SessionState()
    state.set_pending("Which file did you mean?", (r"D:\\A\\report.pdf", r"D:\\B\\report.pdf"))

    assert state.select_candidate("the second one") == r"D:\\B\\report.pdf"
    assert state.pending_question is None
    assert state.candidates == ()


def test_session_state_keeps_context_for_invalid_reply_and_can_clear() -> None:
    state = SessionState()
    state.set_pending("Choose a file", ("one", "two"))

    assert state.select_candidate("maybe") is None
    assert state.pending_question == "Choose a file"
    assert state.select_candidate("3") is None
    assert state.is_candidate_reply("the third one") is True
    assert state.is_candidate_reply("Find budget") is False
    state.clear()
    assert state.pending_question is None
    assert state.candidates == ()


def test_conversation_retains_multiple_search_results_for_ordinal_reply() -> None:
    session = ConversationSession(memory=TaskMemory(":memory:"))
    report = {
        "status": "completed",
        "goal": "Find report.pdf",
        "plan": {"tool_name": "search_files"},
        "tool_result": {"status": "success", "output": {"matches": ["D:/Reports/report.pdf", "D:/Archive/report.pdf"], "complete": True}},
        "verification": {"passed": True},
    }
    first_turn = session.handle("Find report.pdf", lambda _: report)

    assert first_turn["follow_up_question"] == "Which search result do you mean?"
    selected = session.handle("the second one", lambda _: pytest.fail("A result reply must not start a new task."))
    assert selected["status"] == "candidate_selected"
    assert selected["selected_path"] == "D:/Archive/report.pdf"


def test_conversation_reasks_after_out_of_range_ordinal_then_accepts_choice() -> None:
    session = ConversationSession(memory=TaskMemory(":memory:"))
    report = {
        "status": "completed",
        "plan": {"tool_name": "search_files"},
        "tool_result": {"status": "success", "output": {"matches": ["D:/A/report.pdf", "D:/B/report.pdf"], "complete": True}},
        "verification": {"passed": True},
    }
    session.handle("Find report.pdf", lambda _: report)

    invalid = session.handle("the third one", lambda _: pytest.fail("An invalid ordinal is still a selection reply."))
    assert invalid["status"] == "clarification_required"
    assert invalid["candidate_kind"] == "search_result"
    assert len(invalid["candidates"]) == 2
    assert "Matching search results:" in format_report(invalid)

    selected = session.handle("the first one", lambda _: pytest.fail("Pending choice should resolve in memory."))
    assert selected["selected_path"] == "D:/A/report.pdf"


def test_debug_report_rendering_preserves_structured_candidate_evidence() -> None:
    report = {
        "status": "candidate_selected",
        "goal": "the second one",
        "selected_path": "D:/Reports/report.pdf",
        "message": "Selected result.",
        "candidates": ["D:/Reports/report.pdf", "D:/Archive/report.pdf"],
    }

    rendered = render_report(report, debug=True)

    assert json.loads(rendered) == report
    assert "D:/Archive/report.pdf" in rendered


def test_interactive_cli_loops_through_invalid_and_valid_candidate_replies(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from autopilot_ai import cli

    goals: list[str] = []
    report = {
        "status": "completed",
        "goal": "Find report.pdf",
        "plan": {"tool_name": "search_files", "description": "Search filenames."},
        "tool_result": {
            "status": "success",
            "output": {"query": "report.pdf", "match_mode": "exact", "roots": ["D:/Reports"], "matches": ["D:/Reports/report.pdf", "D:/Archive/report.pdf"], "count": 2, "truncated": False, "complete": True, "errors": []},
        },
        "verification": {"passed": True, "summary": "Search independently verified."},
    }

    def fake_run_goal(goal: str, *args: object, **kwargs: object) -> dict:
        goals.append(goal)
        return report

    user_inputs = iter(("Find report.pdf", "the third one", "the second one", "exit"))
    monkeypatch.setattr("sys.argv", ["autopilot_ai.cli", "--interactive", "--planner", "heuristic"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(user_inputs))
    monkeypatch.setattr(cli, "load_approved_dirs", lambda: [])
    monkeypatch.setattr(cli, "run_goal", fake_run_goal)

    cli.main()

    output = capsys.readouterr().out
    assert goals == ["Find report.pdf"]
    assert "Matching search results:" in output
    assert "the third one" in output
    assert "Selected result: D:/Archive/report.pdf" in output
    assert "Conversation ended." in output


def test_conversation_discards_stale_candidates_when_new_task_arrives() -> None:
    session = ConversationSession(memory=TaskMemory(":memory:"))
    multi_match_report = {
        "status": "completed",
        "plan": {"tool_name": "search_files"},
        "tool_result": {"status": "success", "output": {"matches": ["one", "two"], "complete": True}},
        "verification": {"passed": True},
    }
    session.handle("Find reports", lambda _: multi_match_report)
    new_report = {"status": "unsupported", "goal": "open settings"}
    assert session.handle("open settings", lambda goal: new_report)["status"] == "unsupported"
    assert session.handle("the second one", lambda _: {"status": "unsupported"})["status"] == "unsupported"


def test_task_memory_persists_steps_resumes_sessions_and_searches(tmp_path: Path) -> None:
    memory = TaskMemory(tmp_path / "memory.db")
    session_id = memory.create_session()
    memory.record_step(session_id, "Find report.pdf", "search_files", "success", "Found one report", {"matches": ["report.pdf"]})

    resumed = ConversationSession(memory, session_id)
    report = resumed.handle("Read report.pdf", lambda goal: {"status": "unsupported", "goal": goal, "message": "No read requested."})

    history = memory.get_recent_history(session_id)
    results = memory.search_memory("report")
    assert report["session_id"] == session_id
    assert f"Session ID: {session_id}" in format_report(report)
    assert [entry["tool_name"] for entry in history] == ["search_files", "conversation"]
    assert history[0]["output"] == {"matches": ["report.pdf"]}
    assert len(results) == 2


def test_task_memory_rejects_unknown_resume_session(tmp_path: Path) -> None:
    memory = TaskMemory(tmp_path / "memory.db")

    with pytest.raises(ValueError, match="Unknown session ID"):
        ConversationSession(memory, "missing-session")


def test_in_memory_task_memory_persists_across_operations() -> None:
    memory = TaskMemory(":memory:")
    session_id = memory.create_session()
    memory.record_step(session_id, "Find a file", "search_files", "success", "Found it", {"count": 1})

    assert memory.has_session(session_id)
    assert memory.get_recent_history(session_id)[0]["summary"] == "Found it"


def test_task_memory_cli_history_lists_persisted_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from autopilot_ai import cli

    memory_path = tmp_path / "memory.db"
    session_id = TaskMemory(memory_path).create_session()
    TaskMemory(memory_path).record_step(session_id, "Run command echo test", "run_command", "success", "Command completed", {"exit_code": 0})
    monkeypatch.setattr(cli, "TaskMemory", lambda: TaskMemory(memory_path))
    monkeypatch.setattr("sys.argv", ["autopilot_ai.cli", "--history", session_id])

    cli.main()

    output = json.loads(capsys.readouterr().out)
    assert output["history"][0]["session_id"] == session_id
    assert output["history"][0]["tool_name"] == "run_command"


def test_directory_verifier_checks_types_sizes_path_and_count(tmp_path: Path) -> None:
    file_path = tmp_path / "sample.txt"
    file_path.write_text("content", encoding="utf-8")
    result = ToolRegistry(PathPolicy(tmp_path)).execute("list_directory", {"path": "."})
    assert result.status == "success"

    result.output["items"][0]["size_bytes"] = 999
    verification = verify_directory_listing(result, tmp_path.resolve())

    assert verification.passed is False
    assert verification.evidence["path_matches"] is True
    assert verification.evidence["count_matches"] is True


def test_natural_language_search_is_recursive_and_verified(tmp_path: Path) -> None:
    nested = tmp_path / "reports" / "2026"
    nested.mkdir(parents=True)
    (nested / "Budget-Q4.xlsx").write_bytes(b"sheet")
    (nested / "notes.txt").write_text("budget is mentioned in contents only", encoding="utf-8")

    report = run_goal("Find files containing budget", tmp_path)

    assert report["status"] == "completed"
    assert report["plan"]["tool_name"] == "search_files"
    assert report["tool_result"]["output"]["matches"] == [str((nested / "Budget-Q4.xlsx").resolve())]
    assert report["verification"]["passed"] is True
    assert report["verification"]["evidence"]["fresh_matches"] == [str((nested / "Budget-Q4.xlsx").resolve())]


def test_bounded_multi_action_plan_verifies_each_step(tmp_path: Path) -> None:
    (tmp_path / "report.pdf").write_bytes(b"pdf")

    report = run_goal("Show me the files and then find report.pdf", tmp_path)

    assert report["status"] == "completed"
    assert report["planner"] == "heuristic"
    assert [item["plan"]["tool_name"] for item in report["steps"]] == ["list_directory", "search_files"]
    assert all(item["verification"]["passed"] for item in report["steps"])
    assert report["steps"][1]["tool_result"]["output"]["matches"] == [str((tmp_path / "report.pdf").resolve())]


def test_multi_action_failure_reports_partial_completion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original_execute = ToolRegistry.execute

    def fail_search(self: ToolRegistry, name: str, arguments: dict) -> ToolResult:
        if name == "search_files":
            return ToolResult(status="error", error_code="test_failure", output={"detail": "simulated search failure"})
        return original_execute(self, name, arguments)

    monkeypatch.setattr(ToolRegistry, "execute", fail_search)

    report = run_goal("Show files and then find report.pdf", tmp_path)
    rendered = format_report(report)

    assert report["status"] == "partially_completed"
    assert len(report["steps"]) == 2
    assert report["steps"][0]["verification"]["passed"] is True
    assert "Status: Partially completed" in rendered
    assert "simulated search failure" in rendered


def test_incomplete_filename_scan_is_not_reported_as_fully_failed_or_completed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ToolRegistry,
        "execute",
        lambda self, name, arguments: ToolResult(
            status="success",
            output={"query": "report.pdf", "match_mode": "exact", "roots": [str(tmp_path.resolve())], "matches": [], "count": 0, "truncated": False, "complete": False, "errors": ["permission denied"], "entries_scanned": 1},
        ),
    )
    monkeypatch.setattr(
        "autopilot_ai.agent.verify_file_search",
        lambda *args, **kwargs: Verification(passed=False, summary="Search is incomplete."),
    )

    report = run_goal("Find report.pdf", tmp_path)
    rendered = format_report(report)

    assert report["status"] == "partially_completed"
    assert report["verification"]["passed"] is False
    assert "candidate file(s) before the scan became incomplete" in rendered
    assert "more matches may exist" in rendered
    assert "These results are not exhaustive" in rendered


def test_normal_cli_report_is_readable_and_shows_actual_file_location(tmp_path: Path) -> None:
    (tmp_path / "annual-report.pdf").write_bytes(b"pdf")

    rendered = format_report(run_goal("Find annual-report.pdf", tmp_path))

    assert "Task: Find annual-report.pdf" in rendered
    assert "Status: Completed" in rendered
    assert str((tmp_path / "annual-report.pdf").resolve()) in rendered
    assert "Verification: passed" in rendered


def test_normal_cli_report_shows_desktop_observation() -> None:
    report = {
        "status": "completed",
        "goal": "Inspect active window",
        "plan": {"description": "Inspect foreground window", "tool_name": "inspect_active_window"},
        "tool_result": {"status": "success", "output": {"backend": "uia", "window_title": "Editor", "controls_returned": 1, "controls": [{"name": "Save", "control_type": "Button"}]}},
        "verification": {"passed": True, "summary": "Window verified."},
    }

    rendered = format_report(report)

    assert "Window: Editor" in rendered
    assert "Save [Button]" in rendered
    assert "Verification: passed" in rendered


def test_normal_cli_report_explains_screenshot_fallback() -> None:
    report = {
        "status": "completed",
        "goal": "Inspect active window",
        "plan": {"description": "Inspect foreground window", "tool_name": "inspect_active_window"},
        "tool_result": {
            "status": "success",
            "output": {
                "backend": "pyautogui_screenshot",
                "screenshot_path": "D:/Agent/down/screenshots/window.png",
                "uia_note": "UIA could not read this application.",
            },
        },
        "verification": {"passed": True, "summary": "Active-window screenshot independently verified."},
    }

    rendered = format_report(report)

    assert "screenshot saved to" in rendered
    assert "UI Automation could not read the window" in rendered
    assert "UIA could not read this application" in rendered


def test_multi_action_ollama_plan_is_used_when_it_matches_intent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "report.pdf").write_bytes(b"pdf")
    expected = plan_goal_steps("Show files and find report.pdf")
    monkeypatch.setattr("autopilot_ai.agent.plan_with_ollama_steps", lambda goal, tools: expected)

    report = run_goal("Show files and find report.pdf", tmp_path, planner="ollama")

    assert report["status"] == "completed"
    assert report["planner"] == "ollama"
    assert len(report["steps"]) == 2


def test_multi_action_ollama_mismatch_uses_deterministic_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "report.pdf").write_bytes(b"pdf")
    wrong = plan_goal_steps("Show files and find other.pdf")
    monkeypatch.setattr("autopilot_ai.agent.plan_with_ollama_steps", lambda goal, tools: wrong)

    report = run_goal("Show files and find report.pdf", tmp_path, planner="ollama")

    assert report["status"] == "completed"
    assert report["planner"] == "heuristic_fallback"
    assert report["steps"][1]["plan"]["arguments"]["query"] == "report.pdf"


def test_ollama_extra_tool_arguments_fall_back_to_intent_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proposed = PlanStep(
        step_id="step-1",
        description="List files",
        tool_name="list_directory",
        arguments={"path": ".", "unexpected": "value"},
        expected_result="List files",
    )
    monkeypatch.setattr("autopilot_ai.agent.plan_with_ollama", lambda goal, tools: proposed)

    report = run_goal("Show files", tmp_path, planner="ollama")

    assert report["status"] == "completed"
    assert report["planner"] == "heuristic_fallback"
    assert report["plan"]["arguments"] == {"path": "."}


def test_malformed_plan_arguments_are_denied_before_path_access(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    malformed = PlanStep(
        step_id="step-1",
        description="Copy file",
        tool_name="copy_file",
        arguments={"source_path": "source.txt"},
        expected_result="Copied",
        risk_level="medium",
        requires_approval=True,
    )
    monkeypatch.setattr("autopilot_ai.agent.plan_goal_steps", lambda goal: [malformed])

    report = run_goal("Copy file source.txt to backup.txt", tmp_path, planner="heuristic")

    assert report["status"] == "denied"
    assert report["message"].startswith("The proposed plan contains invalid tool arguments")
    assert report["argument_errors"][0]["loc"] == ("destination_path",)
    assert not (tmp_path / "backup.txt").exists()


def test_multi_action_plan_is_bounded_to_three_steps(tmp_path: Path) -> None:
    report = run_goal("Show files and find a.pdf and list files and find b.pdf", tmp_path)
    assert report["status"] == "unsupported"


def test_create_directory_requires_approval_then_verifies(tmp_path: Path) -> None:
    preview = run_goal("Create folder reports", tmp_path)
    assert preview["status"] == "approval_required"
    assert preview["plan"]["requires_approval"] is True
    assert not (tmp_path / "reports").exists()

    approved = run_goal("Create folder reports", tmp_path, approved=True)
    assert approved["status"] == "completed"
    assert approved["verification"]["passed"] is True
    assert (tmp_path / "reports").is_dir()


def test_copy_file_requires_approval_and_verifies_digest(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_bytes(b"copy me\x00\xff")

    preview = run_goal("Copy file source.txt to copy.txt", tmp_path)
    assert preview["status"] == "approval_required"
    assert not (tmp_path / "copy.txt").exists()

    approved = run_goal("Copy file source.txt to copy.txt", tmp_path, approved=True)
    assert approved["status"] == "completed"
    assert approved["verification"]["passed"] is True
    assert (tmp_path / "copy.txt").read_bytes() == source.read_bytes()


def test_react_file_filter_copy_stops_for_approval_then_searches_reads_and_verifies_copy(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    downloads = tmp_path / "Downloads"
    workspace.mkdir()
    downloads.mkdir()
    (downloads / "invoice.txt").write_text("Please pay this invoice.", encoding="utf-8")
    (downloads / "notes.txt").write_text("Meeting notes only.", encoding="utf-8")
    request = "Find all text files in Downloads, check which ones mention 'invoice', and copy them to workspace/invoices"

    preview = run_goal(request, workspace, approved_dirs=[downloads])

    assert preview["status"] == "approval_required"
    assert preview["workflow"] == "react_file_filter_copy"
    assert len(preview["observations"]) == 3  # search and inspect each matching file type result
    assert len(preview["planned_actions"]) == 2  # create destination and copy the matching file
    preview_text = render_report(preview)
    assert "Status: Approval required" in preview_text
    assert "invoice.txt" in preview_text
    assert "rerun the same request with --approve" in preview_text
    assert not (workspace / "invoices").exists()

    result = run_goal(request, workspace, approved_dirs=[downloads], approved=True)

    assert result["status"] == "completed"
    assert result["workflow"] == "react_file_filter_copy"
    assert (workspace / "invoices" / "invoice.txt").read_text(encoding="utf-8") == "Please pay this invoice."
    assert not (workspace / "invoices" / "notes.txt").exists()
    assert all(item["verification"]["passed"] for item in result["observations"] if "verification" in item)


def test_react_file_filter_copy_does_not_copy_when_search_is_incomplete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    downloads = tmp_path / "Downloads"
    workspace.mkdir()
    downloads.mkdir()
    (downloads / "invoice.txt").write_text("invoice", encoding="utf-8")
    original_execute = ToolRegistry.execute

    def incomplete_search(self: ToolRegistry, tool_name: str, arguments: dict) -> ToolResult:
        result = original_execute(self, tool_name, arguments)
        if tool_name == "search_files":
            return result.model_copy(update={"output": {**result.output, "complete": False}})
        return result

    monkeypatch.setattr(ToolRegistry, "execute", incomplete_search)
    result = run_goal(
        "Find text files in Downloads, check which ones mention invoice, and copy them to workspace/invoices",
        workspace,
        approved_dirs=[downloads],
        approved=True,
    )

    assert result["status"] == "partially_completed"
    assert "no files were copied" in result["summary"]
    assert not (workspace / "invoices").exists()


def test_react_file_filter_copy_denies_existing_target_without_claiming_partial_mutation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    downloads = tmp_path / "Downloads"
    destination = workspace / "invoices"
    workspace.mkdir()
    downloads.mkdir()
    destination.mkdir()
    (downloads / "invoice.txt").write_text("invoice", encoding="utf-8")
    (destination / "invoice.txt").write_text("keep existing", encoding="utf-8")

    result = run_goal(
        "Find text files in Downloads, check which ones mention invoice, and copy them to workspace/invoices",
        workspace,
        approved_dirs=[downloads],
        approved=True,
    )

    assert result["status"] == "denied"
    assert (destination / "invoice.txt").read_text(encoding="utf-8") == "keep existing"


def test_copy_file_refuses_overwrite_and_path_escape(tmp_path: Path) -> None:
    (tmp_path / "source.txt").write_text("source", encoding="utf-8")
    (tmp_path / "existing.txt").write_text("keep", encoding="utf-8")
    registry = ToolRegistry(PathPolicy(tmp_path))

    overwrite = registry.execute("copy_file", {"source_path": "source.txt", "destination_path": "existing.txt"})
    escape = registry.execute("create_directory", {"path": "../outside"})

    assert overwrite.status == "denied"
    assert escape.status == "denied"
    assert (tmp_path / "existing.txt").read_text(encoding="utf-8") == "keep"


def test_copy_file_preserves_destination_created_after_precheck(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "source.txt").write_text("source content", encoding="utf-8")
    policy = PathPolicy(tmp_path)
    destination = tmp_path / "raced.txt"

    def destination_appears_during_resolution(path: str, root_index: int = 0) -> Path:
        destination.write_text("concurrent content", encoding="utf-8")
        return destination

    monkeypatch.setattr(policy, "resolve_new_path", destination_appears_during_resolution)
    result = ToolRegistry(policy).execute(
        "copy_file",
        {"source_path": "source.txt", "destination_path": "raced.txt"},
    )

    assert result.status == "denied"
    assert destination.read_text(encoding="utf-8") == "concurrent content"
    assert list(tmp_path.glob(".*.autopilot-tmp")) == []


def test_atomic_new_file_publisher_uses_windows_rename_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from autopilot_ai import tools

    temporary = tmp_path / "stage.tmp"
    destination = tmp_path / "final.txt"
    temporary.write_text("contents", encoding="utf-8")
    rename_calls: list[tuple[Path, Path]] = []

    def denied_hard_link(source: Path, target: Path) -> None:
        raise PermissionError("hard links are unavailable")

    def windows_rename(source: Path, target: Path) -> None:
        rename_calls.append((source, target))
        target.write_bytes(source.read_bytes())
        source.unlink()

    monkeypatch.setattr(tools.os, "name", "nt")
    monkeypatch.setattr(tools.os, "link", denied_hard_link)
    monkeypatch.setattr(tools.os, "rename", windows_rename)
    tools._publish_new_file(temporary, destination, "exists")

    assert rename_calls == [(temporary, destination)]
    assert destination.read_text(encoding="utf-8") == "contents"
    assert not temporary.exists()


def test_atomic_new_file_publisher_fallback_preserves_existing_destination(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from autopilot_ai import tools

    temporary = tmp_path / "stage.tmp"
    destination = tmp_path / "final.txt"
    temporary.write_text("new", encoding="utf-8")
    destination.write_text("existing", encoding="utf-8")
    monkeypatch.setattr(tools.os, "name", "nt")
    monkeypatch.setattr(tools.os, "link", lambda *_: (_ for _ in ()).throw(PermissionError("denied")))
    monkeypatch.setattr(tools.os, "rename", lambda *_: (_ for _ in ()).throw(FileExistsError("exists")))

    with pytest.raises(tools.ToolError, match="destination already exists"):
        tools._publish_new_file(temporary, destination, "The destination already exists; overwrite is not allowed.")
    assert destination.read_text(encoding="utf-8") == "existing"


def test_copy_file_reports_source_read_failure_without_creating_destination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.txt"
    source.write_text("source content", encoding="utf-8")
    original_open = Path.open

    def fail_source_read(path: Path, *args, **kwargs):
        if path == source and args and args[0] == "rb":
            raise PermissionError("simulated access denied")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_source_read)
    result = ToolRegistry(PathPolicy(tmp_path)).execute(
        "copy_file",
        {"source_path": "source.txt", "destination_path": "copy.txt"},
    )

    assert result.status == "error"
    assert result.error_code == "filesystem_error"
    assert not (tmp_path / "copy.txt").exists()
    assert list(tmp_path.glob(".*.autopilot-tmp")) == []


def test_public_webpage_request_runs_and_is_freshly_verified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    page_result = {"requested_url": "https://example.com/", "url": "https://example.com/", "http_status": 200, "title": "Example Domain", "text": "Example page", "text_truncated": False}
    monkeypatch.setattr("autopilot_ai.tools.open_webpage", lambda url: page_result)
    monkeypatch.setattr("autopilot_ai.verifier.open_webpage", lambda url: page_result)

    report = run_goal("Open https://example.com/", tmp_path)

    assert report["status"] == "completed"
    assert report["plan"]["tool_name"] == "open_webpage"
    assert report["planner"] == "heuristic"
    assert report["verification"]["passed"] is True
    assert report["verification"]["evidence"]["fresh_http_status"] == 200


def test_search_web_tool_normalizes_limits_and_verifies_provider_results(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from autopilot_ai.verifier import verify_web_search

    calls: list[tuple[str, int, str]] = []

    class FakeDDGS:
        def __init__(self, timeout: int) -> None:
            assert timeout == 10

        def text(self, query: str, max_results: int, backend: str) -> list[dict[str, str]]:
            calls.append((query, max_results, backend))
            return [
                {"title": "Python 3.12 What's New", "href": "https://docs.python.org/3.12/whatsnew/3.12.html", "body": "Release notes and highlights."},
                {"title": "Unsafe local link", "href": "http://127.0.0.1/admin", "body": "Must be discarded."},
                {"title": "Duplicate", "href": "https://docs.python.org/3.12/whatsnew/3.12.html", "body": "Duplicate URL."},
            ]

    monkeypatch.setitem(sys.modules, "ddgs", types.SimpleNamespace(DDGS=FakeDDGS))
    result = ToolRegistry(PathPolicy(tmp_path)).execute("search_web", {"query": "  Python 3.12 release notes  ", "max_results": 5})

    assert result.status == "success"
    assert calls == [("Python 3.12 release notes", 5, "auto")]
    assert result.output["provider"] == "DDGS metasearch"
    assert result.output["count"] == 1
    assert result.output["results"][0]["rank"] == 1
    verification = verify_web_search(result, "Python 3.12 release notes", 5)
    assert verification.passed is True


def test_search_web_rejects_invalid_args_and_reports_provider_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from autopilot_ai.web_search import WebSearchError

    registry = ToolRegistry(PathPolicy(tmp_path))
    invalid = registry.execute("search_web", {"query": " ", "max_results": 11})
    assert invalid.status == "denied"
    assert invalid.error_code == "invalid_arguments"

    class FailedDDGS:
        def __init__(self, timeout: int) -> None:
            pass

        def text(self, query: str, max_results: int, backend: str) -> list[dict[str, str]]:
            raise TimeoutError("provider timed out")

    monkeypatch.setitem(sys.modules, "ddgs", types.SimpleNamespace(DDGS=FailedDDGS))
    failed = registry.execute("search_web", {"query": "Python releases"})
    assert failed.status == "error"
    assert failed.error_code == "web_search_error"
    assert "provider timed out" in failed.output["detail"]


def test_search_web_treats_ddgs_no_results_exception_as_verified_empty_search(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from ddgs.exceptions import DDGSException
    from autopilot_ai.verifier import verify_web_search

    class EmptyDDGS:
        def __init__(self, timeout: int) -> None:
            pass

        def text(self, query: str, max_results: int, backend: str) -> list[dict[str, str]]:
            raise DDGSException("No results found.")

    monkeypatch.setitem(sys.modules, "ddgs", types.SimpleNamespace(DDGS=EmptyDDGS))
    result = ToolRegistry(PathPolicy(tmp_path)).execute("search_web", {"query": "a query with no hits"})

    assert result.status == "success"
    assert result.output["results"] == []
    assert result.output["count"] == 0
    assert verify_web_search(result, "a query with no hits", 5).passed is True


def test_heuristic_search_google_phrase_selects_duckduckgo_and_preserves_query(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from autopilot_ai import tools

    page = {"query": "Python 3.12 release notes", "provider": "DDGS metasearch", "count": 1,
            "results": [{"rank": 1, "title": "Python 3.12 What's New", "url": "https://docs.python.org/3.12/whatsnew/3.12.html", "snippet": "Release notes and highlights."}]}
    monkeypatch.setattr(tools, "search_web", lambda query, max_results=5: page)

    report = run_goal("Search Google for Python 3.12 release notes and summarize the top result", tmp_path, planner="heuristic")

    assert report["status"] == "completed"
    assert report["plan"]["tool_name"] == "search_web"
    assert report["plan"]["arguments"]["query"] == "Python 3.12 release notes"
    assert report["verification"]["passed"] is True
    output = render_report(report)
    assert "Provider: DDGS metasearch" in output
    assert "Python 3.12 What's New" in output
    assert "Top-result snippet: Release notes and highlights." in output


@pytest.mark.parametrize(
    ("reported_url", "fresh_status"),
    [("https://other.example/", 200), ("https://example.com/", 503)],
)
def test_webpage_verifier_rejects_mismatched_request_or_response(
    monkeypatch: pytest.MonkeyPatch, reported_url: str, fresh_status: int
) -> None:
    requested = "https://example.com/"
    observed = {
        "requested_url": reported_url,
        "url": requested,
        "http_status": 200,
        "title": "Example",
        "text": "Visible page text",
    }
    fresh = {**observed, "requested_url": requested, "http_status": fresh_status}
    monkeypatch.setattr("autopilot_ai.verifier.open_webpage", lambda url: fresh)

    verification = verify_webpage_open(ToolResult(status="success", output=observed), requested)

    assert verification.passed is False
    if reported_url != requested:
        assert verification.evidence["reported_requested_url"] == reported_url
    else:
        assert verification.evidence["status_matches"] is False


def test_webpage_verifier_accepts_dynamic_title_when_url_status_and_text_are_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested = "https://example.com/"
    observed = {
        "requested_url": requested,
        "url": requested,
        "http_status": 200,
        "title": "Example at 10:00",
        "text": "Page content",
    }
    fresh = {**observed, "title": "Example at 10:01"}
    monkeypatch.setattr("autopilot_ai.verifier.open_webpage", lambda url: fresh)

    verification = verify_webpage_open(ToolResult(status="success", output=observed), requested)

    assert verification.passed is True
    assert verification.evidence["title_matches"] is False
    assert verification.evidence["visible_text_present"] is True


def test_webpage_verifier_rejects_empty_visible_text(monkeypatch: pytest.MonkeyPatch) -> None:
    requested = "https://example.com/"
    observed = {
        "requested_url": requested,
        "url": requested,
        "http_status": 200,
        "title": "Example",
        "text": "  ",
    }

    verification = verify_webpage_open(ToolResult(status="success", output=observed), requested)

    assert verification.passed is False
    assert verification.evidence["http_status"] == 200


def test_browser_tool_rejects_local_and_non_http_urls(tmp_path: Path) -> None:
    registry = ToolRegistry(PathPolicy(tmp_path))

    local = registry.execute("open_webpage", {"url": "http://127.0.0.1:11434/api/tags"})
    file_url = registry.execute("open_webpage", {"url": "file:///C:/Users/ASUS/secret.txt"})

    assert local.status == "denied"
    assert file_url.status == "denied"


def test_browser_dns_resolution_timeout_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout(*args, **kwargs):
        raise TimeoutError("simulated DNS timeout")

    monkeypatch.setattr("autopilot_ai.browser.socket.getaddrinfo", timeout)

    with pytest.raises(BrowserToolError, match="Could not resolve public web host"):
        open_webpage("https://example.test")


def test_browser_startup_failure_is_returned_as_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from playwright.sync_api import Error as PlaywrightError

    class FakeChromium:
        def launch(self, **kwargs):
            raise PlaywrightError("simulated missing browser binary")

    class FakePlaywrightContext:
        chromium = FakeChromium()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr("autopilot_ai.browser._validate_public_url", lambda url, dns_cache=None: url)
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: FakePlaywrightContext())

    with pytest.raises(BrowserToolError, match="Could not start Chromium"):
        open_webpage("https://example.test")


def test_browser_403_explains_that_the_site_refused_access() -> None:
    message = _http_status_error(403)

    assert "HTTP 403 (Forbidden)" in message
    assert "sign-in" in message
    assert "automated browser access" in message


def test_failed_browser_report_does_not_show_empty_page_metadata() -> None:
    report = {
        "status": "failed",
        "goal": "Open https://chatgpt.com",
        "plan": {"description": "Open a public web page", "tool_name": "open_webpage"},
        "tool_result": {"status": "error", "error_code": "browser_error", "output": {"detail": _http_status_error(403)}},
        "verification": {"passed": False, "summary": "Browser navigation did not complete successfully."},
    }

    rendered = format_report(report)

    assert "Attempted operation:" in rendered
    assert "(untitled)" not in rendered
    assert "HTTP 403 (Forbidden)" in rendered
    assert "open this site manually" in rendered


def test_active_window_inspection_runs_and_verifies_without_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    observation = {
        "backend": "uia",
        "window_title": "Test Window",
        "process_id": 4321,
        "window_handle": 6789,
        "controls": [{"name": "Save", "control_type": "Button", "automation_id": "save"}],
        "controls_returned": 1,
        "controls_truncated": False,
    }
    monkeypatch.setattr("autopilot_ai.tools.inspect_active_window", lambda max_controls=60, screenshot_fallback=True: observation)
    monkeypatch.setattr("autopilot_ai.verifier.inspect_active_window", lambda max_controls=60, screenshot_fallback=False: observation)

    report = run_goal("Inspect active window", tmp_path)

    assert report["status"] == "completed"
    assert report["plan"]["tool_name"] == "inspect_active_window"
    assert report["tool_result"]["output"]["window_title"] == "Test Window"
    assert report["verification"]["passed"] is True


def test_desktop_tool_rejects_unbounded_control_count(tmp_path: Path) -> None:
    result = ToolRegistry(PathPolicy(tmp_path)).execute("inspect_active_window", {"max_controls": 1000})
    assert result.status == "denied"
    assert result.error_code == "invalid_arguments"


def test_desktop_inspection_uses_screenshot_fallback_only_after_uia_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def failed_uia(max_controls: int) -> dict:
        raise DesktopToolError("UIA unavailable in the test.")

    screenshot = {"backend": "pyautogui_screenshot", "screenshot_path": "D:/Agent/down/screenshots/test.png", "width": 800, "height": 600}
    monkeypatch.setattr("autopilot_ai.desktop._inspect_uia", failed_uia)
    monkeypatch.setattr("autopilot_ai.desktop._capture_screenshot", lambda: screenshot)

    observed = inspect_active_window()

    assert observed["backend"] == "pyautogui_screenshot"
    assert observed["uia_note"] == "UIA unavailable in the test."
    with pytest.raises(DesktopToolError, match="UIA unavailable"):
        inspect_active_window(screenshot_fallback=False)


def test_screenshot_fallback_captures_only_active_window_region(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    class FakeImage:
        width = 200
        height = 100

        def save(self, path: Path) -> None:
            captured["path"] = Path(path)
            Path(path).write_bytes(b"mock screenshot")

    fake_win32gui = types.SimpleNamespace(
        GetForegroundWindow=lambda: 123,
        GetWindowRect=lambda handle: (10, 20, 210, 120),
        GetWindowText=lambda handle: "Test window",
        GetWindowThreadProcessId=lambda handle: (1, 456),
    )
    fake_win32process = types.SimpleNamespace(GetWindowThreadProcessId=lambda handle: (1, 456))
    fake_pyautogui = types.SimpleNamespace(
        size=lambda: (1920, 1080),
        screenshot=lambda region: captured.update(region=region) or FakeImage(),
    )
    monkeypatch.setitem(sys.modules, "win32gui", fake_win32gui)
    monkeypatch.setitem(sys.modules, "win32process", fake_win32process)
    monkeypatch.setitem(sys.modules, "pyautogui", fake_pyautogui)

    result = _capture_screenshot()

    try:
        assert captured["region"] == (10, 20, 200, 100)
        assert result["window_handle"] == 123
        assert result["window_title"] == "Test window"
    finally:
        captured["path"].unlink(missing_ok=True)


def test_screenshot_verifier_checks_image_and_window_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    from PIL import Image

    screenshot_dir = Path(__file__).resolve().parents[1] / "down" / "screenshots"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    image_path = screenshot_dir / "autopilot-verifier-test.png"
    Image.new("RGB", (40, 20), color="white").save(image_path)
    monkeypatch.setitem(sys.modules, "win32gui", types.SimpleNamespace(GetForegroundWindow=lambda: 123))
    tool_result = ToolResult(
        status="success",
        output={"backend": "pyautogui_screenshot", "screenshot_path": str(image_path), "width": 40, "height": 20, "window_handle": 123, "window_rect": [0, 0, 40, 20]},
    )

    try:
        verified = verify_desktop_inspection(tool_result)
        assert verified.passed is True
        monkeypatch.setitem(sys.modules, "win32gui", types.SimpleNamespace(GetForegroundWindow=lambda: 456))
        changed_window = verify_desktop_inspection(tool_result)
        assert changed_window.passed is False
        monkeypatch.setitem(sys.modules, "win32gui", types.SimpleNamespace(GetForegroundWindow=lambda: 123))
        wrong_bounds_result = tool_result.model_copy(update={"output": {**tool_result.output, "window_rect": [0, 0, 50, 20]}})
        wrong_bounds = verify_desktop_inspection(wrong_bounds_result)
        assert wrong_bounds.passed is False
        assert wrong_bounds.evidence["rect_dimensions_match"] is False
    finally:
        image_path.unlink(missing_ok=True)


def test_uia_verifier_detects_window_handle_change(monkeypatch: pytest.MonkeyPatch) -> None:
    fresh_limits: list[int] = []

    def fresh_inspection(**kwargs):
        fresh_limits.append(kwargs["max_controls"])
        return {"backend": "uia", "window_title": "Editor", "process_id": 55, "window_handle": 456}

    monkeypatch.setattr(
        "autopilot_ai.verifier.inspect_active_window",
        fresh_inspection,
    )
    result = ToolResult(
        status="success",
        output={
            "backend": "uia", "window_title": "Editor", "process_id": 55, "window_handle": 123,
            "controls": [{"name": "Save", "control_type": "Button", "automation_id": "save"}],
            "controls_returned": 1, "controls_limit": 7, "controls_skipped": 0, "controls_truncated": False,
        },
    )

    verification = verify_desktop_inspection(result)

    assert verification.passed is False
    assert verification.evidence["fresh_window_handle"] == 456
    assert fresh_limits == [7]


def test_uia_verifier_rejects_inconsistent_control_count(monkeypatch: pytest.MonkeyPatch) -> None:
    result = ToolResult(
        status="success",
        output={
            "backend": "uia", "window_title": "Editor", "process_id": 55, "window_handle": 123,
            "controls": [{"name": "Save", "control_type": "Button", "automation_id": "save"}],
            "controls_returned": 2, "controls_limit": 7, "controls_skipped": 0, "controls_truncated": False,
        },
    )
    monkeypatch.setattr(
        "autopilot_ai.verifier.inspect_active_window",
        lambda **kwargs: pytest.fail("Malformed output must be rejected before a fresh UIA read."),
    )

    verification = verify_desktop_inspection(result)

    assert verification.passed is False
    assert "inconsistent control list" in verification.summary


def test_uia_inspects_the_foreground_window_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    control = types.SimpleNamespace(
        element_info=types.SimpleNamespace(control_type="Button", automation_id="ok"),
        window_text=lambda: "OK",
    )

    class Window:
        def window_text(self) -> str:
            return "Dialog"

        def process_id(self) -> int:
            return 1234

        def descendants(self) -> list[object]:
            return [control]

    class WindowSpec:
        def wrapper_object(self) -> Window:
            return Window()

    class Desktop:
        def __init__(self, backend: str) -> None:
            assert backend == "uia"

        def window(self, handle: int) -> WindowSpec:
            assert handle == 5678
            return WindowSpec()

    monkeypatch.setitem(sys.modules, "win32gui", types.SimpleNamespace(GetForegroundWindow=lambda: 5678))
    monkeypatch.setitem(sys.modules, "pywinauto", types.SimpleNamespace(Desktop=Desktop))

    observed = _inspect_uia(20)

    assert observed["window_title"] == "Dialog"
    assert observed["window_handle"] == 5678
    assert observed["controls"][0]["name"] == "OK"


def test_uia_reports_skipped_controls_separately_from_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    def control(name: str | None, control_type: str | None = "Button"):
        return types.SimpleNamespace(
            element_info=types.SimpleNamespace(control_type=control_type, automation_id=""),
            window_text=lambda: name or "",
        )

    class Unreadable:
        @property
        def element_info(self):
            raise RuntimeError("not available")

    class Window:
        def wrapper_object(self):
            return self

        def window_text(self):
            return "Editor"

        def descendants(self):
            return [control("Save"), control("", ""), Unreadable(), control("Open")]

        def process_id(self):
            return 42

    class Desktop:
        def __init__(self, backend: str):
            pass

        def window(self, handle: int):
            return Window()

    monkeypatch.setitem(sys.modules, "win32gui", types.SimpleNamespace(GetForegroundWindow=lambda: 5678))
    monkeypatch.setitem(sys.modules, "pywinauto", types.SimpleNamespace(Desktop=Desktop))

    observed = _inspect_uia(10)

    assert observed["controls_returned"] == 2
    assert observed["controls_examined"] == 4
    assert observed["controls_skipped"] == 2
    assert observed["controls_truncated"] is False
    assert observed["controls_limit"] == 10


def test_uia_marks_control_limit_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    controls = [
        types.SimpleNamespace(
            element_info=types.SimpleNamespace(control_type="Button", automation_id=""),
            window_text=lambda: "Button",
        )
        for _ in range(3)
    ]

    class Window:
        def wrapper_object(self):
            return self

        def window_text(self):
            return "Editor"

        def descendants(self):
            return controls

        def process_id(self):
            return 42

    class Desktop:
        def __init__(self, backend: str):
            pass

        def window(self, handle: int):
            return Window()

    monkeypatch.setitem(sys.modules, "win32gui", types.SimpleNamespace(GetForegroundWindow=lambda: 5678))
    monkeypatch.setitem(sys.modules, "pywinauto", types.SimpleNamespace(Desktop=Desktop))

    observed = _inspect_uia(1)

    assert observed["controls_returned"] == 1
    assert observed["controls_examined"] == 1
    assert observed["controls_skipped"] == 0
    assert observed["controls_truncated"] is True


def test_desktop_delay_waits_before_inspection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    observation = {"backend": "uia", "window_title": "Test Window", "process_id": 4321, "controls": [], "controls_returned": 0, "controls_truncated": False}
    monkeypatch.setattr("autopilot_ai.tools.inspect_active_window", lambda max_controls=60, screenshot_fallback=True: observation)
    monkeypatch.setattr("autopilot_ai.verifier.inspect_active_window", lambda max_controls=60, screenshot_fallback=False: observation)
    waited: list[int] = []
    monkeypatch.setattr("autopilot_ai.agent.time.sleep", waited.append)

    report = run_goal("Inspect active window", tmp_path, desktop_delay_seconds=5)

    assert report["status"] == "completed"
    assert waited == [5]


def test_read_only_tool_retries_once_after_verification_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "sample.txt").write_text("sample", encoding="utf-8")
    verification_count = 0

    def transient_verification(result: ToolResult, directory: Path) -> Verification:
        nonlocal verification_count
        verification_count += 1
        return Verification(passed=verification_count == 2, summary="temporary mismatch" if verification_count == 1 else "verified")

    monkeypatch.setattr("autopilot_ai.agent.verify_directory_listing", transient_verification)

    report = run_goal("Show me the files in this folder", tmp_path)

    assert report["status"] == "completed"
    assert report["attempts"] == 2
    assert report["attempt_history"][0]["verification_passed"] is False
    assert report["attempt_history"][1]["verification_passed"] is True


def test_mutating_tool_is_never_retried_after_verification_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("autopilot_ai.agent.verify_created_directory", lambda result, destination: Verification(passed=False, summary="test mismatch"))

    report = run_goal("Create folder reports", tmp_path, approved=True)

    assert report["status"] == "failed"
    assert report["attempts"] == 1
    assert (tmp_path / "reports").is_dir()


def test_recovery_does_not_retry_incomplete_search_or_http_client_error() -> None:
    incomplete_search = ToolResult(status="success", output={"complete": False})
    failed_verification = Verification(passed=False, summary="incomplete")

    assert should_retry("search_files", incomplete_search, failed_verification, 1) is False


@pytest.mark.parametrize(
    "detail",
    [
        "The page returned HTTP 404.",
        "The website returned HTTP 401 (Unauthorized). It may require sign-in.",
        "The website returned HTTP 403 (Forbidden). It may block automated access.",
    ],
)
def test_recovery_does_not_retry_any_browser_http_client_error(detail: str) -> None:
    browser_client_error = ToolResult(status="error", error_code="browser_error", output={"detail": detail})
    failed_verification = Verification(passed=False, summary="navigation failed")

    assert should_retry("open_webpage", browser_client_error, failed_verification, 1) is False


def test_exact_filename_search_spans_all_approved_roots(tmp_path: Path) -> None:
    primary = tmp_path / "downloads"
    additional = tmp_path / "documents"
    primary.mkdir()
    additional.mkdir()
    (primary / "report.pdf").write_bytes(b"one")
    (primary / "report-draft.pdf").write_bytes(b"draft")
    (additional / "report.pdf").write_bytes(b"two")
    (additional / "README").write_text("extensionless filename", encoding="utf-8")

    report = run_goal(
        "report.pdf",
        primary,
        approved_dirs=[additional],
    )

    expected = sorted([str((primary / "report.pdf").resolve()), str((additional / "report.pdf").resolve())], key=str.casefold)
    assert report["status"] == "completed"
    assert report["tool_result"]["output"]["matches"] == expected
    assert report["verification"]["passed"] is True
    assert report["approved_roots"] == [str(primary.resolve()), str(additional.resolve())]

    extensionless = run_goal("README", primary, approved_dirs=[additional])
    assert extensionless["status"] == "completed"
    assert extensionless["tool_result"]["output"]["matches"] == [str((additional / "README").resolve())]


def test_approved_directories_can_be_saved_listed_removed_and_cleared(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    config = tmp_path / "data" / "approved_directories.json"

    added = add_approved_dirs([first, first, second], config)
    assert added == [first.resolve(), second.resolve()]
    assert load_approved_dirs(config) == [first.resolve(), second.resolve()]
    assert remove_approved_dirs([first], config) == [second.resolve()]
    clear_approved_dirs(config)
    assert load_approved_dirs(config) == []


def test_stale_approved_directory_can_be_unapproved(tmp_path: Path) -> None:
    stale = tmp_path / "removed-folder"
    config = tmp_path / "data" / "approved_directories.json"
    config.parent.mkdir()
    config.write_text(json.dumps([str(stale)]), encoding="utf-8")

    assert load_approved_dirs(config) == [stale.resolve()]
    assert remove_approved_dirs([stale], config) == []


def test_supported_search_falls_back_when_model_says_unsupported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "budget-notes.txt").write_text("safe test", encoding="utf-8")

    def unsupported_from_model(goal: str, available_tools: tuple[str, ...]) -> None:
        raise PlanningError("Model marked the supported search request unsupported.")

    monkeypatch.setattr("autopilot_ai.agent.plan_with_ollama", unsupported_from_model)
    report = run_goal("Find files containing budget", tmp_path, planner="ollama")

    assert report["status"] == "completed"
    assert report["planner"] == "heuristic_fallback"
    assert report["tool_result"]["output"]["matches"] == [str((tmp_path / "budget-notes.txt").resolve())]
    assert report["verification"]["passed"] is True


def test_search_is_filename_only_and_rejects_parent_path(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("find this phrase", encoding="utf-8")
    registry = ToolRegistry(PathPolicy(tmp_path))

    no_content_match = registry.execute("search_files", {"query": "phrase"})
    path_escape = registry.execute("search_files", {"path": "../outside", "query": "notes"})

    assert no_content_match.status == "success"
    assert no_content_match.output["matches"] == []
    assert path_escape.status == "denied"


def test_search_reports_inaccessible_subtree_as_incomplete_and_does_not_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def inaccessible_walk(root, topdown=True, onerror=None, followlinks=False):
        if onerror:
            onerror(PermissionError("simulated locked subtree"))
        yield str(root), [], []

    monkeypatch.setattr("autopilot_ai.tools.os.walk", inaccessible_walk)

    report = run_goal("Find notes.txt", tmp_path)
    rendered = format_report(report)

    assert report["status"] == "partially_completed"
    assert report["tool_result"]["output"]["complete"] is False
    assert "simulated locked subtree" in report["tool_result"]["output"]["errors"][0]
    assert report["attempts"] == 1
    assert report["verification"]["passed"] is False
    assert "Search warning: simulated locked subtree" in rendered


def test_search_entry_limit_is_explicitly_incomplete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    too_many_directories = [f"directory-{index}" for index in range(100_001)]
    monkeypatch.setattr(
        "autopilot_ai.tools.os.walk",
        lambda root, topdown=True, onerror=None, followlinks=False: iter([(str(root), too_many_directories, [])]),
    )

    result = ToolRegistry(PathPolicy(tmp_path)).execute("search_files", {"query": "missing.txt"})

    assert result.status == "success"
    assert result.output["complete"] is False
    assert result.output["truncated"] is True
    assert result.output["entries_scanned"] == 100_000


def test_search_skips_symlink_files_and_directories_without_following_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    real_match = workspace / "report-real.txt"
    outside_file = outside / "report-outside.txt"
    real_match.write_text("inside", encoding="utf-8")
    outside_file.write_text("outside", encoding="utf-8")
    real_is_symlink = Path.is_symlink
    simulated_links = {"report-link.txt", "outside-link"}

    def is_symlink(path: Path) -> bool:
        return path.name in simulated_links or real_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", is_symlink)
    monkeypatch.setattr(
        "autopilot_ai.tools.os.walk",
        lambda root, topdown=True, onerror=None, followlinks=False: iter(
            [(str(root), ["outside-link"], ["report-link.txt", "report-real.txt"])]
        ),
    )

    report = run_goal("Find files containing report", workspace)

    output = report["tool_result"]["output"]
    assert report["status"] == "partially_completed"
    assert output["matches"] == [str(real_match.resolve())]
    assert output["complete"] is False
    assert any("Skipped symlink file" in error for error in output["errors"])
    assert any("Skipped symlink directory" in error for error in output["errors"])
    assert report["verification"]["passed"] is False
    assert report["verification"]["evidence"]["fresh_matches"] == [str(real_match.resolve())]


def test_unknown_tool_is_denied(tmp_path: Path) -> None:
    result = ToolRegistry(PathPolicy(tmp_path)).execute("run_shell", {"command": "whoami"})
    assert result.status == "denied"
    assert result.error_code == "unknown_tool"


@pytest.mark.parametrize("path", ["../outside", "..\\outside", "C:\\Users\\ASUS"])
def test_outside_or_traversal_path_is_rejected(tmp_path: Path, path: str) -> None:
    policy = PathPolicy(tmp_path)
    with pytest.raises(PolicyError):
        policy.resolve(path)


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    link = tmp_path / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation is unavailable in this Windows environment.")
    with pytest.raises(PolicyError):
        PathPolicy(tmp_path).resolve("escape")


def test_unsupported_goal_does_not_execute(tmp_path: Path) -> None:
    report = run_goal("Delete all files", tmp_path)
    assert report["status"] == "unsupported"


@pytest.mark.parametrize(
    ("goal", "question_fragment"),
    [
        ("Find files", "What filename"),
        ("Create folder", "What name"),
        ("Copy file source.txt", "Which source file"),
    ],
)
def test_missing_required_details_request_clarification(tmp_path: Path, goal: str, question_fragment: str) -> None:
    report = run_goal(goal, tmp_path)

    assert report["status"] == "clarification_required"
    assert question_fragment in report["question"]


@pytest.mark.parametrize(
    ("goal", "tool_name", "expected_arguments"),
    [
        ("Make a new folder called reports", "create_directory", {"path": "reports"}),
        ("Duplicate file source.txt to backup.txt", "copy_file", {"source_path": "source.txt", "destination_path": "backup.txt"}),
        ("Open the website https://example.com", "open_webpage", {"url": "https://example.com"}),
        ("Browse https://example.com", "open_webpage", {"url": "https://example.com"}),
        ("Show files in workspace", "list_directory", {"path": "."}),
        ("Search for autopilot in workspace", "search_files", {"path": ".", "query": "autopilot", "match_mode": "contains", "max_results": 100}),
        ("Find .pdf files in workspace", "search_files", {"path": ".", "query": "", "match_mode": "exact", "extensions": [".pdf"], "max_results": 100}),
        ("Find .txt files in workspace", "search_files", {"path": ".", "query": "", "match_mode": "exact", "extensions": [".txt"], "max_results": 100}),
        ("Run dir command in workspace", "run_command", {"command": "dir", "working_dir": ".", "root_index": 0}),
        ("Find me a file named report.pdf", "search_files", {"path": ".", "query": "report.pdf", "match_mode": "exact", "max_results": 100}),
        ("Search for the file called report.pdf", "search_files", {"path": ".", "query": "report.pdf", "match_mode": "exact", "max_results": 100}),
        ("Locate report.pdf on my computer", "search_files", {"path": ".", "query": "report.pdf", "match_mode": "exact", "max_results": 100}),
        ("Find files containing budget in this folder", "search_files", {"path": ".", "query": "budget", "match_mode": "contains", "max_results": 100}),
    ],
)
def test_common_paraphrases_keep_the_same_bounded_tool_intent(goal: str, tool_name: str, expected_arguments: dict) -> None:
    plan = plan_goal_steps(goal)[0]

    assert plan.tool_name == tool_name
    assert plan.arguments == expected_arguments


def test_workspace_dir_quick_action_is_read_only_and_needs_no_approval() -> None:
    plan = plan_goal_steps("Run dir command in workspace")[0]

    assert plan.risk_level == "low"
    assert plan.requires_approval is False


def test_workspace_dir_quick_action_runs_and_verifies(tmp_path: Path) -> None:
    (tmp_path / "visible.txt").write_text("smoke test", encoding="utf-8")

    report = run_goal("Run dir command in workspace", tmp_path, planner="heuristic")

    assert report["status"] == "completed"
    assert report["plan"]["arguments"]["command"] == "dir"
    assert "visible.txt" in report["tool_result"]["output"]["stdout"]
    assert report["verification"]["passed"] is True


def test_workspace_search_quick_action_finds_partial_name(tmp_path: Path) -> None:
    target = tmp_path / "autopilot-overview.txt"
    target.write_text("agent overview", encoding="utf-8")

    report = run_goal("Search for autopilot in workspace", tmp_path, planner="heuristic")

    assert report["status"] == "completed"
    assert report["tool_result"]["output"]["matches"] == [str(target.resolve())]
    assert report["verification"]["passed"] is True


def test_workspace_extension_quick_action_searches_workspace_root(tmp_path: Path) -> None:
    pdf_file = tmp_path / "guide.pdf"
    text_file = tmp_path / "notes.txt"
    pdf_file.write_bytes(b"pdf placeholder")
    text_file.write_text("text placeholder", encoding="utf-8")

    report = run_goal("Find .pdf files in workspace", tmp_path, planner="heuristic")

    assert report["status"] == "completed"
    assert report["tool_result"]["output"]["matches"] == [str(pdf_file.resolve())]
    assert report["verification"]["passed"] is True


@pytest.mark.parametrize(
    "goal",
    [
        "Find me a file named report.pdf",
        "Search for the file called report.pdf",
        "Locate report.pdf on my computer",
    ],
)
def test_search_paraphrases_find_and_verify_exact_filename(tmp_path: Path, goal: str) -> None:
    target = tmp_path / "nested" / "report.pdf"
    target.parent.mkdir()
    target.write_bytes(b"sample")

    report = run_goal(goal, tmp_path)

    assert report["status"] == "completed"
    assert report["plan"]["arguments"]["query"] == "report.pdf"
    assert report["tool_result"]["output"]["matches"] == [str(target.resolve())]
    assert report["verification"]["passed"] is True


@pytest.mark.parametrize(
    ("goal", "extensions"),
    [
        ("Find PDFs", [".pdf"]),
        ("Find all PDF files in approved folders", [".pdf"]),
        ("Search for DOCX files in my approved directories", [".docx"]),
        ("Find images", [".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".svg", ".heic"]),
        ("Find files with extension .md", [".md"]),
        ("Show me word documents", [".doc", ".docx"]),
        ("Shows pdf files in ML folder", [".pdf"]),
        ("Show PDF files inside ML", [".pdf"]),
        ("Find all PDFs in all approved folders", [".pdf"]),
    ],
)
def test_extension_search_phrasings_are_deterministic(goal: str, extensions: list[str]) -> None:
    plan = plan_goal_steps(goal)[0]

    assert plan.tool_name == "search_files"
    assert plan.arguments["query"] == ""
    assert plan.arguments["extensions"] == extensions


def test_extension_search_is_verified_and_stays_inside_approved_roots(tmp_path: Path) -> None:
    primary = tmp_path / "workspace"
    extra = tmp_path / "approved-documents"
    unapproved = tmp_path / "private"
    for folder in (primary, extra, unapproved):
        folder.mkdir()
    (primary / "report.PDF").write_bytes(b"pdf")
    (primary / "archive.pdf.backup").write_bytes(b"not a pdf suffix")
    (primary / "photo.JPG").write_bytes(b"jpg")
    (extra / "notes.docx").write_bytes(b"docx")
    (unapproved / "private.pdf").write_bytes(b"private")

    report = run_goal("Find PDFs", primary, approved_dirs=[extra])

    expected = [str((primary / "report.PDF").resolve())]
    assert report["status"] == "completed"
    assert report["plan"]["arguments"]["extensions"] == [".pdf"]
    assert report["tool_result"]["output"]["matches"] == expected
    assert report["tool_result"]["output"]["roots"] == [str(primary.resolve()), str(extra.resolve())]
    assert str((unapproved / "private.pdf").resolve()) not in report["tool_result"]["output"]["matches"]
    assert report["verification"]["passed"] is True


def test_named_folder_extension_search_is_scoped_to_primary_workspace(tmp_path: Path) -> None:
    primary = tmp_path / "workspace"
    extra = tmp_path / "other-approved"
    ml = primary / "ML"
    ml.mkdir(parents=True)
    extra.mkdir()
    (ml / "lecture.pdf").write_bytes(b"pdf")
    (primary / "outside.pdf").write_bytes(b"pdf")
    (extra / "approved-but-out-of-scope.pdf").write_bytes(b"pdf")

    report = run_goal("Show PDF files in ML folder", primary, approved_dirs=[extra])

    assert report["status"] == "completed"
    assert report["plan"]["arguments"]["path"] == "ML"
    assert report["tool_result"]["output"]["roots"] == [str(ml.resolve())]
    assert report["tool_result"]["output"]["matches"] == [str((ml / "lecture.pdf").resolve())]
    assert report["verification"]["passed"] is True


def test_named_folder_filename_search_is_scoped_and_verified(tmp_path: Path) -> None:
    ml = tmp_path / "ML"
    ml.mkdir()
    (ml / "Renish_Resume.pdf").write_bytes(b"resume")
    (tmp_path / "Renish_Resume_old.pdf").write_bytes(b"other")

    report = run_goal("Find files containing Renish_Resume in ML folder", tmp_path)

    assert report["status"] == "completed"
    assert report["plan"]["arguments"]["query"] == "Renish_Resume"
    assert report["plan"]["arguments"]["path"] == "ML"
    assert report["tool_result"]["output"]["matches"] == [str((ml / "Renish_Resume.pdf").resolve())]
    assert report["verification"]["passed"] is True


def test_named_folder_without_folder_word_keeps_filename_query_and_scope(tmp_path: Path) -> None:
    ml = tmp_path / "ML"
    ml.mkdir()
    (ml / "Renish_Resume.pdf").write_bytes(b"resume")

    report = run_goal("Find files containing Renish_Resume in ML", tmp_path)

    assert report["status"] == "completed"
    assert report["plan"]["arguments"]["query"] == "Renish_Resume"
    assert report["tool_result"]["output"]["matches"] == [str((ml / "Renish_Resume.pdf").resolve())]
    assert report["verification"]["passed"] is True


def test_named_folder_search_rejects_path_escape(tmp_path: Path) -> None:
    report = run_goal("Find PDFs in .. folder", tmp_path)

    assert report["status"] == "denied"
    assert "Parent-directory traversal" in report["message"]


def test_named_folder_missing_from_all_approved_roots_has_clear_message(tmp_path: Path) -> None:
    report = run_goal("Find PDFs in MissingFolder", tmp_path)

    assert report["status"] == "denied"
    assert "No folder named 'MissingFolder'" in report["message"]
    assert "approved directories" in report["message"]


def test_inaccessible_named_folder_candidate_is_not_treated_as_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from autopilot_ai.agent import _resolve_search_scope

    workspace = tmp_path / "workspace"
    inaccessible = tmp_path / "approved" / "ML"
    workspace.mkdir()
    inaccessible.mkdir(parents=True)
    policy = PathPolicy(workspace, [inaccessible.parent])
    original_stat = Path.stat

    def deny_candidate_stat(path: Path, *args: object, **kwargs: object):
        if path == inaccessible:
            raise PermissionError("simulated access denied")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", deny_candidate_stat)
    with pytest.raises(PolicyError, match="Could not inspect the requested folder.*access denied"):
        _resolve_search_scope(policy, {"path": "ML", "root_index": 0})


def test_named_approved_root_is_selected_when_folder_name_matches_root(tmp_path: Path) -> None:
    primary = tmp_path / "workspace"
    approved_ml = tmp_path / "ML"
    primary.mkdir()
    approved_ml.mkdir()
    (approved_ml / "course.pdf").write_bytes(b"pdf")

    report = run_goal("Find PDFs in ML folder", primary, approved_dirs=[approved_ml])

    assert report["status"] == "completed"
    assert report["tool_result"]["output"]["roots"] == [str(approved_ml.resolve())]
    assert report["tool_result"]["output"]["matches"] == [str((approved_ml / "course.pdf").resolve())]
    assert report["verification"]["passed"] is True


def test_named_folder_ambiguity_requests_exact_approved_path(tmp_path: Path) -> None:
    primary = tmp_path / "workspace"
    primary_ml = primary / "ML"
    approved_ml = tmp_path / "ML"
    primary_ml.mkdir(parents=True)
    approved_ml.mkdir()

    report = run_goal("Find PDFs in ML folder", primary, approved_dirs=[approved_ml])

    assert report["status"] == "clarification_required"
    assert "full path" in report["question"]
    assert set(report["candidates"]) == {str(primary_ml.resolve()), str(approved_ml.resolve())}
    rendered = format_report(report)
    assert str(primary_ml.resolve()) in rendered
    assert str(approved_ml.resolve()) in rendered


def test_exact_approved_root_path_can_disambiguate_named_folder(tmp_path: Path) -> None:
    primary = tmp_path / "workspace"
    approved_ml = tmp_path / "ML"
    primary.mkdir()
    approved_ml.mkdir()
    (approved_ml / "course.pdf").write_bytes(b"pdf")

    report = run_goal(f'Find PDFs in "{approved_ml}" folder', primary, approved_dirs=[approved_ml])

    assert report["status"] == "completed"
    assert report["tool_result"]["output"]["roots"] == [str(approved_ml.resolve())]
    assert report["verification"]["passed"] is True


def test_interactive_ordinal_selects_ambiguous_folder_and_runs_verified_search(tmp_path: Path) -> None:
    primary = tmp_path / "workspace"
    first_ml = primary / "ML"
    second_root = tmp_path / "approved"
    second_ml = second_root / "ML"
    first_ml.mkdir(parents=True)
    second_ml.mkdir(parents=True)
    (first_ml / "first.pdf").write_bytes(b"first")
    (second_ml / "second.pdf").write_bytes(b"second")
    session = ConversationSession(memory=TaskMemory(":memory:"))

    def execute(goal: str) -> dict:
        return run_goal(goal, primary, approved_dirs=[second_root])

    clarification = session.handle("Find PDFs in ML folder", execute)
    assert clarification["status"] == "clarification_required"
    assert clarification["scope_kind"] == "folder"
    rendered = format_report(clarification)
    assert "Reply with a number or ordinal" in rendered
    assert "repeat the request with the exact folder path" not in rendered

    result = session.handle("the second one", execute)

    assert result["status"] == "completed"
    assert result["goal"] == "Find PDFs in ML folder"
    assert result["selected_folder"] == str(second_ml.resolve())
    assert result["tool_result"]["output"]["matches"] == [str((second_ml / "second.pdf").resolve())]
    assert result["verification"]["passed"] is True


def test_image_extension_search_matches_multiple_suffixes(tmp_path: Path) -> None:
    (tmp_path / "photo.png").write_bytes(b"png")
    (tmp_path / "scan.JPEG").write_bytes(b"jpeg")
    (tmp_path / "graphic.svg").write_text("<svg/>", encoding="utf-8")
    (tmp_path / "archive.zip").write_bytes(b"zip")

    report = run_goal("Find images", tmp_path)

    matches = report["tool_result"]["output"]["matches"]
    expected = sorted(
        [str((tmp_path / name).resolve()) for name in ("photo.png", "scan.JPEG", "graphic.svg")],
        key=str.casefold,
    )
    assert report["status"] == "completed"
    assert matches == expected
    assert report["verification"]["passed"] is True


def test_extension_search_rejects_unapproved_roots(tmp_path: Path) -> None:
    missing_root = tmp_path / "not-approved"

    report = run_goal("Find PDFs", tmp_path, approved_dirs=[missing_root])

    assert report["status"] == "denied"
    assert "Approved directory rejected" in report["message"]


def test_extension_search_validates_extension_values(tmp_path: Path) -> None:
    registry = ToolRegistry(PathPolicy(tmp_path))

    invalid = registry.execute("search_files", {"query": "", "extensions": ["../outside"]})
    empty = registry.execute("search_files", {"query": "", "extensions": []})

    assert invalid.status == "denied"
    assert invalid.error_code == "invalid_arguments"
    assert empty.status == "denied"


def test_ollama_planner_returns_validated_registered_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    step = PlanStep(
        step_id=">'Find files containing budget',",
        description="Copied prompt text that is not a useful plan description.",
        tool_name="list_directory",
        arguments={"path": "."},
        expected_result="Unsupported",
        risk_level="low",
        requires_approval=False,
    )

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            import json

            task_plan = TaskPlan(steps=[step])
            return json.dumps({"message": {"content": task_plan.model_dump_json()}}).encode()

    def fake_urlopen(request: object, timeout: float) -> FakeResponse:
        assert timeout == 90
        return FakeResponse()

    monkeypatch.setattr("autopilot_ai.planner.urlopen", fake_urlopen)
    planned = plan_with_ollama("Show files", ("list_directory",))
    assert planned.step_id == "step-1"
    assert planned.description == "List entries in the approved workspace."
    assert planned.tool_name == step.tool_name
    assert planned.arguments == step.arguments
    assert planned.expected_result == "A fresh workspace scan matches the tool's returned entry names."


def test_ollama_planner_rejects_unregistered_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    step = PlanStep(
        step_id="step-1",
        description="Run a command.",
        tool_name="run_shell",
        arguments={"command": "whoami"},
        expected_result="Done.",
        risk_level="low",
        requires_approval=False,
    )

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            import json

            task_plan = TaskPlan(steps=[step])
            return json.dumps({"message": {"content": task_plan.model_dump_json()}}).encode()

    monkeypatch.setattr("autopilot_ai.planner.urlopen", lambda request, timeout: FakeResponse())
    with pytest.raises(ModelPlannerError, match="unavailable"):
        plan_with_ollama("Run a command", ("list_directory",))


def test_ollama_structured_plan_accepts_two_safe_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = TaskPlan(steps=[
        PlanStep(step_id="first", description="list", tool_name="list_directory", arguments={"path": "."}, expected_result="ignored"),
        PlanStep(step_id="second", description="search", tool_name="search_files", arguments={"path": ".", "query": "report.pdf", "match_mode": "exact", "max_results": 100}, expected_result="ignored"),
    ])

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps({"message": {"content": plan.model_dump_json()}}).encode()

    monkeypatch.setattr("autopilot_ai.planner.urlopen", lambda request, timeout: FakeResponse())
    steps = plan_with_ollama_steps("Show files and find report.pdf", ("list_directory", "search_files"))

    assert [step.tool_name for step in steps] == ["list_directory", "search_files"]
    assert [step.step_id for step in steps] == ["step-1", "step-2"]


def test_ollama_http_error_includes_server_response(monkeypatch: pytest.MonkeyPatch) -> None:
    error = HTTPError(
        "http://127.0.0.1:11434/api/chat",
        404,
        "Not Found",
        hdrs=None,
        fp=BytesIO(b'{"error":"model not found"}'),
    )
    monkeypatch.setattr("autopilot_ai.planner.urlopen", lambda request, timeout: (_ for _ in ()).throw(error))
    with pytest.raises(ModelPlannerError, match="model not found"):
        plan_with_ollama("Show files", ("list_directory",))
