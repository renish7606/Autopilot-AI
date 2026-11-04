"""Opt-in tests that exercise real local integrations rather than mocks."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import threading

import pytest

from autopilot_ai.agent import run_goal
from autopilot_ai.browser import open_webpage
from autopilot_ai.desktop import _inspect_uia


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
