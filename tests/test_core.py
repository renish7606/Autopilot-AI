from pathlib import Path
from io import BytesIO
import json
from urllib.error import HTTPError

import pytest

from autopilot_ai.approved_dirs import add_approved_dirs, clear_approved_dirs, load_approved_dirs, remove_approved_dirs
from autopilot_ai.agent import run_goal
from autopilot_ai.contracts import PlanStep
from autopilot_ai.planner import ModelPlannerError, PlanningError, plan_with_ollama
from autopilot_ai.policy import PathPolicy, PolicyError
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

            return json.dumps({"message": {"content": step.model_dump_json()}}).encode()

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

            return json.dumps({"message": {"content": step.model_dump_json()}}).encode()

    monkeypatch.setattr("autopilot_ai.planner.urlopen", lambda request, timeout: FakeResponse())
    with pytest.raises(ModelPlannerError, match="unavailable tool"):
        plan_with_ollama("Run a command", ("list_directory",))


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
