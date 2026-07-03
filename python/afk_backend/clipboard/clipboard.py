"""Clipboard integration and synthetic paste.

Provides the primitives the dictation and Clarify flows need:
  * read/write the clipboard (unicode-safe)
  * synthetic paste into the focused application
  * capture the current selection for Clarify
  * replace the current selection with new text

We deliberately preserve and restore the user's clipboard around
selection-capture so dictation never clobbers what they had copied.
"""

import threading
import time
import uuid
import sys

try:
    import pyperclip
except Exception as exc:  # pragma: no cover
    pyperclip = None
    _PYPERCLIP_ERR = exc
else:
    _PYPERCLIP_ERR = None

try:
    from pynput.keyboard import Controller, Key
except Exception as exc:  # pragma: no cover
    Controller = None
    Key = None
    _PYNPUT_ERR = exc
else:
    _PYNPUT_ERR = None

from .. import logutil

# Small delays let the target app observe clipboard changes / key events.
_CLIPBOARD_SETTLE = 0.015
_KEY_SETTLE = 0.01


class Clipboard:
    def __init__(self) -> None:
        self._kb = Controller() if Controller else None
        self._lock = threading.Lock()

    # ---- raw clipboard ----
    def get_text(self) -> str:
        if pyperclip is None:
            raise RuntimeError(f"pyperclip unavailable: {_PYPERCLIP_ERR}")
        try:
            return pyperclip.paste() or ""
        except Exception as exc:  # noqa: BLE001
            logutil.warn(f"clipboard read failed: {exc}")
            return ""

    def set_text(self, text: str) -> None:
        if pyperclip is None:
            raise RuntimeError(f"pyperclip unavailable: {_PYPERCLIP_ERR}")
        pyperclip.copy(text if text is not None else "")

    # ---- synthetic key events ----
    def _release_stuck_modifiers(self) -> None:
        """Force Ctrl/Shift/Alt/Win up before we synthesize a combo.

        Hotkeys fire on key-down, so the user's modifier keys (e.g. the
        Shift in a custom Ctrl+Shift+B clarify binding) are often still
        physically held when we get here. Sending a synthetic key-up tells
        the OS those modifiers are no longer down, so our own Ctrl+C/Ctrl+V
        isn't misread as Ctrl+Shift+C/Ctrl+Shift+V (which many apps don't
        bind to copy/paste at all). A release for a key that isn't actually
        down is a harmless no-op.
        """
        if self._kb is None:
            return
        for mod in (Key.shift, Key.alt, Key.ctrl, Key.cmd):
            try:
                self._kb.release(mod)
            except Exception:
                pass

    def _tap_combo(self, modifier, letter: str) -> None:
        if self._kb is None:
            raise RuntimeError(f"pynput unavailable: {_PYNPUT_ERR}")
        with self._lock:
            self._release_stuck_modifiers()
            self._kb.press(modifier)
            self._kb.press(letter)
            time.sleep(_KEY_SETTLE)
            self._kb.release(letter)
            self._kb.release(modifier)

    def _shortcut_modifier(self):
        return Key.cmd if sys.platform == "darwin" else Key.ctrl

    def paste(self) -> None:
        """Send paste shortcut to the focused window."""
        self._tap_combo(self._shortcut_modifier(), "v")

    def _copy(self) -> None:
        self._tap_combo(self._shortcut_modifier(), "c")

    def type_text(self, text: str) -> None:
        """Type `text` directly into the focused window without touching
        the clipboard at all."""
        if self._kb is None:
            raise RuntimeError(f"pynput unavailable: {_PYNPUT_ERR}")
        with self._lock:
            self._release_stuck_modifiers()
            self._kb.type(text)

    def delete_selection(self) -> None:
        """Delete the current selection (Backspace removes a selection in
        virtually every text field, same as typing over it)."""
        if self._kb is None:
            raise RuntimeError(f"pynput unavailable: {_PYNPUT_ERR}")
        with self._lock:
            self._release_stuck_modifiers()
            self._kb.press(Key.backspace)
            time.sleep(_KEY_SETTLE)
            self._kb.release(Key.backspace)

    # ---- high-level flows ----
    def paste_text(self, text: str, restore: bool = False) -> bool:
        """Put `text` on the clipboard and paste it. Optionally restore prior."""
        if not text:
            return False
        prior = self.get_text() if restore else None
        self.set_text(text)
        time.sleep(_CLIPBOARD_SETTLE)
        self.paste()
        if restore:
            time.sleep(_CLIPBOARD_SETTLE)
            try:
                self.set_text(prior or "")
            except Exception:
                pass
        return True

    def capture_selection(self) -> str:
        """Copy the current selection and return it, restoring the clipboard.

        Returns '' if nothing is selected (clipboard unchanged by the copy).
        """
        prior = self.get_text()
        sentinel = f"__AFK_NO_SELECTION_{uuid.uuid4().hex}__"
        try:
            self.set_text(sentinel)
            time.sleep(_CLIPBOARD_SETTLE)
            self._copy()
            time.sleep(_CLIPBOARD_SETTLE)
            selected = self.get_text()
        finally:
            # Restore the user's original clipboard contents. Clipboard writes
            # can transiently fail under contention (another app/clipboard
            # manager grabbing it right after our synthetic Ctrl+C), so retry
            # rather than silently leaving the sentinel stuck on the clipboard.
            for attempt in range(3):
                try:
                    self.set_text(prior)
                    if self.get_text() == (prior or ""):
                        break
                except Exception:
                    pass
                time.sleep(_CLIPBOARD_SETTLE)
        return "" if selected == sentinel else selected

    def replace_selection(self, text: str) -> bool:
        """Replace the currently selected text by pasting over it."""
        return self.paste_text(text, restore=True)

    def replace_selection_typed(self, text: str) -> bool:
        """Replace the currently selected text by deleting it and typing the
        replacement directly. Never touches the clipboard, so the user's
        clipboard is left exactly as it was before Clarify ran."""
        if not text:
            return False
        self.delete_selection()
        time.sleep(_KEY_SETTLE)
        self.type_text(text)
        return True

    def paste_or_copy(self, text: str) -> str:
        """Type directly into the focused window; copy only if typing fails.

        Typing avoids the clipboard entirely so dictation never clobbers
        whatever the user had copied. Falls back to leaving the text on the
        clipboard only if synthetic typing itself raises (e.g. no keyboard
        backend available).
        """
        if not text:
            return "empty"
        try:
            self.type_text(text)
            return "pasted"
        except Exception as exc:  # noqa: BLE001
            logutil.warn(f"type failed; copying instead: {exc}")
            self.set_text(text)
            return "copied"


def active_text_target() -> bool:
    """Best-effort Windows check for whether the foreground focus has a caret."""
    try:
        import ctypes

        return _active_text_target_windows(ctypes)
    except Exception:
        return False


def _active_text_target_windows(ctypes_module) -> bool:
    try:
        from ctypes import wintypes

        class RECT(ctypes_module.Structure):
            _fields_ = [
                ("left", wintypes.LONG),
                ("top", wintypes.LONG),
                ("right", wintypes.LONG),
                ("bottom", wintypes.LONG),
            ]

        class GUITHREADINFO(ctypes_module.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND),
                ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND),
                ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND),
                ("hwndCaret", wintypes.HWND),
                ("rcCaret", RECT),
            ]

        user32 = ctypes_module.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        thread_id = user32.GetWindowThreadProcessId(hwnd, None)
        if not thread_id:
            return False

        info = GUITHREADINFO()
        info.cbSize = ctypes_module.sizeof(GUITHREADINFO)
        if not user32.GetGUIThreadInfo(thread_id, ctypes_module.byref(info)):
            return False
        return bool(info.hwndCaret)
    except Exception:
        return False
