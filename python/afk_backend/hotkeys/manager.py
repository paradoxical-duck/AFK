"""Global keyboard hotkeys with press/release semantics.

Electron's globalShortcut only fires on key-down, so it cannot implement
push-to-talk (which needs key-up to stop). We therefore run a global hook in
the backend (pynput), co-located with the audio/ASR code for the lowest
possible latency on the dictation hot path.

Matching uses *exact* modifier sets so overlapping combos disambiguate:
  Ctrl+Space          -> push-to-talk   (Ctrl)
  Ctrl+Shift+Space    -> toggle         (Ctrl+Shift)
  Ctrl+Alt+K          -> clarify        (Ctrl+Alt)
  Option              -> push-to-talk   (macOS, modifier-only)
  Option+Space        -> toggle         (macOS)
  Ctrl+Alt+L          -> learn correction from the last dictation

Heavy work (recording/transcription/paste) must be dispatched off the listener
thread by the callbacks; this class only detects and routes events.
"""

import threading
import sys
from typing import Callable, Dict, FrozenSet, Optional, Tuple

try:
    from pynput import keyboard
except Exception as exc:  # pragma: no cover
    keyboard = None
    _PYNPUT_ERR = exc
else:
    _PYNPUT_ERR = None

from .. import logutil

_MOD_ALIASES = {
    "control": "ctrl", "ctrl": "ctrl", "ctl": "ctrl",
    "shift": "shift",
    "alt": "alt", "option": "alt", "altgr": "alt",
    "win": "win", "cmd": "win", "command": "win", "super": "win", "meta": "win", "windows": "win",
}

_MAC_MOD_VKS = {
    54: "win", 55: "win",
    56: "shift", 60: "shift",
    58: "alt", 61: "alt",
    59: "ctrl", 62: "ctrl",
}

_MAC_KEY_VKS = {
    0: "a", 1: "s", 2: "d", 3: "f", 4: "h", 5: "g", 6: "z", 7: "x",
    8: "c", 9: "v", 11: "b", 12: "q", 13: "w", 14: "e", 15: "r",
    16: "y", 17: "t", 18: "1", 19: "2", 20: "3", 21: "4", 22: "6",
    23: "5", 25: "9", 26: "7", 28: "8", 29: "0", 31: "o", 32: "u",
    34: "i", 35: "p", 37: "l", 38: "j", 40: "k", 45: "n", 46: "m",
    49: "space", 53: "esc",
}

Combo = Tuple[FrozenSet[str], Optional[str]]


def _is_macos() -> bool:
    return sys.platform == "darwin"


def parse_combo(combo: str) -> Optional[Combo]:
    """Parse 'Ctrl+Alt+K' -> (frozenset({'ctrl','alt'}), 'k')."""
    if not combo:
        return None
    mods = set()
    main = None
    for raw in combo.split("+"):
        p = raw.strip().lower()
        if not p:
            continue
        if p in _MOD_ALIASES:
            mods.add(_MOD_ALIASES[p])
        elif p in ("space", "spacebar"):
            main = "space"
        else:
            main = p
    # Modifier-only bindings are useful for macOS Option push-to-talk, but
    # avoid accepting ambiguous multi-modifier strings like "Ctrl+Shift".
    if main is None and len(mods) != 1:
        return None
    return frozenset(mods), main


def _norm(key) -> Tuple[str, str]:
    """Normalise a pynput key to ('mod'|'main', token)."""
    K = keyboard.Key
    KC = keyboard.KeyCode
    mod_map = {
        K.ctrl: "ctrl", K.ctrl_l: "ctrl", K.ctrl_r: "ctrl",
        K.shift: "shift", K.shift_l: "shift", K.shift_r: "shift",
        K.alt: "alt", K.alt_l: "alt", K.alt_r: "alt", K.alt_gr: "alt",
        K.cmd: "win", K.cmd_l: "win", K.cmd_r: "win",
    }
    if key in mod_map:
        return "mod", mod_map[key]
    name = str(getattr(key, "name", "") or "").lower()
    if name.endswith(("_l", "_r")):
        name = name[:-2]
    if name in _MOD_ALIASES:
        return "mod", _MOD_ALIASES[name]
    if key == K.space:
        return "main", "space"
    if isinstance(key, K):
        return "main", key.name  # enter, tab, f1, esc, ...
    vk = getattr(key, "vk", None)
    if _is_macos() and vk in _MAC_MOD_VKS:
        return "mod", _MAC_MOD_VKS[vk]
    if _is_macos() and vk in _MAC_KEY_VKS:
        return "main", _MAC_KEY_VKS[vk]
    if vk is not None:
        if 65 <= vk <= 90:
            return "main", chr(vk).lower()
        if 48 <= vk <= 57:
            return "main", chr(vk)
    ch = getattr(key, "char", None)
    if ch and ch.isprintable():
        return "main", ch.lower()
    if vk is not None:
        return "main", f"vk{vk}"
    return "main", "?"


class HotkeyManager:
    def __init__(self, callbacks: Dict[str, Callable[[], None]]):
        """callbacks: keys 'ptt_start','ptt_stop','toggle','clarify','learn_correction','cancel'."""
        self._cb = callbacks
        self._listener = None
        self._lock = threading.Lock()

        self._bindings: Dict[str, Combo] = {}
        self._pressed_mods = set()
        self._main_down: Optional[str] = None
        self._ptt_on = False
        self._ptt_pending_timer: Optional[threading.Timer] = None
        self._fired_edge = False  # debounce edge-triggered actions per press
        self._esc_fired = False  # debounce Escape (cancel) per press
        self._injecting = False
        self._modifier_only_ptt_delay = 0.12
        self._last_error = ""

    # ---- configuration ----
    def set_bindings(self, hotkeys: Dict[str, str]) -> None:
        binds: Dict[str, Combo] = {}
        for action, default in (
            ("push_to_talk", "option" if _is_macos() else "ctrl+space"),
            ("toggle", "option+space" if _is_macos() else "ctrl+shift+space"),
            ("clarify", "ctrl+alt+k"),
            ("learn_correction", "ctrl+alt+l"),
        ):
            parsed = parse_combo(hotkeys.get(action, default))
            if parsed:
                binds[action] = parsed
        with self._lock:
            self._bindings = binds
        logutil.debug(f"Hotkeys set: { {k: f'{sorted(v[0])}+{v[1]}' for k,v in binds.items()} }")

    def set_injecting(self, value: bool) -> None:
        """Suppress event handling while we synthesize keystrokes (paste/copy)."""
        self._injecting = value

    # ---- lifecycle ----
    def available(self) -> bool:
        return keyboard is not None

    def status(self) -> Dict[str, object]:
        return {
            "available": self.available(),
            "listening": self._listener is not None,
            "mac_accessibility_trusted": mac_accessibility_trusted(prompt=False) if _is_macos() else True,
            "error": self._last_error,
        }

    def start(self) -> None:
        if keyboard is None:
            self._last_error = str(_PYNPUT_ERR)
            logutil.warn(f"Hotkeys unavailable: {_PYNPUT_ERR}")
            return
        if self._listener is not None:
            return
        if _is_macos() and mac_accessibility_trusted(prompt=False) is False:
            logutil.warn("macOS Accessibility access is required for global hotkeys; requesting permission")
            mac_accessibility_trusted(prompt=True)
        try:
            self._listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
            self._listener.daemon = True
            self._listener.start()
            self._last_error = ""
            logutil.info("Global hotkey listener started")
        except Exception as exc:  # noqa: BLE001
            self._listener = None
            self._last_error = str(exc)
            logutil.error(f"Failed to start global hotkey listener: {exc}")

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None
        self._cancel_pending_ptt()

    # ---- event handling ----
    def _on_press(self, key):
        if self._injecting:
            return
        kind, token = _norm(key)
        if kind == "mod":
            self._pressed_mods.add(token)
            self._evaluate_ptt()
            return
        # Escape always cancels whatever is in progress (dictation or
        # Clarify), regardless of which modifiers happen to be held.
        if token == "esc":
            if not self._esc_fired:
                self._esc_fired = True
                self._fire("cancel")
            return
        # main key down
        self._main_down = token
        self._evaluate_ptt()
        self._evaluate_edge()

    def _on_release(self, key):
        if self._injecting:
            return
        kind, token = _norm(key)
        if kind == "mod":
            self._pressed_mods.discard(token)
            self._evaluate_ptt()
            return
        if token == "esc":
            self._esc_fired = False
            return
        if self._main_down == token:
            self._main_down = None
            self._fired_edge = False
            self._evaluate_ptt()

    def _evaluate_ptt(self):
        combo = self._bindings.get("push_to_talk")
        if not combo:
            return
        mods, main = combo
        active = (self._main_down == main) and (self._pressed_mods == mods)
        if active and not self._ptt_on:
            if main is None:
                self._schedule_modifier_only_ptt()
            else:
                self._ptt_on = True
                self._fire("ptt_start")
        elif not active and self._ptt_on:
            self._cancel_pending_ptt()
            self._ptt_on = False
            self._fire("ptt_stop")
        elif not active:
            self._cancel_pending_ptt()

    def _schedule_modifier_only_ptt(self):
        if self._ptt_pending_timer is not None:
            return
        timer = threading.Timer(self._modifier_only_ptt_delay, self._fire_pending_ptt)
        timer.daemon = True
        self._ptt_pending_timer = timer
        timer.start()

    def _cancel_pending_ptt(self):
        timer = self._ptt_pending_timer
        if timer is not None:
            timer.cancel()
            self._ptt_pending_timer = None

    def _fire_pending_ptt(self):
        combo = self._bindings.get("push_to_talk")
        if not combo:
            return
        mods, main = combo
        active = main is None and self._main_down is None and self._pressed_mods == mods
        if not active or self._ptt_on:
            return
        self._ptt_pending_timer = None
        self._ptt_on = True
        self._fire("ptt_start")

    def _evaluate_edge(self):
        if self._fired_edge or self._main_down is None:
            return
        for action in ("toggle", "clarify", "learn_correction"):
            combo = self._bindings.get(action)
            if not combo:
                continue
            mods, main = combo
            if self._main_down == main and self._pressed_mods == mods:
                self._fired_edge = True
                self._fire(action)
                return

    def _fire(self, action: str):
        cb = self._cb.get(action)
        if cb is None:
            return
        try:
            cb()
        except Exception as exc:  # noqa: BLE001
            logutil.error(f"Hotkey callback '{action}' failed: {exc}")


def mac_accessibility_trusted(prompt: bool = False) -> Optional[bool]:
    if not _is_macos():
        return True
    try:
        try:
            from ApplicationServices import (
                AXIsProcessTrusted,
                AXIsProcessTrustedWithOptions,
                kAXTrustedCheckOptionPrompt,
            )
        except Exception:
            from HIServices import (
                AXIsProcessTrusted,
                AXIsProcessTrustedWithOptions,
                kAXTrustedCheckOptionPrompt,
            )
    except Exception as exc:  # noqa: BLE001
        logutil.warn(f"Unable to check macOS Accessibility trust: {exc}")
        return None
    try:
        if prompt:
            return bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True}))
        return bool(AXIsProcessTrusted())
    except Exception as exc:  # noqa: BLE001
        logutil.warn(f"macOS Accessibility trust check failed: {exc}")
        return None
