"""Read-only Windows UI Automation with a screenshot fallback."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import uuid4


class DesktopToolError(RuntimeError):
    pass


def _inspect_uia(max_controls: int) -> dict:
    try:
        import win32gui
        from pywinauto import Desktop
    except ImportError as exc:
        raise DesktopToolError("pywinauto and pywin32 are required for Windows UI Automation.") from exc

    try:
        handle = win32gui.GetForegroundWindow()
        if not handle:
            raise DesktopToolError("Windows did not report a foreground window.")
        window = Desktop(backend="uia").window(handle=handle).wrapper_object()
        title = window.window_text()
        controls = []
        elements = window.descendants()
        for element in elements:
            if len(controls) >= max_controls:
                break
            try:
                info = element.element_info
                name = element.window_text().strip()
                control_type = str(info.control_type or "")
                if not name and not control_type:
                    continue
                controls.append({
                    "name": name[:300],
                    "control_type": control_type[:80],
                    "automation_id": str(info.automation_id or "")[:100],
                })
            except Exception:
                continue
        return {
            "backend": "uia",
            "window_title": str(title or "")[:500],
            "process_id": int(window.process_id()),
            "window_handle": int(handle),
            "controls": controls,
            "controls_returned": len(controls),
            "controls_truncated": len(elements) > len(controls),
        }
    except Exception as exc:
        raise DesktopToolError(f"Windows UI Automation could not inspect the active window: {exc}") from exc


def _capture_screenshot() -> dict:
    try:
        import pyautogui
    except ImportError as exc:
        raise DesktopToolError("PyAutoGUI is not installed for screenshot fallback.") from exc

    directory = Path(__file__).resolve().parents[1] / "down" / "screenshots"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = directory / f"active-window-{stamp}-{uuid4().hex[:8]}.png"
    try:
        image = pyautogui.screenshot()
        image.save(path)
    except Exception as exc:
        raise DesktopToolError(f"PyAutoGUI could not capture the screen: {exc}") from exc
    return {
        "backend": "pyautogui_screenshot",
        "screenshot_path": str(path),
        "width": int(image.width),
        "height": int(image.height),
    }


def inspect_active_window(max_controls: int = 60, screenshot_fallback: bool = True) -> dict:
    """Read controls from the foreground window; save a screenshot if UIA fails."""
    try:
        return _inspect_uia(max_controls)
    except DesktopToolError as uia_error:
        if not screenshot_fallback:
            raise
        try:
            output = _capture_screenshot()
            output["uia_note"] = str(uia_error)[:500]
            return output
        except DesktopToolError as screenshot_error:
            raise DesktopToolError(f"UI Automation and screenshot fallback both failed. {uia_error} {screenshot_error}") from screenshot_error
