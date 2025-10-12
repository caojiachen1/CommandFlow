"""Node-based desktop automation tool with a drag-and-drop workflow editor.

This script implements a lightweight ComfyUI-style workflow builder that lets
users assemble desktop automation pipelines. The GUI is built with PySide6 and
relies on ``pyautogui`` for automation primitives such as screenshots, mouse
clicks, and keyboard input. The workflow logic itself lives in
``workflow_core.py`` where it is unit-tested independently.
"""

from __future__ import annotations

import math
import sys
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, cast

from automation_runtime import PyAutoGuiRuntime
from PySide6.QtCore import QPoint, QPointF, Qt, QMimeData, QObject, Signal
from PySide6.QtGui import QColor, QDrag, QIcon, QPainter, QPainterPath, QPen, QTransform
from PySide6.QtWidgets import (
	QApplication,
	QComboBox,
	QDialog,
	QDialogButtonBox,
	QDoubleSpinBox,
	QFormLayout,
	QGraphicsView,
	QGraphicsEllipseItem,
	QGraphicsItem,
	QGraphicsPathItem,
	QGraphicsRectItem,
	QGraphicsScene,
	QGraphicsTextItem,
	QHBoxLayout,
	QLabel,
	QLineEdit,
	QListWidget,
	QListWidgetItem,
	QMainWindow,
	QMessageBox,
	QPushButton,
	QSpinBox,
	QSplitter,
	QTextEdit,
	QVBoxLayout,
	QWidget,
	QAbstractItemView,
	QStatusBar,
)

try:
	from qfluentwidgets import (
		FluentWindow,
		Theme,
		setTheme,
		setThemeColor,
		PrimaryPushButton,
		BodyLabel,
		InfoBar,
		InfoBarPosition,
		FluentTranslator,
		MessageBox,
		MessageBoxBase,
		ComboBox as FluentComboBox,
		SpinBox as FluentSpinBox,
		DoubleSpinBox as FluentDoubleSpinBox,
		LineEdit as FluentLineEdit,
		TextEdit as FluentTextEdit,
		SubtitleLabel,
		FluentIcon,
	)
	HAVE_FLUENT_WIDGETS = True
except ImportError:  # pragma: no cover - optional dependency
	HAVE_FLUENT_WIDGETS = False
	FluentWindow = QMainWindow  # type: ignore[assignment]
	Theme = None

	def setTheme(*_args, **_kwargs):  # type: ignore[func-name-matches]
		return None

	def setThemeColor(*_args, **_kwargs):  # type: ignore[func-name-matches]
		return None

	PrimaryPushButton = QPushButton  # type: ignore[assignment]
	BodyLabel = QLabel  # type: ignore[assignment]

	class _InfoBarStub:
		@staticmethod
		def success(*_args, **_kwargs):
			return None

	class _InfoBarPositionStub:
		TOP_RIGHT = None

	InfoBar = _InfoBarStub
	InfoBarPosition = _InfoBarPositionStub
	FluentTranslator = None
	MessageBox = None

	class _MessageBoxBase(QDialog):
		pass

	MessageBoxBase = _MessageBoxBase
	FluentComboBox = QComboBox  # type: ignore[assignment]
	FluentSpinBox = QSpinBox  # type: ignore[assignment]
	FluentDoubleSpinBox = QDoubleSpinBox  # type: ignore[assignment]
	FluentLineEdit = QLineEdit  # type: ignore[assignment]
	FluentTextEdit = QTextEdit  # type: ignore[assignment]
	SubtitleLabel = QLabel  # type: ignore[assignment]
	FluentIcon = None
else:
	MessageBoxBase = cast(Type[QDialog], MessageBoxBase)

BaseMainWindow: Type[QMainWindow]
if HAVE_FLUENT_WIDGETS:
	BaseMainWindow = cast(Type[QMainWindow], FluentWindow)
else:
	BaseMainWindow = QMainWindow

ConfigDialogBase: Type[QDialog]
if HAVE_FLUENT_WIDGETS:
	ConfigDialogBase = cast(Type[QDialog], MessageBoxBase)
else:
	ConfigDialogBase = QDialog
INFOBAR_AVAILABLE = HAVE_FLUENT_WIDGETS and InfoBar is not None and InfoBarPosition is not None

from workflow_core import (
	AutomationRuntime,
	ExecutionError,
	WorkflowExecutor,
	WorkflowGraph,
	create_node,
	iter_registry,
)


def configure_windows_dpi() -> None:
	"""Best-effort override to reduce DPI awareness warnings on Windows."""

	if sys.platform != "win32":
		return
	try:
		import ctypes

		ctypes.windll.shcore.SetProcessDpiAwareness(2)
	except Exception:
		try:
			import ctypes

			ctypes.windll.user32.SetProcessDPIAware()
		except Exception:
			pass


def _show_message(parent: QWidget, title: str, message: str, kind: str) -> None:
	if HAVE_FLUENT_WIDGETS and MessageBox is not None:
		box = MessageBox(title, message, parent)
		if hasattr(box, "cancelButton"):
			box.cancelButton.hide()
		if hasattr(box, "yesButton"):
			text = "确认" if kind == "warning" else "确定"
			box.yesButton.setText(text)
		box.exec()
		return

	if kind == "warning":
		QMessageBox.warning(parent, title, message)
	else:
		QMessageBox.information(parent, title, message)


def show_information(parent: QWidget, title: str, message: str) -> None:
	_show_message(parent, title, message, "info")


def show_warning(parent: QWidget, title: str, message: str) -> None:
	_show_message(parent, title, message, "warning")
# -- GUI helpers -----------------------------------------------------------


class NodePalette(QListWidget):
	"""Left-hand palette that enumerates available node types."""

	def __init__(self, parent: Optional[QWidget] = None) -> None:
		super().__init__(parent)
		self.setDragEnabled(True)
		self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
		self.populate()

	def populate(self) -> None:
		self.clear()
		for node in iter_registry():
			item = QListWidgetItem(node.display_name)
			item.setData(Qt.ItemDataRole.UserRole, node.type_name)
			self.addItem(item)

	def startDrag(self, supported_actions: Qt.DropAction) -> None:  # noqa: N802
		item = self.currentItem()
		if item is None:
			return
		node_type = item.data(Qt.ItemDataRole.UserRole)
		if not node_type:
			return
		mime = QMimeData()
		mime.setData("application/x-workflow-node", node_type.encode("utf-8"))
		drag = QDrag(self)
		drag.setMimeData(mime)
		drag.exec(supported_actions)


class WorkflowView(QGraphicsView):
	"""Graphics view hosting the workflow scene."""

	def __init__(self, scene: "WorkflowScene", parent: Optional[QWidget] = None) -> None:
		super().__init__(scene, parent)
		self.setAcceptDrops(True)
		self.setRenderHints(
			self.renderHints() | QPainter.RenderHint.Antialiasing
		)
		self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
		self.setViewportUpdateMode(
			QGraphicsView.ViewportUpdateMode.BoundingRectViewportUpdate
		)
		self._panning = False
		self._pan_start = QPoint()
		self._pan_scroll_start = QPoint()

	def dragEnterEvent(self, event):  # noqa: D401
		if event.mimeData().hasFormat("application/x-workflow-node"):
			event.acceptProposedAction()
		else:
			super().dragEnterEvent(event)

	def dragMoveEvent(self, event):  # noqa: D401
		if event.mimeData().hasFormat("application/x-workflow-node"):
			event.acceptProposedAction()
		else:
			super().dragMoveEvent(event)

	def dropEvent(self, event):  # noqa: D401
		if not event.mimeData().hasFormat("application/x-workflow-node"):
			super().dropEvent(event)
			return
		node_type = (
			event.mimeData()
			.data("application/x-workflow-node")
			.data()
			.decode("utf-8")
		)
		pos = self.mapToScene(event.position().toPoint())
		scene = cast(WorkflowScene, self.scene())
		scene.create_node_from_palette(node_type, pos)
		event.acceptProposedAction()

	def keyPressEvent(self, event):  # noqa: D401
		if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
			scene = cast(WorkflowScene, self.scene())
			scene.delete_selection()
		else:
			super().keyPressEvent(event)

	def mousePressEvent(self, event):  # noqa: D401
		if self._begin_panning(event):
			return
		super().mousePressEvent(event)

	def mouseMoveEvent(self, event):  # noqa: D401
		if self._panning:
			self._update_pan(event)
			return
		super().mouseMoveEvent(event)

	def mouseReleaseEvent(self, event):  # noqa: D401
		if self._panning and event.button() in (
			Qt.MouseButton.MiddleButton,
			Qt.MouseButton.RightButton,
			Qt.MouseButton.LeftButton,
		):
			self._end_panning()
			event.accept()
			return
		super().mouseReleaseEvent(event)

	def _begin_panning(self, event) -> bool:
		if event.button() in (
			Qt.MouseButton.MiddleButton,
			Qt.MouseButton.RightButton,
		) or (
			event.button() == Qt.MouseButton.LeftButton
			and event.modifiers() & Qt.KeyboardModifier.AltModifier
		):
			self._panning = True
			self._pan_start = event.position().toPoint()
			self._pan_scroll_start = QPoint(
				self.horizontalScrollBar().value(),
				self.verticalScrollBar().value(),
			)
			self.setCursor(Qt.CursorShape.ClosedHandCursor)
			event.accept()
			return True
		return False

	def _update_pan(self, event) -> None:
		delta = event.position().toPoint() - self._pan_start
		self.horizontalScrollBar().setValue(
			self._pan_scroll_start.x() - delta.x()
		)
		self.verticalScrollBar().setValue(
			self._pan_scroll_start.y() - delta.y()
		)
		event.accept()

	def _end_panning(self) -> None:
		self._panning = False
		self.setCursor(Qt.CursorShape.ArrowCursor)


class NodePort(QGraphicsEllipseItem):
	"""Circular port used for incoming or outgoing connections."""

	def __init__(self, parent: "WorkflowNodeItem", kind: str) -> None:
		super().__init__(-6, -6, 12, 12, parent)
		self._is_hovered = False
		self._is_highlighted = False
		self._default_color = QColor(220, 220, 220)
		self._hover_color = QColor(255, 200, 120)
		self._highlight_color = QColor(255, 230, 150)
		self.setBrush(self._default_color)
		self.setPen(QPen(QColor(70, 70, 70), 1))
		self.setFlag(
			QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations
		)
		self.kind = kind
		self.setAcceptHoverEvents(True)

	def hoverEnterEvent(self, event):  # noqa: D401
		self._is_hovered = True
		self._update_brush()
		super().hoverEnterEvent(event)

	def hoverLeaveEvent(self, event):  # noqa: D401
		self._is_hovered = False
		self._update_brush()
		super().hoverLeaveEvent(event)

	def set_highlighted(self, highlighted: bool) -> None:
		self._is_highlighted = highlighted
		self._update_brush()

	def _update_brush(self) -> None:
		if self._is_highlighted:
			self.setBrush(self._highlight_color)
		elif self._is_hovered:
			self.setBrush(self._hover_color)
		else:
			self.setBrush(self._default_color)

	def mousePressEvent(self, event):  # noqa: D401
		scene = cast(WorkflowScene, self.scene())
		scene.handle_port_press(self)
		event.accept()

	def mouseReleaseEvent(self, event):  # noqa: D401
		scene = cast(WorkflowScene, self.scene())
		scene.handle_port_release(self)
		event.accept()

	@property
	def center_in_scene(self) -> QPointF:
		return self.scenePos() + QPointF(6, 6)


class ConnectionItem(QGraphicsPathItem):
	"""Graphics item representing an edge between two scene items."""

	def __init__(self, source: QGraphicsItem, target: QGraphicsItem) -> None:
		super().__init__()
		self.source = source
		self.target = target
		self.setPen(QPen(QColor(90, 120, 255), 2))
		self.setZValue(-1)
		self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
		self.refresh_path()

	@staticmethod
	def _center(item: QGraphicsItem) -> QPointF:
		rect = item.sceneBoundingRect()
		return rect.center()

	def refresh_path(self) -> None:
		start = self._center(self.source)
		end = self._center(self.target)
		path = QPainterPath(start)
		ctrl_dx = max(60.0, abs(end.x() - start.x()) / 2)
		c1 = QPointF(start.x() + ctrl_dx, start.y())
		c2 = QPointF(end.x() - ctrl_dx, end.y())
		path.cubicTo(c1, c2, end)
		self.setPath(path)


class WorkflowNodeItem(QGraphicsRectItem):
	"""Rectangular node wrapper that mirrors a ``WorkflowNodeModel``."""

	WIDTH = 200
	HEIGHT = 100

	def __init__(self, node_id: str, title: str) -> None:
		super().__init__(0, 0, self.WIDTH, self.HEIGHT)
		self.node_id = node_id
		self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable)
		self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
		self.setFlag(
			QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
		)
		self.setBrush(QColor(40, 40, 50))
		self.setPen(QPen(QColor(90, 90, 110), 2))
		self.title_item = QGraphicsTextItem(title, self)
		self.title_item.setDefaultTextColor(QColor(245, 245, 245))
		self.title_item.setPos(12, 10)
		self.input_port = NodePort(self, "input")
		self.output_port = NodePort(self, "output")
		self.update_ports()

	def update_ports(self) -> None:
		self.input_port.setPos(0, self.HEIGHT / 2 - 6)
		self.output_port.setPos(self.WIDTH - 12, self.HEIGHT / 2 - 6)

	def set_title(self, title: str) -> None:
		self.title_item.setPlainText(title)

	def itemChange(self, change, value):  # noqa: D401
		if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
			scene_obj = self.scene()
			if scene_obj is not None:
				scene = cast(WorkflowScene, scene_obj)
				scene.refresh_connections(self)
		return super().itemChange(change, value)

	def mouseDoubleClickEvent(self, event):  # noqa: D401
		scene = cast(WorkflowScene, self.scene())
		scene.request_config(self.node_id)
		super().mouseDoubleClickEvent(event)


# -- Workflow scene --------------------------------------------------------


class WorkflowScene(QGraphicsScene):
	"""Scene graph bridging the core workflow model with graphics items."""

	message_posted = Signal(str)
	config_requested = Signal(str)

	def __init__(self, parent: Optional[QObject] = None) -> None:
		super().__init__(parent)
		self.graph = WorkflowGraph()
		self.node_items: Dict[str, WorkflowNodeItem] = {}
		self.connections: List[ConnectionItem] = []
		self._pending_output: Optional[NodePort] = None
		self._temp_connection: Optional[ConnectionItem] = None
		self._temp_target_item: Optional[QGraphicsEllipseItem] = None
		self._hover_port: Optional[NodePort] = None

	def create_node_from_palette(self, node_type: str, pos: QPointF) -> None:
		node_id = self._generate_node_id(node_type)
		try:
			node_model = create_node(node_type, node_id)
			self.graph.add_node(node_model)
		except ValueError as exc:
			self.message_posted.emit(f"无法创建节点: {exc}")
			return
		item = WorkflowNodeItem(node_id, node_model.title)
		item.setPos(pos - QPointF(item.WIDTH / 2, item.HEIGHT / 2))
		self.addItem(item)
		self.node_items[node_id] = item
		summary = self._format_node_summary(node_model.config)
		item.setToolTip(summary)
		self.message_posted.emit(f"已添加节点: {node_model.title}")

	def handle_port_press(self, port: NodePort) -> None:
		if port.kind != "output":
			return
		self._start_temp_connection(port, port.sceneBoundingRect().center())

	def _start_temp_connection(self, source_port: NodePort, cursor_pos: Optional[QPointF] = None) -> None:
		self._clear_temp_line()
		self._pending_output = source_port
		center = cursor_pos or source_port.sceneBoundingRect().center()
		self._temp_target_item = QGraphicsEllipseItem(-6, -6, 12, 12)
		self._temp_target_item.setBrush(Qt.BrushStyle.NoBrush)
		self._temp_target_item.setPen(
			QPen(QColor(180, 180, 180), 1, Qt.PenStyle.DashLine)
		)
		self._temp_target_item.setPos(center - QPointF(6, 6))
		self.addItem(self._temp_target_item)
		self._temp_connection = ConnectionItem(source_port, self._temp_target_item)
		self.addItem(self._temp_connection)
		self._set_hover_port(None)
		self._update_hover_port(center)

	def handle_port_release(self, port: NodePort) -> None:
		if self._pending_output is None or self._temp_connection is None:
			return
		source_port = self._pending_output
		target_port: Optional[NodePort] = None
		if port.kind == "input" and port is not source_port:
			target_port = port
		elif self._hover_port is not None and self._hover_port is not source_port:
			target_port = self._hover_port
		if target_port is None:
			self._clear_temp_line()
			return
		self._finalize_connection(target_port)

	def mouseReleaseEvent(self, event):  # noqa: D401
		if self._pending_output is not None and self._temp_connection is not None:
			target_port = self._find_nearest_input(event.scenePos(), self._pending_output)
			if target_port is not None:
				self._finalize_connection(target_port)
			else:
				self._clear_temp_line()
		super().mouseReleaseEvent(event)

	def mousePressEvent(self, event):  # noqa: D401
		if (
			event.button() == Qt.MouseButton.LeftButton
			and self._pending_output is None
		):
			transform = self.views()[0].transform() if self.views() else QTransform()
			item = self.itemAt(event.scenePos(), transform)
			if (
				isinstance(item, ConnectionItem)
				and isinstance(item.source, NodePort)
				and isinstance(item.target, NodePort)
			):
				source_port = cast(NodePort, item.source)
				target_port = cast(NodePort, item.target)
				source_node = cast(WorkflowNodeItem, source_port.parentItem()).node_id
				target_node = cast(WorkflowNodeItem, target_port.parentItem()).node_id
				self.graph.remove_edge(source_node, target_node)
				self.removeItem(item)
				if item in self.connections:
					self.connections.remove(item)
				self.message_posted.emit(f"重新连接 {source_node} -> {target_node}")
				self._start_temp_connection(source_port, event.scenePos())
				event.accept()
				return
		super().mousePressEvent(event)

	def _finalize_connection(self, target_port: NodePort) -> None:
		source_port = self._pending_output
		if source_port is None or self._temp_connection is None:
			return
		source_node = cast(WorkflowNodeItem, source_port.parentItem()).node_id
		target_node = cast(WorkflowNodeItem, target_port.parentItem()).node_id
		try:
			self.graph.add_edge(source_node, target_node)
		except ValueError as exc:
			self.message_posted.emit(str(exc))
			self._clear_temp_line()
			return
		connection = ConnectionItem(source_port, target_port)
		self.connections.append(connection)
		self.addItem(connection)
		self.message_posted.emit(f"已连接 {source_node} -> {target_node}")
		self._clear_temp_line()

	def _clear_temp_line(self) -> None:
		if self._temp_connection is not None:
			self.removeItem(self._temp_connection)
			self._temp_connection = None
		if self._temp_target_item is not None:
			self.removeItem(self._temp_target_item)
			self._temp_target_item = None
		self._pending_output = None
		self._set_hover_port(None)

	def _set_hover_port(self, port: Optional[NodePort]) -> None:
		if port is self._hover_port:
			return
		if self._hover_port is not None:
			self._hover_port.set_highlighted(False)
		self._hover_port = port
		if self._hover_port is not None:
			self._hover_port.set_highlighted(True)

	def _update_hover_port(self, pos: QPointF) -> None:
		if self._pending_output is None:
			self._set_hover_port(None)
			return
		threshold = 24.0
		best_port: Optional[NodePort] = None
		best_distance = threshold
		for item in self.node_items.values():
			candidate = item.input_port
			if candidate is self._pending_output:
				continue
			center = candidate.sceneBoundingRect().center()
			distance = math.hypot(center.x() - pos.x(), center.y() - pos.y())
			if distance <= best_distance:
				best_distance = distance
				best_port = candidate
		self._set_hover_port(best_port)

	def _find_nearest_input(
		self,
		pos: QPointF,
		source_port: Optional[NodePort],
		threshold: float = 24.0,
	) -> Optional[NodePort]:
		best_port: Optional[NodePort] = None
		best_distance = threshold
		for item in self.node_items.values():
			candidate = item.input_port
			if candidate is source_port:
				continue
			center = candidate.sceneBoundingRect().center()
			distance = math.hypot(center.x() - pos.x(), center.y() - pos.y())
			if distance <= best_distance:
				best_distance = distance
				best_port = candidate
		return best_port

	def mouseMoveEvent(self, event):  # noqa: D401
		if (
			self._pending_output
			and self._temp_connection
			and self._temp_target_item is not None
		):
			scene_pos = event.scenePos()
			self._update_hover_port(scene_pos)
			target_point = scene_pos
			if self._hover_port is not None:
				target_point = self._hover_port.sceneBoundingRect().center()
			pos = target_point - QPointF(6, 6)
			self._temp_target_item.setPos(pos)
			self._temp_connection.refresh_path()
		super().mouseMoveEvent(event)

	def refresh_connections(self, node_item: WorkflowNodeItem) -> None:
		for conn in list(self.connections):
			if conn.source.parentItem() == node_item or conn.target.parentItem() == node_item:
				conn.refresh_path()

	def delete_selection(self) -> None:
		for item in list(self.selectedItems()):
			if isinstance(item, ConnectionItem):
				self._remove_connection(item)
			elif isinstance(item, WorkflowNodeItem):
				self._remove_node(item.node_id)

	def _remove_node(self, node_id: str) -> None:
		item = self.node_items.get(node_id)
		if not item:
			return
		for conn in list(self.connections):
			if (
				cast(WorkflowNodeItem, conn.source.parentItem()).node_id
				== node_id
			):
				self._remove_connection(conn)
			elif (
				cast(WorkflowNodeItem, conn.target.parentItem()).node_id
				== node_id
			):
				self._remove_connection(conn)
		self.removeItem(item)
		del self.node_items[node_id]
		self.graph.remove_node(node_id)
		self.message_posted.emit(f"已删除节点 {node_id}")

	def _remove_connection(self, connection: ConnectionItem) -> None:
		source = cast(WorkflowNodeItem, connection.source.parentItem()).node_id
		target = cast(WorkflowNodeItem, connection.target.parentItem()).node_id
		self.graph.remove_edge(source, target)
		self.removeItem(connection)
		if connection in self.connections:
			self.connections.remove(connection)
		self.message_posted.emit(f"已断开 {source} -> {target}")

	def request_config(self, node_id: str) -> None:
		self.config_requested.emit(node_id)

	def update_node_tooltip(self, node_id: str) -> None:
		model = self.graph.nodes[node_id]
		item = self.node_items[node_id]
		item.setToolTip(self._format_node_summary(model.config))
		item.set_title(model.title)

	def _generate_node_id(self, node_type: str) -> str:
		base = node_type.split("_")[0]
		return f"{base}_{uuid.uuid4().hex[:6]}"

	@staticmethod
	def _format_node_summary(config: Dict[str, object]) -> str:
		parts = [f"{key}: {value}" for key, value in config.items()]
		return "\n".join(parts)


# -- Configuration dialog --------------------------------------------------


class ConfigDialog(ConfigDialogBase):
	"""Generic configuration dialog built from a node schema."""

	def __init__(self, node_model, parent: Optional[QWidget] = None) -> None:
		super().__init__(parent)
		self.node_model = node_model
		self.widgets: Dict[str, QWidget] = {}
		self._build_layout(node_model)

	def _build_layout(self, node_model) -> None:
		form_container = QWidget(self)
		form_layout = QFormLayout(form_container)
		form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
		for field in node_model.config_schema():
			widget = self._create_widget(field, node_model.config)
			label_text = field.get("label", field["key"])
			label_widget: QWidget
			if HAVE_FLUENT_WIDGETS:
				label_widget = SubtitleLabel(label_text, form_container)
			else:
				label_widget = QLabel(label_text, form_container)
			form_layout.addRow(label_widget, widget)
			self.widgets[field["key"]] = widget

		if HAVE_FLUENT_WIDGETS:
			self.titleLabel = SubtitleLabel(f"配置 {node_model.title}", self)
			fluent_self = cast(Any, self)
			fluent_self.viewLayout.addWidget(self.titleLabel)
			fluent_self.viewLayout.addWidget(form_container)
			if hasattr(fluent_self, "widget"):
				fluent_self.widget.setMinimumWidth(420)
			if hasattr(fluent_self, "yesButton"):
				fluent_self.yesButton.setText("保存")
			if hasattr(fluent_self, "cancelButton"):
				fluent_self.cancelButton.setText("取消")
		else:
			self.setWindowTitle(f"配置 {node_model.title}")
			layout = QVBoxLayout(self)
			layout.addWidget(form_container)
			buttons = QDialogButtonBox(
				QDialogButtonBox.StandardButton.Ok
				| QDialogButtonBox.StandardButton.Cancel
			)
			buttons.accepted.connect(self.accept)
			buttons.rejected.connect(self.reject)
			layout.addWidget(buttons)

	def _create_widget(self, field: Dict[str, Any], values: Dict[str, Any]) -> QWidget:
		key = cast(str, field["key"])
		value = values.get(key)
		ftype = cast(str, field.get("type", "str"))
		if ftype == "int":
			widget_cls = FluentSpinBox if HAVE_FLUENT_WIDGETS else QSpinBox
			widget = widget_cls(self)
			widget.setRange(
				int(cast(int, field.get("min", 0))),
				int(cast(int, field.get("max", 10000))),
			)
			widget.setValue(int(value) if value is not None else 0)
			return widget
		if ftype == "float":
			widget_cls = FluentDoubleSpinBox if HAVE_FLUENT_WIDGETS else QDoubleSpinBox
			widget = widget_cls(self)
			if hasattr(widget, "setDecimals"):
				widget.setDecimals(3)
			widget.setRange(
				float(cast(float, field.get("min", 0.0))),
				float(cast(float, field.get("max", 9999.0))),
			)
			widget.setSingleStep(float(cast(float, field.get("step", 0.1))))
			widget.setValue(float(value) if value is not None else 0.0)
			return widget
		if ftype == "choices":
			widget_cls = FluentComboBox if HAVE_FLUENT_WIDGETS else QComboBox
			widget = widget_cls(self)
			choices = cast(List[Tuple[str, str]], field.get("choices", []))
			for ident, label in choices:
				widget.addItem(label, ident)
			index = widget.findData(value)
			if index >= 0:
				widget.setCurrentIndex(index)
			return widget
		if ftype == "multiline":
			widget_cls = FluentTextEdit if HAVE_FLUENT_WIDGETS else QTextEdit
			widget = widget_cls(self)
			widget.setPlainText(str(value or ""))
			widget.setMinimumHeight(80)
			return widget
		widget_cls = FluentLineEdit if HAVE_FLUENT_WIDGETS else QLineEdit
		widget = widget_cls(self)
		widget.setText(str(value or ""))
		return widget

	def values(self) -> Dict[str, object]:
		result: Dict[str, object] = {}
		for key, widget in self.widgets.items():
			if isinstance(widget, QSpinBox):
				result[key] = widget.value()
			elif isinstance(widget, QDoubleSpinBox):
				result[key] = widget.value()
			elif isinstance(widget, QComboBox):
				result[key] = widget.currentData()
			elif isinstance(widget, QTextEdit):
				result[key] = widget.toPlainText()
			elif isinstance(widget, QLineEdit):
				result[key] = widget.text()
		return result


# -- Execution runner ------------------------------------------------------


class WorkflowRunner(QObject):
	"""Execute workflows in a worker-friendly wrapper."""

	started = Signal()
	finished = Signal(bool, str)

	def __init__(
		self,
		graph_supplier: Callable[[], WorkflowGraph],
		runtime_factory: Callable[[], AutomationRuntime] | None = None,
		parent: Optional[QObject] = None,
	) -> None:
		super().__init__(parent)
		self._graph_supplier = graph_supplier
		self._runtime_factory = runtime_factory or PyAutoGuiRuntime
		self._running = False

	def run(self) -> None:
		if self._running:
			return
		self._running = True
		self.started.emit()
		QApplication.processEvents()
		try:
			graph_copy = self._graph_supplier()
			executor = WorkflowExecutor(self._runtime_factory())
			executor.run(graph_copy)
		except ExecutionError as exc:
			self.finished.emit(False, str(exc))
		except Exception as exc:  # pragma: no cover - defensive
			self.finished.emit(False, f"执行失败: {exc}")
		else:
			self.finished.emit(True, "执行完成")
		finally:
			self._running = False


# -- Fluent workflow interface --------------------------------------------


class WorkflowInterface(QWidget):
	"""Workflow editor surface using Fluent UI components when available."""

	def __init__(self, parent: Optional[QWidget] = None) -> None:
		super().__init__(parent)

		self.scene = WorkflowScene(self)
		self.scene.message_posted.connect(self.append_log)
		self.scene.config_requested.connect(self.configure_node)

		self.node_palette = NodePalette(self)
		self.view = WorkflowView(self.scene, self)

		self.log_widget = QTextEdit(self)
		self.log_widget.setReadOnly(True)
		self.log_widget.setObjectName("workflowLog")

		right_panel = QWidget(self)
		right_layout = QVBoxLayout(right_panel)
		right_layout.setContentsMargins(16, 16, 16, 16)
		right_layout.setSpacing(12)
		self.run_button = PrimaryPushButton("运行工作流", right_panel)
		self.run_button.clicked.connect(self.execute_workflow)
		right_layout.addWidget(self.run_button)
		log_label = BodyLabel("日志", right_panel) if HAVE_FLUENT_WIDGETS else QLabel("日志", right_panel)
		right_layout.addWidget(log_label)
		right_layout.addWidget(self.log_widget, 1)

		splitter = QSplitter(self)
		splitter.addWidget(self.node_palette)
		splitter.addWidget(self.view)
		splitter.addWidget(right_panel)
		splitter.setStretchFactor(1, 1)
		splitter.setSizes([180, 720, 280])
		splitter.setChildrenCollapsible(False)

		layout = QVBoxLayout(self)
		layout.setContentsMargins(16, 16, 16, 16)
		layout.setSpacing(12)
		layout.addWidget(splitter, 1)

		self.status_bar = QStatusBar(self)
		self.status_bar.setSizeGripEnabled(False)
		self.status_bar.setObjectName("workflowStatusBar")
		layout.addWidget(self.status_bar)
		self.show_status("就绪")

		self.runner = WorkflowRunner(
			graph_supplier=self.scene.graph.copy,
			runtime_factory=PyAutoGuiRuntime,
			parent=self,
		)
		self.runner.started.connect(lambda: self.append_log("开始执行工作流"))
		self.runner.finished.connect(self.on_execution_finished)

	def append_log(self, message: str) -> None:
		timestamp = time.strftime("%H:%M:%S")
		self.log_widget.append(f"[{timestamp}] {message}")
		self.show_status(message)
		if INFOBAR_AVAILABLE:
			# Mirror log output in a Fluent info bar for quick visual feedback.
			info_bar_cls = cast(Any, InfoBar)
			position = cast(Any, InfoBarPosition.TOP_RIGHT)
			info_bar_cls.success(
				title="提示",
				content=message,
				orient=Qt.Orientation.Horizontal,
				isClosable=True,
				position=position,
				duration=2000,
				parent=self,
			)

	def configure_node(self, node_id: str) -> None:
		node_model = self.scene.graph.nodes.get(node_id)
		if node_model is None:
			return
		dialog = ConfigDialog(node_model, self)
		if not dialog.exec():
			return
		new_values = dialog.values()
		try:
			node_model.config.update(new_values)
			node_model.validate_config()
		except ValueError as exc:
			show_warning(self, "配置错误", str(exc))
			return
		self.scene.update_node_tooltip(node_id)
		self.append_log(f"已更新 {node_model.title} 配置")

	def execute_workflow(self) -> None:
		if not self.scene.graph.nodes:
			show_information(self, "运行工作流", "请先添加节点")
			return
		try:
			self.scene.graph.topological_order()
		except ExecutionError as exc:
			show_warning(self, "拓扑错误", str(exc))
			return
		self.run_button.setEnabled(False)
		self.runner.run()

	def on_execution_finished(self, success: bool, message: str) -> None:
		self.run_button.setEnabled(True)
		self.append_log(message)
		if not success:
			show_warning(self, "执行失败", message)

	def show_status(self, message: str, timeout_ms: int = 0) -> None:
		self.status_bar.showMessage(message, timeout_ms)


# -- Main window -----------------------------------------------------------


class MainWindow(BaseMainWindow):
	"""Top-level window hosting the workflow interface with Fluent styling."""

	def __init__(self) -> None:
		super().__init__()
		self.setWindowTitle("Workflow Capture Studio")
		self.resize(1200, 720)

		self.workflow_interface = WorkflowInterface(self)
		if HAVE_FLUENT_WIDGETS and isinstance(self, FluentWindow):
			self.workflow_interface.setObjectName("workflow-interface")
			icon = FluentIcon.HOME if FluentIcon is not None else QIcon()
			fluent_window = cast(Any, self)
			if hasattr(fluent_window, "addSubInterface"):
				fluent_window.addSubInterface(self.workflow_interface, icon, "工作流")
				if hasattr(fluent_window, "stackedWidget"):
					fluent_window.stackedWidget.setCurrentWidget(self.workflow_interface)
				navigation = getattr(fluent_window, "navigationInterface", None)
				if navigation is not None:
					navigation.setVisible(False)
			elif hasattr(fluent_window, "setCentralWidget"):
				fluent_window.setCentralWidget(self.workflow_interface)
			if hasattr(fluent_window, "setMicaEffectEnabled"):
				fluent_window.setMicaEffectEnabled(True)
		else:
			self.setCentralWidget(self.workflow_interface)


# -- Application entry point ----------------------------------------------


def main() -> None:
	configure_windows_dpi()
	app = QApplication(sys.argv)
	if HAVE_FLUENT_WIDGETS:
		app.setAttribute(Qt.ApplicationAttribute.AA_DontCreateNativeWidgetSiblings)
		if FluentTranslator is not None:
			translator = FluentTranslator()
			app.installTranslator(translator)
			setattr(app, "_fluent_translator", translator)
		if Theme is not None:
			setTheme(Theme.AUTO)
		setThemeColor(QColor(0, 120, 212), save=False)
	window = MainWindow()
	window.show()
	sys.exit(app.exec())


if __name__ == "__main__":
	main()