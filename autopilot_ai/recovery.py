"""Small retry policy for transient, read-only failures."""

from .contracts import ToolResult, Verification


MAX_ATTEMPTS = 2
RETRYABLE_READ_ONLY_TOOLS = frozenset({"list_directory", "search_files", "open_webpage", "inspect_active_window"})


def should_retry(tool_name: str, result: ToolResult, verification: Verification, attempt: int) -> bool:
    """Retry at most once, and never repeat a mutating operation."""
    if attempt >= MAX_ATTEMPTS or tool_name not in RETRYABLE_READ_ONLY_TOOLS or result.status == "denied":
        return False
    if result.status == "error":
        detail = str(result.output.get("detail", ""))
        if result.error_code == "browser_error" and "page returned HTTP 4" in detail:
            return False
        return result.error_code in {"browser_error", "desktop_error"}
    if result.status == "success" and not verification.passed:
        if tool_name == "search_files" and result.output.get("complete") is False:
            return False
        return True
    return False
