"""Runtime adapters for desktop automation backends.

Contains the default ``PyAutoGuiRuntime`` used by the GUI layer as well as
helpers that make the automation dependency injectable for tests or alternate
backends.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any, Tuple

import ctypes
import sys


def get_system_dpi_scale() -> float:
    """Return the system DPI scaling factor relative to the 96-DPI baseline."""

    if sys.platform != "win32":
        return 1.0
    
    # Method 1: Try GetScaleFactorForDevice (Windows 8.1+)
    try:
        shcore = ctypes.windll.shcore
        scale_value = ctypes.c_uint(0)
        # DEVICE_PRIMARY = 0
        if shcore.GetScaleFactorForDevice(0, ctypes.byref(scale_value)) == 0:
            if scale_value.value > 0:
                return max(scale_value.value / 100.0, 0.1)
    except (AttributeError, OSError):
        pass

    # Method 2: Try GetDpiForSystem (Windows 10 1607+)
    try:
        user32 = ctypes.windll.user32
        dpi = user32.GetDpiForSystem()
        if dpi > 0:
            return max(dpi / 96.0, 0.1)
    except (AttributeError, OSError):
        pass

    # Method 3: Try GetDeviceCaps with screen DC (Windows 7+)
    try:
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        hdc = user32.GetDC(0)
        if hdc:
            LOGPIXELSX = 88
            dpi_x = gdi32.GetDeviceCaps(hdc, LOGPIXELSX)
            user32.ReleaseDC(0, hdc)
            if dpi_x > 0:
                return max(dpi_x / 96.0, 0.1)
    except (AttributeError, OSError):
        pass

    # Method 4: Try GetDpiForWindow with desktop window (fallback)
    try:
        user32 = ctypes.windll.user32
        desktop_hwnd = user32.GetDesktopWindow()
        if desktop_hwnd:
            dpi = user32.GetDpiForWindow(desktop_hwnd)
            if dpi > 0:
                return max(dpi / 96.0, 0.1)
    except (AttributeError, OSError):
        pass

    return 1.0


class PyAutoGuiRuntime:
    """Expose :mod:`pyautogui` operations behind the AutomationRuntime protocol."""

    def __init__(
        self,
        pyautogui_module: Any | None = None,
        dpi_scale: float | None = None,
    ) -> None:
        self._pyautogui = pyautogui_module or self._import_pyautogui()
        self._pyautogui.FAILSAFE = True
        self._pyautogui.PAUSE = 0.05
        self._failsafe_exception_type: type[BaseException] = getattr(
            self._pyautogui,
            "FailSafeException",
            RuntimeError,
        )
        # 禁用 DPI 缩放：始终使用 1.0，输入坐标直接对应物理像素
        self._dpi_scale = 1.0

    @staticmethod
    def _import_pyautogui() -> Any:
        try:
            import pyautogui  # type: ignore
        except ImportError as exc:  # pragma: no cover - runtime requirement
            raise RuntimeError("pyautogui is required to run the GUI workflow") from exc
        return pyautogui

    def _scale_value(self, value: int, minimum: int = 0) -> int:
        scaled = int(round(value * self._dpi_scale))
        return max(scaled, minimum)

    def _scale_point(self, x: int, y: int) -> tuple[int, int]:
        return self._scale_value(x), self._scale_value(y)

    def _scale_optional_point(self, x: int | None, y: int | None) -> tuple[int | None, int | None]:
        if x is None and y is None:
            return None, None
        scaled_x = self._scale_value(x, 0) if x is not None else None
        scaled_y = self._scale_value(y, 0) if y is not None else None
        return scaled_x, scaled_y

    def _unscale_value(self, value: int) -> int:
        if self._dpi_scale == 0:
            return value
        return int(round(value / self._dpi_scale))

    def take_screenshot(self, region: Tuple[int, int, int, int]) -> Path:
        x, y, width, height = region
        scaled_region = (
            self._scale_value(x, 0),
            self._scale_value(y, 0),
            self._scale_value(width, 1),
            self._scale_value(height, 1),
        )
        image = self._pyautogui.screenshot(region=scaled_region)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
            image.save(tmp.name)
            return Path(tmp.name)

    @property
    def dpi_scale(self) -> float:
        return self._dpi_scale

    def mouse_click(
        self,
        x: int,
        y: int,
        button: str,
        clicks: int,
        interval: float,
    ) -> None:
        scaled_x, scaled_y = self._scale_point(x, y)
        try:
            self._pyautogui.click(
                x=scaled_x,
                y=scaled_y,
                button=button,
                clicks=clicks,
                interval=interval,
            )
        except self._failsafe_exception_type as exc:
            raise RuntimeError(
                f"PyAutoGUI fail-safe triggered while clicking at ({scaled_x}, {scaled_y}). "
                "The pointer reached a screen corner. Adjust the coordinates to avoid "
                "screen corners, or disable pyautogui.FAILSAFE only if you understand the risks."
            ) from exc

    def move_mouse(self, x: int, y: int, duration: float) -> None:
        scaled_x, scaled_y = self._scale_point(x, y)
        try:
            self._pyautogui.moveTo(scaled_x, scaled_y, duration=duration)
        except self._failsafe_exception_type as exc:
            raise RuntimeError(
                f"PyAutoGUI fail-safe triggered while moving to ({scaled_x}, {scaled_y})."
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
        scaled_start = self._scale_point(start_x, start_y)
        scaled_end = self._scale_point(end_x, end_y)
        try:
            self._pyautogui.moveTo(*scaled_start, duration=move_duration)
            self._pyautogui.dragTo(
                *scaled_end,
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
        scaled_x, scaled_y = self._scale_optional_point(x, y)
        if orientation == "horizontal":
            self._pyautogui.hscroll(clicks, x=scaled_x, y=scaled_y)
        else:
            self._pyautogui.scroll(clicks, x=scaled_x, y=scaled_y)

    def mouse_down(self, x: int, y: int, button: str) -> None:
        scaled_x, scaled_y = self._scale_point(x, y)
        try:
            self._pyautogui.mouseDown(x=scaled_x, y=scaled_y, button=button)
        except self._failsafe_exception_type as exc:
            raise RuntimeError(
                f"PyAutoGUI fail-safe triggered while pressing the {button} button at ({scaled_x}, {scaled_y})."
            ) from exc

    def mouse_up(self, x: int, y: int, button: str) -> None:
        scaled_x, scaled_y = self._scale_point(x, y)
        try:
            self._pyautogui.mouseUp(x=scaled_x, y=scaled_y, button=button)
        except self._failsafe_exception_type as exc:
            raise RuntimeError(
                f"PyAutoGUI fail-safe triggered while releasing the {button} button at ({scaled_x}, {scaled_y})."
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

    def get_pixel_color(self, x: int, y: int) -> tuple[int, int, int]:
        scaled_x, scaled_y = self._scale_point(x, y)
        try:
            color = self._pyautogui.pixel(scaled_x, scaled_y)
        except Exception:
            screenshot = self._pyautogui.screenshot()
            color = screenshot.getpixel((scaled_x, scaled_y))
        return int(color[0]), int(color[1]), int(color[2])

    def locate_image(
        self,
        image_path: str,
        confidence: float,
        region: tuple[int, int, int, int] | None,
        grayscale: bool,
    ) -> tuple[int, int] | None:
        scaled_region: tuple[int, int, int, int] | None = None
        if region is not None:
            scaled_region = (
                self._scale_value(region[0], 0),
                self._scale_value(region[1], 0),
                self._scale_value(region[2], 1),
                self._scale_value(region[3], 1),
            )
        locate_center = getattr(self._pyautogui, "locateCenterOnScreen", None)
        if locate_center is None:
            raise RuntimeError("PyAutoGUI locateCenterOnScreen is unavailable")
        kwargs: dict[str, Any] = {"grayscale": grayscale}
        if scaled_region is not None:
            kwargs["region"] = scaled_region
        try:
            location = locate_center(
                image_path,
                confidence=confidence,
                **kwargs,
            )
        except TypeError:
            retry_kwargs = kwargs.copy()
            co_varnames = getattr(getattr(locate_center, "__code__", None), "co_varnames", ())
            if "confidence" in co_varnames:
                retry_kwargs["confidence"] = confidence
            else:
                if confidence < 1.0:
                    raise RuntimeError(
                        "PyAutoGUI locateCenterOnScreen requires OpenCV for confidence parameter"
                    )
            if "grayscale" not in co_varnames:
                retry_kwargs.pop("grayscale", None)
            try:
                location = locate_center(image_path, **retry_kwargs)
            except TypeError as exc:
                raise RuntimeError(
                    "当前 PyAutoGUI 版本不支持提供的 locateCenterOnScreen 参数"
                ) from exc
        if location is None:
            return None
        return self._unscale_value(location[0]), self._unscale_value(location[1])

    def run_command(
        self,
        command: str,
        timeout: float | None,
        cwd: str | None,
    ) -> Tuple[int, str, str]:
        try:
            completed = subprocess.run(
                command,
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"命令执行超时: {command}") from exc
        return completed.returncode, completed.stdout, completed.stderr
