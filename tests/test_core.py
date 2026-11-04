from pathlib import Path
from io import BytesIO
import json
import sys
import types
from urllib.error import HTTPError

import pytest

from autopilot_ai.approved_dirs import add_approved_dirs, clear_approved_dirs, load_approved_dirs, remove_approved_dirs
from autopilot_ai.agent import run_goal
from autopilot_ai.contracts import PlanStep, TaskPlan, ToolResult, Verification
from autopilot_ai.desktop import DesktopToolError, _inspect_uia, inspect_active_window
from autopilot_ai.planner import ModelPlannerError, PlanningError, plan_goal_steps, plan_with_ollama, plan_with_ollama_steps
from autopilot_ai.policy import PathPolicy, PolicyError
from autopilot_ai.recovery import should_retry
from autopilot_ai.tools import ToolRegistry


def test_natural_language_listing_runs_and_verifies(tmp_path: Path) -> None:
    (tmp_path / "sample.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "subfolder").mkdir()
    report = run_goal("Show me the files in this folder", tmp_path)
    assert report["status"] == "completed"
    assert report["verification"]["passed"] is True
    assert report["verification"]["evidence"]["fresh_names"] == ["sample.txt", "subfolder"]


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


def test_copy_file_refuses_overwrite_and_path_escape(tmp_path: Path) -> None:
    (tmp_path / "source.txt").write_text("source", encoding="utf-8")
    (tmp_path / "existing.txt").write_text("keep", encoding="utf-8")
    registry = ToolRegistry(PathPolicy(tmp_path))

    overwrite = registry.execute("copy_file", {"source_path": "source.txt", "destination_path": "existing.txt"})
    escape = registry.execute("create_directory", {"path": "../outside"})

    assert overwrite.status == "denied"
    assert escape.status == "denied"
    assert (tmp_path / "existing.txt").read_text(encoding="utf-8") == "keep"


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


def test_browser_tool_rejects_local_and_non_http_urls(tmp_path: Path) -> None:
    registry = ToolRegistry(PathPolicy(tmp_path))

    local = registry.execute("open_webpage", {"url": "http://127.0.0.1:11434/api/tags"})
    file_url = registry.execute("open_webpage", {"url": "file:///C:/Users/ASUS/secret.txt"})

    assert local.status == "denied"
    assert file_url.status == "denied"


def test_active_window_inspection_runs_and_verifies_without_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    observation = {
        "backend": "uia",
        "window_title": "Test Window",
        "process_id": 4321,
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
    browser_404 = ToolResult(status="error", error_code="browser_error", output={"detail": "The page returned HTTP 404."})

    assert should_retry("search_files", incomplete_search, failed_verification, 1) is False
    assert should_retry("open_webpage", browser_404, failed_verification, 1) is False


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
