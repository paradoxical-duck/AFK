"""Small local history log for successful dictations."""

import json
import os
import tempfile
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, List

from .. import config, logutil


MAX_HISTORY = 80


class HistoryStore:
    def __init__(self) -> None:
        self._path = config.history_path()
        self._lock = threading.Lock()
        self._data = self._load()

    def snapshot(self, limit: int = 24) -> Dict[str, Any]:
        with self._lock:
            items = list(self._data.get("items", []))
        limit = max(1, min(int(limit or 24), MAX_HISTORY))
        return {"items": deepcopy(items[-limit:][::-1]), "count": len(items)}

    def record(self, text: str, *, action: str = "") -> Dict[str, Any]:
        text = _clean_text(text)
        if not text:
            return {"ok": False, "reason": "empty"}
        item = {
            "id": uuid.uuid4().hex,
            "text": text,
            "action": action or "",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        with self._lock:
            items: List[Dict[str, Any]] = self._data.setdefault("items", [])
            items.append(item)
            del items[:-MAX_HISTORY]
            self._save()
        return {"ok": True, "item": deepcopy(item)}

    def delete(self, item_id: str) -> Dict[str, Any]:
        item_id = str(item_id or "")
        with self._lock:
            items = self._data.setdefault("items", [])
            before = len(items)
            self._data["items"] = [item for item in items if item.get("id") != item_id]
            changed = len(self._data["items"]) != before
            if changed:
                self._save()
        return {"ok": changed, **self.snapshot()}

    def clear(self) -> Dict[str, Any]:
        with self._lock:
            self._data = _default_data()
            self._save()
        return self.snapshot()

    def _load(self) -> Dict[str, Any]:
        data = _default_data()
        if not self._path.exists():
            return data
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                user = json.load(fh)
            if isinstance(user, dict) and isinstance(user.get("items"), list):
                data["items"] = user["items"][-MAX_HISTORY:]
        except Exception as exc:  # noqa: BLE001
            logutil.warn(f"Failed to read transcription history: {exc}")
        return data

    def _save(self) -> None:
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, self._path)


def _default_data() -> Dict[str, Any]:
    return {"version": 1, "items": []}


def _clean_text(value: str) -> str:
    return " ".join(str(value or "").split())
