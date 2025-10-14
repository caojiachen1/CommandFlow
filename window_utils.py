"""Utilities for enumerating and activating desktop windows on Windows."""

from __future__ import annotations

from typing import Any, Dict, List, Optional
import ctypes
from ctypes import wintypes
import sys

__all__ = [
    "list_windows",
    "activate_window",
    "find_window_by_title",
    "is_window_valid",
]

if sys.platform != "win32":  # pragma: no cover - non-Windows fallback

    def list_windows() -> List[Dict[str, Any]]:
        return []

    def activate_window(hwnd: int) -> bool:
        return False

    def find_window_by_title(title: str) -> Optional[int]:
        return None

    def is_window_valid(hwnd: int) -> bool:
        return False

else:  # pragma: no cover - Windows-only implementation

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    SW_RESTORE = 9

    def _get_window_text(hwnd: int) -> str:
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value.strip()

    def _get_class_name(hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buffer, 256)
        return buffer.value

    def _should_include(hwnd: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return False
        title = _get_window_text(hwnd)
        if not title:
            return False
        class_name = _get_class_name(hwnd)
        if class_name in {"Shell_TrayWnd", "Progman"}:
            return False
        return True

    def list_windows() -> List[Dict[str, Any]]:
        windows: List[Dict[str, Any]] = []

        def _callback(hwnd, _lparam):
            if _should_include(hwnd):
                windows.append(
                    {
                        "hwnd": int(hwnd),
                        "title": _get_window_text(hwnd),
                        "class_name": _get_class_name(hwnd),
                        "is_minimized": bool(user32.IsIconic(hwnd)),
                    }
                )
            return True

        user32.EnumWindows(EnumWindowsProc(_callback), 0)
        return windows

    def is_window_valid(hwnd: int) -> bool:
        return bool(hwnd) and bool(user32.IsWindow(hwnd))

    def _restore_window(hwnd: int) -> None:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)

    def _attach_threads(source_thread: int, target_thread: int, attach: bool) -> None:
        if source_thread and target_thread and source_thread != target_thread:
            user32.AttachThreadInput(source_thread, target_thread, attach)

    def activate_window(hwnd: int) -> bool:
        if not is_window_valid(hwnd):
            return False
        _restore_window(hwnd)
        foreground = user32.GetForegroundWindow()
        current_thread = kernel32.GetCurrentThreadId()
        target_thread = user32.GetWindowThreadProcessId(hwnd, None)
        foreground_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0

        _attach_threads(current_thread, target_thread, True)
        if foreground_thread:
            _attach_threads(current_thread, foreground_thread, True)

        user32.BringWindowToTop(hwnd)
        success = bool(user32.SetForegroundWindow(hwnd))
        if not success:
            switch = getattr(user32, "SwitchToThisWindow", None)
            if switch is not None:
                try:
                    switch(hwnd, True)
                    success = True
                except Exception:  # pragma: no cover - defensive
                    success = False

        _attach_threads(current_thread, target_thread, False)
        if foreground_thread:
            _attach_threads(current_thread, foreground_thread, False)

        return success

    def find_window_by_title(title: str) -> Optional[int]:
        normalized = title.strip().lower()
        if not normalized:
            return None
        windows = list_windows()
        for window in windows:
            title_value = str(window.get("title", ""))
            if title_value.lower() == normalized:
                return int(window.get("hwnd", 0))
        for window in windows:
            title_value = str(window.get("title", ""))
            if normalized in title_value.lower():
                return int(window.get("hwnd", 0))
        return None
