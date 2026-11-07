"""Independent postcondition checks for filesystem, browser, and desktop tools."""

from pathlib import Path
import os
import hashlib

from .contracts import ToolResult, Verification
from .browser import open_webpage
from .desktop import DesktopToolError, inspect_active_window
from .filename_matching import matches_filename


def verify_directory_listing(result: ToolResult, directory: Path) -> Verification:
    if result.status != "success":
        return Verification(passed=False, summary="Tool did not complete successfully.", evidence={"status": result.status, "error_code": result.error_code})
    observed = result.output.get("items")
    if not isinstance(observed, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("name"), str)
        or item.get("type") not in {"file", "directory", "symlink"}
        for item in observed
    ):
        return Verification(passed=False, summary="Tool output did not contain a valid entry list.")
    tool_items = sorted(observed, key=lambda item: item["name"].casefold())
    fresh_items = []
    try:
        for child in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
            if child.is_symlink():
                fresh_items.append({"name": child.name, "type": "symlink", "size_bytes": child.lstat().st_size})
            elif child.is_dir():
                fresh_items.append({"name": child.name, "type": "directory", "size_bytes": None})
            else:
                fresh_items.append({"name": child.name, "type": "file", "size_bytes": child.stat().st_size})
    except OSError as exc:
        return Verification(passed=False, summary="A fresh directory scan could not verify the listing.", evidence={"error": str(exc)})
    tool_names = [item["name"] for item in tool_items]
    fresh_names = [item["name"] for item in fresh_items]
    path_matches = result.output.get("path") == str(directory)
    count_matches = result.output.get("count") == len(tool_items) == len(fresh_items)
    passed = tool_items == fresh_items and path_matches and count_matches
    return Verification(
        passed=passed,
        summary="Workspace listing independently verified." if passed else "Workspace listing differs from a fresh filesystem scan.",
        evidence={
            "tool_names": tool_names,
            "fresh_names": fresh_names,
            "tool_items": tool_items,
            "fresh_items": fresh_items,
            "path_matches": path_matches,
            "count_matches": count_matches,
        },
    )


def verify_file_search(
    result: ToolResult,
    roots: list[Path],
    query: str,
    match_mode: str,
    max_results: int,
    extensions: list[str] | None = None,
) -> Verification:
    """Repeat a deterministic filename or extension scan across approved roots."""
    if result.status != "success":
        return Verification(passed=False, summary="Search tool did not complete successfully.", evidence={"status": result.status, "error_code": result.error_code})

    observed = result.output.get("matches")
    if not isinstance(observed, list) or any(not isinstance(item, str) for item in observed):
        return Verification(passed=False, summary="Search tool output did not contain a valid match list.")

    needle = query.casefold().strip()
    extensions = [extension.casefold() for extension in (extensions or [])]
    fresh: list[str] = []
    seen_matches: set[str] = set()
    scan_errors: list[str] = []
    visited = 0
    truncated = False
    for root in roots:
        def record_walk_error(error: OSError) -> None:
            scan_errors.append(str(error))

        for current, directories, files in os.walk(root, topdown=True, onerror=record_walk_error, followlinks=False):
            visited += len(directories) + len(files)
            if visited > 100_000:
                truncated = True
                break
            safe_directories = []
            for name in directories:
                directory = Path(current) / name
                if directory.is_symlink():
                    scan_errors.append(f"Skipped symlink directory: {directory}")
                else:
                    safe_directories.append(name)
            directories[:] = sorted(safe_directories, key=str.casefold)
            for name in sorted(files, key=str.casefold):
                candidate = Path(current) / name
                if candidate.is_symlink():
                    scan_errors.append(f"Skipped symlink file: {candidate}")
                    continue
                matched = matches_filename(name, needle, match_mode, extensions)
                if not matched:
                    continue
                full_path = str(candidate.resolve())
                match_key = os.path.normcase(full_path)
                if match_key in seen_matches:
                    continue
                seen_matches.add(match_key)
                fresh.append(full_path)
                if len(fresh) > max_results:
                    truncated = True
                    break
            if truncated:
                break
        if truncated:
            break

    fresh = sorted(fresh[:max_results], key=str.casefold)
    observed_sorted = sorted(observed, key=str.casefold)
    output_errors = result.output.get("errors", [])
    complete = not truncated and not scan_errors and not output_errors
    output_consistent = (
        result.output.get("query") == query
        and result.output.get("match_mode") == match_mode
        and result.output.get("extensions", []) == extensions
        and result.output.get("roots") == [str(root) for root in roots]
        and result.output.get("count") == len(observed)
        and result.output.get("truncated") == truncated
        and result.output.get("complete") is complete
    )
    passed = observed_sorted == fresh and output_consistent and complete
    if passed:
        summary = "Filename search independently verified within the approved search scope."
    elif not complete:
        summary = "Search was incomplete because a scan limit or filesystem access error was encountered."
    else:
        summary = "Search output differs from a fresh filesystem scan."
    return Verification(
        passed=passed,
        summary=summary,
        evidence={
            "tool_matches": observed_sorted,
            "fresh_matches": fresh,
            "truncated": truncated,
            "scan_errors": scan_errors,
            "tool_errors": output_errors,
            "entries_rechecked": min(visited, 100_000),
        },
    )


def verify_created_directory(result: ToolResult, destination: Path) -> Verification:
    exists = destination.is_dir() and not destination.is_symlink()
    output_matches = result.output.get("path") == str(destination) and result.output.get("created") is True
    passed = result.status == "success" and exists and output_matches
    return Verification(passed=passed, summary="Created directory independently verified." if passed else "Created directory could not be verified.", evidence={"path": str(destination), "is_directory": exists, "tool_output_matches": output_matches})


def verify_copied_file(result: ToolResult, source: Path, destination: Path) -> Verification:
    if result.status != "success" or not destination.is_file() or destination.is_symlink():
        return Verification(passed=False, summary="Copied file is missing or the tool failed.", evidence={"destination": str(destination), "exists": destination.is_file()})
    def file_digest(path: Path) -> tuple[int, str]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        return size, digest.hexdigest()
    try:
        source_size, source_hash = file_digest(source)
        dest_size, dest_hash = file_digest(destination)
    except OSError as exc:
        return Verification(passed=False, summary="Could not independently read the source and copied file.", evidence={"error": str(exc)})
    output_matches = (result.output.get("source") == str(source) and result.output.get("destination") == str(destination)
                      and result.output.get("size_bytes") == dest_size and result.output.get("sha256") == dest_hash)
    passed = source_size == dest_size and source_hash == dest_hash and output_matches
    return Verification(passed=passed, summary="Copied file content independently verified by SHA-256." if passed else "Copied file differs from source or tool output.", evidence={"source_size_bytes": source_size, "destination_size_bytes": dest_size, "source_sha256": source_hash, "destination_sha256": dest_hash, "tool_output_matches": output_matches})


def verify_webpage_open(result: ToolResult, requested_url: str) -> Verification:
    if result.status != "success":
        return Verification(passed=False, summary="Browser navigation did not complete successfully.", evidence={"status": result.status, "error_code": result.error_code})
    observed = result.output
    required = (
        observed.get("requested_url") == requested_url,
        isinstance(observed.get("url"), str),
        isinstance(observed.get("title"), str),
        isinstance(observed.get("text"), str) and bool(observed.get("text", "").strip()),
        isinstance(observed.get("http_status"), int),
    )
    if not all(required) or not 200 <= observed.get("http_status", 0) < 400:
        return Verification(
            passed=False,
            summary="Browser output did not match the requested URL or contain a valid successful page response.",
            evidence={
                "requested_url": requested_url,
                "reported_requested_url": observed.get("requested_url"),
                "http_status": observed.get("http_status"),
            },
        )
    try:
        fresh = open_webpage(requested_url)
    except Exception as exc:
        return Verification(passed=False, summary="A fresh browser load could not independently verify the page.", evidence={"error": str(exc)})
    if (
        not isinstance(fresh, dict)
        or fresh.get("requested_url") != requested_url
        or not isinstance(fresh.get("url"), str)
        or not isinstance(fresh.get("title"), str)
        or not isinstance(fresh.get("text"), str)
        or not isinstance(fresh.get("http_status"), int)
    ):
        return Verification(
            passed=False,
            summary="The fresh browser result did not contain a valid page observation.",
            evidence={"requested_url": requested_url, "fresh_observation": fresh},
        )
    same_url = fresh["url"].rstrip("/").casefold() == observed["url"].rstrip("/").casefold()
    same_title = fresh["title"] == observed["title"]
    same_status = fresh["http_status"] == observed["http_status"]
    visible_text_present = bool(fresh["text"].strip()) and bool(observed["text"].strip())
    passed = 200 <= fresh["http_status"] < 400 and same_url and same_status and visible_text_present
    return Verification(
        passed=passed,
        summary="Page load independently verified in a fresh browser context." if passed else "A fresh page load differed from the original navigation or contained no visible text.",
        evidence={
            "first_url": observed["url"],
            "fresh_url": fresh["url"],
            "first_title": observed["title"],
            "fresh_title": fresh["title"],
            "first_http_status": observed["http_status"],
            "fresh_http_status": fresh["http_status"],
            "url_matches": same_url,
            "title_matches": same_title,
            "status_matches": same_status,
            "visible_text_present": visible_text_present,
        },
    )


def verify_desktop_inspection(result: ToolResult) -> Verification:
    if result.status != "success":
        return Verification(passed=False, summary="Desktop inspection did not complete successfully.", evidence={"status": result.status, "error_code": result.error_code})
    output = result.output
    if output.get("backend") == "uia":
        controls = output.get("controls")
        controls_returned = output.get("controls_returned")
        controls_limit = output.get("controls_limit", 60)
        controls_skipped = output.get("controls_skipped", 0)
        controls_truncated = output.get("controls_truncated")
        controls_valid = (
            isinstance(controls, list)
            and isinstance(controls_returned, int)
            and not isinstance(controls_returned, bool)
            and controls_returned == len(controls)
            and isinstance(controls_limit, int)
            and not isinstance(controls_limit, bool)
            and 1 <= controls_limit <= 100
            and controls_returned <= controls_limit
            and isinstance(controls_skipped, int)
            and not isinstance(controls_skipped, bool)
            and controls_skipped >= 0
            and isinstance(controls_truncated, bool)
            and all(
                isinstance(control, dict)
                and isinstance(control.get("name"), str)
                and isinstance(control.get("control_type"), str)
                and isinstance(control.get("automation_id"), str)
                for control in controls
            )
        )
        if not controls_valid:
            return Verification(
                passed=False,
                summary="UI Automation returned a malformed or inconsistent control list.",
                evidence={
                    "controls_returned": controls_returned,
                    "actual_control_count": len(controls) if isinstance(controls, list) else None,
                    "controls_limit": controls_limit,
                    "controls_skipped": controls_skipped,
                    "controls_truncated": controls_truncated,
                },
            )
        try:
            fresh = inspect_active_window(max_controls=controls_limit, screenshot_fallback=False)
        except DesktopToolError as exc:
            return Verification(passed=False, summary="A fresh UI Automation read could not verify the window.", evidence={"error": str(exc)})
        same_window = (
            fresh.get("window_handle") == output.get("window_handle")
            and fresh.get("window_title") == output.get("window_title")
            and fresh.get("process_id") == output.get("process_id")
        )
        return Verification(
            passed=same_window,
            summary="Active window independently verified with UI Automation." if same_window else "The active window changed before verification.",
            evidence={
                "window_handle": output.get("window_handle"),
                "fresh_window_handle": fresh.get("window_handle"),
                "window_title": output.get("window_title"),
                "fresh_window_title": fresh.get("window_title"),
                "process_id": output.get("process_id"),
                "fresh_process_id": fresh.get("process_id"),
                "same_window": same_window,
                "controls_returned": controls_returned,
                "controls_skipped": controls_skipped,
                "controls_truncated": controls_truncated,
                "controls_limit": controls_limit,
            },
        )
    if output.get("backend") == "pyautogui_screenshot":
        path = Path(str(output.get("screenshot_path", ""))).resolve()
        screenshot_root = (Path(__file__).resolve().parents[1] / "down" / "screenshots").resolve()
        try:
            path.relative_to(screenshot_root)
            from PIL import Image
            with Image.open(path) as image:
                width, height = image.size
                image.verify()
        except (OSError, ValueError, ImportError) as exc:
            return Verification(passed=False, summary="The saved screenshot could not be independently opened.", evidence={"screenshot_path": str(path), "error": str(exc)})
        dimensions_match = width == output.get("width") and height == output.get("height")
        rect = output.get("window_rect")
        rect_valid = (
            isinstance(rect, list)
            and len(rect) == 4
            and all(isinstance(value, int) and not isinstance(value, bool) for value in rect)
            and rect[2] > rect[0]
            and rect[3] > rect[1]
        )
        rect_dimensions_match = rect_valid and width == rect[2] - rect[0] and height == rect[3] - rect[1]
        try:
            import win32gui

            active_handle = int(win32gui.GetForegroundWindow())
        except Exception as exc:
            return Verification(passed=False, summary="The active window could not be rechecked after screenshot capture.", evidence={"screenshot_path": str(path), "error": str(exc)})
        same_window = active_handle != 0 and active_handle == output.get("window_handle")
        passed = dimensions_match and rect_dimensions_match and same_window
        summary = "Active-window screenshot independently verified." if passed else "Screenshot dimensions, captured window bounds, or active-window identity could not be verified."
        return Verification(
            passed=passed,
            summary=summary,
            evidence={
                "screenshot_path": str(path),
                "width": width,
                "height": height,
                "dimensions_match": dimensions_match,
                "window_rect": rect,
                "rect_dimensions_match": rect_dimensions_match,
                "captured_window_handle": output.get("window_handle"),
                "fresh_window_handle": active_handle,
                "same_window": same_window,
            },
        )
    return Verification(passed=False, summary="Desktop result used an unknown inspection backend.")
