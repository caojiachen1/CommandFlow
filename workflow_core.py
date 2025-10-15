"""Core workflow engine for node-based desktop automation.

Provides reusable classes for building and executing automation workflows made of
node models. The GUI layer (``script.py``) constructs a ``WorkflowGraph`` using
these primitives, while unit tests exercise the pure-Python execution code.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, Set, Tuple, cast

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


_ALLOWED_EXPR_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    ast.BoolOp,
    ast.BinOp,
    ast.UnaryOp,
    ast.Compare,
    ast.Call,
    ast.Name,
    ast.Load,
    ast.Subscript,
    ast.Attribute,
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.Set,
    ast.Slice,
    ast.IfExp,
    ast.And,
    ast.Or,
    ast.Not,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.Is,
    ast.IsNot,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.USub,
    ast.UAdd,
    ast.BitAnd,
    ast.BitOr,
    ast.BitXor,
    ast.Invert,
)

_ALLOWED_CALLABLES: Dict[str, Callable[..., Any]] = {
    "len": len,
    "min": min,
    "max": max,
    "sum": sum,
    "any": any,
    "all": all,
    "abs": abs,
    "round": round,
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
    "range": range,
}

_ADDITIONAL_ALLOWED_CALLS: Set[str] = {"value"}

_ALLOWED_NAME_OVERRIDES: Dict[str, Any] = {
    "True": True,
    "False": False,
    "None": None,
}


def _validate_expression_ast(tree: ast.AST) -> None:
    allowed_calls = set(_ALLOWED_CALLABLES).union(_ADDITIONAL_ALLOWED_CALLS)
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_EXPR_NODES):
            raise ExecutionError(f"表达式包含不支持的语法: {type(node).__name__}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in allowed_calls:
                allowed = ", ".join(sorted(allowed_calls))
                raise ExecutionError(f"表达式只能调用受支持的函数: {allowed}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise ExecutionError("不允许访问以 '__' 开头的属性")
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise ExecutionError("不允许访问以 '__' 开头的名称")


def evaluate_expression(
    expression: str,
    context: ExecutionContext,
    extra_values: Optional[Dict[str, Any]] = None,
) -> Any:
    expr = expression.strip()
    if not expr:
        raise ExecutionError("表达式不能为空")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:  # pragma: no cover - defensive parsing guard
        raise ExecutionError(f"表达式语法错误: {exc}") from exc
    _validate_expression_ast(tree)
    scope: Dict[str, Any] = dict(_ALLOWED_CALLABLES)
    scope.update(_ALLOWED_NAME_OVERRIDES)
    scope["results"] = context.results
    scope["value"] = context.get
    if extra_values:
        scope.update(extra_values)
    compiled = compile(tree, "<workflow-expression>", "eval")
    return eval(compiled, {"__builtins__": {}}, scope)


def evaluate_condition(
    expression: str,
    context: ExecutionContext,
    extra_values: Optional[Dict[str, Any]] = None,
) -> bool:
    result = evaluate_expression(expression, context, extra_values)
    return bool(result)

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

    def output_ports(self) -> List[str]:
        """Return display labels for each output port."""

        return ["继续"]

    def input_ports(self) -> List[str]:
        """Return display labels for each input port."""

        return ["执行"]

    def determine_next(
        self,
        graph: "WorkflowGraph",
        context: ExecutionContext,
    ) -> Optional[str]:
        return graph.get_outgoing_target(self.id, 0)


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


class ConditionNodeBase(WorkflowNodeModel):
    """Base class for reusable condition evaluation nodes."""

    category = "条件判断"

    def output_ports(self) -> List[str]:
        return ["下一步", "条件结果"]


class IfConditionNode(WorkflowNodeModel):
    type_name = "if_condition"
    display_name = "条件判断"
    category = "流程控制"

    def default_config(self) -> Dict[str, Any]:
        return {"expression": "True"}

    def input_ports(self) -> List[str]:
        return ["执行", "条件"]

    def output_ports(self) -> List[str]:
        return ["条件成立", "条件不成立"]

    def validate_config(self) -> None:
        expression = self.config.get("expression", "")
        if not isinstance(expression, str):
            raise ValueError("表达式必须是字符串")
        expr = expression.strip()
        self.config["expression"] = expr

    def config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "expression",
                "label": "条件表达式",
                "type": "multiline",
            }
        ]

    def execute(self, context: ExecutionContext, runtime: AutomationRuntime) -> None:  # noqa: ARG002
        # 实际的条件判断在 ``determine_next`` 中完成
        context.record(self.id, {})

    def _evaluate_condition(
        self,
        graph: "WorkflowGraph",
        context: ExecutionContext,
    ) -> Tuple[bool, Any, Optional[str]]:
        incoming = graph.get_incoming_edge(self.id, target_port=1)
        if incoming is not None:
            source_state = context.get(incoming.source)
            if source_state is None:
                raise ExecutionError("条件输入尚未执行")
            raw_value = None
            if isinstance(source_state, dict) and "condition" in source_state:
                raw_value = source_state.get("value", source_state["condition"])
                result = bool(source_state["condition"])
            else:
                raw_value = source_state
                result = bool(source_state)
            return result, raw_value, incoming.source
        expression = self.config["expression"]
        raw_value = evaluate_expression(expression, context)
        return bool(raw_value), raw_value, None

    def determine_next(
        self,
        graph: "WorkflowGraph",
        context: ExecutionContext,
    ) -> Optional[str]:
        result, raw_value, source = self._evaluate_condition(graph, context)
        context.record(
            self.id,
            {
                "condition": result,
                "value": raw_value,
                "source": source,
            },
        )
        if result:
            return graph.get_outgoing_target(self.id, 0)
        return graph.get_outgoing_target(self.id, 1)


class BinaryExpressionConditionNode(ConditionNodeBase):
    """Condition node that evaluates two expressions and compares them."""

    left_key = "left_expression"
    right_key = "right_expression"
    left_label = "左侧表达式"
    right_label = "右侧表达式"
    default_left_expression = "0"
    default_right_expression = "0"

    def default_config(self) -> Dict[str, Any]:
        return {
            self.left_key: self.default_left_expression,
            self.right_key: self.default_right_expression,
        }

    def validate_config(self) -> None:
        for key in (self.left_key, self.right_key):
            value = self.config.get(key, "")
            if not isinstance(value, str):
                raise ValueError(f"{key} 必须是字符串表达式")
            expr = value.strip()
            if not expr:
                raise ValueError(f"{key} 表达式不能为空")
            self.config[key] = expr

    def config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": self.left_key,
                "label": self.left_label,
                "type": "multiline",
            },
            {
                "key": self.right_key,
                "label": self.right_label,
                "type": "multiline",
            },
        ]

    def _evaluate_operand(
        self,
        expression_key: str,
        context: ExecutionContext,
    ) -> Any:
        expression = self.config[expression_key]
        try:
            return evaluate_expression(expression, context)
        except ExecutionError:
            raise
        except Exception as exc:  # pragma: no cover - safeguard
            raise ExecutionError(f"计算表达式失败: {expression}") from exc

    def transform_operands(self, left: Any, right: Any) -> Tuple[Any, Any]:
        return left, right

    def compare(self, left: Any, right: Any) -> bool:
        raise NotImplementedError

    def execute(self, context: ExecutionContext, runtime: AutomationRuntime) -> bool:  # noqa: ARG002
        left_value = self._evaluate_operand(self.left_key, context)
        right_value = self._evaluate_operand(self.right_key, context)
        try:
            operand_left, operand_right = self.transform_operands(left_value, right_value)
            result = bool(self.compare(operand_left, operand_right))
        except ExecutionError:
            raise
        except Exception as exc:  # pragma: no cover - conversion guard
            raise ExecutionError(f"条件比较失败: {exc}") from exc
        context.record(
            self.id,
            {
                "condition": result,
                "left": operand_left,
                "right": operand_right,
                "value": result,
            },
        )
        return result


class NumericComparisonConditionNode(BinaryExpressionConditionNode):
    """Condition node that compares numeric operands."""

    default_left_expression = "0"
    default_right_expression = "0"

    def transform_operands(self, left: Any, right: Any) -> Tuple[float, float]:
        try:
            left_num = float(left)
            right_num = float(right)
        except (TypeError, ValueError) as exc:
            raise ExecutionError("数值比较需要可转换为数字的结果") from exc
        return left_num, right_num


class EqualsConditionNode(BinaryExpressionConditionNode):
    type_name = "condition_equals"
    display_name = "判断等于"

    def compare(self, left: Any, right: Any) -> bool:
        return left == right


class NotEqualsConditionNode(BinaryExpressionConditionNode):
    type_name = "condition_not_equals"
    display_name = "判断不等于"

    def compare(self, left: Any, right: Any) -> bool:
        return left != right


class GreaterThanConditionNode(NumericComparisonConditionNode):
    type_name = "condition_greater_than"
    display_name = "判断大于"

    def compare(self, left: float, right: float) -> bool:
        return left > right


class GreaterOrEqualConditionNode(NumericComparisonConditionNode):
    type_name = "condition_greater_or_equal"
    display_name = "判断大于等于"

    def compare(self, left: float, right: float) -> bool:
        return left >= right


class LessThanConditionNode(NumericComparisonConditionNode):
    type_name = "condition_less_than"
    display_name = "判断小于"

    def compare(self, left: float, right: float) -> bool:
        return left < right


class LessOrEqualConditionNode(NumericComparisonConditionNode):
    type_name = "condition_less_or_equal"
    display_name = "判断小于等于"

    def compare(self, left: float, right: float) -> bool:
        return left <= right


class ContainsConditionNode(BinaryExpressionConditionNode):
    type_name = "condition_contains"
    display_name = "判断包含"
    default_left_expression = "[]"
    default_right_expression = "0"
    left_label = "容器表达式"
    right_label = "待检查表达式"

    def compare(self, left: Any, right: Any) -> bool:
        try:
            return right in left
        except TypeError as exc:
            raise ExecutionError("包含判断需要可迭代或字符串类型的左侧表达式") from exc


class WhileLoopNode(WorkflowNodeModel):
    type_name = "while_loop"
    display_name = "While 循环"
    category = "控制流"

    def default_config(self) -> Dict[str, Any]:
        return {"expression": "False", "max_iterations": 1000}

    def output_ports(self) -> List[str]:
        return ["循环结束", "循环入口", "循环出口"]

    def validate_config(self) -> None:
        expression = self.config.get("expression", "")
        if not isinstance(expression, str):
            raise ValueError("表达式必须是字符串")
        expr = expression.strip()
        if not expr:
            raise ValueError("循环条件不能为空")
        max_iterations = self.config.get("max_iterations", 1000)
        try:
            max_iter_value = int(max_iterations)
        except (TypeError, ValueError) as exc:
            raise ValueError("最大迭代次数必须为正整数") from exc
        if max_iter_value <= 0:
            raise ValueError("最大迭代次数必须为正整数")
        self.config["expression"] = expr
        self.config["max_iterations"] = max_iter_value

    def config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "expression",
                "label": "循环条件表达式",
                "type": "multiline",
            },
            {
                "key": "max_iterations",
                "label": "最大迭代次数",
                "type": "int",
                "min": 1,
                "max": 1_000_000,
            },
        ]

    def execute(self, context: ExecutionContext, runtime: AutomationRuntime) -> bool:  # noqa: ARG002
        state = context.get(self.id)
        iteration = 0
        if isinstance(state, dict):
            iteration = int(state.get("iteration", 0))
        expression = self.config["expression"]
        condition = evaluate_condition(expression, context, {"iteration": iteration})
        if condition:
            iteration += 1
            max_iterations = int(self.config["max_iterations"])
            if iteration > max_iterations:
                raise ExecutionError("While 循环超过最大迭代次数限制")
        context.record(
            self.id,
            {
                "condition": condition,
                "iteration": iteration,
            },
        )
        return condition

    def determine_next(
        self,
        graph: "WorkflowGraph",
        context: ExecutionContext,
    ) -> Optional[str]:
        state = context.get(self.id)
        condition = False
        if isinstance(state, dict):
            condition = bool(state.get("condition"))
        if condition:
            return graph.get_outgoing_target(self.id, 1)
        return graph.get_outgoing_target(self.id, 0)


class ForLoopNode(WorkflowNodeModel):
    type_name = "for_loop"
    display_name = "For 循环"
    category = "控制流"

    def default_config(self) -> Dict[str, Any]:
        return {
            "start": 0,
            "end": 1,
            "step": 1,
            "max_iterations": 1000,
        }

    def output_ports(self) -> List[str]:
        return ["循环结束", "循环入口", "循环出口"]

    def validate_config(self) -> None:
        numeric_keys = ("start", "end", "step")
        for key in numeric_keys:
            value = self.config.get(key, 0)
            try:
                int_value = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} 必须为整数") from exc
            self.config[key] = int_value
        if int(self.config["step"]) == 0:
            raise ValueError("步长不能为 0")
        max_iterations = self.config.get("max_iterations", 1000)
        try:
            max_iter_value = int(max_iterations)
        except (TypeError, ValueError) as exc:
            raise ValueError("最大迭代次数必须为正整数") from exc
        if max_iter_value <= 0:
            raise ValueError("最大迭代次数必须为正整数")
        self.config["max_iterations"] = max_iter_value

    def config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "start",
                "label": "起始值",
                "type": "int",
                "min": -1_000_000,
                "max": 1_000_000,
            },
            {
                "key": "end",
                "label": "结束值(不含)",
                "type": "int",
                "min": -1_000_000,
                "max": 1_000_000,
            },
            {
                "key": "step",
                "label": "步长",
                "type": "int",
                "min": -1_000_000,
                "max": 1_000_000,
            },
            {
                "key": "max_iterations",
                "label": "最大迭代次数",
                "type": "int",
                "min": 1,
                "max": 1_000_000,
            },
        ]

    def execute(self, context: ExecutionContext, runtime: AutomationRuntime) -> Dict[str, Any]:  # noqa: ARG002
        cfg = self.config
        start = int(cfg["start"])
        step = int(cfg["step"])
        end = int(cfg["end"])
        max_iterations = int(cfg["max_iterations"])
        state = context.get(self.id)
        if not isinstance(state, dict) or state.get("completed"):
            current = start
            iteration = 0
        else:
            current = int(state.get("next_value", start))
            iteration = int(state.get("iteration", 0))
        if step > 0:
            should_continue = current < end
        else:
            should_continue = current > end
        if should_continue:
            iteration += 1
            if iteration > max_iterations:
                raise ExecutionError("For 循环超过最大迭代次数限制")
            next_value = current + step
            record = {
                "value": current,
                "iteration": iteration,
                "next_value": next_value,
                "should_continue": True,
                "completed": False,
            }
        else:
            record = {
                "value": current,
                "iteration": iteration,
                "next_value": current,
                "should_continue": False,
                "completed": True,
            }
        context.record(self.id, record)
        return record

    def determine_next(
        self,
        graph: "WorkflowGraph",
        context: ExecutionContext,
    ) -> Optional[str]:
        state = context.get(self.id)
        should_continue = False
        if isinstance(state, dict):
            should_continue = bool(state.get("should_continue"))
        if should_continue:
            return graph.get_outgoing_target(self.id, 1)
        return graph.get_outgoing_target(self.id, 0)


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
    IfConditionNode.type_name: IfConditionNode,
    EqualsConditionNode.type_name: EqualsConditionNode,
    NotEqualsConditionNode.type_name: NotEqualsConditionNode,
    GreaterThanConditionNode.type_name: GreaterThanConditionNode,
    GreaterOrEqualConditionNode.type_name: GreaterOrEqualConditionNode,
    LessThanConditionNode.type_name: LessThanConditionNode,
    LessOrEqualConditionNode.type_name: LessOrEqualConditionNode,
    ContainsConditionNode.type_name: ContainsConditionNode,
    WhileLoopNode.type_name: WhileLoopNode,
    ForLoopNode.type_name: ForLoopNode,
}


@dataclass(frozen=True)
class OutgoingEdge:
    target: str
    source_port: int
    target_port: int


@dataclass(frozen=True)
class IncomingEdge:
    source: str
    target_port: int
    source_port: int


class WorkflowGraph:
    """In-memory representation of a node graph."""

    def __init__(self) -> None:
        self.nodes: Dict[str, WorkflowNodeModel] = {}
        self.edges: Dict[str, List[OutgoingEdge]] = {}
        self.reverse_edges: Dict[str, List[IncomingEdge]] = {}

    def add_node(self, node: WorkflowNodeModel) -> None:
        if node.id in self.nodes:
            raise ValueError(f"Node {node.id} already exists")
        self.nodes[node.id] = node
        self.edges[node.id] = []
        self.reverse_edges[node.id] = []

    def remove_node(self, node_id: str) -> None:
        if node_id not in self.nodes:
            return
        for incoming in list(self.reverse_edges[node_id]):
            self.remove_edge(
                incoming.source,
                node_id,
                source_port=incoming.source_port,
                target_port=incoming.target_port,
            )
        for edge in list(self.edges[node_id]):
            self.remove_edge(
                node_id,
                edge.target,
                source_port=edge.source_port,
                target_port=edge.target_port,
            )
        del self.nodes[node_id]
        del self.edges[node_id]
        del self.reverse_edges[node_id]

    def add_edge(
        self,
        source_id: str,
        target_id: str,
        *,
        source_port: int = 0,
        target_port: int = 0,
    ) -> None:
        if source_id == target_id:
            raise ValueError("Cannot connect node to itself")
        if source_id not in self.nodes or target_id not in self.nodes:
            raise ValueError("Both nodes must exist")
        source_node = self.nodes[source_id]
        target_node = self.nodes[target_id]
        source_ports = source_node.output_ports()
        target_ports = target_node.input_ports()
        if not source_ports:
            raise ValueError(f"节点 {source_node.title} 不支持输出连接")
        if source_port < 0 or source_port >= len(source_ports):
            raise ValueError(
                f"节点 {source_node.title} 的输出端口索引无效: {source_port}"
            )
        if target_port < 0 or target_port >= len(target_ports):
            raise ValueError(
                f"节点 {target_node.title} 的输入端口索引无效: {target_port}"
            )
        if isinstance(target_node, IfConditionNode) and target_port == 1:
            if not isinstance(source_node, ConditionNodeBase) or source_port != 1:
                raise ValueError(
                    f"节点 {target_node.title} 的条件输入只能连接条件判断节点的结果端口"
                )
        if isinstance(source_node, ConditionNodeBase) and source_port == 1:
            if not isinstance(target_node, IfConditionNode) or target_port != 1:
                raise ValueError(
                    f"节点 {source_node.title} 的条件结果端口只能连接到条件判断控件的条件输入"
                )
        for edge in self.edges[source_id]:
            if edge.source_port == source_port and edge.target_port == target_port and edge.target == target_id:
                raise ValueError("连接已存在")
            if edge.source_port == source_port:
                raise ValueError(
                    f"节点 {source_node.title} 的输出端口 '{source_ports[source_port]}' 已连接，不能重复连接"
                )
        for incoming in self.reverse_edges[target_id]:
            if incoming.target_port == target_port:
                raise ValueError(
                    f"节点 {target_node.title} 的输入端口 '{target_ports[target_port]}' 已连接"
                )
        new_edge = OutgoingEdge(target_id, source_port, target_port)
        self.edges[source_id].append(new_edge)
        self.reverse_edges[target_id].append(
            IncomingEdge(source_id, target_port, source_port)
        )

    def remove_edge(
        self,
        source_id: str,
        target_id: str,
        *,
        source_port: int | None = None,
        target_port: int | None = None,
    ) -> None:
        if source_id not in self.edges:
            return
        removed = False
        remaining: List[OutgoingEdge] = []
        for edge in self.edges[source_id]:
            matches_source = source_port is None or edge.source_port == source_port
            matches_target = target_port is None or edge.target_port == target_port
            if edge.target == target_id and matches_source and matches_target:
                removed = True
                continue
            remaining.append(edge)
        self.edges[source_id] = remaining
        if removed and target_id in self.reverse_edges:
            filtered: List[IncomingEdge] = []
            for incoming in self.reverse_edges[target_id]:
                matches_source = source_port is None or incoming.source_port == source_port
                matches_target = target_port is None or incoming.target_port == target_port
                if incoming.source == source_id and matches_source and matches_target:
                    continue
                filtered.append(incoming)
            self.reverse_edges[target_id] = filtered

    def entry_nodes(self) -> List[str]:
        entries: List[str] = []
        for node_id, incoming in self.reverse_edges.items():
            if not any(edge.target_port == 0 for edge in incoming):
                entries.append(node_id)
        return sorted(entries)

    def validate(self) -> None:
        if not self.nodes:
            raise ExecutionError("工作流为空")
        entries = self.entry_nodes()
        if not entries:
            raise ExecutionError("工作流缺少入口节点，请至少保留一个没有输入连接的节点")
        if len(entries) > 1:
            joined_entries = ", ".join(entries)
            raise ExecutionError(
                f"检测到多个入口节点: {joined_entries}。当前版本仅支持单一入口节点"
            )
        reachable: Set[str] = set()
        stack = list(entries)
        while stack:
            current = stack.pop()
            if current in reachable:
                continue
            reachable.add(current)
            for edge in self.edges.get(current, []):
                if edge.target_port != 0:
                    continue
                stack.append(edge.target)
        if len(reachable) != len(self.nodes):
            unreachable = sorted(set(self.nodes) - reachable)
            joined = ", ".join(unreachable)
            raise ExecutionError(f"存在无法到达的节点: {joined}")
        loop_tail_sources: Dict[str, str] = {}
        for node_id, node in self.nodes.items():
            port_labels = node.output_ports()
            if not port_labels:
                continue
            mapped: Dict[int, List[OutgoingEdge]] = {}
            for edge in self.edges.get(node_id, []):
                mapped.setdefault(edge.source_port, []).append(edge)
                if edge.source_port >= len(port_labels):
                    raise ExecutionError(
                        f"节点 {node.title} 的连接数据无效: 端口索引 {edge.source_port} 超出范围"
                    )
            if isinstance(node, ConditionNodeBase):
                for idx, label in enumerate(port_labels):
                    edges_for_port = mapped.get(idx, [])
                    if not edges_for_port:
                        raise ExecutionError(
                            f"{node.title} 的输出端口 '{label}' 未连接"
                        )
                    if len(edges_for_port) > 1:
                        raise ExecutionError(
                            f"{node.title} 的输出端口 '{label}' 只能连接一个目标"
                        )
                    if idx == 0 and edges_for_port[0].target_port != 0:
                        raise ExecutionError(
                            f"{node.title} 的输出端口 '{label}' 必须连接到执行输入端口"
                        )
                    if idx == 1 and edges_for_port[0].target_port == 0:
                        raise ExecutionError(
                            f"{node.title} 的条件结果输出必须连接到条件输入端口"
                        )
            elif isinstance(node, IfConditionNode):
                condition_edge = self.get_incoming_edge(node_id, target_port=1)
                if condition_edge is None and not node.config.get("expression"):
                    raise ExecutionError(f"{node.title} 缺少条件输入或表达式配置")
                if condition_edge is not None:
                    source_node = self.nodes.get(condition_edge.source)
                    if source_node is None or not isinstance(source_node, ConditionNodeBase):
                        raise ExecutionError(
                            f"{node.title} 的条件输入必须来自条件判断节点"
                        )
                    if condition_edge.source_port != 1:
                        raise ExecutionError(
                            f"{node.title} 的条件输入必须连接条件节点的结果端口"
                        )
            elif isinstance(node, (WhileLoopNode, ForLoopNode)):
                required_ports = {
                    1: port_labels[1],
                    2: port_labels[2],
                }
                for idx, label in required_ports.items():
                    edges_for_port = mapped.get(idx)
                    if not edges_for_port:
                        raise ExecutionError(
                            f"{node.title} 的输出端口 '{label}' 未连接"
                        )
                    if len(edges_for_port) > 1:
                        raise ExecutionError(
                            f"{node.title} 的输出端口 '{label}' 只能连接一个目标"
                        )
                    if edges_for_port[0].target_port != 0:
                        raise ExecutionError(
                            f"{node.title} 的输出端口 '{label}' 必须连接到执行输入端口"
                        )
                tail_edge = mapped[2][0]
                tail_target = tail_edge.target
                if tail_target == node_id:
                    raise ExecutionError(
                        f"{node.title} 的输出端口 '{port_labels[2]}' 不能连接到节点自身"
                    )
                previous_loop = loop_tail_sources.get(tail_target)
                if previous_loop is not None and previous_loop != node_id:
                    tail_title = (
                        self.nodes[tail_target].title
                        if tail_target in self.nodes
                        else tail_target
                    )
                    prev_title = (
                        self.nodes[previous_loop].title
                        if previous_loop in self.nodes
                        else previous_loop
                    )
                    raise ExecutionError(
                        f"节点 {tail_title} 已被循环节点 {prev_title} 用作循环结尾"
                    )
                loop_tail_sources[tail_target] = node_id
            else:
                control_edges = [edge for edges in mapped.values() for edge in edges if edge.target_port == 0]
                if len(control_edges) > 1:
                    raise ExecutionError(
                        f"{node.title} 只能连接到一个后续节点，如需分支请使用条件节点"
                    )

    def _has_cycle(self) -> bool:
        try:
            self.topological_order()
        except ExecutionError:
            return True
        return False

    def topological_order(self) -> List[str]:
        indegree: Dict[str, int] = {}
        for node_id, incoming in self.reverse_edges.items():
            indegree[node_id] = sum(1 for edge in incoming if edge.target_port == 0)
        queue = [node for node, degree in indegree.items() if degree == 0]
        order: List[str] = []
        tmp_indegree = indegree.copy()
        while queue:
            current = queue.pop(0)
            order.append(current)
            for edge in self.edges.get(current, []):
                if edge.target_port != 0:
                    continue
                neighbor = edge.target
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
            for edge in targets:
                graph.edges[source].append(
                    OutgoingEdge(edge.target, edge.source_port, edge.target_port)
                )
                graph.reverse_edges[edge.target].append(
                    IncomingEdge(source, edge.target_port, edge.source_port)
                )
        return graph

    def build_loop_back_map(self) -> Dict[str, str]:
        mapping: Dict[str, str] = {}
        for node_id, node in self.nodes.items():
            if not isinstance(node, (WhileLoopNode, ForLoopNode)):
                continue
            for edge in self.edges.get(node_id, []):
                if edge.source_port == 2 and edge.target_port == 0:
                    mapping[edge.target] = node_id
        return mapping

    def get_outgoing_target(
        self,
        node_id: str,
        port_index: int,
        *,
        target_port: int = 0,
    ) -> Optional[str]:
        for edge in self.edges.get(node_id, []):
            if edge.source_port == port_index and edge.target_port == target_port:
                return edge.target
        return None

    def get_incoming_edge(
        self,
        node_id: str,
        *,
        target_port: int,
    ) -> Optional[IncomingEdge]:
        for edge in self.reverse_edges.get(node_id, []):
            if edge.target_port == target_port:
                return edge
        return None


class WorkflowExecutor:
    """Execute a ``WorkflowGraph`` following control-flow edges."""

    def __init__(self, runtime: AutomationRuntime, *, max_steps: int = 10000) -> None:
        if max_steps <= 0:
            raise ValueError("max_steps must be positive")
        self.runtime = runtime
        self.max_steps = int(max_steps)

    def run(
        self,
        graph: WorkflowGraph,
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> ExecutionContext:
        graph.validate()
        loop_back_map = graph.build_loop_back_map()
        context = ExecutionContext()
        executed_steps = 0
        for start_id in graph.entry_nodes():
            executed_steps = self._run_from(
                start_id,
                graph,
                context,
                should_stop,
                executed_steps,
                loop_back_map,
            )
        return context

    def _run_from(
        self,
        start_id: str,
        graph: WorkflowGraph,
        context: ExecutionContext,
        should_stop: Callable[[], bool] | None,
        executed_steps: int,
        loop_back_map: Dict[str, str],
    ) -> int:
        current = start_id
        while current is not None:
            if should_stop is not None and should_stop():
                raise ExecutionError("Execution cancelled")
            node = graph.nodes.get(current)
            if node is None:
                raise ExecutionError(f"节点 {current} 不存在")
            executed_steps += 1
            if executed_steps > self.max_steps:
                raise ExecutionError("执行步数超过上限，可能存在无限循环")
            try:
                node.execute(context, self.runtime)
            except Exception as exc:  # pragma: no cover - GUI handles error display
                raise ExecutionError(f"Node {node.title} failed: {exc}") from exc
            next_id = node.determine_next(graph, context)
            loop_controller = loop_back_map.get(current)
            if loop_controller is not None:
                next_id = loop_controller
            current = next_id
        return executed_steps


def create_node(node_type: str, node_id: str, title: Optional[str] = None) -> WorkflowNodeModel:
    node_cls = NODE_REGISTRY.get(node_type)
    if node_cls is None:
        raise ValueError(f"Unknown node type: {node_type}")
    return node_cls(node_id, title)


def iter_registry() -> Iterable[WorkflowNodeModel]:
    for cls in NODE_REGISTRY.values():
        yield cls("__preview__")
