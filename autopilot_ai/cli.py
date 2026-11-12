"""Command-line entry point for the first runnable agent slice."""

import argparse
import json
from pathlib import Path

from .approved_dirs import (
    ApprovedDirectoryError,
    add_approved_dirs,
    clear_approved_dirs,
    load_approved_dirs,
    remove_approved_dirs,
)
from .approved_apps import ApprovedAppError, approve_app, load_approved_apps
from .agent import run_goal
from .conversation import ConversationSession
from .memory import TaskMemory


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a bounded AutoPilot AI task.")
    parser.add_argument("goal", nargs="?", help="Natural-language task request or a bare filename")
    parser.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parents[1] / "workspace")
    parser.add_argument(
        "--approved-dir",
        type=Path,
        action="append",
        default=[],
        help="Additional existing directory approved for filename search and file reading; may be repeated.",
    )
    parser.add_argument("--approve-dir", type=Path, action="append", default=[], help="Permanently add an existing directory to the local search and file-read allowlist (stored under data/ on D:).")
    parser.add_argument("--unapprove-dir", type=Path, action="append", default=[], help="Remove a directory from the persistent search allowlist.")
    parser.add_argument("--list-approved-dirs", action="store_true", help="Show the persistent search allowlist.")
    parser.add_argument("--clear-approved-dirs", action="store_true", help="Clear the persistent search allowlist.")
    parser.add_argument("--approve-app", type=str, action="append", default=[], help="Permanently approve an application name for launching; may be repeated.")
    parser.add_argument("--list-approved-apps", action="store_true", help="Show applications approved for launching.")
    parser.add_argument("--planner", choices=("ollama", "heuristic"), default="ollama", help="Use local Ollama (default) or the deterministic starter planner")
    parser.add_argument("--approve", action="store_true", help="Explicitly approve the planned filesystem change (create/copy).")
    parser.add_argument("--debug", action="store_true", help="Print the full structured JSON report, including plans and verification evidence.")
    parser.add_argument("--delay-seconds", type=int, choices=range(0, 31), default=0, help="Wait up to 30 seconds before desktop inspection so you can switch to the app to inspect.")
    parser.add_argument("--interactive", action="store_true", help="Start a persistent conversation; numbered search results can be selected in the next turn.")
    parser.add_argument("--history", nargs="?", const="", metavar="SESSION_ID", help="Show recent task history, optionally for one session.")
    parser.add_argument("--resume", metavar="SESSION_ID", help="Continue an existing session (use with --interactive or a task goal).")
    args = parser.parse_args()

    managing = bool(args.approve_dir or args.unapprove_dir or args.list_approved_dirs or args.clear_approved_dirs or args.approve_app or args.list_approved_apps)
    if managing and args.goal:
        parser.error("Manage approved directories in a separate command without a task goal.")
    management_operations = (args.approve_dir, args.unapprove_dir, args.list_approved_dirs, args.clear_approved_dirs, args.approve_app, args.list_approved_apps)
    if sum(bool(value) for value in management_operations) > 1:
        parser.error("Choose one approved-directory or approved-application management operation per command.")
    if args.interactive and args.goal:
        parser.error("Use --interactive without a task goal; enter tasks at the prompt.")
    if args.interactive and args.approve:
        parser.error("Interactive mode cannot pre-approve filesystem changes; approve changes with a one-shot command.")
    if args.history is not None and (args.goal or args.interactive or args.resume):
        parser.error("Use --history by itself, optionally followed by a session ID.")

    try:
        if args.approve_dir:
            roots = add_approved_dirs(args.approve_dir)
            print(json.dumps({"status": "updated", "approved_dirs": [str(path) for path in roots]}, indent=2))
            return
        if args.unapprove_dir:
            roots = remove_approved_dirs(args.unapprove_dir)
            print(json.dumps({"status": "updated", "approved_dirs": [str(path) for path in roots]}, indent=2))
            return
        if args.clear_approved_dirs:
            clear_approved_dirs()
            print(json.dumps({"status": "cleared", "approved_dirs": []}, indent=2))
            return
        if args.list_approved_dirs:
            roots = load_approved_dirs()
            print(json.dumps({"status": "ok", "approved_dirs": [str(path) for path in roots]}, indent=2))
            return
        if args.approve_app:
            for app_name in args.approve_app:
                approve_app(app_name)
            print(json.dumps({"status": "updated", "approved_apps": sorted(load_approved_apps())}, indent=2))
            return
        if args.list_approved_apps:
            print(json.dumps({"status": "ok", "approved_apps": sorted(load_approved_apps())}, indent=2))
            return
        if args.history is not None:
            memory = TaskMemory()
            entries = memory.get_recent_history(args.history) if args.history else memory.get_recent_sessions_history()
            print(json.dumps({"status": "ok", "history": entries}, indent=2, ensure_ascii=False))
            return
        if args.interactive:
            _run_interactive(args)
            return
        if not args.goal:
            parser.error("Provide a natural-language task or filename, or use an approved-directory management option.")
        approved_dirs = [*load_approved_dirs(), *args.approved_dir]
        memory = TaskMemory()
        conversation = ConversationSession(memory, args.resume) if args.resume else ConversationSession(memory)
        report = conversation.handle(
            args.goal,
            lambda goal: run_goal(goal, args.workspace, planner=args.planner, approved_dirs=approved_dirs, approved=args.approve, desktop_delay_seconds=args.delay_seconds),
        )
    except (ApprovedDirectoryError, ApprovedAppError, ValueError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, indent=2))
        raise SystemExit(1) from exc
    print(render_report(report, debug=args.debug))
    if report["status"] not in ("completed", "approval_required"):
        raise SystemExit(1)


def _run_interactive(args: argparse.Namespace) -> None:
    """Run a persistent prompt loop; each task still goes through run_goal."""
    memory = TaskMemory()
    session = ConversationSession(memory, args.resume) if args.resume else ConversationSession(memory)
    approved_dirs = [*load_approved_dirs(), *args.approved_dir]
    print(f"AutoPilot conversation started. Session ID: {session.session_id}")
    print("Type 'exit' or 'quit' to end. Task history persists across runs; numbered search choices remain temporary.")
    while True:
        try:
            message = input("You> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("Conversation ended.")
            return
        if message.casefold() in {"exit", "quit"}:
            print("Conversation ended.")
            return
        if not message:
            continue
        report = session.handle(
            message,
            lambda goal: run_goal(
                goal,
                args.workspace,
                planner=args.planner,
                approved_dirs=approved_dirs,
                approved=args.approve,
                desktop_delay_seconds=args.delay_seconds,
            ),
        )
        print(render_report(report, debug=args.debug))


def render_report(report: dict, debug: bool = False) -> str:
    """Render normal user output or preserve the complete structured report."""
    if debug:
        return json.dumps(report, indent=2, ensure_ascii=True)
    if report.get("status") == "candidate_selected":
        return format_selection(report)
    return format_report(report)


def format_report(report: dict) -> str:
    """Render a concise human-readable result; use --debug for full JSON."""
    labels = {
        "completed": "Completed",
        "approval_required": "Approval required",
        "clarification_required": "Clarification required",
        "failed": "Failed",
        "partially_completed": "Partially completed",
        "unsupported": "Unsupported",
        "denied": "Denied",
        "error": "Failed",
    }
    lines = [f"Task: {report.get('goal', 'AutoPilot task')}", f"Status: {labels.get(report.get('status'), 'Failed')}"]
    if report.get("session_id"):
        lines.append(f"Session ID: {report['session_id']}")
    if report.get("selected_folder"):
        lines.append(f"Selected folder: {report['selected_folder']}")
    if report.get("workflow") == "react_file_filter_copy":
        if report.get("summary") and report.get("status") != "clarification_required":
            lines.append(str(report["summary"]))
        if report.get("status") == "clarification_required":
            lines.append(f"Question: {report.get('summary', 'Please clarify the request.')}")
        observations = report.get("observations", [])
        if observations:
            lines.append("Actions and checks:")
            for index, observation in enumerate(observations, start=1):
                action = observation.get("action", "Action")
                path = observation.get("path") or observation.get("destination") or ""
                result = observation.get("tool_result", {})
                verification = observation.get("verification", {})
                detail = f" {path}" if path else ""
                if "passed" in verification:
                    checked = "verified" if verification["passed"] else "verification failed"
                    lines.append(f"  {index}. {action}{detail} — {checked}")
                elif "verified" in observation:
                    lines.append(f"  {index}. {action}{detail} — {'verified' if observation['verified'] else 'verification failed'}")
                elif result:
                    lines.append(f"  {index}. {action}{detail} — {result.get('status', 'unknown')}")
        planned = report.get("planned_actions", [])
        if planned:
            lines.append("Awaiting approval for:")
            for action in planned:
                if action["action"] == "create_directory":
                    lines.append(f"  - Create folder: {action['path']}")
                else:
                    lines.append(f"  - Copy: {action['source']} -> {action['destination']}")
        if report.get("status") == "approval_required":
            lines.append("Next: rerun the same request with --approve to apply these changes.")
        elif report.get("status") in {"failed", "partially_completed", "denied"}:
            lines.append(f"Details: {report.get('summary') or report.get('message', 'The workflow stopped.')}")
        return "\n".join(lines)
    steps = report.get("steps")
    reports = steps if isinstance(steps, list) else ([report] if report.get("plan") else [])
    if reports:
        executed = any(item.get("tool_result", {}).get("status") == "success" for item in reports)
        attempted = any("tool_result" in item for item in reports)
        heading = "What was performed:" if executed else ("Attempted operation:" if attempted else "Planned operation:")
        lines.append(heading)
        for item in reports:
            plan = item.get("plan", {})
            result = item.get("tool_result", {})
            output = result.get("output", {})
            lines.append(f"- {plan.get('description', plan.get('tool_name', 'Operation'))}")
            if plan.get("tool_name") == "list_directory":
                items = output.get("items", [])
                visible_items = [item for item in items if isinstance(item, dict)]
                lines.append(f"  Found {len(visible_items)} item(s).")
                for entry in visible_items[:50]:
                    lines.append(f"  {entry.get('name', '')} ({entry.get('type', 'unknown')})")
                if len(visible_items) > 50:
                    lines.append(f"  ...and {len(visible_items) - 50} more item(s). Use --debug for the full listing.")
            elif plan.get("tool_name") == "search_files":
                matches = output.get("matches", [])
                if output.get("complete") is False:
                    lines.append(f"  Found {len(matches)} candidate file(s) before the scan became incomplete; more matches may exist.")
                else:
                    lines.append(f"  Found {len(matches)} matching file(s).")
                if output.get("extensions"):
                    lines.append(f"  File type: {', '.join(output['extensions'])}")
                elif output.get("query"):
                    lines.append(f"  Filename: {output['query']} ({output.get('match_mode', 'exact')} match)")
                if output.get("roots"):
                    lines.append("  Searched in:")
                    lines.extend(f"  - {root}" for root in output["roots"])
                for index, match in enumerate(matches, start=1):
                    lines.append(f"  {index}) {Path(match).name}")
                    lines.append(f"     {match}")
                if output.get("complete") is False:
                    lines.append("  Warning: the search was incomplete; inspect the errors before relying on these results.")
                    for error in output.get("errors", [])[:3]:
                        lines.append(f"  Search warning: {error}")
                    if len(output.get("errors", [])) > 3:
                        lines.append(f"  ...and {len(output['errors']) - 3} more search warning(s). Use --debug for the full report.")
            elif plan.get("tool_name") == "search_web":
                results = output.get("results", [])
                lines.append(f"  Provider: {output.get('provider', 'unknown')}")
                lines.append(f"  Query: {output.get('query', plan.get('arguments', {}).get('query', ''))}")
                lines.append(f"  Found {len(results)} result(s).")
                for index, item in enumerate(results, start=1):
                    lines.append(f"  {index}) {item.get('title', '(untitled)')}")
                    lines.append(f"     {item.get('url', '')}")
                    if item.get("snippet"):
                        label = "Top-result snippet" if index == 1 else "Snippet"
                        lines.append(f"     {label}: {item['snippet'][:350]}")
            elif plan.get("tool_name") == "create_directory":
                lines.append(f"  Directory: {output.get('path', '(not available)')}")
            elif plan.get("tool_name") == "copy_file":
                lines.append(f"  Copied to: {output.get('destination', '(not available)')}")
                lines.append(f"  Size: {output.get('size_bytes', 'unknown')} bytes")
            elif plan.get("tool_name") == "write_file":
                lines.append(f"  File: {output.get('path', '(not available)')}")
                lines.append(f"  Mode: {output.get('mode', plan.get('arguments', {}).get('mode', 'unknown'))}")
                if output.get("bytes_written") is not None:
                    lines.append(f"  Bytes written: {output['bytes_written']}")
                if output.get("sha256"):
                    lines.append(f"  SHA-256: {output['sha256']}")
            elif plan.get("tool_name") == "run_command":
                lines.append(f"  Exit code: {output.get('exit_code', 'unknown')}")
                if output.get("stdout"):
                    lines.append(f"  Output: {output['stdout'][:500]}")
                if output.get("stderr"):
                    lines.append(f"  Error output: {output['stderr'][:500]}")
            elif plan.get("tool_name") == "launch_app":
                lines.append(f"  Application: {output.get('app_name', '(not available)')}")
                lines.append(f"  Process ID: {output.get('pid', 'unknown')}")
                lines.append(f"  Executable: {output.get('executable_path', '(not available)')}")
            elif plan.get("tool_name") == "desktop_action":
                arguments = plan.get("arguments", {})
                action_type = output.get("action_type", arguments.get("action_type", "unknown"))
                lines.append(f"  Action: {action_type}")
                if action_type in {"click", "double_click", "right_click"}:
                    lines.append(f"  Screen position: ({arguments.get('x', 'unknown')}, {arguments.get('y', 'unknown')})")
                elif action_type == "type_text":
                    lines.append(f"  Text: {arguments.get('text', '')[:200]}")
                else:
                    lines.append(f"  Keys: {' + '.join(arguments.get('keys', []))}")
            elif plan.get("tool_name") == "read_file_contents":
                lines.append(f"  File: {output.get('path', '(not available)')}")
                if output.get("page_number") is not None:
                    lines.append(f"  PDF page: {output['page_number']}")
                content = output.get("content")
                if isinstance(content, str):
                    lines.append(f"  Content: {content[:1000]}")
                if output.get("truncated"):
                    lines.append("  Output was truncated; use --debug for the full returned slice metadata.")
            elif plan.get("tool_name") == "open_webpage":
                if result.get("status") == "success":
                    lines.append(f"  Page: {output.get('title', '(untitled)')} — {output.get('url', '')}")
                    if output.get("text"):
                        lines.append(f"  Visible text: {output['text'][:500]}")
            elif plan.get("tool_name") == "inspect_active_window":
                if output.get("backend") == "uia":
                    lines.append(f"  Window: {output.get('window_title', '(untitled)')}; {output.get('controls_returned', 0)} controls read.")
                    if output.get("controls_skipped"):
                        lines.append(f"  {output['controls_skipped']} control(s) could not be read or had no visible label/type.")
                    if output.get("controls_truncated"):
                        lines.append(f"  Control list limited to {output.get('controls_limit', output.get('controls_returned', 0))}; more controls were available.")
                    controls = output.get("controls", [])
                    named_controls = [
                        f"{control.get('name') or '(unnamed)'} [{control.get('control_type') or 'control'}]"
                        for control in controls[:20]
                        if isinstance(control, dict)
                    ]
                    if named_controls:
                        lines.append("  Visible controls:")
                        lines.extend(f"  - {name}" for name in named_controls)
                    if output.get("controls_truncated"):
                        lines.append("  More controls were available; use --debug for the complete structured result.")
                else:
                    lines.append(f"  Active-window screenshot saved to: {output.get('screenshot_path', '(not available)')}")
                    if output.get("uia_note"):
                        lines.append(f"  UI Automation could not read the window: {output['uia_note']}")
            verification = item.get("verification", {})
            if verification:
                label = "passed" if verification.get("passed") else "failed"
                lines.append(f"  Verification: {label}. {verification.get('summary', '')}".rstrip())
            if result.get("status") != "success":
                detail = output.get("detail", result.get("error_code", "Operation failed."))
                lines.append(f"  Error: {detail}")
            if output.get("warning"):
                lines.append(f"  Warning: {output['warning']}")
    elif report.get("message"):
        lines.append(str(report["message"]))
    if reports and report.get("message"):
        lines.append(f"Details: {report['message']}")
    for issue in report.get("argument_errors", []):
        if isinstance(issue, dict):
            location = ".".join(str(part) for part in issue.get("loc", ()))
            detail = issue.get("msg", "Invalid value")
            location_text = f" at {location}" if location else ""
            lines.append(f"Argument error{location_text}: {detail}")
    if report.get("planner_note"):
        lines.append("Note: the local model plan was unavailable or did not match the request, so AutoPilot used its validated deterministic interpretation.")
    status = report.get("status")
    if status == "approval_required":
        plan = report.get("plan", {})
        if plan.get("tool_name") == "write_file":
            arguments = plan.get("arguments", {})
            lines.append(f"Target file: {arguments.get('path', '(not available)')}")
            lines.append(f"Write mode: {arguments.get('mode', 'create_new')}")
            content = arguments.get("content", "")
            if isinstance(content, str):
                content_truncated = len(content) > 500
                preview = content[:500].replace("\r", "\\r").replace("\n", "\\n")
                lines.append(f"Content preview: {preview}")
                if content_truncated:
                    lines.append(f"Preview limited to 500 characters ({len(content)} total); use --debug to inspect the full plan.")
        lines.append("Next: review the planned change, then rerun the same task with --approve.")
    elif status == "clarification_required":
        lines.append(f"Question: {report.get('question', 'Please clarify the request.')}")
        candidates = report.get("candidates", [])
        if candidates:
            candidate_kind = report.get("candidate_kind", report.get("scope_kind", "folder_scope"))
            heading = "Matching search results:" if candidate_kind == "search_result" else ("Matching approved files:" if candidate_kind == "read_file" else "Matching approved folders:")
            lines.append(heading)
            lines.extend(f"  {index}) {candidate}" for index, candidate in enumerate(candidates, start=1))
            if not report.get("follow_up_question"):
                next_hint = "Next: repeat the request with the exact file path from this list." if candidate_kind == "read_file" else "Next: repeat the request with the exact folder path from this list."
                lines.append(next_hint)
    if report.get("follow_up_question"):
        lines.append(f"Follow-up: {report['follow_up_question']} Reply with a number or ordinal (for example, 'the second one').")
    elif status == "unsupported":
        lines.append("Next: use a supported request or wait for that capability to be implemented.")
    elif status == "partially_completed":
        incomplete_search = any(
            item.get("plan", {}).get("tool_name") == "search_files"
            and item.get("tool_result", {}).get("output", {}).get("complete") is False
            for item in reports
        )
        if incomplete_search:
            lines.append("Next: check the scan warnings and folder access above, then rerun. These results are not exhaustive.")
        else:
            lines.append("Next: review the completed step evidence and resolve the failed step before continuing.")
    elif status in {"failed", "denied", "error"} and any(
        item.get("plan", {}).get("tool_name") == "open_webpage"
        and item.get("tool_result", {}).get("error_code") == "browser_error"
        for item in reports
    ):
        lines.append("Next: try a public page that allows automated reading, or open this site manually in your regular browser. AutoPilot does not use your signed-in browser session.")
    elif status in {"failed", "denied", "error"}:
        lines.append("Next: review the error above; no success is claimed for the failed operation.")
    return "\n".join(lines)


def format_selection(report: dict) -> str:
    return f"Selected result: {report['selected_path']}\n{report.get('message', '')}"


if __name__ == "__main__":
    main()
