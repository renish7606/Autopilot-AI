"""Independent postcondition check for the initial directory-listing tool."""

from pathlib import Path
import os

from .contracts import ToolResult, Verification
from .tools import independently_list_names


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
