"""Phase 1 backend tests — pure stdlib (unittest), no third-party deps.

Covers:
  * In-process dispatch (ping, get_info, settings round-trip).
  * End-to-end JSON-RPC over a real subprocess (the contract Electron relies on).

Run:  py -3.11 -m unittest discover -s tests/python
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
PY_DIR = REPO / "python"
sys.path.insert(0, str(PY_DIR))


class TestDispatch(unittest.TestCase):
    def setUp(self):
        # Isolate data dir so we don't touch the user's real settings.
        self._tmp = tempfile.mkdtemp()
        os.environ["AFK_DATA_DIR"] = self._tmp
        from afk_backend.app import AFKApp
        self.app = AFKApp()

    def test_ping(self):
        self.assertEqual(self.app.dispatch("ping", {}), {"pong": True})

    def test_get_info(self):
        info = self.app.dispatch("get_info", {})
        self.assertEqual(info["backend"], "afk-backend")
        self.assertIn("python", info)
        self.assertIn("asr_model", info)

    def test_settings_roundtrip(self):
        before = self.app.dispatch("get_settings", {})
        self.assertIn("hotkeys", before)
        self.assertEqual(before["word_count_threshold"], 100)
        self.assertTrue(before["auto_capitalization"])
        self.assertTrue(before["auto_punctuation"])
        self.assertTrue(before["training_corrections"])
        updated = self.app.dispatch("update_settings", {"patch": {"word_count_threshold": 42}})
        self.assertEqual(updated["word_count_threshold"], 42)
        # nested merge preserves siblings
        updated2 = self.app.dispatch(
            "update_settings", {"patch": {"hotkeys": {"clarify": "Ctrl+Alt+C"}}}
        )
        self.assertEqual(updated2["hotkeys"]["clarify"], "Ctrl+Alt+C")
        expected_ptt = "Option" if sys.platform == "darwin" else "Ctrl+Space"
        self.assertEqual(updated2["hotkeys"]["push_to_talk"], expected_ptt)

    def test_training_methods_registered(self):
        methods = self.app.dispatch("list_methods", {})
        self.assertIn("start_training_sample", methods)
        self.assertIn("finish_training_sample", methods)

    def test_transcript_formatting_helper(self):
        from afk_backend.app import _format_transcript_text

        self.assertEqual(_format_transcript_text("hello there.", capitalization=True, punctuation=True), "Hello there.")
        self.assertEqual(_format_transcript_text("Hello there.", capitalization=False, punctuation=True), "hello there.")
        self.assertEqual(_format_transcript_text("Hello, there!", capitalization=True, punctuation=False), "Hello there")
        self.assertEqual(
            _format_transcript_text("hello comma there exclamation point", capitalization=True, punctuation=True),
            "Hello, there!",
        )
        self.assertEqual(
            _format_transcript_text("first line new line second line", capitalization=True, punctuation=True),
            "First line\nsecond line",
        )

    def test_unknown_method(self):
        from afk_backend.rpc import RpcError
        with self.assertRaises(RpcError):
            self.app.dispatch("does_not_exist", {})

    def test_short_recording_skips_transcription(self):
        class FakeRecorder:
            is_recording = False

            def stop(self):
                return {
                    "audio": np.ones(800, dtype=np.float32),
                    "duration": 0.05,
                    "sr": 16000,
                }

        class BombTranscriber:
            def transcribe(self, *_args, **_kwargs):
                raise AssertionError("short recording should not transcribe")

        self.app.recorder = FakeRecorder()
        self.app.transcriber = BombTranscriber()
        result = self.app.stop_recording({})
        self.assertEqual(result["text"], "")
        self.assertEqual(result["reason"], "too_short")

    def test_stop_recording_waits_for_tail_before_stopping(self):
        events = []

        class FakeRecorder:
            is_recording = True

            def stop(self):
                events.append("stop")
                return {
                    "audio": np.zeros(0, dtype=np.float32),
                    "duration": 0.4,
                    "sr": 16000,
                }

        self.app.recorder = FakeRecorder()
        from afk_backend import app as appmod

        original_sleep = appmod.time.sleep
        try:
            appmod.time.sleep = lambda seconds: events.append(("sleep", seconds))
            self.app.stop_recording({})
        finally:
            appmod.time.sleep = original_sleep
        self.assertEqual(events[0], ("sleep", appmod.RECORDING_TAIL_SECONDS))
        self.assertEqual(events[1], "stop")


class TestSubprocessRpc(unittest.TestCase):
    """Exercise the exact stdio contract Electron uses."""

    def test_handshake(self):
        env = dict(os.environ)
        env["AFK_DATA_DIR"] = tempfile.mkdtemp()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["AFK_NO_PRELOAD"] = "1"  # don't spawn model processes during the test

        proc = subprocess.Popen(
            [sys.executable, str(PY_DIR / "main.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        try:
            # First line should be the 'ready' event.
            ready = json.loads(proc.stdout.readline())
            self.assertEqual(ready.get("event"), "ready")

            # Send a ping request.
            proc.stdin.write(json.dumps({"id": 1, "method": "ping", "params": {}}) + "\n")
            proc.stdin.flush()
            resp = json.loads(proc.stdout.readline())
            self.assertEqual(resp["id"], 1)
            self.assertEqual(resp["result"], {"pong": True})
        finally:
            try:
                proc.stdin.write(json.dumps({"id": 99, "method": "shutdown"}) + "\n")
                proc.stdin.flush()
            except Exception:
                pass
            proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
