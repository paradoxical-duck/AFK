"""Voice adaptation tests."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python"))


class TestAdaptationStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["AFK_DATA_DIR"] = self._tmp

    def test_learned_phrase_applies_to_future_transcripts(self):
        from afk_backend.adaptation import AdaptationStore

        store = AdaptationStore()
        res = store.learn_correction("Pachisia gw", "Pachisia GW")
        self.assertTrue(res["ok"])

        text, changed, applied = store.apply("Send this to Pachisia gw today.")
        self.assertTrue(changed)
        self.assertEqual(text, "Send this to Pachisia GW today.")
        self.assertEqual(applied[0]["intended"], "Pachisia GW")

    def test_learned_correction_persists(self):
        from afk_backend.adaptation import AdaptationStore

        first = AdaptationStore()
        first.learn_correction("kesh of", "Keshav")

        second = AdaptationStore()
        text, changed, _applied = second.apply("Credit kesh of in the docs.")
        self.assertTrue(changed)
        self.assertEqual(text, "Credit Keshav in the docs.")

    def test_clear_resets_learning(self):
        from afk_backend.adaptation import AdaptationStore

        store = AdaptationStore()
        store.learn_correction("af kay", "AFK")
        snapshot = store.clear()
        self.assertEqual(snapshot["correction_count"], 0)

    def test_trigger_training_replaces_spoken_phrase_with_output(self):
        from afk_backend.adaptation import AdaptationStore

        store = AdaptationStore()
        res = store.record_training(
            kind="trigger",
            spoken="my github username is",
            output="paradoxical_duck",
            heard="my github username is",
        )
        self.assertTrue(res["ok"])

        text, changed, _applied = store.apply("my github username is")
        self.assertTrue(changed)
        self.assertEqual(text, "paradoxical_duck")
        self.assertEqual(store.snapshot()["trigger_count"], 1)

    def test_trigger_training_is_case_insensitive_but_preserves_output_case(self):
        from afk_backend.adaptation import AdaptationStore

        store = AdaptationStore()
        res = store.record_training(
            kind="trigger",
            spoken="my github username is",
            output="paradoxical_duck",
            heard="my github username is",
        )
        self.assertTrue(res["ok"])

        text, changed, _applied = store.apply("My GitHub Username Is")
        self.assertTrue(changed)
        self.assertEqual(text, "paradoxical_duck")

    def test_trigger_autofill_keeps_spoken_phrase_and_exact_output(self):
        from afk_backend.adaptation import AdaptationStore

        store = AdaptationStore()
        res = store.record_training(
            kind="trigger",
            spoken="my email is",
            output="sohumarora28@gmail.com",
            heard="my email is",
            trigger_type="autofill",
        )
        self.assertTrue(res["ok"])

        text, changed, _applied = store.apply("My Email Is")
        self.assertTrue(changed)
        self.assertEqual(text, "my email is sohumarora28@gmail.com")

    def test_delete_training_removes_saved_sample_and_correction(self):
        from afk_backend.adaptation import AdaptationStore

        store = AdaptationStore()
        res = store.record_training("word", "sohum", "Sohum", "sohum")
        self.assertTrue(res["ok"])

        snapshot = store.delete_training(res["id"])
        self.assertTrue(snapshot["ok"])
        self.assertEqual(snapshot["training_count"], 0)

        text, changed, _applied = store.apply("hello sohum")
        self.assertFalse(changed)
        self.assertEqual(text, "hello sohum")


if __name__ == "__main__":
    unittest.main()
