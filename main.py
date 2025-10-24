"""Node-based desktop automation tool with a drag-and-drop workflow editor.

This script implements a lightweight ComfyUI-style workflow builder that lets
users assemble desktop automation pipelines. The GUI is built with PySide6 and
relies on ``pyautogui`` for automation primitives such as screenshots, mouse
clicks, and keyboard input. The workflow logic itself lives in
``workflow_core.py`` where it is unit-tested independently.
"""

from __future__ import annotations

import sys

# CRITICAL: Configure DPI awareness BEFORE any GUI imports
# This must happen before QApplication or any Qt imports
if sys.platform == "win32":
	try:
		import ctypes
		dpi_awareness_set = False
		# Try Per-Monitor V2 (Windows 10 1703+) - best option
		try:
			result = ctypes.windll.shcore.SetProcessDpiAwareness(2)
			if result == 0:
				dpi_awareness_set = True
				print("[DPI] Set Per-Monitor DPI Awareness V2")
		except (OSError, AttributeError) as e:
			# Try SetProcessDpiAwarenessContext (Windows 10 1607+)
			try:
				result = ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)
				dpi_awareness_set = True
				print("[DPI] Set DPI Awareness Context (Per-Monitor V2)")
			except (OSError, AttributeError):
				# Fallback to System DPI Aware (Windows Vista+)
				try:
					result = ctypes.windll.user32.SetProcessDPIAware()
					dpi_awareness_set = True
					print("[DPI] Set System DPI Aware")
				except (OSError, AttributeError):
					pass
		
		if not dpi_awareness_set:
			print("[DPI] Warning: Failed to set DPI awareness")
	except ImportError:
		pass

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

try:
	from qfluentwidgets import (
		FluentTranslator,
		Theme,
		setTheme,
		setThemeColor,
	)
	HAVE_FLUENT_WIDGETS = True
except ImportError:  # pragma: no cover - optional dependency
	HAVE_FLUENT_WIDGETS = False
	FluentTranslator = None
	Theme = None

	def setTheme(*_args, **_kwargs):  # type: ignore[func-name-matches]
		return None

	def setThemeColor(*_args, **_kwargs):  # type: ignore[func-name-matches]
		return None

from ui import MainWindow


# -- Application entry point ----------------------------------------------


def main() -> None:
	# Print coordinate mode information
	print("[坐标模式] 物理像素模式 - 输入坐标直接对应屏幕物理像素")
	print("[坐标模式] DPI 缩放已禁用 - 输入 100 将点击物理像素 100")
	
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
	window.showMaximized()
	sys.exit(app.exec())


if __name__ == "__main__":
	main()