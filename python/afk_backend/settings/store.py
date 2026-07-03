"""JSON-backed settings store with sane defaults and atomic writes.

Settings are intentionally simple (a flat-ish dict) so the renderer can read
and patch them over RPC without a schema migration system. Unknown keys from
the user file are preserved; missing keys are filled from defaults.
"""

import json
import os
import sys
import tempfile
import threading
from copy import deepcopy
from typing import Any, Dict

from .. import config, logutil

WINDOWS_HOTKEYS = {
    "push_to_talk": "Ctrl+Space",        # held to record
    "toggle": "Ctrl+Shift+Space",        # toggle recording
    "clarify": "Ctrl+Alt+K",             # clarify selection/clipboard
    "learn_correction": "Ctrl+Alt+L",    # learn selection/clipboard as correction
}

MAC_HOTKEYS = {
    "push_to_talk": "Option",            # held to record
    "toggle": "Option+Space",            # toggle recording
    "clarify": "Cmd+Option+K",           # clarify selection/clipboard
    "learn_correction": "Cmd+Option+L",  # learn selection/clipboard as correction
}

MAC_HOTKEY_MIGRATIONS = {
    "Ctrl+Option+K": "Cmd+Option+K",
    "Control+Option+K": "Cmd+Option+K",
    "Ctrl+Option+L": "Cmd+Option+L",
    "Control+Option+L": "Cmd+Option+L",
    "Ctrl+Option+D": "Cmd+Option+D",
    "Control+Option+D": "Cmd+Option+D",
    "Ctrl+Shift+K": "Cmd+Shift+K",
    "Control+Shift+K": "Cmd+Shift+K",
    "Ctrl+Shift+L": "Cmd+Shift+L",
    "Control+Shift+L": "Cmd+Shift+L",
}


def default_hotkeys() -> Dict[str, str]:
    return deepcopy(MAC_HOTKEYS if sys.platform == "darwin" else WINDOWS_HOTKEYS)


DEFAULT_SETTINGS: Dict[str, Any] = {
    "microphone": None,            # device name; None = system default
    "theme": "dark",
    "startup_on_login": True,
    "launch_minimized": True,
    "auto_paste": True,
    "auto_clarify": False,
    "auto_capitalization": True,
    "auto_punctuation": True,
    "training_corrections": True,
    "word_count_threshold": config.DEFAULT_WORD_THRESHOLD,
    "logging": True,
    "developer_mode": False,
    "hotkeys": default_hotkeys(),
    "noise_suppression": True,
    "auto_gain": True,
    "silence_trim": True,
}


class SettingsStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._path = config.settings_path()
        self._data = self._load()

    def _load(self) -> Dict[str, Any]:
        data = deepcopy(DEFAULT_SETTINGS)
        if self._path.exists():
            try:
                with open(self._path, "r", encoding="utf-8") as fh:
                    user = json.load(fh)
                data = _deep_merge(data, user)
                before_migration = deepcopy(data)
                data = _migrate_settings(data)
                if data != before_migration:
                    _atomic_write_json(self._path, data)
            except Exception as exc:  # noqa: BLE001
                logutil.warn(f"Failed to read settings, using defaults: {exc}")
        return data

    def all(self) -> Dict[str, Any]:
        with self._lock:
            return deepcopy(self._data)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return deepcopy(self._data.get(key, default))

    def update(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self._data = _deep_merge(self._data, patch)
            self._save()
            return deepcopy(self._data)

    def _save(self) -> None:
        try:
            _atomic_write_json(self._path, self._data)
        except Exception as exc:  # noqa: BLE001
            logutil.error(f"Failed to save settings: {exc}")


def _atomic_write_json(path, data) -> None:
    """Write JSON to `path` atomically (temp file + os.replace)."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    out = deepcopy(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = deepcopy(v)
    return out


def _migrate_settings(data: Dict[str, Any]) -> Dict[str, Any]:
    hotkeys = data.get("hotkeys") or {}
    if hotkeys.get("clarify") == "Ctrl+Shift+C":
        hotkeys["clarify"] = DEFAULT_SETTINGS["hotkeys"]["clarify"]
    if hotkeys.get("push_to_talk") in {None, "Ctrl+Shift+Space"}:
        hotkeys["push_to_talk"] = DEFAULT_SETTINGS["hotkeys"]["push_to_talk"]
    if hotkeys.get("toggle") in {None, "Ctrl+Alt+Space"}:
        hotkeys["toggle"] = DEFAULT_SETTINGS["hotkeys"]["toggle"]
    if not hotkeys.get("learn_correction"):
        hotkeys["learn_correction"] = DEFAULT_SETTINGS["hotkeys"]["learn_correction"]
    if sys.platform == "darwin" and data.get("_mac_hotkeys_migrated") is not True:
        if hotkeys.get("push_to_talk") == WINDOWS_HOTKEYS["push_to_talk"]:
            hotkeys["push_to_talk"] = MAC_HOTKEYS["push_to_talk"]
        if hotkeys.get("toggle") == WINDOWS_HOTKEYS["toggle"]:
            hotkeys["toggle"] = MAC_HOTKEYS["toggle"]
        if hotkeys.get("clarify") == WINDOWS_HOTKEYS["clarify"]:
            hotkeys["clarify"] = MAC_HOTKEYS["clarify"]
        if hotkeys.get("learn_correction") == WINDOWS_HOTKEYS["learn_correction"]:
            hotkeys["learn_correction"] = MAC_HOTKEYS["learn_correction"]
        data["_mac_hotkeys_migrated"] = True
    if sys.platform == "darwin" and data.get("_mac_control_hotkeys_migrated") is not True:
        for key, value in list(hotkeys.items()):
            hotkeys[key] = MAC_HOTKEY_MIGRATIONS.get(value, value)
        data["_mac_control_hotkeys_migrated"] = True
    data["hotkeys"] = hotkeys
    if data.get("word_count_threshold") in {4, 42, 60} and data.get("_word_count_threshold_migrated") is not True:
        data["word_count_threshold"] = DEFAULT_SETTINGS["word_count_threshold"]
        data["_word_count_threshold_migrated"] = True
    if data.get("auto_clarify") is True and data.get("_auto_clarify_migrated") is not True:
        data["auto_clarify"] = DEFAULT_SETTINGS["auto_clarify"]
        data["_auto_clarify_migrated"] = True
    return data
