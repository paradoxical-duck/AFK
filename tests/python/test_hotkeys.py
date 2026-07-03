"""Phase 3 hotkey logic tests — drives the manager with synthetic key events.

No real keyboard hook is installed; we call the listener handlers directly.
"""

import sys
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python"))

from afk_backend.hotkeys import parse_combo  # noqa: E402
from afk_backend.hotkeys.manager import HotkeyManager  # noqa: E402
from pynput import keyboard  # noqa: E402

CTRL = keyboard.Key.ctrl_l
SHIFT = keyboard.Key.shift
ALT = keyboard.Key.alt_l
SPACE = keyboard.Key.space
C = keyboard.KeyCode(vk=67)  # 'C'
K = keyboard.KeyCode(vk=75)  # 'K'
L = keyboard.KeyCode(vk=76)  # 'L'


class TestParse(unittest.TestCase):
    def test_parse_basic(self):
        self.assertEqual(parse_combo("Ctrl+Space"), (frozenset({"ctrl"}), "space"))
        self.assertEqual(parse_combo("Ctrl+Alt+K"), (frozenset({"ctrl", "alt"}), "k"))
        self.assertEqual(parse_combo("Alt+Win+K"), (frozenset({"alt", "win"}), "k"))
        self.assertEqual(parse_combo("Option"), (frozenset({"alt"}), None))

    def test_parse_aliases(self):
        self.assertEqual(parse_combo("control+space")[0], frozenset({"ctrl"}))
        self.assertEqual(parse_combo("cmd+k")[0], frozenset({"win"}))

    def test_parse_invalid(self):
        self.assertIsNone(parse_combo(""))
        self.assertIsNone(parse_combo("Ctrl+Shift"))  # no main key


class TestManager(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.mgr = HotkeyManager(
            {
                "ptt_start": lambda: self.events.append("ptt_start"),
                "ptt_stop": lambda: self.events.append("ptt_stop"),
                "toggle": lambda: self.events.append("toggle"),
                "clarify": lambda: self.events.append("clarify"),
                "learn_correction": lambda: self.events.append("learn_correction"),
                "cancel": lambda: self.events.append("cancel"),
            }
        )
        self.mgr.set_bindings(
            {
                "push_to_talk": "ctrl+space",
                "toggle": "ctrl+shift+space",
                "clarify": "ctrl+alt+k",
                "learn_correction": "ctrl+alt+l",
            }
        )

    def test_push_to_talk_hold(self):
        self.mgr._on_press(CTRL)
        self.mgr._on_press(SPACE)
        self.assertEqual(self.events, ["ptt_start"])
        self.mgr._on_release(SPACE)
        self.assertEqual(self.events, ["ptt_start", "ptt_stop"])
        self.mgr._on_release(CTRL)

    def test_ptt_stops_when_modifier_released_first(self):
        self.mgr._on_press(CTRL)
        self.mgr._on_press(SPACE)
        self.mgr._on_release(CTRL)  # release ctrl while still holding space
        self.assertEqual(self.events, ["ptt_start", "ptt_stop"])

    def test_toggle_not_confused_with_ptt(self):
        # Ctrl+Shift+Space must fire toggle, NOT push-to-talk.
        self.mgr._on_press(CTRL)
        self.mgr._on_press(SHIFT)
        self.mgr._on_press(SPACE)
        self.assertEqual(self.events, ["toggle"])
        self.assertNotIn("ptt_start", self.events)
        self.mgr._on_release(SPACE)
        self.mgr._on_release(SHIFT)
        self.mgr._on_release(CTRL)

    def test_toggle_fires_once_per_press(self):
        self.mgr._on_press(CTRL)
        self.mgr._on_press(SHIFT)
        self.mgr._on_press(SPACE)
        self.mgr._on_press(SPACE)  # auto-repeat shouldn't re-fire
        self.assertEqual(self.events.count("toggle"), 1)

    def test_clarify(self):
        self.mgr._on_press(CTRL)
        self.mgr._on_press(keyboard.Key.alt_l)
        self.mgr._on_press(K)
        self.assertEqual(self.events, ["clarify"])

    def test_learn_correction(self):
        self.mgr._on_press(CTRL)
        self.mgr._on_press(keyboard.Key.alt_l)
        self.mgr._on_press(L)
        self.assertEqual(self.events, ["learn_correction"])

    def test_injecting_suppresses_events(self):
        self.mgr.set_injecting(True)
        self.mgr._on_press(CTRL)
        self.mgr._on_press(SPACE)
        self.assertEqual(self.events, [])
        self.mgr.set_injecting(False)

    def test_escape_fires_cancel(self):
        self.mgr._on_press(keyboard.Key.esc)
        self.assertEqual(self.events, ["cancel"])
        self.mgr._on_release(keyboard.Key.esc)

    def test_escape_fires_once_per_press(self):
        self.mgr._on_press(keyboard.Key.esc)
        self.mgr._on_press(keyboard.Key.esc)  # auto-repeat shouldn't re-fire
        self.assertEqual(self.events.count("cancel"), 1)
        self.mgr._on_release(keyboard.Key.esc)
        self.mgr._on_press(keyboard.Key.esc)
        self.assertEqual(self.events.count("cancel"), 2)

    def test_escape_cancels_regardless_of_held_modifiers(self):
        # e.g. user is mid-push-to-talk (holding Ctrl+Space) and hits Escape.
        self.mgr._on_press(CTRL)
        self.mgr._on_press(SPACE)
        self.mgr._on_press(keyboard.Key.esc)
        self.assertIn("cancel", self.events)
        self.assertNotIn("clarify", self.events)
        self.assertNotIn("toggle", self.events)

    def test_modifier_only_push_to_talk(self):
        self.mgr._modifier_only_ptt_delay = 0.01
        self.mgr.set_bindings(
            {
                "push_to_talk": "option",
                "toggle": "option+space",
                "clarify": "ctrl+alt+k",
            }
        )
        self.mgr._on_press(ALT)
        time.sleep(0.03)
        self.assertEqual(self.events, ["ptt_start"])
        self.mgr._on_release(ALT)
        self.assertEqual(self.events, ["ptt_start", "ptt_stop"])

    def test_option_space_toggle_does_not_start_ptt(self):
        self.mgr._modifier_only_ptt_delay = 0.05
        self.mgr.set_bindings(
            {
                "push_to_talk": "option",
                "toggle": "option+space",
                "clarify": "ctrl+alt+k",
            }
        )
        self.mgr._on_press(ALT)
        self.mgr._on_press(SPACE)
        time.sleep(0.08)
        self.assertEqual(self.events, ["toggle"])
        self.mgr._on_release(SPACE)
        self.mgr._on_release(ALT)


if __name__ == "__main__":
    unittest.main()
