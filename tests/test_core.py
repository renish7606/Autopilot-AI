from pathlib import Path
from io import BytesIO
import json
import sys
import types
from urllib.error import HTTPError

import pytest

from autopilot_ai.approved_dirs import add_approved_dirs, clear_approved_dirs, load_approved_dirs, remove_approved_dirs
from autopilot_ai.agent import run_goal
from autopilot_ai.browser import BrowserToolError, _http_status_error, open_webpage
from autopilot_ai.cli import format_report
from autopilot_ai.contracts import PlanStep, TaskPlan, ToolResult, Verification
from autopilot_ai.desktop import DesktopToolError, _capture_screenshot, _inspect_uia, inspect_active_window
from autopilot_ai.planner import ModelPlannerError, PlanningError, plan_goal_steps, plan_with_ollama, plan_with_ollama_steps
from autopilot_ai.policy import PathPolicy, PolicyError
from autopilot_ai.recovery import should_retry
from autopilot_ai.tools import ToolRegistry
from autopilot_ai.verifier import verify_desktop_inspection, verify_directory_listing, verify_webpage_open


def test_natural_language_listing_runs_and_verifies(tmp_path: Path) -> None:
    (tmp_path / "sample.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "subfolder").mkdir()
    report = run_goal("Show me the files in this folder", tmp_path)
    assert report["status"] == "completed"
    assert report["verification"]["passed"] is True
    assert report["verification"]["evidence"]["fresh_names"] == ["sample.txt", "subfolder"]


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

    assert report["status"] == "partially_completed"
    assert report["verification"]["passed"] is False


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
