"""Application settings management for CommandFlow.

This module provides a thin wrapper around a JSON settings file to
persist user preferences such as keyboard shortcuts and general
behavioural toggles.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Tuple

from PySide6.QtCore import QObject, Signal


class SettingsManager(QObject):
	"""Persist and expose application settings."""

	DEFAULTS: Dict[str, Dict[str, Any]] = {
		"shortcuts": {
			"new_workflow": "Ctrl+N",
			"open_workflow": "Ctrl+O",
			"save_workflow": "Ctrl+S",
			"save_as_workflow": "Ctrl+Shift+S",
			"run_workflow": "F5",
			"zoom_in": "Ctrl++",
			"zoom_out": "Ctrl+-",
			"zoom_reset": "Ctrl+0",
			"zoom_fit": "Ctrl+F",
			"clear_log": "Ctrl+Shift+L",
			"open_settings": "Ctrl+,",
		},
		"general": {
			"auto_save_before_run": False,
			"confirm_before_delete": True,
		},
	}

	SHORTCUT_METADATA: Dict[str, Dict[str, str]] = {
		"new_workflow": {"label": "新建工作流", "description": "创建新的空白工作流"},
		"open_workflow": {"label": "打开工作流", "description": "从磁盘加载已有工作流"},
		"save_workflow": {"label": "保存工作流", "description": "将当前工作流保存到当前文件"},
		"save_as_workflow": {"label": "另存工作流", "description": "保存为新的工作流文件"},
		"run_workflow": {"label": "运行工作流", "description": "执行当前工作流"},
		"zoom_in": {"label": "放大画布", "description": "放大工作流视图"},
		"zoom_out": {"label": "缩小画布", "description": "缩小工作流视图"},
		"zoom_reset": {"label": "重置缩放", "description": "重置工作流视图缩放"},
		"zoom_fit": {"label": "适配视图", "description": "将视图自适应当前节点"},
		"clear_log": {"label": "清空日志", "description": "清除执行日志输出"},
		"open_settings": {"label": "打开设置", "description": "打开设置对话框"},
	}

	GENERAL_METADATA: Dict[str, Dict[str, str]] = {
		"auto_save_before_run": {
			"label": "运行前自动保存",
			"description": "在运行工作流前自动保存当前文件（若已关联文件）",
		},
		"confirm_before_delete": {
			"label": "删除前进行确认",
			"description": "删除已保存的工作流文件前弹出确认对话框",
		},
	}

	settings_changed = Signal(dict)
	shortcuts_changed = Signal(dict)
	general_changed = Signal(dict)

	def __init__(self, config_path: Path | None = None) -> None:
		super().__init__()
		self._config_path = config_path or self._default_config_path()
		self._data: Dict[str, Dict[str, Any]] = {
			"shortcuts": {},
			"general": {},
		}
		self._load()

	@staticmethod
	def _default_config_path() -> Path:
		return Path.home() / ".commandflow" / "settings.json"

	@staticmethod
	def _merge(defaults: Dict[str, Any], provided: Mapping[str, Any]) -> Dict[str, Any]:
		merged: Dict[str, Any] = deepcopy(defaults)
		for key, value in provided.items():
			if key not in defaults:
				continue
			default_value = defaults[key]
			if isinstance(default_value, dict) and isinstance(value, Mapping):
				merged[key] = SettingsManager._merge(dict(default_value), value)
			else:
				merged[key] = value
		return merged

	def _load(self) -> None:
		defaults: Dict[str, Dict[str, Any]] = {
			"shortcuts": dict(self.DEFAULTS["shortcuts"]),
			"general": dict(self.DEFAULTS["general"]),
		}
		if self._config_path.exists():
			try:
				loaded = json.loads(self._config_path.read_text(encoding="utf-8"))
			except Exception:
				loaded = {}
			else:
				if not isinstance(loaded, dict):
					loaded = {}
			defaults = SettingsManager._merge(defaults, loaded)
		self._data = {
			"shortcuts": dict(defaults.get("shortcuts", {})),
			"general": dict(defaults.get("general", {})),
		}
		self._emit_full_update()

	def save(self) -> None:
		self._config_path.parent.mkdir(parents=True, exist_ok=True)
		payload = {
			"shortcuts": dict(self._data["shortcuts"]),
			"general": dict(self._data["general"]),
		}
		self._config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

	def get_shortcut(self, action_id: str) -> str:
		value = self._data.setdefault("shortcuts", {}).get(action_id, "")
		return str(value) if value is not None else ""

	def get_shortcut_label(self, action_id: str) -> str:
		meta = self.SHORTCUT_METADATA.get(action_id, {})
		return str(meta.get("label", action_id))

	def shortcuts(self) -> Dict[str, str]:
		return {key: str(value) for key, value in self._data.setdefault("shortcuts", {}).items()}

	def shortcut_items(self) -> Iterable[Tuple[str, Mapping[str, str]]]:
		for key in self.SHORTCUT_METADATA:
			yield key, self.SHORTCUT_METADATA[key]

	def general_items(self) -> Iterable[Tuple[str, Mapping[str, str]]]:
		for key in self.GENERAL_METADATA:
			yield key, self.GENERAL_METADATA[key]

	def get_general(self, key: str) -> bool:
		value = self._data.setdefault("general", {}).get(key)
		if value is None:
			return bool(self.DEFAULTS["general"].get(key, False))
		return bool(value)

	def apply(
		self,
		*,
		shortcuts: Mapping[str, str] | None = None,
		general: Mapping[str, object] | None = None,
	) -> None:
		updated_shortcuts = False
		updated_general = False
		if shortcuts is not None:
			cleaned: Dict[str, str] = {key: str(value) for key, value in self.DEFAULTS["shortcuts"].items()}
			for key, value in shortcuts.items():
				if key in cleaned:
					cleaned[key] = str(value)
			if cleaned != self._data["shortcuts"]:
				self._data["shortcuts"] = cleaned
				updated_shortcuts = True
		if general is not None:
			cleaned_general = dict(self.DEFAULTS["general"])
			for key, value in general.items():
				if key in cleaned_general:
					cleaned_general[key] = bool(value)
			if cleaned_general != self._data["general"]:
				self._data["general"] = cleaned_general
				updated_general = True
		if updated_shortcuts or updated_general:
			self.save()
			self._emit_full_update()
		if updated_shortcuts:
			self.shortcuts_changed.emit(self.shortcuts())
		if updated_general:
			self.general_changed.emit(dict(self._data["general"]))

	def _emit_full_update(self) -> None:
		payload = {
			"shortcuts": self.shortcuts(),
			"general": {key: bool(value) for key, value in self._data["general"].items()},
		}
		self.settings_changed.emit(payload)
