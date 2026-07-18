"""Local usage statistics — tracked entirely on-device (JSON-backed).

Records dictation and clarification events and derives every metric the
spec asks for: words spoken (today/week/month/lifetime), WPM, longest and
average recording, total transcription time, average transcription and
clarification latency, clarification count, current/longest streak, and typing
time saved (assuming config.TYPING_WPM).

Raw per-day buckets are persisted; aggregates are computed on read so the
windows (today/week/month) are always correct relative to the current date.
"""

import threading
from copy import deepcopy
from datetime import date, datetime, timedelta
from typing import Any, Dict

from .. import config, logutil
from ..settings.store import _atomic_write_json  # shared atomic JSON writer


def _today_str() -> str:
    return date.today().isoformat()


class StatsStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._path = config.stats_path()
        self._data = self._load()

    # ---- persistence ----
    def _default(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "totals": {
                "words": 0,
                "recordings": 0,
                "recording_seconds": 0.0,
                "longest_recording": 0.0,
                "transcription_seconds": 0.0,   # wall time spent transcribing
                "transcription_latency_ms_sum": 0,
                "clarifications": 0,
                "clarify_latency_ms_sum": 0,
            },
            "daily": {},   # "YYYY-MM-DD" -> {"words": int, "recordings": int, "recording_seconds": float}
            "streak": {"current": 0, "longest": 0, "last_day": None},
        }

    def _load(self) -> Dict[str, Any]:
        import json
        data = self._default()
        if self._path.exists():
            try:
                with open(self._path, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                # shallow-merge known keys
                for k in data:
                    if k in loaded:
                        data[k] = loaded[k]
            except Exception as exc:  # noqa: BLE001
                logutil.warn(f"Failed to read stats, starting fresh: {exc}")
        return data

    def _save(self) -> None:
        try:
            _atomic_write_json(self._path, self._data)
        except Exception as exc:  # noqa: BLE001
            logutil.error(f"Failed to save stats: {exc}")

    # ---- recording ----
    def record_dictation(self, words: int, recording_seconds: float, transcribe_ms: int) -> None:
        if words <= 0 and recording_seconds <= 0:
            return
        with self._lock:
            t = self._data["totals"]
            t["words"] += int(words)
            t["recordings"] += 1
            t["recording_seconds"] += float(recording_seconds)
            t["longest_recording"] = max(t["longest_recording"], float(recording_seconds))
            t["transcription_latency_ms_sum"] += int(transcribe_ms)
            t["transcription_seconds"] += float(transcribe_ms) / 1000.0

            day = _today_str()
            d = self._data["daily"].setdefault(day, {"words": 0, "recordings": 0, "recording_seconds": 0.0})
            d["words"] += int(words)
            d["recordings"] += 1
            d["recording_seconds"] += float(recording_seconds)

            self._update_streak(day)
            self._save()

    def record_clarification(self, latency_ms: int) -> None:
        with self._lock:
            t = self._data["totals"]
            t["clarifications"] += 1
            t["clarify_latency_ms_sum"] += int(latency_ms)
            self._save()

    def _update_streak(self, day: str) -> None:
        s = self._data["streak"]
        last = s.get("last_day")
        if last == day:
            return
        if last is None:
            s["current"] = 1
        else:
            try:
                gap = (date.fromisoformat(day) - date.fromisoformat(last)).days
            except Exception:
                gap = 99
            s["current"] = s["current"] + 1 if gap == 1 else 1
        s["last_day"] = day
        s["longest"] = max(s.get("longest", 0), s["current"])

    # ---- aggregation ----
    def _window_words(self, days: int) -> int:
        today = date.today()
        start = today - timedelta(days=days - 1)
        total = 0
        for k, v in self._data["daily"].items():
            try:
                d = date.fromisoformat(k)
            except Exception:
                continue
            if start <= d <= today:
                total += v.get("words", 0)
        return total

    def _current_streak(self) -> int:
        # A streak is only "current" if the last active day was today or yesterday.
        s = self._data["streak"]
        last = s.get("last_day")
        if not last:
            return 0
        try:
            gap = (date.today() - date.fromisoformat(last)).days
        except Exception:
            return 0
        return s.get("current", 0) if gap <= 1 else 0

    def _activity(self, days: int = 14) -> list[Dict[str, Any]]:
        """Return a continuous recent series for truthful UI charts."""
        today = date.today()
        start = today - timedelta(days=days - 1)
        activity = []
        for offset in range(days):
            current = start + timedelta(days=offset)
            bucket = self._data["daily"].get(current.isoformat(), {})
            activity.append({
                "date": current.isoformat(),
                "words": int(bucket.get("words", 0)),
                "recordings": int(bucket.get("recordings", 0)),
                "recording_seconds": round(float(bucket.get("recording_seconds", 0.0)), 1),
            })
        return activity

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            t = self._data["totals"]
            recordings = max(1, t["recordings"])
            words = t["words"]
            rec_sec = t["recording_seconds"]
            wpm = (words / (rec_sec / 60.0)) if rec_sec > 0 else 0.0
            typing_minutes_saved = words / float(config.TYPING_WPM) if config.TYPING_WPM else 0.0

            return {
                "words": {
                    "today": self._window_words(1),
                    "week": self._window_words(7),
                    "month": self._window_words(30),
                    "lifetime": words,
                },
                "wpm_avg": round(wpm, 1),
                "longest_recording_sec": round(t["longest_recording"], 1),
                "avg_recording_sec": round(rec_sec / recordings, 1),
                "total_transcription_sec": round(t["transcription_seconds"], 1),
                "avg_transcription_latency_ms": round(t["transcription_latency_ms_sum"] / recordings),
                "avg_clarify_latency_ms": (
                    round(t["clarify_latency_ms_sum"] / t["clarifications"])
                    if t["clarifications"] else 0
                ),
                "clarifications": t["clarifications"],
                "recordings": t["recordings"],
                "streak_current": self._current_streak(),
                "streak_longest": self._data["streak"].get("longest", 0),
                "typing_minutes_saved": round(typing_minutes_saved, 1),
                "typing_wpm_assumed": config.TYPING_WPM,
                "activity": self._activity(),
            }

    def reset(self) -> None:
        with self._lock:
            self._data = self._default()
            self._save()
