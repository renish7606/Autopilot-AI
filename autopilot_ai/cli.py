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
from .agent import run_goal


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a bounded AutoPilot AI task.")
    parser.add_argument("goal", nargs="?", help="Natural-language task request or a bare filename")
    parser.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parents[1] / "workspace")
    parser.add_argument(
        "--approved-dir",
        type=Path,
        action="append",
        default=[],
        help="Additional existing directory approved for filename search; may be repeated.",
    )
    parser.add_argument("--approve-dir", type=Path, action="append", default=[], help="Permanently add an existing directory to the local search allowlist (stored under data/ on D:).")
    parser.add_argument("--unapprove-dir", type=Path, action="append", default=[], help="Remove a directory from the persistent search allowlist.")
    parser.add_argument("--list-approved-dirs", action="store_true", help="Show the persistent search allowlist.")
    parser.add_argument("--clear-approved-dirs", action="store_true", help="Clear the persistent search allowlist.")
    parser.add_argument("--planner", choices=("ollama", "heuristic"), default="ollama", help="Use local Ollama (default) or the deterministic starter planner")
    parser.add_argument("--approve", action="store_true", help="Explicitly approve the planned filesystem change (create/copy).")
    args = parser.parse_args()

    managing = bool(args.approve_dir or args.unapprove_dir or args.list_approved_dirs or args.clear_approved_dirs)
    if managing and args.goal:
        parser.error("Manage approved directories in a separate command without a task goal.")
    if sum(bool(value) for value in (args.approve_dir, args.unapprove_dir, args.list_approved_dirs, args.clear_approved_dirs)) > 1:
        parser.error("Choose one approved-directory management operation per command.")

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
        if not args.goal:
            parser.error("Provide a natural-language task or filename, or use an approved-directory management option.")
        approved_dirs = [*load_approved_dirs(), *args.approved_dir]
        report = run_goal(args.goal, args.workspace, planner=args.planner, approved_dirs=approved_dirs, approved=args.approve)
    except ApprovedDirectoryError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, indent=2))
        raise SystemExit(1) from exc
    print(json.dumps(report, indent=2, ensure_ascii=True))
    if report["status"] not in ("completed", "approval_required"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
