"""Phase 4 Clarify routing tests — stubbed models, no GGUF needed."""

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python"))

from afk_backend.clarify.engine import ClarifyEngine, _clean_correction  # noqa: E402


class FakeModel:
    def __init__(self, name, available=True, fail=False):
        self.name = name
        self._available = available
        self.fail = fail
        self.calls = []

    @property
    def available(self):
        return self._available

    @property
    def status(self):
        return "loaded" if self._available else "missing"

    def clarify(self, text):
        self.calls.append(text)
        if self.fail:
            raise RuntimeError(f"{self.name} failed")
        return f"[{self.name}] {text}"


class TestRouting(unittest.TestCase):
    def setUp(self):
        self.engine = ClarifyEngine()
        self.short = FakeModel("short")
        self.long = FakeModel("long")
        self.engine.short = self.short
        self.engine.long = self.long

    def test_short_text_uses_short_model(self):
        res = self.engine.clarify("one two three", threshold=60)
        self.assertEqual(res["model"], "short")
        self.assertEqual(self.short.calls, ["one two three"])
        self.assertEqual(self.long.calls, [])

    def test_long_text_uses_long_model(self):
        text = " ".join(["word"] * 80)
        res = self.engine.clarify(text, threshold=60)
        self.assertEqual(res["model"], "long")
        self.assertEqual(len(self.long.calls), 1)

    def test_threshold_boundary_inclusive_for_short(self):
        text = " ".join(["w"] * 60)  # exactly threshold -> short (<=)
        res = self.engine.clarify(text, threshold=60)
        self.assertEqual(res["model"], "short")

    def test_fallback_when_long_missing(self):
        self.long._available = False
        text = " ".join(["w"] * 100)
        res = self.engine.clarify(text, threshold=60)
        self.assertEqual(res["model"], "short")  # fell back

    def test_fallback_when_preferred_model_fails(self):
        self.long.fail = True
        text = " ".join(["w"] * 100)
        res = self.engine.clarify(text, threshold=60)
        self.assertEqual(res["model"], "short")
        self.assertEqual(len(self.long.calls), 1)
        self.assertEqual(len(self.short.calls), 1)

    def test_fallback_when_short_missing(self):
        self.short._available = False
        res = self.engine.clarify("hi there", threshold=60)
        self.assertEqual(res["model"], "long")

    def test_passthrough_when_none_available(self):
        self.short._available = False
        self.long._available = False
        res = self.engine.clarify("untouched text", threshold=60)
        self.assertEqual(res["model"], "none")
        self.assertEqual(res["text"], "untouched text")

    def test_empty_text(self):
        res = self.engine.clarify("   ", threshold=60)
        self.assertEqual(res["model"], "none")
        self.assertEqual(res["text"], "")

    def test_clean_correction_removes_wrappers(self):
        self.assertEqual(_clean_correction('Correction: "Hello there."'), "Hello there.")
        self.assertEqual(_clean_correction("```\nHello there.\n```"), "Hello there.")

    def test_clean_correction_removes_chat_template_leak(self):
        leaked = (
            "This is fixed.<|im_end|>\n<|im_start|>user\nInput: i dont know where he wnet"
            "<|im_end|>\n<|im_start|>assistant\nI do not know where he went."
        )
        self.assertEqual(_clean_correction(leaked), "This is fixed.")

    def test_clean_correction_stops_before_extra_examples(self):
        leaked = "This is fixed.\nInput: he dont know where he wnet yesterday"
        self.assertEqual(_clean_correction(leaked), "This is fixed.")


if __name__ == "__main__":
    unittest.main()
