"""Phase 2 audio DSP tests — no model, no microphone required."""

import sys
import unittest
from unittest import mock
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python"))

from afk_backend.audio.recorder import Recorder, _resample, _trim_silence, levels, process, signal_too_quiet  # noqa: E402


class TestDsp(unittest.TestCase):
    def test_resample_length(self):
        x = np.linspace(-1, 1, 48000, dtype=np.float32)
        y = _resample(x, 48000, 16000)
        self.assertEqual(len(y), 16000)
        self.assertEqual(y.dtype, np.float32)

    def test_resample_noop(self):
        x = np.zeros(100, dtype=np.float32)
        self.assertIs(_resample(x, 16000, 16000), x)

    def test_trim_silence_removes_lead_trail(self):
        sr = 16000
        speech = (np.random.randn(sr).astype(np.float32)) * 0.5
        clip = np.concatenate([np.zeros(sr, np.float32), speech, np.zeros(sr, np.float32)])
        trimmed = _trim_silence(clip, sr)
        self.assertLess(len(trimmed), len(clip))
        self.assertGreater(len(trimmed), 0)

    def test_trim_all_silence_keeps_audio(self):
        x = np.zeros(8000, dtype=np.float32)
        self.assertEqual(len(_trim_silence(x, 16000)), len(x))

    def test_process_auto_gain_normalizes(self):
        sr = 16000
        quiet = (np.random.randn(sr).astype(np.float32)) * 0.05
        out = process(quiet, sr, noise_suppression=False, silence_trim=False, auto_gain=True)
        self.assertGreater(float(np.max(np.abs(out))), float(np.max(np.abs(quiet))))
        self.assertLessEqual(float(np.max(np.abs(out))), 1.0)

    def test_process_empty(self):
        out = process(np.zeros(0, dtype=np.float32))
        self.assertEqual(len(out), 0)

    def test_levels_and_quiet_signal_detection(self):
        quiet = np.ones(16000, dtype=np.float32) * 0.00001
        loud = np.ones(16000, dtype=np.float32) * 0.01

        self.assertTrue(signal_too_quiet(quiet))
        self.assertFalse(signal_too_quiet(loud))
        self.assertEqual(levels(quiet)["samples"], 16000)
        self.assertGreater(levels(loud)["peak"], levels(quiet)["peak"])


class TestMacCapture(unittest.TestCase):
    def test_uses_selected_device_native_sample_rate(self):
        from afk_backend.audio import recorder as recorder_module

        opened = {}

        class FakeStream:
            def __init__(self, **kwargs):
                opened.update(kwargs)

            def start(self):
                return None

        fake_sd = mock.Mock()
        fake_sd.default.device = [0, 1]
        fake_sd.query_devices.side_effect = [
            [{"name": "Phone Mic", "max_input_channels": 1, "default_samplerate": 48000}],
            {"name": "Phone Mic", "max_input_channels": 1, "default_samplerate": 48000},
        ]
        fake_sd.InputStream = FakeStream

        with mock.patch.object(recorder_module, "sd", fake_sd), mock.patch.object(
            recorder_module.sys, "platform", "darwin"
        ):
            recorder = Recorder()
            recorder.start("Phone Mic")

        self.assertEqual(opened["device"], 0)
        self.assertEqual(opened["samplerate"], 48000)
        self.assertEqual(opened["blocksize"], 0)


if __name__ == "__main__":
    unittest.main()
