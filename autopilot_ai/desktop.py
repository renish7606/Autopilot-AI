"""Read-only Windows UI Automation with a screenshot fallback."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import uuid4


class DesktopToolError(RuntimeError):
    pass


class DesktopActionPolicyError(DesktopToolError):
    """A desktop action failed a local geometry or state policy check."""


def _desktop_state(pyautogui) -> dict:
    """Read current primary-screen geometry, cursor, and foreground window."""
    try:
        import win32gui
    except ImportError as exc:
        raise DesktopToolError("pywin32 is required to verify the active Windows window.") from exc
    try:
        size = pyautogui.size()
        position = pyautogui.position()
        width, height = int(size.width), int(size.height)
        x, y = int(position.x), int(position.y)
        handle = int(win32gui.GetForegroundWindow())
        if width <= 0 or height <= 0 or not handle:
            raise DesktopToolError("Windows did not report valid screen dimensions and a foreground window.")
        return {"screen_width": width, "screen_height": height, "cursor_x": x, "cursor_y": y, "foreground_window": handle}
    except DesktopToolError:
        raise
    except Exception as exc:
        raise DesktopToolError(f"Could not inspect the active desktop state: {exc}") from exc


def inspect_desktop_state() -> dict:
    """Fresh desktop observation used by the independent action verifier."""
    try:
        import pyautogui
    except ImportError as exc:
        raise DesktopToolError("PyAutoGUI is required for desktop actions.") from exc
    return _desktop_state(pyautogui)


def desktop_action(action_type: str, x: int | None, y: int | None, text: str | None, keys: list[str]) -> dict:
    """Perform one approved, screen-bounded mouse or keyboard action."""
    try:
        import pyautogui
    except ImportError as exc:
        raise DesktopToolError("PyAutoGUI is required for desktop actions.") from exc
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.25
    before = _desktop_state(pyautogui)
    if action_type in {"click", "double_click", "right_click"}:
        if x is None or y is None:
            raise DesktopActionPolicyError("Pointer actions require screen coordinates.")
        if not (0 < x < before["screen_width"] - 1 and 0 < y < before["screen_height"] - 1):
            raise DesktopActionPolicyError(
                f"Coordinates must be strictly inside the primary screen bounds (1..{before['screen_width'] - 2}, 1..{before['screen_height'] - 2})."
            )
    try:
        if action_type == "click":
            pyautogui.click(x=x, y=y)
        elif action_type == "double_click":
            pyautogui.doubleClick(x=x, y=y)
        elif action_type == "right_click":
            pyautogui.rightClick(x=x, y=y)
        elif action_type == "type_text":
            pyautogui.write(text, interval=0.01)
        elif action_type == "press_key":
            pyautogui.press(keys[0])
        elif action_type == "hotkey":
            pyautogui.hotkey(*keys)
        else:
            raise DesktopToolError("Unsupported desktop action.")
        after = _desktop_state(pyautogui)
    except DesktopToolError:
        raise
    except Exception as exc:
        raise DesktopToolError(f"Desktop action failed: {exc}") from exc
    return {
        "action_type": action_type,
        "x": x,
        "y": y,
        "text_length": len(text) if text is not None else None,
        "keys": keys,
        "before": before,
        "after": after,
    }


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
        controls_skipped = 0
        elements = window.descendants()
        elements_examined = 0
        for element in elements:
            if len(controls) >= max_controls:
                break
            elements_examined += 1
            try:
                info = element.element_info
                name = element.window_text().strip()
                control_type = str(info.control_type or "")
                if not name and not control_type:
                    controls_skipped += 1
                    continue
                controls.append({
                    "name": name[:300],
                    "control_type": control_type[:80],
                    "automation_id": str(info.automation_id or "")[:100],
                })
            except Exception:
                controls_skipped += 1
                continue
        return {
            "backend": "uia",
            "window_title": str(title or "")[:500],
            "process_id": int(window.process_id()),
            "window_handle": int(handle),
            "controls": controls,
            "controls_returned": len(controls),
            "controls_limit": max_controls,
            "controls_examined": elements_examined,
            "controls_skipped": controls_skipped,
            "controls_truncated": elements_examined < len(elements),
        }
    except Exception as exc:
        raise DesktopToolError(f"Windows UI Automation could not inspect the active window: {exc}") from exc


def _capture_screenshot() -> dict:
    try:
        import win32gui
        import win32process
        import pyautogui
    except ImportError as exc:
        raise DesktopToolError("PyAutoGUI and pywin32 are required for active-window screenshot fallback.") from exc

    try:
        handle = win32gui.GetForegroundWindow()
        if not handle:
            raise DesktopToolError("Windows did not report a foreground window to capture.")
        left, top, right, bottom = win32gui.GetWindowRect(handle)
        screen_width, screen_height = pyautogui.size()
        # Capture only the part of the foreground window visible on the primary
        # display. Never silently fall back to a screenshot of every window.
        left, top = max(0, left), max(0, top)
        right, bottom = min(screen_width, right), min(screen_height, bottom)
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0:
            raise DesktopToolError("The active window is not visible on the primary display.")
        title = str(win32gui.GetWindowText(handle) or "")[:500]
        _, process_id = win32process.GetWindowThreadProcessId(handle)
    except DesktopToolError:
        raise
    except Exception as exc:
        raise DesktopToolError(f"Could not determine the active window bounds: {exc}") from exc

    directory = Path(__file__).resolve().parents[1] / "down" / "screenshots"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = directory / f"active-window-{stamp}-{uuid4().hex[:8]}.png"
    try:
        image = pyautogui.screenshot(region=(left, top, width, height))
        image.save(path)
    except Exception as exc:
        raise DesktopToolError(f"PyAutoGUI could not capture the screen: {exc}") from exc
    return {
        "backend": "pyautogui_screenshot",
        "screenshot_path": str(path),
        "width": int(image.width),
        "height": int(image.height),
        "window_title": title,
        "process_id": int(process_id),
        "window_handle": int(handle),
        "window_rect": [left, top, right, bottom],
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
