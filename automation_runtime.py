"""Runtime adapters for desktop automation backends.

Contains the default ``PyAutoGuiRuntime`` used by the GUI layer as well as
helpers that make the automation dependency injectable for tests or alternate
backends.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Tuple


class PyAutoGuiRuntime:
    """Expose :mod:`pyautogui` operations behind the AutomationRuntime protocol."""

    def __init__(self, pyautogui_module: Any | None = None) -> None:
        self._pyautogui = pyautogui_module or self._import_pyautogui()
        self._pyautogui.FAILSAFE = True
        self._pyautogui.PAUSE = 0.05
        self._failsafe_exception_type: type[BaseException] = getattr(
            self._pyautogui,
            "FailSafeException",
            RuntimeError,
        )

    @staticmethod
    def _import_pyautogui() -> Any:
        try:
            import pyautogui  # type: ignore
        except ImportError as exc:  # pragma: no cover - runtime requirement
            raise RuntimeError("pyautogui is required to run the GUI workflow") from exc
        return pyautogui

    def take_screenshot(self, region: Tuple[int, int, int, int]) -> Path:
        image = self._pyautogui.screenshot(region=region)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
            image.save(tmp.name)
            return Path(tmp.name)

    def mouse_click(
        self,
        x: int,
        y: int,
        button: str,
        clicks: int,
        interval: float,
    ) -> None:
        try:
            self._pyautogui.click(
                x=x,
                y=y,
                button=button,
                clicks=clicks,
                interval=interval,
            )
        except self._failsafe_exception_type as exc:
            raise RuntimeError(
                f"PyAutoGUI fail-safe triggered while clicking at ({x}, {y}). "
                "The pointer reached a screen corner. Adjust the coordinates to avoid "
                "screen corners, or disable pyautogui.FAILSAFE only if you understand the risks."
            ) from exc

    def move_mouse(self, x: int, y: int, duration: float) -> None:
        try:
            self._pyautogui.moveTo(x, y, duration=duration)
        except self._failsafe_exception_type as exc:
            raise RuntimeError(
                f"PyAutoGUI fail-safe triggered while moving to ({x}, {y})."
            ) from exc

    def drag_mouse(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        button: str,
        move_duration: float,
        drag_duration: float,
    ) -> None:
        try:
            self._pyautogui.moveTo(start_x, start_y, duration=move_duration)
            self._pyautogui.dragTo(
                end_x,
                end_y,
                duration=drag_duration,
                button=button,
            )
        except self._failsafe_exception_type as exc:
            raise RuntimeError(
                "PyAutoGUI fail-safe triggered while dragging the pointer."
            ) from exc

    def mouse_scroll(
        self,
        clicks: int,
        orientation: str,
        x: int | None,
        y: int | None,
    ) -> None:
        if orientation == "horizontal":
            self._pyautogui.hscroll(clicks, x=x, y=y)
        else:
            self._pyautogui.scroll(clicks, x=x, y=y)

    def mouse_down(self, x: int, y: int, button: str) -> None:
        try:
            self._pyautogui.mouseDown(x=x, y=y, button=button)
        except self._failsafe_exception_type as exc:
            raise RuntimeError(
                f"PyAutoGUI fail-safe triggered while pressing the {button} button at ({x}, {y})."
            ) from exc

    def mouse_up(self, x: int, y: int, button: str) -> None:
        try:
            self._pyautogui.mouseUp(x=x, y=y, button=button)
        except self._failsafe_exception_type as exc:
            raise RuntimeError(
                f"PyAutoGUI fail-safe triggered while releasing the {button} button at ({x}, {y})."
            ) from exc

    def type_text(self, text: str, interval: float) -> None:
        self._pyautogui.write(text, interval=interval)

    def press_key(self, key: str, presses: int, interval: float) -> None:
        self._pyautogui.press(key, presses=presses, interval=interval)

    def key_down(self, key: str) -> None:
        self._pyautogui.keyDown(key)

    def key_up(self, key: str) -> None:
        self._pyautogui.keyUp(key)

    def press_hotkey(self, keys: list[str], interval: float) -> None:
        if not keys:
            return
        self._pyautogui.hotkey(*keys, interval=interval)
