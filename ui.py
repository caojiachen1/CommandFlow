"""UI components for the desktop automation workflow editor.

This module contains all GUI-related classes and functions, separated from the main application logic.
"""

from __future__ import annotations

import json
import math
import sys
import time
import uuid
from collections import defaultdict
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, cast

from automation_runtime import PyAutoGuiRuntime, get_system_dpi_scale
from PySide6.QtCore import QPoint, QPointF, Qt, QMimeData, QObject, Signal, QRectF, QLineF, QRect, QSizeF, QSize
from PySide6.QtGui import (
	QColor,
	QDrag,
	QIcon,
	QLinearGradient,
	QPainter,
	QPainterPath,
	QPen,
	QTransform,
	QFont,
	QTextOption,
)
from PySide6.QtWidgets import (
	QApplication,
	QComboBox,
	QDialog,
	QDialogButtonBox,
	QDoubleSpinBox,
	QFileDialog,
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
	QFrame,
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
	WorkflowNodeModel,
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

			ctypes.windll.user32.SetProcessDpiAware()
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

	HEADER_ROLE = Qt.ItemDataRole.UserRole + 1
	CATEGORY_NAME_ROLE = Qt.ItemDataRole.UserRole + 2
	CATEGORY_ORDER = (
		"鼠标操作",
		"键盘操作",
		"图像识别",
		"流程控制",
		"系统操作",
		"其他",
	)

	def __init__(self, parent: Optional[QWidget] = None) -> None:
		super().__init__(parent)
		self.setObjectName("nodePalette")
		self.setDragEnabled(True)
		self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
		self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
		self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
		self.setFrameShape(QFrame.Shape.NoFrame)
		self.setSpacing(4)
		self.setUniformItemSizes(True)
		self._category_headers: Dict[str, QListWidgetItem] = {}
		self._category_nodes: Dict[str, List[QListWidgetItem]] = {}
		self._category_collapsed: Dict[str, bool] = {}
		self._apply_palette_style()
		self.populate()

	def populate(self) -> None:
		self.clear()
		self._category_headers.clear()
		self._category_nodes.clear()
		self._category_collapsed.clear()
		nodes_by_category: Dict[str, List[WorkflowNodeModel]] = defaultdict(list)
		for node in iter_registry():
			category = getattr(node, "category", "其他") or "其他"
			nodes_by_category[category].append(node)
		if not nodes_by_category:
			return
		ordered_categories = [cat for cat in self.CATEGORY_ORDER if cat in nodes_by_category]
		remaining = sorted(cat for cat in nodes_by_category if cat not in self.CATEGORY_ORDER)
		for category in ordered_categories + remaining:
			nodes = sorted(nodes_by_category[category], key=lambda item: item.display_name.lower())
			if not nodes:
				continue
			header = self._add_category_header(category)
			self._category_headers[category] = header
			self._category_nodes[category] = []
			for node in nodes:
				item = QListWidgetItem(f"    {node.display_name}")
				item.setData(Qt.ItemDataRole.UserRole, node.type_name)
				item.setData(self.HEADER_ROLE, False)
				item.setData(self.CATEGORY_NAME_ROLE, category)
				self.addItem(item)
				self._category_nodes[category].append(item)
			self._set_category_collapsed(category, True, force=True)

	def _add_category_header(self, category: str) -> QListWidgetItem:
		header = QListWidgetItem(category)
		header.setFlags(Qt.ItemFlag.ItemIsEnabled)
		header_font = QFont(self.font())
		header_font.setBold(True)
		header.setFont(header_font)
		header.setData(Qt.ItemDataRole.UserRole, None)
		header.setData(self.HEADER_ROLE, True)
		header.setData(self.CATEGORY_NAME_ROLE, category)
		header.setForeground(QColor(210, 210, 210))
		header.setData(Qt.ItemDataRole.BackgroundRole, QColor(45, 45, 45))
		header.setData(Qt.ItemDataRole.SizeHintRole, QSize(header.sizeHint().width(), header.sizeHint().height() + 6))
		self.addItem(header)
		return header

	def _set_category_collapsed(self, category: str, collapsed: bool, *, force: bool = False) -> None:
		if not force and self._category_collapsed.get(category) == collapsed:
			return
		header = self._category_headers.get(category)
		if header is not None:
			indicator = "▶" if collapsed else "▼"
			header.setText(f"{indicator} {category}")
		self._category_collapsed[category] = collapsed
		items = self._category_nodes.get(category, [])
		if collapsed:
			if self.currentItem() in items:
				self.clearSelection()
		for item in items:
			item.setHidden(collapsed)

	def _toggle_category(self, category: str) -> None:
		current = self._category_collapsed.get(category, True)
		self._set_category_collapsed(category, not current)

	def mousePressEvent(self, event):  # noqa: D401
		item = self.itemAt(event.pos())
		if item is not None and item.data(self.HEADER_ROLE):
			category = item.data(self.CATEGORY_NAME_ROLE)
			if category:
				self._toggle_category(str(category))
			event.accept()
			return
		super().mousePressEvent(event)

	def _apply_palette_style(self) -> None:
		self.setStyleSheet(
			"""
			QListWidget#nodePalette {
				background: #1e1e1e;
				border: none;
				padding: 8px;
				color: #e6e6e6;
			}
			QListWidget#nodePalette::item {
				padding: 8px 10px;
				margin: 2px 0;
				border-radius: 6px;
			}
			QListWidget#nodePalette::item:selected {
				background: rgba(255, 255, 255, 40);
				color: #f2f2f2;
			}
			QListWidget#nodePalette::item:hover {
				background: rgba(255, 255, 255, 20);
			}
			"""
		)

	def startDrag(self, supported_actions: Qt.DropAction) -> None:  # noqa: N802
		item = self.currentItem()
		if item is None or item.data(self.HEADER_ROLE):
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

	zoomChanged = Signal(float)

	def __init__(self, scene: "WorkflowScene", parent: Optional[QWidget] = None) -> None:
		super().__init__(scene, parent)
		self.setAcceptDrops(True)
		self.setRenderHints(
			self.renderHints() | QPainter.RenderHint.Antialiasing
		)
		self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
		self.setViewportUpdateMode(
			QGraphicsView.ViewportUpdateMode.FullViewportUpdate
		)
		self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
		self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
		self._panning = False
		self._pan_start = QPoint()
		self._pan_scroll_start = QPoint()
		self._zoom = 1.0
		self._min_zoom = 0.05
		self._max_zoom = 3.0
		self._zoom_step = 1.15

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

	def resizeEvent(self, event):  # noqa: D401
		scene_obj = self.scene()
		if scene_obj is not None:
			scene = cast("WorkflowScene", scene_obj)
			scene.handle_view_resize(QSizeF(self.viewport().size()))
		super().resizeEvent(event)

	def wheelEvent(self, event):  # noqa: D401
		delta = event.angleDelta().y()
		if delta == 0:
			pixel_delta = event.pixelDelta()
			delta = pixel_delta.y() if not pixel_delta.isNull() else 0
		if delta == 0:
			event.ignore()
			return
		steps = delta / 120.0
		factor = math.pow(self._zoom_step, steps)
		if self._apply_zoom(factor):
			event.accept()
		else:
			event.ignore()

	def _apply_zoom(self, factor: float) -> bool:
		if factor <= 0:
			return False
		new_zoom = self._zoom * factor
		if new_zoom < self._min_zoom:
			factor = self._min_zoom / self._zoom
			new_zoom = self._min_zoom
		elif new_zoom > self._max_zoom:
			factor = self._max_zoom / self._zoom
			new_zoom = self._max_zoom
		if math.isclose(new_zoom, self._zoom, rel_tol=1e-4):
			return False
		self.scale(factor, factor)
		self._zoom = new_zoom
		self.zoomChanged.emit(self._zoom)
		return True

	def zoom_in(self) -> bool:
		return self._apply_zoom(self._zoom_step)

	def zoom_out(self) -> bool:
		return self._apply_zoom(1.0 / self._zoom_step)

	def reset_zoom(self) -> None:
		self.resetTransform()
		self._zoom = 1.0
		self.zoomChanged.emit(self._zoom)

	def fit_to_rect(self, rect: QRectF, padding: float = 40.0) -> None:
		if rect.isNull() or rect.width() <= 0 or rect.height() <= 0:
			self.reset_zoom()
			return
		padded = rect.adjusted(-padding, -padding, padding, padding)
		self.resetTransform()
		self.fitInView(padded, Qt.AspectRatioMode.KeepAspectRatio)
		self._zoom = self.transform().m11()
		self.zoomChanged.emit(self._zoom)

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
		self._default_color = QColor(90, 90, 90)
		self._hover_color = QColor(130, 130, 130)
		self._highlight_color = QColor(200, 200, 200)
		self.setBrush(self._default_color)
		pen = QPen(QColor(45, 45, 45), 1.4)
		pen.setCosmetic(True)
		self.setPen(pen)
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
		pen = QPen(QColor(120, 120, 120), 3)
		pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
		pen.setCapStyle(Qt.PenCapStyle.RoundCap)
		self.setPen(pen)
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
		self.setBrush(Qt.BrushStyle.NoBrush)
		self.setPen(Qt.PenStyle.NoPen)
		self.setAcceptHoverEvents(True)
		self._hovered = False
		self._base_color = QColor(53, 53, 53)
		self._accent_color = QColor(90, 90, 90)
		self._paint_margin = 8.0
		self.title_item = QGraphicsTextItem(title, self)
		title_font = QFont(self.title_item.font())
		title_font.setPointSizeF(title_font.pointSizeF() + 1.5)
		title_font.setBold(True)
		self.title_item.setFont(title_font)
		self.title_item.setDefaultTextColor(QColor(220, 220, 220))
		self.title_item.setPos(20, 14)
		self.title_item.setZValue(1)
		self.input_port = NodePort(self, "input")
		self.output_port = NodePort(self, "output")
		self.update_ports()

	def update_ports(self) -> None:
		self.input_port.setPos(0, self.HEIGHT / 2 - 6)
		self.output_port.setPos(self.WIDTH - 1, self.HEIGHT / 2 - 6)

	def set_title(self, title: str) -> None:
		self.title_item.setPlainText(title)

	def itemChange(self, change, value):  # noqa: D401
		if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
			scene_obj = self.scene()
			if scene_obj is not None:
				scene = cast(WorkflowScene, scene_obj)
				scene.refresh_connections(self)
				scene.ensure_scene_visible(self)
				scene.modified.emit()
		return super().itemChange(change, value)

	def mouseDoubleClickEvent(self, event):  # noqa: D401
		scene = cast(WorkflowScene, self.scene())
		scene.request_config(self.node_id)
		super().mouseDoubleClickEvent(event)

	def mousePressEvent(self, event):  # noqa: D401
		scene = self.scene()
		if scene is not None:
			scene_obj = cast(WorkflowScene, scene)
			scene_obj._promote_node(self)
		super().mousePressEvent(event)

	def hoverEnterEvent(self, event):  # noqa: D401
		self._hovered = True
		self.update()
		super().hoverEnterEvent(event)

	def hoverLeaveEvent(self, event):  # noqa: D401
		self._hovered = False
		self.update()
		super().hoverLeaveEvent(event)

	def boundingRect(self) -> QRectF:  # noqa: D401
		rect = super().boundingRect()
		margin = self._paint_margin
		return rect.adjusted(-margin, -margin, margin, margin)

	def paint(self, painter: QPainter, option, widget=None):  # noqa: D401
		painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
		inner_rect = self.rect().adjusted(1, 1, -1, -1)
		body_path = QPainterPath()
		body_path.addRoundedRect(inner_rect, 18, 18)
		painter.fillPath(body_path, self._base_color)
		border_color = QColor(90, 90, 90)
		if self._hovered:
			border_color = QColor(140, 140, 140)
		if self.isSelected():
			border_color = QColor(210, 210, 210)
		border_pen = QPen(border_color, 2.2)
		border_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
		painter.setPen(border_pen)
		painter.drawPath(body_path)

		header_rect = QRectF(inner_rect.left() + 8, inner_rect.top() + 8, inner_rect.width() - 16, 34)
		header_path = QPainterPath()
		header_path.addRoundedRect(header_rect, 10, 10)
		clip_path = QPainterPath()
		clip_path.addRoundedRect(inner_rect, 18, 18)
		header_clip = clip_path.intersected(header_path)
		painter.save()
		painter.setClipPath(header_clip)
		painter.fillPath(header_path, self._accent_color)
		painter.restore()

		glow_color = QColor(140, 140, 140, 90)
		if self.isSelected():
			glow_color = QColor(210, 210, 210, 130)
		if self._hovered or self.isSelected():
			glow_pen = QPen(glow_color, 6)
			glow_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
			painter.setPen(glow_pen)
			painter.drawPath(body_path)
		painter.setPen(Qt.PenStyle.NoPen)


# -- Workflow scene --------------------------------------------------------


class WorkflowScene(QGraphicsScene):
	"""Scene graph bridging the core workflow model with graphics items."""

	message_posted = Signal(str)
	config_requested = Signal(str)
	modified = Signal()

	def __init__(self, parent: Optional[QObject] = None) -> None:
		super().__init__(parent)
		self.graph = WorkflowGraph()
		self.node_items: Dict[str, WorkflowNodeItem] = {}
		self.connections: List[ConnectionItem] = []
		self._pending_output: Optional[NodePort] = None
		self._temp_connection: Optional[ConnectionItem] = None
		self._temp_target_item: Optional[QGraphicsEllipseItem] = None
		self._hover_port: Optional[NodePort] = None
		self._z_counter = 0
		self._scene_threshold = 220.0
		self._scene_growth_step = 800.0
		self._default_scene_rect = QRectF(-400, -300, 800, 600)
		self.setSceneRect(self._default_scene_rect)

	def drawBackground(self, painter: QPainter, rect: QRectF | QRect) -> None:  # noqa: D401
		grid_step = 28
		major_every = 4
		rectf = QRectF(rect)
		painter.fillRect(rectf, QColor(30, 30, 30))
		left = int(math.floor(rectf.left() / grid_step) * grid_step)
		right = int(math.ceil(rectf.right() / grid_step) * grid_step)
		top = int(math.floor(rectf.top() / grid_step) * grid_step)
		bottom = int(math.ceil(rectf.bottom() / grid_step) * grid_step)
		minor_pen = QPen(QColor(60, 60, 60), 1)
		minor_pen.setCosmetic(True)
		major_pen = QPen(QColor(90, 90, 90), 1.4)
		major_pen.setCosmetic(True)
		vertical_minor: List[QLineF] = []
		vertical_major: List[QLineF] = []
		horizontal_minor: List[QLineF] = []
		horizontal_major: List[QLineF] = []
		x = left
		index = 0
		while x <= right:
			line = QLineF(x, top, x, bottom)
			if index % major_every == 0:
				vertical_major.append(line)
			else:
				vertical_minor.append(line)
			x += grid_step
			index += 1
		y = top
		index = 0
		while y <= bottom:
			line = QLineF(left, y, right, y)
			if index % major_every == 0:
				horizontal_major.append(line)
			else:
				horizontal_minor.append(line)
			y += grid_step
			index += 1
		if vertical_minor or horizontal_minor:
			painter.setPen(minor_pen)
			if vertical_minor:
				painter.drawLines(vertical_minor)
			if horizontal_minor:
				painter.drawLines(horizontal_minor)
		if vertical_major or horizontal_major:
			painter.setPen(major_pen)
			if vertical_major:
				painter.drawLines(vertical_major)
			if horizontal_major:
				painter.drawLines(horizontal_major)

	def _expand_scene_for_rect(self, item_rect: QRectF) -> bool:
		rect = self.sceneRect()
		threshold = self._scene_threshold
		step = self._scene_growth_step
		expand_left = step if item_rect.left() < rect.left() + threshold else 0.0
		expand_right = step if item_rect.right() > rect.right() - threshold else 0.0
		expand_top = step if item_rect.top() < rect.top() + threshold else 0.0
		expand_bottom = step if item_rect.bottom() > rect.bottom() - threshold else 0.0
		if not any((expand_left, expand_right, expand_top, expand_bottom)):
			return False
		new_rect = QRectF(
			rect.left() - expand_left,
			rect.top() - expand_top,
			rect.width() + expand_left + expand_right,
			rect.height() + expand_top + expand_bottom,
		)
		self.setSceneRect(new_rect)
		return True

	def ensure_scene_visible(self, item: WorkflowNodeItem) -> None:
		item_rect = item.sceneBoundingRect()
		self._expand_scene_for_rect(item_rect)

	def _update_scene_metrics(self, width: float, height: float) -> None:
		short_edge = max(min(width, height), 200.0)
		long_edge = max(max(width, height), 300.0)
		self._scene_threshold = min(220.0, short_edge * 0.3)
		self._scene_growth_step = max(400.0, long_edge * 0.5)

	def handle_view_resize(self, size: QSizeF) -> None:
		width = float(size.width())
		height = float(size.height())
		if width <= 0 or height <= 0:
			return
		self._update_scene_metrics(width, height)
		default_rect = QRectF(-width / 2.0, -height / 2.0, width, height)
		self._default_scene_rect = default_rect
		if not self.node_items:
			self.setSceneRect(default_rect)

	def _recalculate_scene_rect(self) -> None:
		if not self.node_items:
			self.setSceneRect(QRectF(self._default_scene_rect))
			return
		items_rect = self.itemsBoundingRect()
		padding = max(self._scene_threshold, self._scene_growth_step * 0.5)
		expanded = items_rect.adjusted(-padding, -padding, padding, padding)
		default_rect = self._default_scene_rect
		left = min(expanded.left(), default_rect.left())
		top = min(expanded.top(), default_rect.top())
		right = max(expanded.right(), default_rect.right())
		bottom = max(expanded.bottom(), default_rect.bottom())
		final_rect = QRectF(QPointF(left, top), QPointF(right, bottom))
		self.setSceneRect(final_rect)
		self._update_scene_metrics(final_rect.width(), final_rect.height())

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
		self._promote_node(item)
		summary = self._format_node_summary(node_model.config)
		item.setToolTip(summary)
		self.ensure_scene_visible(item)
		self.message_posted.emit(f"已添加节点: {node_model.title}")
		self.modified.emit()

	def _promote_node(self, item: "WorkflowNodeItem") -> None:
		self._z_counter += 1
		item.setZValue(float(self._z_counter))

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
		self.modified.emit()

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
		self._recalculate_scene_rect()
		self.modified.emit()

	def _remove_connection(self, connection: ConnectionItem) -> None:
		source = cast(WorkflowNodeItem, connection.source.parentItem()).node_id
		target = cast(WorkflowNodeItem, connection.target.parentItem()).node_id
		self.graph.remove_edge(source, target)
		self.removeItem(connection)
		if connection in self.connections:
			self.connections.remove(connection)
		self.message_posted.emit(f"已断开 {source} -> {target}")
		self.modified.emit()

	def request_config(self, node_id: str) -> None:
		self.config_requested.emit(node_id)

	def update_node_tooltip(self, node_id: str) -> None:
		model = self.graph.nodes[node_id]
		item = self.node_items[node_id]
		item.setToolTip(self._format_node_summary(model.config))
		item.set_title(model.title)
		self._promote_node(item)
		self.modified.emit()

	def _generate_node_id(self, node_type: str) -> str:
		base = node_type.split("_")[0]
		return f"{base}_{uuid.uuid4().hex[:6]}"

	@staticmethod
	def _format_node_summary(config: Dict[str, object]) -> str:
		parts = [f"{key}: {value}" for key, value in config.items()]
		return "\n".join(parts)

	def clear_workflow(self, notify: bool = True, mark_modified: bool = True) -> None:
		self._clear_temp_line()
		self.clear()
		self.graph = WorkflowGraph()
		self.node_items.clear()
		self.connections.clear()
		self._pending_output = None
		self._temp_connection = None
		self._temp_target_item = None
		self._hover_port = None
		self._z_counter = 0
		self.setSceneRect(QRectF(self._default_scene_rect))
		if notify:
			self.message_posted.emit("工作流已清空")
		if mark_modified:
			self.modified.emit()

	def export_workflow(self) -> Dict[str, Any]:
		nodes: List[Dict[str, Any]] = []
		for node_id, node_model in self.graph.nodes.items():
			item = self.node_items.get(node_id)
			if item is None:
				continue
			pos = item.pos()
			nodes.append(
				{
					"id": node_id,
					"type": node_model.type_name,
					"title": node_model.title,
					"config": dict(node_model.config),
					"position": {"x": float(pos.x()), "y": float(pos.y())},
				}
			)
		edges: List[Dict[str, str]] = []
		for source, targets in self.graph.edges.items():
			for target in targets:
				edges.append({"source": source, "target": target})
		return {"schema": 1, "nodes": nodes, "edges": edges}

	def import_workflow(self, data: Dict[str, Any], *, mark_modified: bool = False) -> None:
		nodes_data = cast(List[Dict[str, Any]], data.get("nodes", []))
		edges_data = cast(List[Dict[str, Any]], data.get("edges", []))
		self.clear_workflow(notify=False, mark_modified=False)
		for entry in nodes_data:
			node_id = cast(str, entry.get("id"))
			node_type = cast(str, entry.get("type"))
			if not node_id or not node_type:
				continue
			try:
				node_model = create_node(node_type, node_id)
			except ValueError as exc:
				self.message_posted.emit(f"忽略节点 {node_id}: {exc}")
				continue
			title = cast(str, entry.get("title", node_model.title))
			config_values = entry.get("config", {})
			if isinstance(config_values, dict):
				node_model.config.update(config_values)
			try:
				node_model.validate_config()
			except ValueError as exc:
				self.message_posted.emit(f"节点 {node_id} 配置无效: {exc}")
				continue
			node_model.title = title
			self.graph.add_node(node_model)
			item = WorkflowNodeItem(node_id, node_model.title)
			position = entry.get("position", {})
			x_val = float(position.get("x", 0.0)) if isinstance(position, dict) else 0.0
			y_val = float(position.get("y", 0.0)) if isinstance(position, dict) else 0.0
			item.setPos(QPointF(x_val, y_val))
			self.addItem(item)
			self.node_items[node_id] = item
			self._promote_node(item)
			summary = self._format_node_summary(node_model.config)
			item.setToolTip(summary)
		for entry in edges_data:
			source = cast(str, entry.get("source"))
			target = cast(str, entry.get("target"))
			if not source or not target:
				continue
			if source not in self.node_items or target not in self.node_items:
				self.message_posted.emit(f"忽略无效连接 {source} -> {target}")
				continue
			try:
				self.graph.add_edge(source, target)
			except ValueError as exc:
				self.message_posted.emit(f"连接 {source} -> {target} 失败: {exc}")
				continue
			connection = ConnectionItem(
				self.node_items[source].output_port,
				self.node_items[target].input_port,
			)
			self.connections.append(connection)
			self.addItem(connection)
		self._recalculate_scene_rect()
		if mark_modified:
			self.modified.emit()


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
		self._current_workflow_path: Optional[Path] = None
		self._unsaved_changes = False
		self._last_directory = str(Path.home())
		self._workflow_filter = "JSON Files (*.json);;All Files (*.*)"
		self._window_base_title = "Command Flow Studio"

		self.scene = WorkflowScene(self)
		self.scene.message_posted.connect(self.append_log)
		self.scene.config_requested.connect(self.configure_node)

		self.node_palette = NodePalette(self)
		self.node_palette.setMinimumWidth(220)
		self.view = WorkflowView(self.scene, self)
		self.view.setObjectName("workflowView")
		self.view.setFrameShape(QFrame.Shape.NoFrame)
		self.view.setStyleSheet("QGraphicsView#workflowView { background: transparent; border: none; }")
		self.view.zoomChanged.connect(lambda value: self.show_status(f"缩放 {value * 100:.0f}%"))

		self.log_widget = QTextEdit(self)
		self.log_widget.setReadOnly(True)
		self.log_widget.setObjectName("workflowLog")
		log_font = QFont(self.log_widget.font())
		log_font.setFamily("Consolas")
		log_font.setPointSizeF(max(log_font.pointSizeF(), 9.5))
		self.log_widget.setFont(log_font)
		self.log_widget.setWordWrapMode(QTextOption.WrapMode.NoWrap)

		right_panel = QWidget(self)
		right_panel.setObjectName("workflowSidePanel")
		right_layout = QVBoxLayout(right_panel)
		right_layout.setContentsMargins(16, 16, 16, 16)
		right_layout.setSpacing(12)
		file_controls_top = QHBoxLayout()
		file_controls_top.setContentsMargins(0, 0, 0, 0)
		file_controls_top.setSpacing(8)
		self.new_workflow_button = self._make_side_button("新建", right_panel)
		self.new_workflow_button.clicked.connect(self.new_workflow)
		file_controls_top.addWidget(self.new_workflow_button)
		self.open_workflow_button = self._make_side_button("打开", right_panel)
		self.open_workflow_button.clicked.connect(self.open_workflow)
		file_controls_top.addWidget(self.open_workflow_button)
		self.save_workflow_button = self._make_side_button("保存", right_panel)
		self.save_workflow_button.clicked.connect(self.save_workflow)
		file_controls_top.addWidget(self.save_workflow_button)
		right_layout.addLayout(file_controls_top)
		file_controls_bottom = QHBoxLayout()
		file_controls_bottom.setContentsMargins(0, 0, 0, 0)
		file_controls_bottom.setSpacing(8)
		self.save_as_workflow_button = self._make_side_button("另存为", right_panel)
		self.save_as_workflow_button.clicked.connect(self.save_workflow_as)
		file_controls_bottom.addWidget(self.save_as_workflow_button)
		self.delete_workflow_button = self._make_side_button("删除", right_panel)
		self.delete_workflow_button.clicked.connect(self.delete_workflow_file)
		file_controls_bottom.addWidget(self.delete_workflow_button)
		file_controls_bottom.addStretch(1)
		right_layout.addLayout(file_controls_bottom)
		self.file_label = BodyLabel("当前文件: 未命名", right_panel) if HAVE_FLUENT_WIDGETS else QLabel("当前文件: 未命名", right_panel)
		right_layout.addWidget(self.file_label)
		self.run_button = PrimaryPushButton("运行工作流", right_panel)
		self.run_button.clicked.connect(self.execute_workflow)
		self.run_button.setCursor(Qt.CursorShape.PointingHandCursor)
		self.run_button.setMinimumHeight(40)
		right_layout.addWidget(self.run_button)
		zoom_controls = QHBoxLayout()
		zoom_controls.setContentsMargins(0, 0, 0, 0)
		zoom_controls.setSpacing(8)
		# Provide quick zoom operations alongside the primary run control.
		self.zoom_out_button = self._make_side_button("缩小", right_panel)
		self.zoom_out_button.clicked.connect(self.handle_zoom_out)
		zoom_controls.addWidget(self.zoom_out_button)
		self.zoom_reset_button = self._make_side_button("重置", right_panel)
		self.zoom_reset_button.clicked.connect(self.handle_zoom_reset)
		zoom_controls.addWidget(self.zoom_reset_button)
		self.zoom_in_button = self._make_side_button("放大", right_panel)
		self.zoom_in_button.clicked.connect(self.handle_zoom_in)
		zoom_controls.addWidget(self.zoom_in_button)
		self.zoom_fit_button = self._make_side_button("适配", right_panel)
		self.zoom_fit_button.clicked.connect(self.fit_workflow_to_nodes)
		zoom_controls.addWidget(self.zoom_fit_button)
		right_layout.addLayout(zoom_controls)
		log_label = BodyLabel("日志", right_panel) if HAVE_FLUENT_WIDGETS else QLabel("日志", right_panel)
		log_header = QHBoxLayout()
		log_header.setContentsMargins(0, 0, 0, 0)
		log_header.setSpacing(8)
		log_header.addWidget(log_label)
		log_header.addStretch(1)
		self.clear_log_button = self._make_side_button("清空日志", right_panel)
		self.clear_log_button.clicked.connect(self.clear_log)
		log_header.addWidget(self.clear_log_button)
		right_layout.addLayout(log_header)
		right_layout.addWidget(self.log_widget, 1)

		splitter = QSplitter(self)
		splitter.addWidget(self.node_palette)
		splitter.addWidget(self.view)
		splitter.addWidget(right_panel)
		splitter.setStretchFactor(1, 1)
		splitter.setSizes([180, 720, 280])
		splitter.setChildrenCollapsible(False)
		splitter.setHandleWidth(4)

		layout = QVBoxLayout(self)
		layout.setContentsMargins(16, 16, 16, 16)
		layout.setSpacing(12)
		layout.addWidget(splitter, 1)

		self.status_bar = QStatusBar(self)
		self.status_bar.setSizeGripEnabled(False)
		self.status_bar.setObjectName("workflowStatusBar")
		layout.addWidget(self.status_bar)
		self._dpi_scale = get_system_dpi_scale()
		self.show_status(f"就绪 (DPI缩放 {self._dpi_scale:.2f}x)")
		self.setObjectName("workflowInterfaceRoot")
		self._apply_styles()

		self.runner = WorkflowRunner(
			graph_supplier=lambda: self.scene.graph.copy(),
			runtime_factory=lambda: PyAutoGuiRuntime(dpi_scale=self._dpi_scale),
			parent=self,
		)
		self.runner.started.connect(lambda: self.append_log("开始执行工作流"))
		self.runner.finished.connect(self.on_execution_finished)
		self.scene.modified.connect(self.mark_workflow_modified)
		self.update_file_display()

	def _make_side_button(self, text: str, parent: QWidget) -> QPushButton:
		button = QPushButton(text, parent)
		button.setObjectName("workflowSideButton")
		button.setCursor(Qt.CursorShape.PointingHandCursor)
		button.setMinimumHeight(32)
		button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
		return button

	def handle_zoom_in(self) -> None:
		if not self.view.zoom_in():
			self.show_status("已达到最大缩放", 2000)

	def handle_zoom_out(self) -> None:
		if not self.view.zoom_out():
			self.show_status("已达到最小缩放", 2000)

	def handle_zoom_reset(self) -> None:
		self.view.reset_zoom()
		self.show_status("缩放已重置", 2000)

	def fit_workflow_to_nodes(self) -> None:
		if not self.scene.node_items:
			self.view.reset_zoom()
			self.show_status("没有可适配的节点，已重置缩放", 2000)
			return
		bounds = self.scene.itemsBoundingRect()
		if bounds.isNull() or bounds.width() <= 0 or bounds.height() <= 0:
			self.view.reset_zoom()
			self.show_status("视图已重置", 2000)
			return
		self.view.fit_to_rect(bounds)
		self.show_status("视图已适配当前工作流", 2000)

	def clear_log(self) -> None:
		self.log_widget.clear()
		self.show_status("日志已清空", 2000)

	def set_base_window_title(self, title: str) -> None:
		clean_title = (title or "").strip()
		if clean_title.endswith(" *"):
			clean_title = clean_title[:-2]
		placeholder = " - 未命名"
		if clean_title.endswith(placeholder):
			clean_title = clean_title[: -len(placeholder)]
		if clean_title:
			self._window_base_title = clean_title
		self.update_file_display()

	def update_file_display(self) -> None:
		filename = self._current_workflow_path.name if self._current_workflow_path else "未命名"
		suffix = "*" if self._unsaved_changes else ""
		self.file_label.setText(f"当前文件: {filename}{suffix}")
		self.delete_workflow_button.setEnabled(self._current_workflow_path is not None)
		self.update_window_title()

	def update_window_title(self) -> None:
		window = self.window()
		if window is None or not hasattr(window, "setWindowTitle"):
			return
		if self._current_workflow_path is not None:
			title = f"{self._window_base_title} - {self._current_workflow_path.name}"
		else:
			title = f"{self._window_base_title} - 未命名"
		if self._unsaved_changes:
			title += " *"
		window.setWindowTitle(title)

	def mark_workflow_modified(self) -> None:
		state_changed = not self._unsaved_changes
		self._unsaved_changes = True
		if state_changed:
			self.update_file_display()
			self.show_status("工作流已修改", 2000)

	def confirm_discard_changes(self) -> bool:
		if not self._unsaved_changes:
			return True
		reply = QMessageBox.question(
			self,
			"放弃未保存的更改",
			"当前工作流存在未保存的更改，是否继续？",
			QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
			QMessageBox.StandardButton.No,
		)
		return reply == QMessageBox.StandardButton.Yes

	def new_workflow(self) -> None:
		if not self.confirm_discard_changes():
			return
		self.scene.clear_workflow(notify=True, mark_modified=False)
		self._current_workflow_path = None
		self._unsaved_changes = False
		self.update_file_display()
		self.append_log("已创建新的空白工作流")

	def open_workflow(self) -> None:
		if not self.confirm_discard_changes():
			return
		file_path, _ = QFileDialog.getOpenFileName(
			self,
			"打开工作流",
			self._last_directory,
			self._workflow_filter,
		)
		if not file_path:
			return
		path = Path(file_path)
		try:
			with path.open("r", encoding="utf-8") as handle:
				data = json.load(handle)
		except (OSError, JSONDecodeError) as exc:
			show_warning(self, "打开失败", f"无法读取文件: {exc}")
			return
		self.scene.import_workflow(data, mark_modified=False)
		self._current_workflow_path = path
		self._last_directory = str(path.parent)
		self._unsaved_changes = False
		self.update_file_display()
		self.append_log(f"已打开工作流: {path.name}")

	def save_workflow(self) -> None:
		if self._current_workflow_path is None:
			self.save_workflow_as()
			return
		self._save_to_path(self._current_workflow_path)

	def save_workflow_as(self) -> None:
		directory = Path(self._last_directory)
		if not directory.exists():
			directory = Path.home()
		default_name = (
			self._current_workflow_path.name
			if self._current_workflow_path is not None
			else "workflow.json"
		)
		initial_path = str((directory / default_name).resolve())
		file_path, _ = QFileDialog.getSaveFileName(
			self,
			"另存工作流",
			initial_path,
			self._workflow_filter,
		)
		if not file_path:
			return
		path = Path(file_path)
		if path.suffix == "":
			path = path.with_suffix(".json")
		if self._save_to_path(path):
			self._current_workflow_path = path

	def delete_workflow_file(self) -> None:
		if self._current_workflow_path is None:
			show_information(self, "删除工作流", "当前没有关联的工作流文件")
			return
		path = self._current_workflow_path
		if HAVE_FLUENT_WIDGETS and MessageBox is not None:
			box = MessageBox("删除工作流", f"确定要删除文件 {path.name} 吗？", self)
			if hasattr(box, "yesButton"):
				box.yesButton.setText("删除")
			if hasattr(box, "cancelButton"):
				box.cancelButton.setText("取消")
				box.cancelButton.show()
			if box.exec() != QDialog.DialogCode.Accepted:
				return
		else:
			reply = QMessageBox.question(
				self,
				"删除工作流",
				f"确定要删除文件 {path.name} 吗？",
				QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
				QMessageBox.StandardButton.No,
			)
			if reply != QMessageBox.StandardButton.Yes:
				return
		file_existed = path.exists()
		try:
			if file_existed:
				path.unlink()
		except OSError as exc:
			show_warning(self, "删除失败", f"无法删除文件: {exc}")
			return
		if file_existed:
			self.append_log(f"已删除工作流文件: {path.name}")
		else:
			self.append_log(f"目标文件不存在，已重置关联: {path.name}")
		self._last_directory = str(path.parent)
		self._current_workflow_path = None
		self._unsaved_changes = True
		self.update_file_display()
		self.show_status("文件已删除，请另存为新文件", 4000)

	def _save_to_path(self, path: Path) -> bool:
		data = self.scene.export_workflow()
		try:
			path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
		except OSError as exc:
			show_warning(self, "保存失败", f"无法写入文件: {exc}")
			return False
		self._current_workflow_path = path
		self._last_directory = str(path.parent)
		self._unsaved_changes = False
		self.update_file_display()
		self.append_log(f"已保存工作流: {path.name}")
		return True

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

	def showEvent(self, event):  # noqa: D401
		super().showEvent(event)
		self.scene.handle_view_resize(QSizeF(self.view.viewport().size()))

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

	def _apply_styles(self) -> None:
		base_style = """
		#workflowInterfaceRoot {
			background: transparent;
		}
		QWidget#workflowSidePanel {
			background: transparent;
			border: none;
			border-radius: 12px;
		}
		QTextEdit#workflowLog {
			background: transparent;
			border: 1px solid rgba(255, 255, 255, 25);
			border-radius: 10px;
			padding: 8px;
			color: #d6def0;
		}
		QStatusBar#workflowStatusBar {
			background: transparent;
			border-top: 1px solid rgba(255, 255, 255, 30);
			color: #ffffff;
			font-size: 11pt;
		}
		QSplitter::handle {
			background: rgba(255, 255, 255, 20);
			width: 4px;
			border-radius: 2px;
		}
		QSplitter::handle:hover {
			background: rgba(255, 255, 255, 35);
		}
		QPushButton#workflowSideButton {
			background: rgba(255, 255, 255, 18);
			border: 1px solid rgba(255, 255, 255, 28);
			border-radius: 8px;
			padding: 6px 12px;
			color: #ffffff;
			font-weight: 500;
		}
		QPushButton#workflowSideButton:hover {
			background: rgba(255, 255, 255, 28);
		}
		QPushButton#workflowSideButton:pressed {
			background: rgba(255, 255, 255, 18);
		}
		"""
		if not HAVE_FLUENT_WIDGETS:
			base_style += """
		QPushButton {
			background: rgba(255, 255, 255, 20);
			border: 1px solid rgba(255, 255, 255, 40);
			border-radius: 10px;
			padding: 10px 16px;
			color: #ffffff;
			font-weight: 600;
		}
		QPushButton:hover {
			background: rgba(255, 255, 255, 35);
		}
		QPushButton:pressed {
			background: rgba(255, 255, 255, 15);
		}
		"""
		self.setStyleSheet(base_style)


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
		self.workflow_interface.set_base_window_title(self.windowTitle())
