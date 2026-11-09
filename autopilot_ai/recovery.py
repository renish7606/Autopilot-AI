"""Small retry policy for transient, read-only failures."""

import re

from .contracts import ToolResult, Verification


MAX_ATTEMPTS = 2
RETRYABLE_READ_ONLY_TOOLS = frozenset({"list_directory", "search_files", "open_webpage", "inspect_active_window", "read_file_contents"})
NON_RETRYABLE_MUTATING_TOOLS = frozenset({"create_directory", "copy_file", "write_file", "run_command", "launch_app"})


def should_retry(tool_name: str, result: ToolResult, verification: Verification, attempt: int) -> bool:
    """Retry at most once, and never repeat a mutating operation."""
    if (
        attempt >= MAX_ATTEMPTS
        or tool_name in NON_RETRYABLE_MUTATING_TOOLS
        or tool_name not in RETRYABLE_READ_ONLY_TOOLS
        or result.status == "denied"
    ):
        return False
    if result.status == "error":
        detail = str(result.output.get("detail", ""))
        if result.error_code == "browser_error" and re.search(r"\bHTTP\s+4\d{2}\b", detail, re.IGNORECASE):
            return False
        return result.error_code in {"browser_error", "desktop_error", "filesystem_error"}
    if result.status == "success" and not verification.passed:
        if tool_name == "search_files" and result.output.get("complete") is False:
            return False
        return True
    return False
