"""Independent postcondition check for the initial directory-listing tool."""

from pathlib import Path
import os
import hashlib

from .contracts import ToolResult, Verification
from .tools import independently_list_names
from .browser import open_webpage
from .desktop import DesktopToolError, inspect_active_window


def verify_directory_listing(result: ToolResult, directory: Path) -> Verification:
    if result.status != "success":
        return Verification(passed=False, summary="Tool did not complete successfully.", evidence={"status": result.status, "error_code": result.error_code})
    observed = result.output.get("items")
    if not isinstance(observed, list) or any(not isinstance(item, dict) or not isinstance(item.get("name"), str) for item in observed):
        return Verification(passed=False, summary="Tool output did not contain a valid entry list.")
    tool_names = sorted((item["name"] for item in observed), key=str.casefold)
    fresh_names = independently_list_names(directory)
    passed = tool_names == fresh_names
    return Verification(
        passed=passed,
        summary="Workspace listing independently verified." if passed else "Workspace listing differs from a fresh filesystem scan.",
        evidence={"tool_names": tool_names, "fresh_names": fresh_names},
    )


def verify_file_search(
    result: ToolResult,
    roots: list[Path],
    query: str,
    match_mode: str,
    max_results: int,
) -> Verification:
    """Repeat an exact/substring filename scan across every approved root."""
    if result.status != "success":
        return Verification(passed=False, summary="Search tool did not complete successfully.", evidence={"status": result.status, "error_code": result.error_code})

    observed = result.output.get("matches")
    if not isinstance(observed, list) or any(not isinstance(item, str) for item in observed):
        return Verification(passed=False, summary="Search tool output did not contain a valid match list.")

    needle = query.casefold().strip()
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
                folded_name = name.casefold()
                matched = folded_name == needle if match_mode == "exact" else needle in folded_name
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
        and result.output.get("roots") == [str(root) for root in roots]
        and result.output.get("count") == len(observed)
        and result.output.get("truncated") == truncated
        and result.output.get("complete") is complete
    )
    passed = observed_sorted == fresh and output_consistent and complete
    if passed:
        summary = "Filename search independently verified across all approved roots."
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
    required = (isinstance(observed.get("url"), str), isinstance(observed.get("title"), str), isinstance(observed.get("text"), str), isinstance(observed.get("http_status"), int))
    if not all(required) or not 200 <= observed.get("http_status", 0) < 400:
        return Verification(passed=False, summary="Browser output did not contain a valid successful page response.")
    try:
        fresh = open_webpage(requested_url)
    except Exception as exc:
        return Verification(passed=False, summary="A fresh browser load could not independently verify the page.", evidence={"error": str(exc)})
    same_url = fresh["url"].rstrip("/").casefold() == observed["url"].rstrip("/").casefold()
    same_title = fresh["title"] == observed["title"]
    passed = 200 <= fresh["http_status"] < 400 and same_url and same_title
    return Verification(passed=passed, summary="Page load independently verified in a fresh browser context." if passed else "A fresh page load differed from the original navigation.", evidence={"first_url": observed["url"], "fresh_url": fresh["url"], "first_title": observed["title"], "fresh_title": fresh["title"], "fresh_http_status": fresh["http_status"], "url_matches": same_url, "title_matches": same_title})


def verify_desktop_inspection(result: ToolResult) -> Verification:
    if result.status != "success":
        return Verification(passed=False, summary="Desktop inspection did not complete successfully.", evidence={"status": result.status, "error_code": result.error_code})
    output = result.output
    if output.get("backend") == "uia":
        try:
            fresh = inspect_active_window(max_controls=output.get("controls_returned", 60), screenshot_fallback=False)
        except DesktopToolError as exc:
            return Verification(passed=False, summary="A fresh UI Automation read could not verify the window.", evidence={"error": str(exc)})
        same_window = fresh.get("window_title") == output.get("window_title") and fresh.get("process_id") == output.get("process_id")
        return Verification(passed=same_window, summary="Active window independently verified with UI Automation." if same_window else "The active window changed before verification.", evidence={"window_title": output.get("window_title"), "fresh_window_title": fresh.get("window_title"), "process_id": output.get("process_id"), "fresh_process_id": fresh.get("process_id"), "same_window": same_window})
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
        return Verification(passed=dimensions_match, summary="Saved desktop screenshot independently verified." if dimensions_match else "Screenshot dimensions differ from tool output.", evidence={"screenshot_path": str(path), "width": width, "height": height, "dimensions_match": dimensions_match})
    return Verification(passed=False, summary="Desktop result used an unknown inspection backend.")
