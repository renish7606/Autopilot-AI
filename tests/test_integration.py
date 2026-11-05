"""Opt-in tests that exercise real local integrations rather than mocks."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

import pytest

from autopilot_ai.agent import run_goal
from autopilot_ai.browser import open_webpage
from autopilot_ai.contracts import ToolResult
from autopilot_ai.desktop import _capture_screenshot, _inspect_uia
from autopilot_ai.verifier import verify_desktop_inspection


@pytest.fixture
def browser_fixture_server():
    post_attempts: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/redirect-private":
                self.send_response(302)
                self.send_header("Location", "http://192.168.1.1/private")
                self.end_headers()
                return
            if self.path == "/post-page":
                body = b'<html><body><form method="post" action="/submit"><input name="x" value="1"></form><script>document.forms[0].submit()</script></body></html>'
            elif self.path == "/slow":
                time.sleep(1)
                body = b"<title>Too slow</title>"
            else:
                body = b"<!doctype html><title>Fixture</title><main>Ready</main>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self) -> None:
            post_attempts.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", post_attempts
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.integration
def test_real_filesystem_workflow_creates_copies_and_searches(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("integration sample", encoding="utf-8")

    copy_report = run_goal("Copy file source.txt to archive.txt", tmp_path, approved=True)
    search_report = run_goal("Find archive.txt", tmp_path)

    assert copy_report["status"] == "completed"
    assert copy_report["verification"]["passed"] is True
    assert (tmp_path / "archive.txt").read_text(encoding="utf-8") == "integration sample"
    assert search_report["status"] == "completed"
    assert search_report["tool_result"]["output"]["matches"] == [str((tmp_path / "archive.txt").resolve())]


@pytest.mark.integration
def test_real_search_skips_symlinks_without_following_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    inside_match = root / "report-inside.txt"
    outside_match = outside / "report-outside.txt"
    inside_match.write_text("inside", encoding="utf-8")
    outside_match.write_text("outside", encoding="utf-8")
    try:
        (root / "linked-file.txt").symlink_to(outside_match)
        (root / "linked-folder").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"This Windows account cannot create symlinks: {exc}")

    report = run_goal("Find files containing report", root)

    output = report["tool_result"]["output"]
    assert report["status"] == "partially_completed"
    assert output["matches"] == [str(inside_match.resolve())]
    assert output["complete"] is False
    assert any("Skipped symlink" in warning for warning in output["errors"])


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("AUTOPILOT_RUN_BROWSER_INTEGRATION") != "1", reason="set AUTOPILOT_RUN_BROWSER_INTEGRATION=1 to launch local Chromium")
def test_real_playwright_follows_redirect_to_local_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/fixture")
                self.end_headers()
                return
            body = b"<!doctype html><title>AutoPilot fixture</title><main>Local browser integration</main>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/redirect"
        # The fixture is deliberately loopback-only. Production URL checks are
        # covered separately by unit tests; this bypass is scoped to this test.
        monkeypatch.setattr("autopilot_ai.browser._validate_public_url", lambda value, dns_cache=None: value)
        result = open_webpage(url)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result["http_status"] == 200
    assert result["title"] == "AutoPilot fixture"
    assert result["url"].endswith("/fixture")
    assert "Local browser integration" in result["text"]


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("AUTOPILOT_RUN_BROWSER_INTEGRATION") != "1", reason="set AUTOPILOT_RUN_BROWSER_INTEGRATION=1 to launch local Chromium")
def test_real_playwright_denies_private_redirect(monkeypatch: pytest.MonkeyPatch, browser_fixture_server) -> None:
    from autopilot_ai.browser import BrowserPolicyError, _validate_public_url

    base_url, _ = browser_fixture_server
    original_validate = _validate_public_url

    def allow_only_fixture(url: str, dns_cache=None) -> str:
        if urlsplit(url).hostname == "127.0.0.1":
            return url
        return original_validate(url, dns_cache)

    monkeypatch.setattr("autopilot_ai.browser._validate_public_url", allow_only_fixture)
    with pytest.raises(BrowserPolicyError, match="AutoPilot blocked"):
        open_webpage(f"{base_url}/redirect-private")


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("AUTOPILOT_RUN_BROWSER_INTEGRATION") != "1", reason="set AUTOPILOT_RUN_BROWSER_INTEGRATION=1 to launch local Chromium")
def test_real_playwright_blocks_post_form(monkeypatch: pytest.MonkeyPatch, browser_fixture_server) -> None:
    from autopilot_ai.browser import BrowserPolicyError

    base_url, post_attempts = browser_fixture_server
    monkeypatch.setattr("autopilot_ai.browser._validate_public_url", lambda value, dns_cache=None: value)
    with pytest.raises(BrowserPolicyError, match="Only public HTTP/HTTPS GET and HEAD"):
        open_webpage(f"{base_url}/post-page")
    assert post_attempts == []


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("AUTOPILOT_RUN_BROWSER_INTEGRATION") != "1", reason="set AUTOPILOT_RUN_BROWSER_INTEGRATION=1 to launch local Chromium")
def test_real_playwright_reports_navigation_timeout(monkeypatch: pytest.MonkeyPatch, browser_fixture_server) -> None:
    from autopilot_ai.browser import BrowserToolError

    base_url, _ = browser_fixture_server
    monkeypatch.setattr("autopilot_ai.browser._validate_public_url", lambda value, dns_cache=None: value)
    monkeypatch.setattr("autopilot_ai.browser._NAVIGATION_TIMEOUT_MS", 100)
    with pytest.raises(BrowserToolError, match="within 0.1 seconds"):
        open_webpage(f"{base_url}/slow")


@pytest.mark.ollama_integration
@pytest.mark.skipif(os.environ.get("AUTOPILOT_RUN_OLLAMA_INTEGRATION") != "1", reason="set AUTOPILOT_RUN_OLLAMA_INTEGRATION=1 to use the configured local Ollama model")
def test_live_ollama_plan_is_used_only_when_it_matches_intent(tmp_path: Path) -> None:
    (tmp_path / "report.pdf").write_bytes(b"local model fixture")

    report = run_goal("Find report.pdf", tmp_path, planner="ollama")

    assert report["status"] == "completed", report
    assert report["planner"] == "ollama", report
    assert report["plan"]["arguments"]["query"] == "report.pdf"
    assert report["verification"]["passed"] is True


@pytest.mark.windows_integration
@pytest.mark.skipif(
    os.name != "nt" or os.environ.get("AUTOPILOT_RUN_WINDOWS_INTEGRATION") != "1",
    reason="set AUTOPILOT_RUN_WINDOWS_INTEGRATION=1 on Windows to inspect the live foreground window",
)
def test_real_windows_uia_reads_foreground_window() -> None:
    import win32gui

    if not win32gui.GetForegroundWindow():
        pytest.skip("The test runner has no interactive foreground window; run from an unlocked user desktop.")
    observation = _inspect_uia(max_controls=20)
    assert observation["backend"] == "uia"
    assert observation["process_id"] > 0
    assert len(observation["controls"]) <= 20


@pytest.mark.windows_integration
@pytest.mark.skipif(
    os.name != "nt" or os.environ.get("AUTOPILOT_RUN_WINDOWS_INTEGRATION") != "1",
    reason="set AUTOPILOT_RUN_WINDOWS_INTEGRATION=1 on an unlocked interactive Windows desktop to capture a screenshot",
)
def test_real_windows_active_window_screenshot_is_saved_and_verified() -> None:
    import win32gui

    if not win32gui.GetForegroundWindow():
        pytest.skip("The test runner has no interactive foreground window; run from an unlocked user desktop.")
    output = _capture_screenshot()
    result = ToolResult(status="success", output=output)
    try:
        verification = verify_desktop_inspection(result)
        assert verification.passed, verification.model_dump()
        assert Path(output["screenshot_path"]).is_file()
    finally:
        Path(output["screenshot_path"]).unlink(missing_ok=True)


@pytest.mark.windows_integration
@pytest.mark.skipif(
    os.name != "nt" or os.environ.get("AUTOPILOT_RUN_WINDOWS_INTEGRATION") != "1",
    reason="set AUTOPILOT_RUN_WINDOWS_INTEGRATION=1 on Windows to test a real locked file",
)
def test_windows_copy_of_exclusively_locked_file_fails_safely(tmp_path: Path) -> None:
    import win32file

    source = tmp_path / "locked-source.txt"
    destination = tmp_path / "copy.txt"
    source.write_text("locked fixture", encoding="utf-8")
    handle = win32file.CreateFile(
        str(source), win32file.GENERIC_READ, 0, None, win32file.OPEN_EXISTING,
        win32file.FILE_ATTRIBUTE_NORMAL, None,
    )
    try:
        report = run_goal("Copy file locked-source.txt to copy.txt", tmp_path, approved=True)
    finally:
        win32file.CloseHandle(handle)

    assert report["status"] == "failed", report
    assert report["tool_result"]["status"] == "error", report
    assert destination.exists() is False
