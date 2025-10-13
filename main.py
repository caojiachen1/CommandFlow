"""Node-based desktop automation tool with a drag-and-drop workflow editor.

This script implements a lightweight ComfyUI-style workflow builder that lets
users assemble desktop automation pipelines. The GUI is built with PySide6 and
relies on ``pyautogui`` for automation primitives such as screenshots, mouse
clicks, and keyboard input. The workflow logic itself lives in
``workflow_core.py`` where it is unit-tested independently.
"""

from __future__ import annotations

import sys

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

from ui import configure_windows_dpi, MainWindow


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
	window.showMaximized()
	sys.exit(app.exec())


if __name__ == "__main__":
	main()