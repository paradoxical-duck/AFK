"""Transcription history tests."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python"))


class TestHistoryStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["AFK_DATA_DIR"] = self._tmp

    def test_records_recent_transcriptions_newest_first(self):
        from afk_backend.history import HistoryStore

        store = HistoryStore()
        first = store.record("first transcript", action="pasted")
        second = store.record("second transcript", action="copied")

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        items = store.snapshot()["items"]
        self.assertEqual([item["text"] for item in items], ["second transcript", "first transcript"])

    def test_delete_history_item(self):
        from afk_backend.history import HistoryStore

        store = HistoryStore()
        item = store.record("keep me", action="pasted")["item"]
        result = store.delete(item["id"])

        self.assertTrue(result["ok"])
        self.assertEqual(result["items"], [])


if __name__ == "__main__":
    unittest.main()
