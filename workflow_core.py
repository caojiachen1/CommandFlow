"""Core workflow engine for node-based desktop automation.

Provides reusable classes for building and executing automation workflows made of
node models. The GUI layer (``script.py``) constructs a ``WorkflowGraph`` using
these primitives, while unit tests exercise the pure-Python execution code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, Tuple, cast

from window_utils import activate_window, find_window_by_title, is_window_valid


class ExecutionError(RuntimeError):
    """Raised when a node fails to execute."""


class AutomationRuntime(Protocol):
    """Minimal protocol required by workflow nodes."""

    def take_screenshot(self, region: tuple[int, int, int, int]) -> Path: ...

    def mouse_click(
        self,
        x: int,
        y: int,
        button: str,
        clicks: int,
        interval: float,
    ) -> None: ...

    def move_mouse(self, x: int, y: int, duration: float) -> None: ...

    def drag_mouse(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        button: str,
        move_duration: float,
        drag_duration: float,
    ) -> None: ...

    def mouse_scroll(
        self,
        clicks: int,
        orientation: str,
        x: int | None,
        y: int | None,
    ) -> None: ...

    def mouse_down(self, x: int, y: int, button: str) -> None: ...

    def mouse_up(self, x: int, y: int, button: str) -> None: ...

    def type_text(self, text: str, interval: float) -> None: ...

    def press_key(self, key: str, presses: int, interval: float) -> None: ...

    def key_down(self, key: str) -> None: ...

    def key_up(self, key: str) -> None: ...

    def press_hotkey(self, keys: list[str], interval: float) -> None: ...

    def get_pixel_color(self, x: int, y: int) -> tuple[int, int, int]: ...

    def locate_image(
        self,
        image_path: str,
        confidence: float,
        region: tuple[int, int, int, int] | None,
        grayscale: bool,
    ) -> tuple[int, int] | None: ...

    def run_command(
        self,
        command: str,
        timeout: float | None,
        cwd: str | None,
    ) -> Tuple[int, str, str]: ...


@dataclass
class ExecutionContext:
    """Container for node execution results."""

    results: Dict[str, Any] = field(default_factory=dict)

    def record(self, node_id: str, value: Any) -> None:
        self.results[node_id] = value

    def get(self, node_id: str) -> Any:
        return self.results.get(node_id)


class WorkflowNodeModel:
    """Base class for workflow node definitions."""

    type_name: str = "base"
    display_name: str = "Base"
    category: str = "其他"

    def __init__(
        self,
        node_id: str,
        title: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.id = node_id
        self.title = title or self.display_name
        default_cfg = self.default_config()
        if config is None:
            self.config: Dict[str, Any] = default_cfg
        else:
            merged = default_cfg.copy()
            merged.update(config)
            self.config = merged
        self.validate_config()

    def default_config(self) -> Dict[str, Any]:
        return {}

    def validate_config(self) -> None:
        """Validate config values; subclasses should raise ``ValueError``."""

    def config_schema(self) -> List[Dict[str, Any]]:
        """Return declarative config schema used by the GUI editor."""

        return []

    def execute(self, context: ExecutionContext, runtime: AutomationRuntime) -> Any:
        raise NotImplementedError


class ScreenshotNode(WorkflowNodeModel):
    type_name = "screenshot"
    display_name = "截图"
    category = "图像识别"

    def default_config(self) -> Dict[str, Any]:
        return {
            "x": 0,
            "y": 0,
            "width": 400,
            "height": 300,
            "output_dir": "captures",
            "filename": "capture_{index:03d}.png",
        }

    def validate_config(self) -> None:
        cfg = self.config
        for key in ("x", "y", "width", "height"):
            value = cfg.get(key)
            if not isinstance(value, int) or value < 0:
                raise ValueError(f"{key} must be a non-negative integer")
        if cfg["width"] == 0 or cfg["height"] == 0:
            raise ValueError("width/height must be positive")
        if not isinstance(cfg.get("output_dir"), str):
            raise ValueError("output_dir must be a string")
        if not isinstance(cfg.get("filename"), str) or "{" not in cfg["filename"]:
            raise ValueError("filename must be a format string containing '{index}'")

    def config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "x", "label": "X", "type": "int", "min": 0, "max": 9999},
            {"key": "y", "label": "Y", "type": "int", "min": 0, "max": 9999},
            {
                "key": "width",
                "label": "宽度",
                "type": "int",
                "min": 1,
                "max": 10000,
            },
            {
                "key": "height",
                "label": "高度",
                "type": "int",
                "min": 1,
                "max": 10000,
            },
            {
                "key": "output_dir",
                "label": "输出目录",
                "type": "directory",
                "dialog_title": "选择输出目录",
            },
            {
                "key": "filename",
                "label": "文件名模式",
                "type": "str",
            },
        ]

    def execute(self, context: ExecutionContext, runtime: AutomationRuntime) -> Path:
        cfg = self.config
        output_dir = Path(cfg["output_dir"]).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        index = len([p for p in output_dir.iterdir() if p.is_file()]) + 1
        filename = cfg["filename"].format(index=index)
        region = (cfg["x"], cfg["y"], cfg["width"], cfg["height"])
        path = runtime.take_screenshot(region)
        target_path = output_dir / filename
        shutil.move(str(path), target_path)
        context.record(self.id, str(target_path))
        return target_path


class MouseClickNode(WorkflowNodeModel):
    type_name = "mouse_click"
    display_name = "鼠标点击"
    category = "鼠标操作"

    def default_config(self) -> Dict[str, Any]:
        return {
            # Default away from fail-safe corners to avoid accidental aborts.
            "x": 100,
            "y": 100,
            "button": "left",
            "clicks": 1,
            "interval": 0.1,
        }

    def validate_config(self) -> None:
        cfg = self.config
        if not isinstance(cfg.get("x"), int) or not isinstance(cfg.get("y"), int):
            raise ValueError("x/y must be integers")
        if cfg.get("button") not in {"left", "right", "middle"}:
            raise ValueError("button must be left/right/middle")
        clicks = cfg.get("clicks")
        if not isinstance(clicks, int) or clicks <= 0:
            raise ValueError("clicks must be a positive integer")
        interval = cfg.get("interval")
        if not isinstance(interval, (int, float)) or interval < 0:
            raise ValueError("interval must be non-negative")

    def config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "x", "label": "X", "type": "int", "min": 0, "max": 10000},
            {"key": "y", "label": "Y", "type": "int", "min": 0, "max": 10000},
            {
                "key": "button",
                "label": "按键",
                "type": "choices",
                "choices": [("left", "左键"), ("right", "右键"), ("middle", "中键")],
            },
            {
                "key": "clicks",
                "label": "点击次数",
                "type": "int",
                "min": 1,
                "max": 10,
            },
            {
                "key": "interval",
                "label": "间隔秒数",
                "type": "float",
                "min": 0.0,
                "max": 5.0,
                "step": 0.1,
            },
        ]

    def execute(self, context: ExecutionContext, runtime: AutomationRuntime) -> None:
        cfg = self.config
        runtime.mouse_click(
            cfg["x"],
            cfg["y"],
            cfg["button"],
            cfg["clicks"],
            float(cfg["interval"]),
        )
        context.record(self.id, "ok")
        return None


class KeyboardInputNode(WorkflowNodeModel):
    type_name = "keyboard_input"
    display_name = "键盘输入"
    category = "键盘操作"

    def default_config(self) -> Dict[str, Any]:
        return {
            "text": "",
            "interval": 0.05,
        }

    def validate_config(self) -> None:
        cfg = self.config
        if not isinstance(cfg.get("text"), str):
            raise ValueError("text must be a string")
        interval = cfg.get("interval")
        if not isinstance(interval, (int, float)) or interval < 0:
            raise ValueError("interval must be non-negative")

    def config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "text", "label": "输入内容", "type": "multiline"},
            {
                "key": "interval",
                "label": "字符间隔",
                "type": "float",
                "min": 0.0,
                "max": 1.0,
                "step": 0.01,
            },
        ]

    def execute(self, context: ExecutionContext, runtime: AutomationRuntime) -> None:
        runtime.type_text(self.config["text"], float(self.config["interval"]))
        context.record(self.id, "ok")
        return None


class MouseMoveNode(WorkflowNodeModel):
    type_name = "mouse_move"
    display_name = "鼠标移动"
    category = "鼠标操作"

    def default_config(self) -> Dict[str, Any]:
        return {"x": 100, "y": 100, "duration": 0.2}

    def validate_config(self) -> None:
        cfg = self.config
        for axis in ("x", "y"):
            if not isinstance(cfg.get(axis), int):
                raise ValueError(f"{axis} must be an integer")
        duration = cfg.get("duration")
        if not isinstance(duration, (int, float)) or duration < 0:
            raise ValueError("duration must be non-negative")

    def config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "x", "label": "X", "type": "int", "min": 0, "max": 10000},
            {"key": "y", "label": "Y", "type": "int", "min": 0, "max": 10000},
            {
                "key": "duration",
                "label": "移动时长",
                "type": "float",
                "min": 0.0,
                "max": 5.0,
                "step": 0.05,
            },
        ]

    def execute(self, context: ExecutionContext, runtime: AutomationRuntime) -> None:
        cfg = self.config
        runtime.move_mouse(cfg["x"], cfg["y"], float(cfg["duration"]))
        context.record(self.id, "ok")
        return None


class MouseDragNode(WorkflowNodeModel):
    type_name = "mouse_drag"
    display_name = "鼠标拖拽"
    category = "鼠标操作"

    def default_config(self) -> Dict[str, Any]:
        return {
            "start_x": 100,
            "start_y": 100,
            "end_x": 300,
            "end_y": 300,
            "button": "left",
            "move_duration": 0.2,
            "drag_duration": 0.5,
        }

    def validate_config(self) -> None:
        cfg = self.config
        for axis in ("start_x", "start_y", "end_x", "end_y"):
            if not isinstance(cfg.get(axis), int):
                raise ValueError(f"{axis} must be an integer")
        if cfg.get("button") not in {"left", "right", "middle"}:
            raise ValueError("button must be left/right/middle")
        for key in ("move_duration", "drag_duration"):
            value = cfg.get(key)
            if not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"{key} must be non-negative")

    def config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "start_x", "label": "起点X", "type": "int", "min": 0, "max": 10000},
            {"key": "start_y", "label": "起点Y", "type": "int", "min": 0, "max": 10000},
            {"key": "end_x", "label": "终点X", "type": "int", "min": 0, "max": 10000},
            {"key": "end_y", "label": "终点Y", "type": "int", "min": 0, "max": 10000},
            {
                "key": "button",
                "label": "按键",
                "type": "choices",
                "choices": [("left", "左键"), ("right", "右键"), ("middle", "中键")],
            },
            {
                "key": "move_duration",
                "label": "移动时长",
                "type": "float",
                "min": 0.0,
                "max": 5.0,
                "step": 0.05,
            },
            {
                "key": "drag_duration",
                "label": "拖拽时长",
                "type": "float",
                "min": 0.0,
                "max": 5.0,
                "step": 0.05,
            },
        ]

    def execute(self, context: ExecutionContext, runtime: AutomationRuntime) -> None:
        cfg = self.config
        runtime.drag_mouse(
            cfg["start_x"],
            cfg["start_y"],
            cfg["end_x"],
            cfg["end_y"],
            cfg["button"],
            float(cfg["move_duration"]),
            float(cfg["drag_duration"]),
        )
        context.record(self.id, "ok")
        return None


class MouseScrollNode(WorkflowNodeModel):
    type_name = "mouse_scroll"
    display_name = "鼠标滚轮"
    category = "鼠标操作"

    def default_config(self) -> Dict[str, Any]:
        return {
            "clicks": -300,
            "orientation": "vertical",
            "x": "",
            "y": "",
        }

    def _parse_optional_coordinate(self, value: Any) -> int | None:
        if value in ("", None):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip():
            try:
                return int(value.strip())
            except ValueError as exc:  # pragma: no cover - validation handles message
                raise ValueError("坐标必须为整数或留空") from exc
        raise ValueError("坐标必须为整数或留空")

    def validate_config(self) -> None:
        cfg = self.config
        clicks = cfg.get("clicks")
        if not isinstance(clicks, int) or clicks == 0:
            raise ValueError("clicks must be a non-zero integer")
        if cfg.get("orientation") not in {"vertical", "horizontal"}:
            raise ValueError("orientation must be vertical/horizontal")
        self.config["x"] = self._parse_optional_coordinate(cfg.get("x"))
        self.config["y"] = self._parse_optional_coordinate(cfg.get("y"))

    def config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "clicks",
                "label": "滚动量",
                "type": "int",
                "min": -10000,
                "max": 10000,
            },
            {
                "key": "orientation",
                "label": "方向",
                "type": "choices",
                "choices": [("vertical", "垂直"), ("horizontal", "水平")],
            },
            {"key": "x", "label": "目标X(可空)", "type": "str"},
            {"key": "y", "label": "目标Y(可空)", "type": "str"},
        ]

    def execute(self, context: ExecutionContext, runtime: AutomationRuntime) -> None:
        cfg = self.config
        x_val = cfg.get("x")
        y_val = cfg.get("y")
        runtime.mouse_scroll(
            cfg["clicks"],
            cfg["orientation"],
            x_val if isinstance(x_val, int) else None,
            y_val if isinstance(y_val, int) else None,
        )
        context.record(self.id, "ok")
        return None


class MouseDownNode(WorkflowNodeModel):
    type_name = "mouse_down"
    display_name = "鼠标按下"
    category = "鼠标操作"

    def default_config(self) -> Dict[str, Any]:
        return {"x": 100, "y": 100, "button": "left"}

    def validate_config(self) -> None:
        cfg = self.config
        for axis in ("x", "y"):
            if not isinstance(cfg.get(axis), int):
                raise ValueError(f"{axis} must be an integer")
        if cfg.get("button") not in {"left", "right", "middle"}:
            raise ValueError("button must be left/right/middle")

    def config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "x", "label": "X", "type": "int", "min": 0, "max": 10000},
            {"key": "y", "label": "Y", "type": "int", "min": 0, "max": 10000},
            {
                "key": "button",
                "label": "按键",
                "type": "choices",
                "choices": [("left", "左键"), ("right", "右键"), ("middle", "中键")],
            },
        ]

    def execute(self, context: ExecutionContext, runtime: AutomationRuntime) -> None:
        cfg = self.config
        runtime.mouse_down(cfg["x"], cfg["y"], cfg["button"])
        context.record(self.id, "ok")
        return None


class MouseUpNode(WorkflowNodeModel):
    type_name = "mouse_up"
    display_name = "鼠标抬起"
    category = "鼠标操作"

    def default_config(self) -> Dict[str, Any]:
        return {"x": 100, "y": 100, "button": "left"}

    def validate_config(self) -> None:
        cfg = self.config
        for axis in ("x", "y"):
            if not isinstance(cfg.get(axis), int):
                raise ValueError(f"{axis} must be an integer")
        if cfg.get("button") not in {"left", "right", "middle"}:
            raise ValueError("button must be left/right/middle")

    def config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "x", "label": "X", "type": "int", "min": 0, "max": 10000},
            {"key": "y", "label": "Y", "type": "int", "min": 0, "max": 10000},
            {
                "key": "button",
                "label": "按键",
                "type": "choices",
                "choices": [("left", "左键"), ("right", "右键"), ("middle", "中键")],
            },
        ]

    def execute(self, context: ExecutionContext, runtime: AutomationRuntime) -> None:
        cfg = self.config
        runtime.mouse_up(cfg["x"], cfg["y"], cfg["button"])
        context.record(self.id, "ok")
        return None


class KeyPressNode(WorkflowNodeModel):
    type_name = "key_press"
    display_name = "按键触发"
    category = "键盘操作"

    def default_config(self) -> Dict[str, Any]:
        return {"key": "enter", "presses": 1, "interval": 0.05}

    def validate_config(self) -> None:
        cfg = self.config
        if not isinstance(cfg.get("key"), str) or not cfg["key"].strip():
            raise ValueError("key must be a non-empty string")
        presses = cfg.get("presses")
        if not isinstance(presses, int) or presses <= 0:
            raise ValueError("presses must be a positive integer")
        interval = cfg.get("interval")
        if not isinstance(interval, (int, float)) or interval < 0:
            raise ValueError("interval must be non-negative")

    def config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "key", "label": "按键", "type": "str"},
            {
                "key": "presses",
                "label": "次数",
                "type": "int",
                "min": 1,
                "max": 50,
            },
            {
                "key": "interval",
                "label": "间隔",
                "type": "float",
                "min": 0.0,
                "max": 5.0,
                "step": 0.05,
            },
        ]

    def execute(self, context: ExecutionContext, runtime: AutomationRuntime) -> None:
        cfg = self.config
        runtime.press_key(cfg["key"].strip(), cfg["presses"], float(cfg["interval"]))
        context.record(self.id, "ok")
        return None


class HotkeyNode(WorkflowNodeModel):
    type_name = "hotkey"
    display_name = "组合按键"
    category = "键盘操作"

    def default_config(self) -> Dict[str, Any]:
        return {"keys": "ctrl+shift+esc", "interval": 0.05}

    def validate_config(self) -> None:
        cfg = self.config
        if not isinstance(cfg.get("keys"), str) or not cfg["keys"].strip():
            raise ValueError("keys must be a non-empty string")
        interval = cfg.get("interval")
        if not isinstance(interval, (int, float)) or interval < 0:
            raise ValueError("interval must be non-negative")

    def config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "keys", "label": "按键序列", "type": "str"},
            {
                "key": "interval",
                "label": "按键间隔",
                "type": "float",
                "min": 0.0,
                "max": 5.0,
                "step": 0.05,
            },
        ]

    def execute(self, context: ExecutionContext, runtime: AutomationRuntime) -> None:
        cfg = self.config
        keys = [part.strip() for part in cfg["keys"].split("+") if part.strip()]
        if not keys:
            raise ExecutionError("组合按键列表不能为空")
        runtime.press_hotkey(keys, float(cfg["interval"]))
        context.record(self.id, "ok")
        return None


class KeyDownNode(WorkflowNodeModel):
    type_name = "key_down"
    display_name = "按键按下"
    category = "键盘操作"

    def default_config(self) -> Dict[str, Any]:
        return {"key": "shift"}

    def validate_config(self) -> None:
        key = self.config.get("key")
        if not isinstance(key, str) or not key.strip():
            raise ValueError("key must be a non-empty string")

    def config_schema(self) -> List[Dict[str, Any]]:
        return [{"key": "key", "label": "按键", "type": "str"}]

    def execute(self, context: ExecutionContext, runtime: AutomationRuntime) -> None:
        runtime.key_down(self.config["key"].strip())
        context.record(self.id, "ok")
        return None


class KeyUpNode(WorkflowNodeModel):
    type_name = "key_up"
    display_name = "按键抬起"
    category = "键盘操作"

    def default_config(self) -> Dict[str, Any]:
        return {"key": "shift"}

    def validate_config(self) -> None:
        key = self.config.get("key")
        if not isinstance(key, str) or not key.strip():
            raise ValueError("key must be a non-empty string")

    def config_schema(self) -> List[Dict[str, Any]]:
        return [{"key": "key", "label": "按键", "type": "str"}]

    def execute(self, context: ExecutionContext, runtime: AutomationRuntime) -> None:
        runtime.key_up(self.config["key"].strip())
        context.record(self.id, "ok")
        return None


class DelayNode(WorkflowNodeModel):
    type_name = "delay"
    display_name = "延迟等待"
    category = "流程控制"

    def default_config(self) -> Dict[str, Any]:
        return {"seconds": 1.0}

    def validate_config(self) -> None:
        seconds = self.config.get("seconds")
        if not isinstance(seconds, (int, float)) or seconds < 0:
            raise ValueError("seconds must be non-negative")

    def config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "seconds",
                "label": "等待秒数",
                "type": "float",
                "min": 0.0,
                "max": 3600.0,
                "step": 0.1,
            }
        ]

    def execute(self, context: ExecutionContext, runtime: AutomationRuntime) -> None:  # noqa: ARG002
        time.sleep(float(self.config["seconds"]))
        context.record(self.id, "ok")
        return None


class ImageLocateNode(WorkflowNodeModel):
    type_name = "image_locate"
    display_name = "图像定位"
    category = "图像识别"

    def default_config(self) -> Dict[str, Any]:
        return {
            "image_path": "",
            "confidence": 0.9,
            "grayscale": "no",
            "region_x": "",
            "region_y": "",
            "region_width": "",
            "region_height": "",
        }

    @staticmethod
    def _parse_optional_int(value: Any, field_name: str) -> int | None:
        if value in ("", None):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip():
            try:
                return int(value.strip())
            except ValueError as exc:
                raise ValueError(f"{field_name} 必须为整数或留空") from exc
        raise ValueError(f"{field_name} 必须为整数或留空")

    def validate_config(self) -> None:
        cfg = self.config
        image_path = cfg.get("image_path")
        if not isinstance(image_path, str):
            raise ValueError("image_path must be a string")
        cfg["image_path"] = image_path.strip()
        confidence = cfg.get("confidence")
        if not isinstance(confidence, (int, float)):
            raise ValueError("confidence must be a number")
        confidence_val = float(confidence)
        if not 0.0 < confidence_val <= 1.0:
            raise ValueError("confidence must be in (0, 1]")
        cfg["confidence"] = confidence_val
        grayscale = cfg.get("grayscale", "no")
        if grayscale not in {"yes", "no"}:
            raise ValueError("grayscale must be 'yes' or 'no'")
        cfg["region_x"] = self._parse_optional_int(cfg.get("region_x"), "region_x")
        cfg["region_y"] = self._parse_optional_int(cfg.get("region_y"), "region_y")
        cfg["region_width"] = self._parse_optional_int(cfg.get("region_width"), "region_width")
        cfg["region_height"] = self._parse_optional_int(cfg.get("region_height"), "region_height")
        width = cfg["region_width"]
        height = cfg["region_height"]
        if width is not None and width <= 0:
            raise ValueError("region_width must be positive when provided")
        if height is not None and height <= 0:
            raise ValueError("region_height must be positive when provided")
        has_partial_region = any(
            value is not None
            for value in (
                cfg["region_x"],
                cfg["region_y"],
                cfg["region_width"],
                cfg["region_height"],
            )
        )
        if has_partial_region and not all(
            value is not None
            for value in (
                cfg["region_x"],
                cfg["region_y"],
                cfg["region_width"],
                cfg["region_height"],
            )
        ):
            raise ValueError("region fields must be all provided or all empty")

    def config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "image_path",
                "label": "图像路径",
                "type": "file_open",
                "dialog_title": "选择图像文件",
                "name_filter": "Image Files (*.png *.jpg *.jpeg *.bmp *.gif);;All Files (*.*)",
            },
            {
                "key": "confidence",
                "label": "匹配度",
                "type": "float",
                "min": 0.1,
                "max": 1.0,
                "step": 0.01,
            },
            {
                "key": "grayscale",
                "label": "灰度匹配",
                "type": "choices",
                "choices": [("no", "否"), ("yes", "是")],
            },
            {"key": "region_x", "label": "区域X(可空)", "type": "str"},
            {"key": "region_y", "label": "区域Y(可空)", "type": "str"},
            {"key": "region_width", "label": "区域宽度(可空)", "type": "str"},
            {"key": "region_height", "label": "区域高度(可空)", "type": "str"},
        ]

    def _build_region(self) -> tuple[int, int, int, int] | None:
        cfg = self.config
        if cfg["region_x"] is None:
            return None
        return (
            int(cfg["region_x"]),
            int(cfg["region_y"]),
            int(cfg["region_width"]),
            int(cfg["region_height"]),
        )

    def execute(self, context: ExecutionContext, runtime: AutomationRuntime) -> Dict[str, int]:
        cfg = self.config
        region = self._build_region()
        image_path = cfg["image_path"]
        if not image_path:
            raise ExecutionError("图像路径不能为空")
        location = runtime.locate_image(
            image_path,
            float(cfg["confidence"]),
            region,
            cfg["grayscale"] == "yes",
        )
        if location is None:
            raise ExecutionError("未能在屏幕上找到目标图像")
        result = {"x": location[0], "y": location[1]}
        context.record(self.id, result)
        return result


class WaitForImageNode(ImageLocateNode):
    type_name = "wait_for_image"
    display_name = "等待图像出现"
    category = "图像识别"

    def default_config(self) -> Dict[str, Any]:
        base = super().default_config()
        base.update(
            {
                "expect_r": 0,
                "expect_g": 0,
                "expect_b": 0,
                "timeout": 10.0,
                "poll_interval": 0.5,
            }
        )
        return base

    def validate_config(self) -> None:
        super().validate_config()
        timeout = self.config.get("timeout")
        poll = self.config.get("poll_interval")
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("timeout must be positive")
        if not isinstance(poll, (int, float)) or poll <= 0:
            raise ValueError("poll_interval must be positive")
        if float(poll) > float(timeout):
            raise ValueError("poll_interval must not exceed timeout")
        self.config["timeout"] = float(timeout)
        self.config["poll_interval"] = float(poll)

    def config_schema(self) -> List[Dict[str, Any]]:
        base = super().config_schema()
        base.extend(
            [
                {
                    "key": "timeout",
                    "label": "超时时间(秒)",
                    "type": "float",
                    "min": 0.1,
                    "max": 3600.0,
                    "step": 0.1,
                },
                {
                    "key": "poll_interval",
                    "label": "轮询间隔(秒)",
                    "type": "float",
                    "min": 0.1,
                    "max": 60.0,
                    "step": 0.1,
                },
            ]
        )
        return base

    def execute(self, context: ExecutionContext, runtime: AutomationRuntime) -> Dict[str, int]:
        cfg = self.config
        deadline = time.monotonic() + float(cfg["timeout"])
        image_path = cfg["image_path"]
        if not image_path:
            raise ExecutionError("图像路径不能为空")
        while True:
            region = self._build_region()
            location = runtime.locate_image(
                image_path,
                float(cfg["confidence"]),
                region,
                cfg["grayscale"] == "yes",
            )
            if location is not None:
                result = {"x": location[0], "y": location[1]}
                context.record(self.id, result)
                return result
            if time.monotonic() >= deadline:
                raise ExecutionError("等待图像超时")
            time.sleep(float(cfg["poll_interval"]))


class ClickImageNode(ImageLocateNode):
    type_name = "click_image"
    display_name = "图像点击"
    category = "图像识别"

    def default_config(self) -> Dict[str, Any]:
        base = super().default_config()
        base.update(
            {
                "offset_x": 0,
                "offset_y": 0,
                "click_button": "left",
                "clicks": 1,
                "interval": 0.1,
            }
        )
        return base

    def validate_config(self) -> None:
        super().validate_config()
        cfg = self.config
        for key in ("offset_x", "offset_y"):
            value = cfg.get(key)
            if not isinstance(value, int):
                raise ValueError(f"{key} must be an integer")
        if cfg.get("click_button") not in {"left", "right", "middle"}:
            raise ValueError("click_button must be left/right/middle")
        clicks = cfg.get("clicks")
        if not isinstance(clicks, int) or clicks <= 0:
            raise ValueError("clicks must be a positive integer")
        interval = cfg.get("interval")
        if not isinstance(interval, (int, float)) or interval < 0:
            raise ValueError("interval must be non-negative")
        cfg["interval"] = float(interval)

    def config_schema(self) -> List[Dict[str, Any]]:
        base = list(super().config_schema())
        base.extend(
            [
                {
                    "key": "offset_x",
                    "label": "点击偏移X",
                    "type": "int",
                    "min": -10000,
                    "max": 10000,
                },
                {
                    "key": "offset_y",
                    "label": "点击偏移Y",
                    "type": "int",
                    "min": -10000,
                    "max": 10000,
                },
                {
                    "key": "click_button",
                    "label": "按键",
                    "type": "choices",
                    "choices": [("left", "左键"), ("right", "右键"), ("middle", "中键")],
                },
                {
                    "key": "clicks",
                    "label": "点击次数",
                    "type": "int",
                    "min": 1,
                    "max": 10,
                },
                {
                    "key": "interval",
                    "label": "点击间隔",
                    "type": "float",
                    "min": 0.0,
                    "max": 5.0,
                    "step": 0.05,
                },
            ]
        )
        return base

    def execute(self, context: ExecutionContext, runtime: AutomationRuntime) -> Dict[str, int]:
        location = super().execute(context, runtime)
        cfg = self.config
        click_x = int(location["x"]) + int(cfg["offset_x"])
        click_y = int(location["y"]) + int(cfg["offset_y"])
        runtime.mouse_click(
            click_x,
            click_y,
            cfg["click_button"],
            int(cfg["clicks"]),
            float(cfg["interval"]),
        )
        result = {"x": click_x, "y": click_y}
        context.record(self.id, result)
        return result


class PixelColorNode(WorkflowNodeModel):
    type_name = "pixel_color"
    display_name = "读取像素颜色"
    category = "图像识别"

    def default_config(self) -> Dict[str, Any]:
        return {
            "x": 0,
            "y": 0,
            "expect_r": "",
            "expect_g": "",
            "expect_b": "",
            "tolerance": 10,
        }

    def _parse_optional_color(self, value: Any, channel: str) -> int | None:
        if value in ("", None):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip():
            try:
                return int(value.strip())
            except ValueError as exc:
                raise ValueError(f"{channel} 必须为 0-255 的整数或留空") from exc
        raise ValueError(f"{channel} 必须为 0-255 的整数或留空")

    def validate_config(self) -> None:
        cfg = self.config
        for axis in ("x", "y"):
            if not isinstance(cfg.get(axis), int):
                raise ValueError(f"{axis} must be an integer")
        tolerance = cfg.get("tolerance")
        if not isinstance(tolerance, int) or tolerance < 0:
            raise ValueError("tolerance must be a non-negative integer")
        for channel in ("expect_r", "expect_g", "expect_b"):
            value = self._parse_optional_color(cfg.get(channel), channel)
            if value is not None and not 0 <= value <= 255:
                raise ValueError(f"{channel} must be between 0 and 255")
            cfg[channel] = value

    def config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "x", "label": "X", "type": "int", "min": 0, "max": 10000},
            {"key": "y", "label": "Y", "type": "int", "min": 0, "max": 10000},
            {"key": "expect_r", "label": "期望R(可空)", "type": "str"},
            {"key": "expect_g", "label": "期望G(可空)", "type": "str"},
            {"key": "expect_b", "label": "期望B(可空)", "type": "str"},
            {
                "key": "tolerance",
                "label": "容差",
                "type": "int",
                "min": 0,
                "max": 255,
            },
        ]

    def execute(self, context: ExecutionContext, runtime: AutomationRuntime) -> Dict[str, int]:
        cfg = self.config
        r, g, b = runtime.get_pixel_color(cfg["x"], cfg["y"])
        result = {"r": r, "g": g, "b": b}
        expect_r = cfg.get("expect_r")
        expect_g = cfg.get("expect_g")
        expect_b = cfg.get("expect_b")
        tolerance = int(cfg.get("tolerance", 0))
        if all(channel is not None for channel in (expect_r, expect_g, expect_b)):
            exp_r = int(cast(int, expect_r))
            exp_g = int(cast(int, expect_g))
            exp_b = int(cast(int, expect_b))
            if not (
                abs(r - exp_r) <= tolerance
                and abs(g - exp_g) <= tolerance
                and abs(b - exp_b) <= tolerance
            ):
                raise ExecutionError("像素颜色与期望值不匹配")
        context.record(self.id, result)
        return result


class WaitForPixelColorNode(PixelColorNode):
    type_name = "wait_for_pixel"
    display_name = "等待像素颜色"
    category = "图像识别"

    def default_config(self) -> Dict[str, Any]:
        base = super().default_config()
        base.update(
            {
                "expect_r": 0,
                "expect_g": 0,
                "expect_b": 0,
                "timeout": 10.0,
                "poll_interval": 0.5,
            }
        )
        return base

    def validate_config(self) -> None:
        super().validate_config()
        cfg = self.config
        if any(cfg.get(channel) is None for channel in ("expect_r", "expect_g", "expect_b")):
            raise ValueError("必须设置完整的期望RGB数值")
        timeout = cfg.get("timeout")
        poll = cfg.get("poll_interval")
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("timeout must be positive")
        if not isinstance(poll, (int, float)) or poll <= 0:
            raise ValueError("poll_interval must be positive")
        if float(poll) > float(timeout):
            raise ValueError("poll_interval must not exceed timeout")
        cfg["timeout"] = float(timeout)
        cfg["poll_interval"] = float(poll)

    def config_schema(self) -> List[Dict[str, Any]]:
        base = list(super().config_schema())
        base.extend(
            [
                {
                    "key": "timeout",
                    "label": "超时时间(秒)",
                    "type": "float",
                    "min": 0.1,
                    "max": 3600.0,
                    "step": 0.1,
                },
                {
                    "key": "poll_interval",
                    "label": "轮询间隔(秒)",
                    "type": "float",
                    "min": 0.05,
                    "max": 60.0,
                    "step": 0.05,
                },
            ]
        )
        return base

    def execute(self, context: ExecutionContext, runtime: AutomationRuntime) -> Dict[str, int]:
        cfg = self.config
        target = (
            int(cast(int, cfg["expect_r"])),
            int(cast(int, cfg["expect_g"])),
            int(cast(int, cfg["expect_b"])),
        )
        tolerance = int(cfg.get("tolerance", 0))
        deadline = time.monotonic() + float(cfg["timeout"])
        poll = float(cfg["poll_interval"])
        while True:
            r, g, b = runtime.get_pixel_color(cfg["x"], cfg["y"])
            if (
                abs(r - target[0]) <= tolerance
                and abs(g - target[1]) <= tolerance
                and abs(b - target[2]) <= tolerance
            ):
                result = {"r": r, "g": g, "b": b}
                context.record(self.id, result)
                return result
            if time.monotonic() >= deadline:
                raise ExecutionError("等待像素颜色超时")
            time.sleep(poll)


class MoveMouseToResultNode(WorkflowNodeModel):
    type_name = "move_to_result"
    display_name = "移动到结果坐标"
    category = "鼠标操作"

    def default_config(self) -> Dict[str, Any]:
        return {"source_node": "", "duration": 0.2}

    def validate_config(self) -> None:
        cfg = self.config
        source = cfg.get("source_node")
        if not isinstance(source, str):
            raise ValueError("source_node must be a string")
        cfg["source_node"] = source.strip()
        duration = cfg.get("duration")
        if not isinstance(duration, (int, float)) or duration < 0:
            raise ValueError("duration must be non-negative")
        cfg["duration"] = float(duration)

    def config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "source_node", "label": "来源节点ID", "type": "str"},
            {
                "key": "duration",
                "label": "移动时长",
                "type": "float",
                "min": 0.0,
                "max": 5.0,
                "step": 0.05,
            },
        ]

    def execute(self, context: ExecutionContext, runtime: AutomationRuntime) -> None:
        source_id = self.config["source_node"]
        if not source_id:
            raise ExecutionError("来源节点ID 未设置")
        result = context.get(source_id)
        if not isinstance(result, dict) or "x" not in result or "y" not in result:
            raise ExecutionError("来源节点的结果不包含坐标信息")
        runtime.move_mouse(int(result["x"]), int(result["y"]), float(self.config["duration"]))
        context.record(self.id, {"x": int(result["x"]), "y": int(result["y"])})
        return None


class FileCopyNode(WorkflowNodeModel):
    type_name = "file_copy"
    display_name = "复制文件/目录"
    category = "系统操作"

    def default_config(self) -> Dict[str, Any]:
        return {
            "source_path": "",
            "destination_path": "",
            "overwrite": "覆盖",
            "make_parents": "是",
        }

    def validate_config(self) -> None:
        cfg = self.config
        source = cfg.get("source_path")
        destination = cfg.get("destination_path")
        if not isinstance(source, str):
            raise ValueError("source_path 必须是字符串")
        if not isinstance(destination, str):
            raise ValueError("destination_path 必须是字符串")
        overwrite = cfg.get("overwrite", "覆盖")
        if overwrite not in {"覆盖", "跳过"}:
            raise ValueError("overwrite 必须为 覆盖 或 跳过")
        make_parents = cfg.get("make_parents", "是")
        if make_parents not in {"是", "否"}:
            raise ValueError("make_parents 必须为 是 或 否")
        cfg["source_path"] = source.strip()
        cfg["destination_path"] = destination.strip()
        cfg["overwrite"] = overwrite
        cfg["make_parents"] = make_parents

    def config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "source_path",
                "label": "源路径",
                "type": "path",
                "dialog_mode": "any",
                "dialog_title": "选择源路径",
            },
            {
                "key": "destination_path",
                "label": "目标路径",
                "type": "path",
                "dialog_mode": "any",
                "dialog_title": "选择目标路径",
            },
            {
                "key": "overwrite",
                "label": "存在时",
                "type": "choices",
                "choices": [("覆盖", "覆盖"), ("跳过", "跳过")],
            },
            {
                "key": "make_parents",
                "label": "创建父目录",
                "type": "choices",
                "choices": [("是", "是"), ("否", "否")],
            },
        ]

    def execute(self, context: ExecutionContext, runtime: AutomationRuntime) -> str:  # noqa: ARG002
        cfg = self.config
        source_value = cfg["source_path"]
        dest_value = cfg["destination_path"]
        if not source_value:
            raise ExecutionError("源路径未设置")
        if not dest_value:
            raise ExecutionError("目标路径未设置")
        source = Path(source_value).expanduser()
        if not source.exists():
            raise ExecutionError("源路径不存在")
        destination = Path(dest_value).expanduser()
        overwrite = cfg.get("overwrite", "覆盖") == "覆盖"
        make_parents = cfg.get("make_parents", "是") == "是"

        final_target = self._determine_target_path(source, destination)
        if final_target.exists():
            if source.is_dir() and final_target.is_file():
                raise ExecutionError("目标路径是文件，无法覆盖目录")
            if not overwrite:
                context.record(self.id, str(final_target.resolve()))
                return str(final_target.resolve())
            if final_target.resolve() == source.resolve():
                context.record(self.id, str(final_target.resolve()))
                return str(final_target.resolve())
            if final_target.is_dir():
                shutil.rmtree(final_target)
            else:
                final_target.unlink()

        parent = final_target.parent
        if not parent.exists():
            if make_parents:
                parent.mkdir(parents=True, exist_ok=True)
            else:
                raise ExecutionError("目标父目录不存在")

        if source.is_dir():
            shutil.copytree(source, final_target)
        else:
            shutil.copy2(source, final_target)

        result = str(final_target.resolve()) if final_target.exists() else str(final_target)
        context.record(self.id, result)
        return result

    @staticmethod
    def _determine_target_path(source: Path, destination: Path) -> Path:
        if destination.exists() and destination.is_dir():
            return destination / source.name
        return destination


class FileMoveNode(WorkflowNodeModel):
    type_name = "file_move"
    display_name = "移动文件/目录"
    category = "系统操作"

    def default_config(self) -> Dict[str, Any]:
        return {
            "source_path": "",
            "destination_path": "",
            "overwrite": "覆盖",
            "make_parents": "是",
        }

    def validate_config(self) -> None:
        cfg = self.config
        source = cfg.get("source_path")
        destination = cfg.get("destination_path")
        if not isinstance(source, str):
            raise ValueError("source_path 必须是字符串")
        if not isinstance(destination, str):
            raise ValueError("destination_path 必须是字符串")
        overwrite = cfg.get("overwrite", "覆盖")
        if overwrite not in {"覆盖", "跳过"}:
            raise ValueError("overwrite 必须为 覆盖 或 跳过")
        make_parents = cfg.get("make_parents", "是")
        if make_parents not in {"是", "否"}:
            raise ValueError("make_parents 必须为 是 或 否")
        cfg["source_path"] = source.strip()
        cfg["destination_path"] = destination.strip()
        cfg["overwrite"] = overwrite
        cfg["make_parents"] = make_parents

    def config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "source_path",
                "label": "源路径",
                "type": "path",
                "dialog_mode": "any",
                "dialog_title": "选择源路径",
            },
            {
                "key": "destination_path",
                "label": "目标路径",
                "type": "path",
                "dialog_mode": "any",
                "dialog_title": "选择目标路径",
            },
            {
                "key": "overwrite",
                "label": "存在时",
                "type": "choices",
                "choices": [("覆盖", "覆盖"), ("跳过", "跳过")],
            },
            {
                "key": "make_parents",
                "label": "创建父目录",
                "type": "choices",
                "choices": [("是", "是"), ("否", "否")],
            },
        ]

    def execute(self, context: ExecutionContext, runtime: AutomationRuntime) -> str:  # noqa: ARG002
        cfg = self.config
        source_value = cfg["source_path"]
        dest_value = cfg["destination_path"]
        if not source_value:
            raise ExecutionError("源路径未设置")
        if not dest_value:
            raise ExecutionError("目标路径未设置")
        source = Path(source_value).expanduser()
        if not source.exists():
            raise ExecutionError("源路径不存在")
        destination = Path(dest_value).expanduser()
        overwrite = cfg.get("overwrite", "覆盖") == "覆盖"
        make_parents = cfg.get("make_parents", "是") == "是"

        final_target = self._determine_target_path(source, destination)

        if final_target.exists():
            if final_target.resolve() == source.resolve():
                context.record(self.id, str(final_target.resolve()))
                return str(final_target.resolve())
            if not overwrite:
                context.record(self.id, str(final_target.resolve()))
                return str(final_target.resolve())
            if final_target.is_dir():
                shutil.rmtree(final_target)
            else:
                final_target.unlink()

        parent = final_target.parent
        if not parent.exists():
            if make_parents:
                parent.mkdir(parents=True, exist_ok=True)
            else:
                raise ExecutionError("目标父目录不存在")

        shutil.move(str(source), str(final_target))

        result = str(final_target.resolve()) if final_target.exists() else str(final_target)
        context.record(self.id, result)
        return result

    @staticmethod
    def _determine_target_path(source: Path, destination: Path) -> Path:
        if destination.exists() and destination.is_dir():
            return destination / source.name
        return destination


class SwitchContextNode(WorkflowNodeModel):
    type_name = "switch_context"
    display_name = "切换窗口"
    category = "系统操作"

    WINDOW_MODE = "window_activate"

    _MODE_CHOICES: Dict[str, Tuple[List[str], str]] = {
        "program_next": (["alt", "tab"], "切换到下一个程序"),
        "program_prev": (["alt", "shift", "tab"], "切换到上一个程序"),
        "desktop_next": (["win", "ctrl", "right"], "切换到下一个桌面"),
        "desktop_prev": (["win", "ctrl", "left"], "切换到上一个桌面"),
        "task_view": (["win", "tab"], "打开任务视图"),
        "show_desktop": (["win", "d"], "显示桌面"),
    }

    def default_config(self) -> Dict[str, Any]:
        return {
            "mode": "program_next",
            "repeat": 1,
            "interval": 0.12,
            "pause_between": 0.15,
            "target_window": {"title": "", "hwnd": 0},
        }

    def validate_config(self) -> None:
        cfg = self.config
        mode = cfg.get("mode")
        if mode not in self._MODE_CHOICES and mode != self.WINDOW_MODE:
            raise ValueError("无效的切换模式")
        repeat = cfg.get("repeat")
        if not isinstance(repeat, int) or repeat <= 0:
            raise ValueError("次数必须为正整数")
        for key in ("interval", "pause_between"):
            value = cfg.get(key)
            if not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"{key} 必须为非负数")
        target_window = cfg.get("target_window", {"title": "", "hwnd": 0})
        if isinstance(target_window, str):
            target_window = {"title": target_window.strip(), "hwnd": 0}
        elif isinstance(target_window, dict):
            title_value = str(target_window.get("title", ""))
            hwnd_value = target_window.get("hwnd", 0)
            try:
                hwnd_value = int(hwnd_value) if hwnd_value is not None else 0
            except (TypeError, ValueError):
                hwnd_value = 0
            target_window = {"title": title_value.strip(), "hwnd": hwnd_value}
        else:
            raise ValueError("target_window 必须为字符串或字典")
        if mode == self.WINDOW_MODE and not target_window["title"]:
            raise ValueError("请选择要切换的窗口")
        cfg["target_window"] = target_window

    def config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "mode",
                "label": "切换方式",
                "type": "choices",
                "choices": [
                    ("program_next", "下一个程序 (Alt+Tab)"),
                    ("program_prev", "上一个程序 (Alt+Shift+Tab)"),
                    ("desktop_next", "下一个桌面 (Win+Ctrl+→)"),
                    ("desktop_prev", "上一个桌面 (Win+Ctrl+←)"),
                    ("task_view", "任务视图 (Win+Tab)"),
                    ("show_desktop", "显示桌面 (Win+D)"),
                    (self.WINDOW_MODE, "指定窗口"),
                ],
            },
            {
                "key": "target_window",
                "label": "目标窗口",
                "type": "window",
                "placeholder": "选择或输入窗口标题",
            },
            {
                "key": "repeat",
                "label": "重复次数",
                "type": "int",
                "min": 1,
                "max": 10,
            },
            {
                "key": "interval",
                "label": "按键间隔 (秒)",
                "type": "float",
                "min": 0.0,
                "max": 1.0,
                "step": 0.01,
            },
            {
                "key": "pause_between",
                "label": "重复间隔 (秒)",
                "type": "float",
                "min": 0.0,
                "max": 2.0,
                "step": 0.05,
            },
        ]

    def execute(self, context: ExecutionContext, runtime: AutomationRuntime) -> None:
        cfg = self.config
        mode = cfg["mode"]
        if mode == self.WINDOW_MODE:
            target = cfg.get("target_window", {"title": "", "hwnd": 0}) or {}
            if isinstance(target, dict):
                title = str(target.get("title", "")).strip()
                hwnd_value = target.get("hwnd", 0)
            else:
                title = str(target).strip()
                hwnd_value = 0
            if not title:
                raise ExecutionError("目标窗口未设置")
            try:
                hwnd_int = int(hwnd_value) if hwnd_value else 0
            except (TypeError, ValueError):
                hwnd_int = 0
            chosen_hwnd = hwnd_int if hwnd_int and is_window_valid(hwnd_int) else 0
            if chosen_hwnd == 0:
                found_hwnd = find_window_by_title(title)
                if found_hwnd is None:
                    raise ExecutionError(f"未找到窗口: {title}")
                chosen_hwnd = found_hwnd
            if not activate_window(chosen_hwnd):
                raise ExecutionError("窗口切换失败")
            context.record(self.id, {
                "mode": mode,
                "title": title,
                "hwnd": chosen_hwnd,
            })
            return

        keys, description = self._MODE_CHOICES[mode]
        repeat = int(cfg.get("repeat", 1))
        press_interval = float(cfg.get("interval", 0.12))
        pause = float(cfg.get("pause_between", 0.15))
        for index in range(repeat):
            runtime.press_hotkey(list(keys), interval=press_interval)
            if index < repeat - 1 and pause > 0:
                time.sleep(pause)
        context.record(self.id, {
            "mode": cfg["mode"],
            "description": description,
            "repeat": repeat,
        })


class FileDeleteNode(WorkflowNodeModel):
    type_name = "file_delete"
    display_name = "删除文件/目录"
    category = "系统操作"

    def default_config(self) -> Dict[str, Any]:
        return {
            "target_path": "",
            "missing": "忽略",
        }

    def validate_config(self) -> None:
        cfg = self.config
        target = cfg.get("target_path")
        if not isinstance(target, str):
            raise ValueError("target_path 必须是字符串")
        missing = cfg.get("missing", "忽略")
        if missing not in {"忽略", "报错"}:
            raise ValueError("missing 必须为 忽略 或 报错")
        cfg["target_path"] = target.strip()
        cfg["missing"] = missing

    def config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "target_path",
                "label": "目标路径",
                "type": "path",
                "dialog_mode": "any",
                "dialog_title": "选择目标路径",
            },
            {
                "key": "missing",
                "label": "缺失时",
                "type": "choices",
                "choices": [("忽略", "忽略"), ("报错", "报错")],
            },
        ]

    def execute(self, context: ExecutionContext, runtime: AutomationRuntime) -> str:  # noqa: ARG002
        cfg = self.config
        target_value = cfg.get("target_path", "")
        if not target_value:
            raise ExecutionError("目标路径未设置")
        target = Path(target_value).expanduser()
        if not target.exists():
            if cfg.get("missing", "忽略") == "报错":
                raise ExecutionError("目标不存在")
            context.record(self.id, "not-found")
            return "not-found"
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        except OSError as exc:
            raise ExecutionError(f"删除失败: {exc}") from exc
        context.record(self.id, "deleted")
        return "deleted"


class CommandNode(WorkflowNodeModel):
    type_name = "command"
    display_name = "执行命令"
    category = "系统操作"

    def default_config(self) -> Dict[str, Any]:
        return {
            "command": "",
            "working_dir": "",
            "timeout": 60.0,
            "on_error": "报错",
        }

    def validate_config(self) -> None:
        cfg = self.config
        command = cfg.get("command")
        if not isinstance(command, str):
            raise ValueError("command 必须是字符串")
        working_dir = cfg.get("working_dir", "")
        if not isinstance(working_dir, str):
            raise ValueError("working_dir 必须是字符串")
        timeout = cfg.get("timeout", 60.0)
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError("timeout 必须为数字") from exc
        if timeout_value <= 0:
            raise ValueError("timeout 必须大于 0")
        on_error = cfg.get("on_error", "报错")
        if on_error not in {"报错", "忽略"}:
            raise ValueError("on_error 必须为 报错 或 忽略")
        cfg["command"] = command.strip()
        cfg["working_dir"] = working_dir.strip()
        cfg["timeout"] = timeout_value
        cfg["on_error"] = on_error

    def config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "command", "label": "命令", "type": "str"},
            {
                "key": "working_dir",
                "label": "工作目录(可空)",
                "type": "directory",
                "dialog_title": "选择工作目录",
            },
            {
                "key": "timeout",
                "label": "超时(秒)",
                "type": "float",
                "min": 0.1,
                "max": 600.0,
                "step": 1.0,
            },
            {
                "key": "on_error",
                "label": "错误处理",
                "type": "choices",
                "choices": [("报错", "报错"), ("忽略", "忽略")],
            },
        ]

    def execute(self, context: ExecutionContext, runtime: AutomationRuntime) -> Dict[str, Any]:
        cfg = self.config
        command = cfg.get("command", "")
        if not command:
            raise ExecutionError("命令未设置")
        working_dir = cfg.get("working_dir") or None
        timeout_value = float(cfg.get("timeout", 60.0))
        try:
            returncode, stdout, stderr = runtime.run_command(command, timeout_value, working_dir)
        except Exception as exc:  # pragma: no cover - runtime error handling
            raise ExecutionError(f"命令执行异常: {exc}") from exc
        result = {
            "returncode": int(returncode),
            "stdout": stdout,
            "stderr": stderr,
        }
        if returncode != 0 and cfg.get("on_error") == "报错":
            raise ExecutionError(f"命令执行失败 (code={returncode})")
        context.record(self.id, result)
        return result


NODE_REGISTRY = {
    ScreenshotNode.type_name: ScreenshotNode,
    MouseClickNode.type_name: MouseClickNode,
    MouseMoveNode.type_name: MouseMoveNode,
    MouseDragNode.type_name: MouseDragNode,
    MouseScrollNode.type_name: MouseScrollNode,
    MouseDownNode.type_name: MouseDownNode,
    MouseUpNode.type_name: MouseUpNode,
    KeyboardInputNode.type_name: KeyboardInputNode,
    KeyPressNode.type_name: KeyPressNode,
    HotkeyNode.type_name: HotkeyNode,
    KeyDownNode.type_name: KeyDownNode,
    KeyUpNode.type_name: KeyUpNode,
    DelayNode.type_name: DelayNode,
    ImageLocateNode.type_name: ImageLocateNode,
    WaitForImageNode.type_name: WaitForImageNode,
    ClickImageNode.type_name: ClickImageNode,
    PixelColorNode.type_name: PixelColorNode,
    WaitForPixelColorNode.type_name: WaitForPixelColorNode,
    MoveMouseToResultNode.type_name: MoveMouseToResultNode,
    FileCopyNode.type_name: FileCopyNode,
    FileMoveNode.type_name: FileMoveNode,
    FileDeleteNode.type_name: FileDeleteNode,
    CommandNode.type_name: CommandNode,
    SwitchContextNode.type_name: SwitchContextNode,
}


class WorkflowGraph:
    """In-memory representation of a node graph."""

    def __init__(self) -> None:
        self.nodes: Dict[str, WorkflowNodeModel] = {}
        self.edges: Dict[str, List[str]] = {}
        self.reverse_edges: Dict[str, List[str]] = {}

    def add_node(self, node: WorkflowNodeModel) -> None:
        if node.id in self.nodes:
            raise ValueError(f"Node {node.id} already exists")
        self.nodes[node.id] = node
        self.edges[node.id] = []
        self.reverse_edges[node.id] = []

    def remove_node(self, node_id: str) -> None:
        if node_id not in self.nodes:
            return
        for upstream in list(self.reverse_edges[node_id]):
            self.remove_edge(upstream, node_id)
        for downstream in list(self.edges[node_id]):
            self.remove_edge(node_id, downstream)
        del self.nodes[node_id]
        del self.edges[node_id]
        del self.reverse_edges[node_id]

    def add_edge(self, source_id: str, target_id: str) -> None:
        if source_id == target_id:
            raise ValueError("Cannot connect node to itself")
        if source_id not in self.nodes or target_id not in self.nodes:
            raise ValueError("Both nodes must exist")
        if target_id in self.edges[source_id]:
            return
        self.edges[source_id].append(target_id)
        self.reverse_edges[target_id].append(source_id)
        if self._has_cycle():
            self.edges[source_id].remove(target_id)
            self.reverse_edges[target_id].remove(source_id)
            raise ValueError("Adding this connection creates a cycle")

    def remove_edge(self, source_id: str, target_id: str) -> None:
        if source_id in self.edges and target_id in self.edges[source_id]:
            self.edges[source_id].remove(target_id)
        if target_id in self.reverse_edges and source_id in self.reverse_edges[target_id]:
            self.reverse_edges[target_id].remove(source_id)

    def _has_cycle(self) -> bool:
        try:
            self.topological_order()
        except ExecutionError:
            return True
        return False

    def topological_order(self) -> List[str]:
        indegree = {node_id: len(deps) for node_id, deps in self.reverse_edges.items()}
        queue = [node for node, degree in indegree.items() if degree == 0]
        order: List[str] = []
        tmp_indegree = indegree.copy()
        while queue:
            current = queue.pop(0)
            order.append(current)
            for neighbor in self.edges[current]:
                tmp_indegree[neighbor] -= 1
                if tmp_indegree[neighbor] == 0:
                    queue.append(neighbor)
        if len(order) != len(self.nodes):
            raise ExecutionError("Workflow contains cycles")
        return order

    def copy(self) -> "WorkflowGraph":
        graph = WorkflowGraph()
        for node in self.nodes.values():
            node_cls = type(node)
            graph.add_node(node_cls(node.id, node.title, node.config.copy()))
        for source, targets in self.edges.items():
            for target in targets:
                graph.edges[source].append(target)
                graph.reverse_edges[target].append(source)
        return graph


class WorkflowExecutor:
    """Execute a ``WorkflowGraph`` sequentially in topological order."""

    def __init__(self, runtime: AutomationRuntime) -> None:
        self.runtime = runtime

    def run(
        self,
        graph: WorkflowGraph,
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> ExecutionContext:
        context = ExecutionContext()
        order = graph.topological_order()
        for node_id in order:
            if should_stop is not None and should_stop():
                raise ExecutionError("Execution cancelled")
            node = graph.nodes[node_id]
            try:
                node.execute(context, self.runtime)
            except Exception as exc:  # pragma: no cover - GUI handles error display
                raise ExecutionError(f"Node {node.title} failed: {exc}") from exc
        return context


def create_node(node_type: str, node_id: str, title: Optional[str] = None) -> WorkflowNodeModel:
    node_cls = NODE_REGISTRY.get(node_type)
    if node_cls is None:
        raise ValueError(f"Unknown node type: {node_type}")
    return node_cls(node_id, title)


def iter_registry() -> Iterable[WorkflowNodeModel]:
    for cls in NODE_REGISTRY.values():
        yield cls("__preview__")
