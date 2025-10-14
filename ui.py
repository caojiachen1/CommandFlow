"""UI components for the desktop automation workflow editor.

This module contains all GUI-related classes and functions, separated from the main application logic.
"""

from __future__ import annotations

import ctypes
import itertools
import json
import math
import sys
import threading
import time
import uuid
from collections import defaultdict
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, cast

if sys.platform == "win32":  # pragma: no cover - platform-specific hotkeys
	from ctypes import wintypes
else:  # pragma: no cover - platform-specific hotkeys
	wintypes = None  # type: ignore[assignment]

from automation_runtime import PyAutoGuiRuntime, get_system_dpi_scale
from PySide6.QtCore import (
	QAbstractNativeEventFilter,
	QCoreApplication,
	QPoint,
	QPointF,
	Qt,
	QMimeData,
	QObject,
	Signal,
	QRectF,
	QLineF,
	QRect,
	QSizeF,
	QSize,
	QTimer,
	QThread,
)
from PySide6.QtGui import (
	QAction,
	QColor,
	QDrag,
	QIcon,
	QKeySequence,
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
	QCheckBox,
	QComboBox,
	QDialog,
	QDialogButtonBox,
	QDoubleSpinBox,
	QMenu,
	QFileDialog,
	QFormLayout,
	QKeySequenceEdit,
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
	QTabWidget,
)

try:
	from PySide6.QtGui import QKeyCombination  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - fallback for older PySide6
	QKeyCombination = None  # type: ignore[assignment]

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

from settings_manager import SettingsManager


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


# -- Global hotkey management ---------------------------------------------


class GlobalHotkeyManager(QObject, QAbstractNativeEventFilter):
	"""Register and dispatch system-wide hotkeys on supported platforms."""

	MOD_ALT = 0x0001
	MOD_CONTROL = 0x0002
	MOD_SHIFT = 0x0004
	MOD_WIN = 0x0008
	MOD_NOREPEAT = 0x4000
	WM_HOTKEY = 0x0312

	def __init__(self, parent: Optional[QObject] = None) -> None:
		QObject.__init__(self, parent)
		QAbstractNativeEventFilter.__init__(self)
		self._available = sys.platform == "win32" and wintypes is not None and QKeyCombination is not None
		self._window: Optional[QWidget] = None
		self._hwnd: int = 0
		self._callbacks: Dict[str, Callable[[], None]] = {}
		self._sequence_map: Dict[str, str] = {}
		self._action_to_id: Dict[str, int] = {}
		self._id_to_action: Dict[int, str] = {}
		self._id_counter = itertools.count(1)
		self._user32 = ctypes.windll.user32 if self._available else None
		self._app = QCoreApplication.instance()
		if self._available and self._app is not None:
			self._app.installNativeEventFilter(self)

	@property
	def is_available(self) -> bool:
		return bool(self._available and self._user32 is not None)

	def set_window(self, window: Optional[QWidget]) -> None:
		if not self.is_available:
			self._window = window
			return
		previous_hwnd = self._hwnd
		self._window = window
		self._hwnd = int(window.winId()) if window is not None else 0
		if previous_hwnd != self._hwnd:
			self._unregister_all()
			self._register_all()

	def set_callback(self, action_id: str, callback: Callable[[], None]) -> None:
		self._callbacks[action_id] = callback

	def update_hotkeys(self, mapping: Dict[str, str]) -> Dict[str, str]:
		self._sequence_map = dict(mapping)
		if not self.is_available or self._hwnd == 0:
			return {}
		return self._register_all()

	def cleanup(self) -> None:
		if not self.is_available:
			return
		self._unregister_all()
		if self._app is not None:
			self._app.removeNativeEventFilter(self)

	def nativeEventFilter(self, event_type, message):
		if not self.is_available or event_type not in ("windows_generic_MSG", "windows_dispatcher_MSG"):
			return False, 0
		if wintypes is None:  # pragma: no cover - defensive
			return False, 0
		msg = wintypes.MSG.from_address(int(message))
		if msg.message != self.WM_HOTKEY:
			return False, 0
		action_id = self._id_to_action.get(int(msg.wParam))
		if not action_id:
			return False, 0
		callback = self._callbacks.get(action_id)
		if callback is None:
			return False, 0
		QTimer.singleShot(0, callback)
		return True, 0

	def _register_all(self) -> Dict[str, str]:
		errors: Dict[str, str] = {}
		self._unregister_all()
		if not self.is_available or self._hwnd == 0 or self._user32 is None:
			return errors
		self._id_counter = itertools.count(1)
		for action_id, sequence in self._sequence_map.items():
			if not sequence:
				continue
			parsed = self._parse_sequence(sequence)
			if parsed is None:
				errors[action_id] = "无法解析快捷键"
				continue
			modifiers, key = parsed
			hotkey_id = next(self._id_counter)
			if not self._user32.RegisterHotKey(self._hwnd, hotkey_id, modifiers | self.MOD_NOREPEAT, key):
				errors[action_id] = "注册失败，可能已被占用"
				continue
			self._action_to_id[action_id] = hotkey_id
			self._id_to_action[hotkey_id] = action_id
		return errors

	def _unregister_all(self) -> None:
		if not self.is_available or self._user32 is None:
			self._action_to_id.clear()
			self._id_to_action.clear()
			return
		if self._hwnd == 0:
			self._action_to_id.clear()
			self._id_to_action.clear()
			return
		for hotkey_id in list(self._id_to_action.keys()):
			self._user32.UnregisterHotKey(self._hwnd, hotkey_id)
		self._action_to_id.clear()
		self._id_to_action.clear()

	def _parse_sequence(self, sequence: str) -> Optional[Tuple[int, int]]:
		if not sequence or QKeyCombination is None:
			return None
		qt_sequence = QKeySequence(sequence)
		if qt_sequence.count() == 0:
			return None
		combined = int(qt_sequence[0])  # type: ignore[index]
		combo = QKeyCombination.fromCombined(combined)
		qt_key = combo.key()
		if qt_key == Qt.Key.Key_unknown:
			return None
		modifiers = combo.keyboardModifiers()
		vk_code = self._qt_key_to_vk(qt_key)
		if vk_code is None:
			return None
		modifier_mask = 0
		if modifiers & Qt.KeyboardModifier.ControlModifier:
			modifier_mask |= self.MOD_CONTROL
		if modifiers & Qt.KeyboardModifier.ShiftModifier:
			modifier_mask |= self.MOD_SHIFT
		if modifiers & Qt.KeyboardModifier.AltModifier:
			modifier_mask |= self.MOD_ALT
		if modifiers & Qt.KeyboardModifier.MetaModifier:
			modifier_mask |= self.MOD_WIN
		return modifier_mask, vk_code

	@staticmethod
	def _qt_key_to_vk(qt_key: Qt.Key) -> Optional[int]:
		# Direct mapping for letters and digits
		if Qt.Key.Key_A <= qt_key <= Qt.Key.Key_Z:
			return int(qt_key)
		if Qt.Key.Key_0 <= qt_key <= Qt.Key.Key_9:
			return int(qt_key)
		function_keys = {
			Qt.Key.Key_F1: 0x70,
			Qt.Key.Key_F2: 0x71,
			Qt.Key.Key_F3: 0x72,
			Qt.Key.Key_F4: 0x73,
			Qt.Key.Key_F5: 0x74,
			Qt.Key.Key_F6: 0x75,
			Qt.Key.Key_F7: 0x76,
			Qt.Key.Key_F8: 0x77,
			Qt.Key.Key_F9: 0x78,
			Qt.Key.Key_F10: 0x79,
			Qt.Key.Key_F11: 0x7A,
			Qt.Key.Key_F12: 0x7B,
		}
		if qt_key in function_keys:
			return function_keys[qt_key]
		special_keys = {
			Qt.Key.Key_Space: 0x20,
			Qt.Key.Key_Tab: 0x09,
			Qt.Key.Key_Backspace: 0x08,
			Qt.Key.Key_Return: 0x0D,
			Qt.Key.Key_Enter: 0x0D,
			Qt.Key.Key_Escape: 0x1B,
			Qt.Key.Key_Plus: 0xBB,
			Qt.Key.Key_Minus: 0xBD,
		}
		if qt_key in special_keys:
			return special_keys[qt_key]
		return None

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
		self.input_port.setPos(1, self.HEIGHT / 2 - 6)
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


class PathPicker(QWidget):
	"""Composite widget that combines a line edit with a browse button."""

	def __init__(
		self,
		parent: Optional[QWidget] = None,
		*,
		initial: str = "",
		mode: str = "file_open",
		dialog_title: str = "选择路径",
		file_title: Optional[str] = None,
		directory_title: Optional[str] = None,
		save_title: Optional[str] = None,
		name_filter: str = "All Files (*.*)",
		placeholder: str = "",
		start_directory: str = "",
	) -> None:
		super().__init__(parent)
		self._mode = mode
		self._dialog_title = dialog_title or "选择路径"
		self._file_title = file_title or self._dialog_title
		self._directory_title = directory_title or self._dialog_title
		self._save_title = save_title or self._file_title
		self._name_filter = name_filter or "All Files (*.*)"
		self._start_directory = start_directory
		layout = QHBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(6)
		line_cls = FluentLineEdit if HAVE_FLUENT_WIDGETS else QLineEdit
		self._line_edit = line_cls(self)
		if placeholder:
			self._line_edit.setPlaceholderText(placeholder)
		self._line_edit.setText(initial or "")
		layout.addWidget(self._line_edit, 1)
		self._button = QPushButton("浏览...", self)
		self._button.setCursor(Qt.CursorShape.PointingHandCursor)
		if mode == "any":
			menu = QMenu(self)
			file_action = menu.addAction("选择文件")
			file_action.triggered.connect(lambda: self._choose("file_open"))
			dir_action = menu.addAction("选择文件夹")
			dir_action.triggered.connect(lambda: self._choose("directory"))
			self._button.setMenu(menu)
		else:
			self._button.clicked.connect(lambda: self._choose(self._mode))
		layout.addWidget(self._button)

	def text(self) -> str:
		return self._line_edit.text()

	def setText(self, value: str) -> None:
		self._line_edit.setText(value or "")

	def value(self) -> str:
		return self.text().strip()

	def _initial_directory(self) -> str:
		current = self._line_edit.text().strip()
		if current:
			candidate = Path(current).expanduser()
			if candidate.is_dir():
				return str(candidate)
			if candidate.parent.exists():
				return str(candidate.parent)
		if self._start_directory:
			return self._start_directory
		return str(Path.home())

	def _choose(self, mode: str) -> None:
		start_dir = self._initial_directory()
		selected = ""
		if mode == "directory":
			selected = QFileDialog.getExistingDirectory(self, self._directory_title, start_dir)
		elif mode == "file_save":
			selected, _ = QFileDialog.getSaveFileName(self, self._save_title, start_dir, self._name_filter)
		else:
			title = self._file_title
			selected, _ = QFileDialog.getOpenFileName(self, title, start_dir, self._name_filter)
		if selected:
			self._line_edit.setText(selected)


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
		if ftype in {"path", "file_open", "file_save", "directory"}:
			mode = ftype if ftype != "path" else cast(str, field.get("dialog_mode", "file_open"))
			picker = PathPicker(
				self,
				initial=str(value or ""),
				mode=mode,
				dialog_title=cast(str, field.get("dialog_title", "选择路径")),
				file_title=cast(Optional[str], field.get("file_dialog_title")),
				directory_title=cast(Optional[str], field.get("directory_dialog_title")),
				save_title=cast(Optional[str], field.get("save_dialog_title")),
				name_filter=cast(str, field.get("name_filter", "All Files (*.*)")),
				placeholder=cast(str, field.get("placeholder", "")),
				start_directory=cast(str, field.get("start_directory", "")),
			)
			return picker
		if ftype == "bool":
			widget = QCheckBox(self)
			widget.setChecked(bool(value))
			return widget
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
			if isinstance(widget, QCheckBox):
				result[key] = widget.isChecked()
			elif isinstance(widget, PathPicker):
				result[key] = widget.value()
			elif isinstance(widget, QSpinBox):
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
	"""Execute workflows on a worker thread with cancellation support."""

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
		self._stop_event: Optional[threading.Event] = None
		self._threads: List[QThread] = []
		self._workers: List[_WorkflowRunnerWorker] = []

	def is_running(self) -> bool:
		return self._running

	def run(self) -> None:
		if self._running:
			return
		self._stop_event = threading.Event()
		worker = _WorkflowRunnerWorker(
			self._graph_supplier,
			self._runtime_factory,
			self._stop_event,
		)
		thread = QThread()
		worker.moveToThread(thread)
		thread.started.connect(worker.run)
		worker.finished.connect(self._handle_worker_finished)
		worker.finished.connect(thread.quit)
		worker.finished.connect(worker.deleteLater)
		thread.finished.connect(self._on_thread_finished)
		thread.finished.connect(thread.deleteLater)
		thread.start()
		self._workers.append(worker)
		self._threads.append(thread)
		self._running = True
		self.started.emit()

	def stop(self) -> None:
		if not self._running or self._stop_event is None:
			return
		self._stop_event.set()

	def _handle_worker_finished(self, success: bool, message: str) -> None:
		sender = self.sender()
		if isinstance(sender, _WorkflowRunnerWorker):
			self._on_worker_finished(sender)
		self._running = False
		self._stop_event = None
		self.finished.emit(success, message)

	def _on_worker_finished(self, worker: _WorkflowRunnerWorker) -> None:
		if worker in self._workers:
			self._workers.remove(worker)

	def _on_thread_finished(self) -> None:
		sender = self.sender()
		if not isinstance(sender, QThread):
			return
		thread = sender
		if thread in self._threads:
			self._threads.remove(thread)


class _WorkflowRunnerWorker(QObject):
	finished = Signal(bool, str)

	def __init__(
		self,
		graph_supplier: Callable[[], WorkflowGraph],
		runtime_factory: Callable[[], AutomationRuntime],
		stop_event: threading.Event,
		parent: Optional[QObject] = None,
	) -> None:
		super().__init__(parent)
		self._graph_supplier = graph_supplier
		self._runtime_factory = runtime_factory
		self._stop_event = stop_event

	def run(self) -> None:
		try:
			graph_copy = self._graph_supplier()
			executor = WorkflowExecutor(self._runtime_factory())
			executor.run(graph_copy, should_stop=self._stop_event.is_set)
		except ExecutionError as exc:
			if self._stop_event.is_set():
				self.finished.emit(False, "执行已取消")
			else:
				self.finished.emit(False, str(exc))
		except Exception as exc:  # pragma: no cover - defensive
			if self._stop_event.is_set():
				self.finished.emit(False, "执行已取消")
			else:
				self.finished.emit(False, f"执行失败: {exc}")
		else:
			self.finished.emit(True, "执行完成")


class QuickControlWindow(QWidget):
	"""Compact always-on-top controller for background workflow execution."""

	exit_requested = Signal()

	def __init__(self, interface: "WorkflowInterface", parent: Optional[QWidget] = None) -> None:
		super().__init__(parent)
		self._interface = interface
		self.setWindowTitle("Command Flow 控制台")
		self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
		self.setWindowFlag(Qt.WindowType.Tool)
		self.setWindowModality(Qt.WindowModality.NonModal)
		self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
		self.setFixedWidth(260)
		layout = QVBoxLayout(self)
		layout.setContentsMargins(16, 16, 16, 16)
		layout.setSpacing(10)
		label_cls = BodyLabel if HAVE_FLUENT_WIDGETS else QLabel
		self._status_label = label_cls("已就绪", self)
		self._status_label.setObjectName("quickPanelStatusLabel")
		layout.addWidget(self._status_label)
		self._file_label = label_cls("当前工作流: 未命名", self)
		self._file_label.setObjectName("quickPanelFileLabel")
		layout.addWidget(self._file_label)
		button_cls = PrimaryPushButton if HAVE_FLUENT_WIDGETS else QPushButton
		self.start_button = button_cls("启动工作流", self)
		self.start_button.setCursor(Qt.CursorShape.PointingHandCursor)
		self.start_button.clicked.connect(self._interface.execute_workflow)
		layout.addWidget(self.start_button)
		self.stop_button = button_cls("停止工作流", self)
		self.stop_button.setCursor(Qt.CursorShape.PointingHandCursor)
		self.stop_button.clicked.connect(self._interface.stop_workflow)
		self.stop_button.setEnabled(False)
		layout.addWidget(self.stop_button)
		self._status_label.setMinimumWidth(200)
		self._update_style()

	def closeEvent(self, event) -> None:  # noqa: D401
		event.ignore()
		self.hide()
		self.exit_requested.emit()

	def show_panel(self) -> None:
		self._ensure_position()
		self.show()
		self.raise_()
		self.activateWindow()

	def set_running(self, running: bool) -> None:
		self.start_button.setEnabled(not running)
		self.stop_button.setEnabled(running)
		self._status_label.setText("运行中" if running else "已就绪")

	def update_file(self, filename: str, modified: bool) -> None:
		suffix = "*" if modified else ""
		self._file_label.setText(f"当前工作流: {filename}{suffix}")

	def set_shortcut_hints(self, start_shortcut: str, stop_shortcut: str) -> None:
		start_label = "启动工作流"
		if start_shortcut:
			start_label += f" ({start_shortcut})"
		stop_label = "停止工作流"
		if stop_shortcut:
			stop_label += f" ({stop_shortcut})"
		self.start_button.setToolTip(start_label)
		self.stop_button.setToolTip(stop_label)

	def _ensure_position(self) -> None:
		screen = QApplication.primaryScreen()
		if screen is None:
			return
		available = screen.availableGeometry()
		self.adjustSize()
		x_pos = available.right() - self.width() - 24
		y_pos = available.bottom() - self.height() - 24
		x_pos = max(available.left() + 16, x_pos)
		y_pos = max(available.top() + 16, y_pos)
		self.move(x_pos, y_pos)

	def _update_style(self) -> None:
		self.setStyleSheet(
			"""
			#quickPanelStatusLabel, #quickPanelFileLabel {
				color: #ffffff;
			}
			"""
		)


class SettingsDialog(ConfigDialogBase):
	"""Global application settings dialog supporting shortcut editing."""

	def __init__(self, settings: SettingsManager, parent: Optional[QWidget] = None) -> None:
		super().__init__(parent)
		self._settings = settings
		self._shortcut_editors: Dict[str, QKeySequenceEdit] = {}
		self._general_checkboxes: Dict[str, QCheckBox] = {}
		if not HAVE_FLUENT_WIDGETS:
			self.setWindowTitle("设置")
			self.setModal(True)
			self.setMinimumWidth(420)
		self._build_layout()

	def _build_layout(self) -> None:
		if HAVE_FLUENT_WIDGETS:
			fluent_self = cast(Any, self)
			content_widget = QWidget(self)
			content_layout = QVBoxLayout(content_widget)
			content_layout.setContentsMargins(16, 0, 16, 0)
			content_layout.setSpacing(12)
			if hasattr(fluent_self, "viewLayout"):
				title_label = SubtitleLabel("应用设置", content_widget)
				title_label.setObjectName("settingsTitleLabel")
				fluent_self.viewLayout.addWidget(title_label)
			tabs = QTabWidget(content_widget)
			tabs.setObjectName("settingsTabs")
			content_layout.addWidget(tabs, 1)
			self._populate_tabs(tabs)
			if hasattr(fluent_self, "viewLayout"):
				fluent_self.viewLayout.addWidget(content_widget)
			else:
				layout = QVBoxLayout(self)
				layout.addWidget(content_widget)
			if hasattr(fluent_self, "widget") and fluent_self.widget is not None:
				fluent_self.widget.setMinimumWidth(520)
			if hasattr(fluent_self, "yesButton"):
				fluent_self.yesButton.setText("保存")
			if hasattr(fluent_self, "cancelButton"):
				fluent_self.cancelButton.setText("取消")
			reset_button = PrimaryPushButton("恢复默认", self)
			reset_button.clicked.connect(self.restore_defaults)
			if hasattr(fluent_self, "buttonLayout"):
				fluent_self.buttonLayout.insertWidget(0, reset_button)
			else:
				content_layout.addWidget(reset_button)
		else:
			container_layout = QVBoxLayout(self)
			container_layout.setContentsMargins(16, 16, 16, 16)
			container_layout.setSpacing(12)
			tabs = QTabWidget(self)
			tabs.setObjectName("settingsTabs")
			container_layout.addWidget(tabs, 1)
			self._populate_tabs(tabs)
			buttons = QDialogButtonBox(
				QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
				parent=self,
			)
			reset_button = buttons.addButton("恢复默认", QDialogButtonBox.ButtonRole.ResetRole)
			reset_button.clicked.connect(self.restore_defaults)
			buttons.accepted.connect(self.accept)
			buttons.rejected.connect(self.reject)
			container_layout.addWidget(buttons)

	def _populate_tabs(self, tabs: QTabWidget) -> None:
		self._shortcut_editors.clear()
		self._general_checkboxes.clear()
		shortcuts_widget = QWidget(tabs)
		shortcuts_layout = QFormLayout(shortcuts_widget)
		shortcuts_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
		shortcuts_layout.setContentsMargins(8, 12, 8, 12)
		shortcuts_layout.setSpacing(10)
		for action_id, meta in self._settings.shortcut_items():
			label_text = meta.get("label", action_id)
			label_widget = SubtitleLabel(label_text, shortcuts_widget) if HAVE_FLUENT_WIDGETS else QLabel(label_text, shortcuts_widget)
			editor = QKeySequenceEdit(shortcuts_widget)
			sequence = self._settings.get_shortcut(action_id)
			if sequence:
				editor.setKeySequence(QKeySequence(sequence))
			editor.setClearButtonEnabled(True)
			editor.setToolTip(meta.get("description", ""))
			label_widget.setToolTip(meta.get("description", ""))
			shortcuts_layout.addRow(label_widget, editor)
			self._shortcut_editors[action_id] = editor
		shortcuts_widget.setLayout(shortcuts_layout)
		tabs.addTab(shortcuts_widget, "快捷键")

		general_widget = QWidget(tabs)
		general_layout = QVBoxLayout(general_widget)
		general_layout.setContentsMargins(16, 16, 16, 16)
		general_layout.setSpacing(10)
		for option_key, meta in self._settings.general_items():
			checkbox = QCheckBox(meta.get("label", option_key), general_widget)
			checkbox.setChecked(self._settings.get_general(option_key))
			description = meta.get("description", "")
			if description:
				checkbox.setToolTip(description)
			general_layout.addWidget(checkbox)
			self._general_checkboxes[option_key] = checkbox
		general_layout.addStretch(1)
		tabs.addTab(general_widget, "常规")

	def restore_defaults(self) -> None:
		for action_id, editor in self._shortcut_editors.items():
			default_value = str(self._settings.DEFAULTS["shortcuts"].get(action_id, ""))
			editor.setKeySequence(QKeySequence(default_value))
		for option_key, checkbox in self._general_checkboxes.items():
			checkbox.setChecked(bool(self._settings.DEFAULTS["general"].get(option_key, False)))

	def accept(self) -> None:  # noqa: D401
		conflicts: Dict[str, List[str]] = {}
		shortcuts_payload: Dict[str, str] = {}
		for action_id, editor in self._shortcut_editors.items():
			sequence = editor.keySequence().toString(QKeySequence.SequenceFormat.PortableText).strip()
			shortcuts_payload[action_id] = sequence
			if sequence:
				conflicts.setdefault(sequence, []).append(self._settings.get_shortcut_label(action_id))
		duplicates = {seq: labels for seq, labels in conflicts.items() if len(labels) > 1}
		if duplicates:
			rows = [f"{seq}: {', '.join(labels)}" for seq, labels in duplicates.items()]
			show_warning(self, "快捷键冲突", "以下快捷键存在冲突:\n" + "\n".join(rows))
			return
		general_payload: Dict[str, bool] = {key: checkbox.isChecked() for key, checkbox in self._general_checkboxes.items()}
		self._settings.apply(shortcuts=shortcuts_payload, general=general_payload)
		super().accept()

# -- Fluent workflow interface --------------------------------------------



class WorkflowInterface(QWidget):
	"""Workflow editor surface using Fluent UI components when available."""

	run_state_changed = Signal(bool)
	file_context_changed = Signal(str, bool)

	def __init__(
		self,
		settings: SettingsManager,
		open_settings_callback: Optional[Callable[[], None]] = None,
		parent: Optional[QWidget] = None,
	) -> None:
		super().__init__(parent)
		self._settings = settings
		self._open_settings_callback = open_settings_callback or (lambda: None)
		self._actions: Dict[str, QAction] = {}
		self._button_map: Dict[str, QPushButton] = {}
		self._current_workflow_path: Optional[Path] = None
		self._unsaved_changes = False
		self._last_directory = str(Path.home())
		self._workflow_filter = "JSON Files (*.json);;All Files (*.*)"
		self._window_base_title = "Command Flow Studio"
		self._is_running = False

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
		self._button_map["new_workflow"] = self.new_workflow_button
		self.open_workflow_button = self._make_side_button("打开", right_panel)
		self.open_workflow_button.clicked.connect(self.open_workflow)
		file_controls_top.addWidget(self.open_workflow_button)
		self._button_map["open_workflow"] = self.open_workflow_button
		self.save_workflow_button = self._make_side_button("保存", right_panel)
		self.save_workflow_button.clicked.connect(self.save_workflow)
		file_controls_top.addWidget(self.save_workflow_button)
		self._button_map["save_workflow"] = self.save_workflow_button
		right_layout.addLayout(file_controls_top)
		file_controls_bottom = QHBoxLayout()
		file_controls_bottom.setContentsMargins(0, 0, 0, 0)
		file_controls_bottom.setSpacing(8)
		self.save_as_workflow_button = self._make_side_button("另存为", right_panel)
		self.save_as_workflow_button.clicked.connect(self.save_workflow_as)
		file_controls_bottom.addWidget(self.save_as_workflow_button)
		self._button_map["save_as_workflow"] = self.save_as_workflow_button
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
		self._button_map["run_workflow"] = self.run_button
		self.stop_button = PrimaryPushButton("停止运行", right_panel)
		self.stop_button.clicked.connect(self.stop_workflow)
		self.stop_button.setCursor(Qt.CursorShape.PointingHandCursor)
		self.stop_button.setMinimumHeight(36)
		self.stop_button.setEnabled(False)
		right_layout.addWidget(self.stop_button)
		self._button_map["stop_workflow"] = self.stop_button
		self.background_button = self._make_side_button("切换后台模式", right_panel)
		self.background_button.clicked.connect(self.toggle_quick_panel)
		right_layout.addWidget(self.background_button)
		self._button_map["toggle_quick_panel"] = self.background_button
		self.settings_button = self._make_side_button("设置", right_panel)
		self.settings_button.clicked.connect(self._invoke_settings)
		right_layout.addWidget(self.settings_button)
		self._button_map["open_settings"] = self.settings_button
		zoom_controls = QHBoxLayout()
		zoom_controls.setContentsMargins(0, 0, 0, 0)
		zoom_controls.setSpacing(8)
		# Provide quick zoom operations alongside the primary run control.
		self.zoom_out_button = self._make_side_button("缩小", right_panel)
		self.zoom_out_button.clicked.connect(self.handle_zoom_out)
		zoom_controls.addWidget(self.zoom_out_button)
		self._button_map["zoom_out"] = self.zoom_out_button
		self.zoom_reset_button = self._make_side_button("重置", right_panel)
		self.zoom_reset_button.clicked.connect(self.handle_zoom_reset)
		zoom_controls.addWidget(self.zoom_reset_button)
		self._button_map["zoom_reset"] = self.zoom_reset_button
		self.zoom_in_button = self._make_side_button("放大", right_panel)
		self.zoom_in_button.clicked.connect(self.handle_zoom_in)
		zoom_controls.addWidget(self.zoom_in_button)
		self._button_map["zoom_in"] = self.zoom_in_button
		self.zoom_fit_button = self._make_side_button("适配", right_panel)
		self.zoom_fit_button.clicked.connect(self.fit_workflow_to_nodes)
		zoom_controls.addWidget(self.zoom_fit_button)
		self._button_map["zoom_fit"] = self.zoom_fit_button
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
		self._button_map["clear_log"] = self.clear_log_button
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
		self.runner.started.connect(self._on_runner_started)
		self.runner.finished.connect(self._on_runner_finished)
		self.scene.modified.connect(self.mark_workflow_modified)
		self._create_actions()
		self._settings.shortcuts_changed.connect(self.reload_shortcuts)
		self.reload_shortcuts()
		self.update_file_display()
		self._set_running_state(False)

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
		self.file_context_changed.emit(filename, self._unsaved_changes)
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
		if self._settings.get_general("confirm_before_delete"):
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
		if self._is_running:
			self.show_status("工作流正在执行中", 2000)
			return
		if not self.scene.graph.nodes:
			show_information(self, "运行工作流", "请先添加节点")
			return
		if self._settings.get_general("auto_save_before_run") and self._unsaved_changes:
			self.save_workflow()
			if self._unsaved_changes:
				self.show_status("运行已取消：更改未保存", 4000)
				return
		try:
			self.scene.graph.topological_order()
		except ExecutionError as exc:
			show_warning(self, "拓扑错误", str(exc))
			return
		self.runner.run()

	def stop_workflow(self) -> None:
		if not self._is_running:
			self.show_status("当前没有正在运行的工作流", 2000)
			return
		self.runner.stop()
		self.append_log("已请求停止工作流")

	def _on_runner_started(self) -> None:
		self._set_running_state(True)
		self.append_log("开始执行工作流")

	def _on_runner_finished(self, success: bool, message: str) -> None:
		self._set_running_state(False)
		if message:
			self.append_log(message)
		if not success and message != "执行已取消":
			show_warning(self, "执行失败", message)
		elif not success:
			self.show_status(message, 3000)

	def _set_running_state(self, running: bool) -> None:
		self._is_running = running
		self.run_button.setEnabled(not running)
		self.stop_button.setEnabled(running)
		run_action = self._actions.get("run_workflow")
		if run_action is not None:
			run_action.setEnabled(not running)
		stop_action = self._actions.get("stop_workflow")
		if stop_action is not None:
			stop_action.setEnabled(running)
		self.run_state_changed.emit(running)

	def toggle_quick_panel(self) -> None:
		window = self.window()
		if window is None:
			return
		toggle = getattr(window, "toggle_quick_panel", None)
		if callable(toggle):
			toggle()

	def show_status(self, message: str, timeout_ms: int = 0) -> None:
		self.status_bar.showMessage(message, timeout_ms)

	def reload_shortcuts(self, *_args, **_kwargs) -> None:
		for action_id, action in self._actions.items():
			sequence = self._settings.get_shortcut(action_id)
			if sequence:
				action.setShortcut(QKeySequence(sequence))
			else:
				action.setShortcut(QKeySequence())
		self._update_button_tooltips()

	def _create_actions(self) -> None:
		action_specs: Dict[str, Tuple[str, Callable[[], None]]] = {
			"new_workflow": ("新建工作流", self.new_workflow),
			"open_workflow": ("打开工作流", self.open_workflow),
			"save_workflow": ("保存工作流", self.save_workflow),
			"save_as_workflow": ("另存工作流", self.save_workflow_as),
			"run_workflow": ("运行工作流", self.execute_workflow),
			"stop_workflow": ("停止工作流", self.stop_workflow),
			"zoom_in": ("放大画布", self.handle_zoom_in),
			"zoom_out": ("缩小画布", self.handle_zoom_out),
			"zoom_reset": ("重置缩放", self.handle_zoom_reset),
			"zoom_fit": ("适配视图", self.fit_workflow_to_nodes),
			"clear_log": ("清空日志", self.clear_log),
			"open_settings": ("打开设置", self._invoke_settings),
			"toggle_quick_panel": ("切换后台面板", self.toggle_quick_panel),
		}
		for action_id, (text, handler) in action_specs.items():
			action = QAction(text, self)
			action.setObjectName(f"workflowAction_{action_id}")
			action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
			action.triggered.connect(handler)
			self.addAction(action)
			self._actions[action_id] = action

	def _update_button_tooltips(self) -> None:
		for action_id, button in self._button_map.items():
			label = button.text().strip() or self._settings.get_shortcut_label(action_id)
			sequence = self._settings.get_shortcut(action_id)
			if sequence:
				button.setToolTip(f"{label} ({sequence})")
			else:
				button.setToolTip(label)

	def _invoke_settings(self) -> None:
		self._open_settings_callback()

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

		self._background_mode = False
		self.settings = SettingsManager()
		self.workflow_interface = WorkflowInterface(self.settings, self.open_settings_dialog, self)
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
		self.settings.settings_changed.connect(self._notify_settings_updated)
		self.quick_panel = QuickControlWindow(self.workflow_interface, self)
		self.quick_panel.hide()
		self.quick_panel.exit_requested.connect(self._exit_background_mode)
		self.workflow_interface.run_state_changed.connect(self.quick_panel.set_running)
		self.workflow_interface.file_context_changed.connect(self.quick_panel.update_file)
		self.workflow_interface.update_file_display()
		self.quick_panel.set_running(False)
		self.hotkey_manager = GlobalHotkeyManager(self)
		self.hotkey_manager.set_callback("toggle_quick_panel", self.toggle_quick_panel)
		self.hotkey_manager.set_callback("run_workflow", self._trigger_run_hotkey)
		self.hotkey_manager.set_callback("stop_workflow", self._trigger_stop_hotkey)
		if self.hotkey_manager.is_available:
			self.hotkey_manager.set_window(self)
		self.settings.shortcuts_changed.connect(self._update_global_hotkeys)
		self._update_global_hotkeys()

	def open_settings_dialog(self) -> None:
		dialog = SettingsDialog(self.settings, self)
		dialog.exec()

	def _notify_settings_updated(self, _payload: Dict[str, object]) -> None:
		self.workflow_interface.show_status("设置已更新", 2000)

	def toggle_quick_panel(self) -> None:
		if self._background_mode:
			self._exit_background_mode()
		else:
			self._enter_background_mode()

	def _enter_background_mode(self) -> None:
		if self._background_mode:
			if not self.quick_panel.isVisible():
				self.quick_panel.show_panel()
			return
		self._background_mode = True
		if self.hotkey_manager.is_available:
			self.hotkey_manager.set_window(self.quick_panel)
		self.quick_panel.show_panel()
		self.workflow_interface.show_status("已切换到后台控制面板", 3000)
		self.hide()

	def _exit_background_mode(self) -> None:
		if self.quick_panel.isVisible():
			self.quick_panel.hide()
		if self.hotkey_manager.is_available:
			self.hotkey_manager.set_window(self)
		if not self._background_mode:
			self.showMaximized()
			self.raise_()
			self.activateWindow()
			return
		self._background_mode = False
		self.showMaximized()
		self.raise_()
		self.activateWindow()
		self.workflow_interface.show_status("已返回编辑界面", 3000)

	def _trigger_run_hotkey(self) -> None:
		self.workflow_interface.execute_workflow()

	def _trigger_stop_hotkey(self) -> None:
		self.workflow_interface.stop_workflow()

	def _update_global_hotkeys(self, *_args, **_kwargs) -> None:
		start_sequence = self.settings.get_shortcut("run_workflow")
		stop_sequence = self.settings.get_shortcut("stop_workflow")
		toggle_sequence = self.settings.get_shortcut("toggle_quick_panel")
		self.quick_panel.set_shortcut_hints(start_sequence, stop_sequence)
		if not self.hotkey_manager.is_available:
			return
		mapping = {
			"toggle_quick_panel": toggle_sequence,
			"run_workflow": start_sequence,
			"stop_workflow": stop_sequence,
		}
		errors = self.hotkey_manager.update_hotkeys(mapping)
		if errors:
			parts = [f"{self.settings.get_shortcut_label(action)}: {reason}" for action, reason in errors.items()]
			self.workflow_interface.show_status("全局快捷键注册失败: " + "; ".join(parts), 6000)

	def showEvent(self, event) -> None:  # noqa: D401
		super().showEvent(event)
		if self._background_mode:
			self._background_mode = False
			if self.quick_panel.isVisible():
				self.quick_panel.hide()
			self.workflow_interface.show_status("已返回编辑界面", 3000)
		if self.hotkey_manager.is_available:
			self.winId()  # ensure window handle is created
			self.hotkey_manager.set_window(self)
			self._update_global_hotkeys()

	def closeEvent(self, event) -> None:  # noqa: D401
		if hasattr(self, "hotkey_manager"):
			self.hotkey_manager.cleanup()
		if hasattr(self, "quick_panel") and self.quick_panel.isVisible():
			self.quick_panel.hide()
		self._background_mode = False
		super().closeEvent(event)
