"""Parakeet local model path detection tests."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python"))

from afk_backend import config  # noqa: E402
from afk_backend.transcription.parakeet import _has_local_model_files  # noqa: E402


class TestParakeetLocalFiles(unittest.TestCase):
    def test_required_files_match_current_onnx_asr_layout(self):
        self.assertNotIn("nemo128.onnx", config.LOCAL_ASR_REQUIRED)
        self.assertIn("encoder-model.int8.onnx", config.LOCAL_ASR_REQUIRED)
        self.assertIn("decoder_joint-model.int8.onnx", config.LOCAL_ASR_REQUIRED)

    def test_complete_local_dir_does_not_need_bundled_preprocessor_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in config.LOCAL_ASR_REQUIRED:
                (root / name).write_text("", encoding="utf-8")
            self.assertTrue(_has_local_model_files(root))
            self.assertFalse((root / "nemo128.onnx").exists())

    def test_local_asr_dir_prefers_complete_bundled_resources_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_config = root / "resources" / "python" / "afk_backend" / "config.py"
            bundled = root / "resources" / "models" / "parakeet-v3"
            bundled.mkdir(parents=True)
            fake_config.parent.mkdir(parents=True, exist_ok=True)
            fake_config.write_text("", encoding="utf-8")
            for name in config.LOCAL_ASR_REQUIRED:
                (bundled / name).write_text("", encoding="utf-8")

            with patch.object(config, "__file__", str(fake_config)):
                with patch.dict(config.os.environ, {"AFK_DATA_DIR": str(root / "data")}, clear=False):
                    self.assertTrue(config.local_asr_dir().samefile(bundled))


if __name__ == "__main__":
    unittest.main()
