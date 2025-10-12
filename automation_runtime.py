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
        

    def type_text(self, text: str, interval: float) -> None:
        self._pyautogui.write(text, interval=interval)
