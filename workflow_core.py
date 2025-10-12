"""Core workflow engine for node-based desktop automation.

Provides reusable classes for building and executing automation workflows made of
node models. The GUI layer (``script.py``) constructs a ``WorkflowGraph`` using
these primitives, while unit tests exercise the pure-Python execution code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Protocol


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

    def type_text(self, text: str, interval: float) -> None: ...


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
                "type": "str",
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

    def default_config(self) -> Dict[str, Any]:
        return {
            "x": 0,
            "y": 0,
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


NODE_REGISTRY = {
    ScreenshotNode.type_name: ScreenshotNode,
    MouseClickNode.type_name: MouseClickNode,
    KeyboardInputNode.type_name: KeyboardInputNode,
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

    def run(self, graph: WorkflowGraph) -> ExecutionContext:
        context = ExecutionContext()
        order = graph.topological_order()
        for node_id in order:
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
