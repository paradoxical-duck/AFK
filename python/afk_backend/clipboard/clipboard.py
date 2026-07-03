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
import subprocess

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

# Delays let Electron/Chromium and sandboxed text boxes observe clipboard
# changes before AFK restores or touches the clipboard again.
_CLIPBOARD_SETTLE = 0.08
_KEY_SETTLE = 0.02
_PASTE_SETTLE = 0.45
_MAC_KEYCODES = {
    "c": 8,
    "v": 9,
}


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
        if sys.platform == "darwin" and modifier == Key.cmd and letter in _MAC_KEYCODES:
            try:
                self._tap_macos_shortcut(letter)
                return
            except Exception as exc:  # noqa: BLE001
                logutil.warn(f"mac pid-targeted shortcut failed; falling back to pynput: {exc}")
        with self._lock:
            self._release_stuck_modifiers()
            self._kb.press(modifier)
            self._kb.press(letter)
            time.sleep(_KEY_SETTLE)
            self._kb.release(letter)
            self._kb.release(modifier)

    def _tap_macos_shortcut(self, letter: str) -> None:
        import Quartz  # noqa: PLC0415
        from AppKit import NSWorkspace  # noqa: PLC0415

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        pid = int(app.processIdentifier()) if app else 0
        if pid <= 0:
            raise RuntimeError("no frontmost app pid")
        flags = Quartz.kCGEventFlagMaskCommand
        keycode = _MAC_KEYCODES[letter]
        with self._lock:
            down = Quartz.CGEventCreateKeyboardEvent(None, keycode, True)
            Quartz.CGEventSetFlags(down, flags)
            Quartz.CGEventPostToPid(pid, down)
            time.sleep(_KEY_SETTLE)
            up = Quartz.CGEventCreateKeyboardEvent(None, keycode, False)
            Quartz.CGEventSetFlags(up, flags)
            Quartz.CGEventPostToPid(pid, up)

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
        if sys.platform == "darwin":
            self._type_macos_text(text)
            return
        if self._kb is None:
            raise RuntimeError(f"pynput unavailable: {_PYNPUT_ERR}")
        with self._lock:
            self._release_stuck_modifiers()
            self._kb.type(text)

    def _type_macos_text(self, text: str) -> None:
        script = f'''
tell application "System Events"
  keystroke "{_applescript_string(text)}"
end tell
'''
        with self._lock:
            self._release_stuck_modifiers()
            result = subprocess.run(
                ["/usr/bin/osascript"],
                input=script,
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
            )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(err or "osascript keystroke failed")

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
        time.sleep(_PASTE_SETTLE)
        if restore:
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
        return self.paste_text(text, restore=False)

    def replace_selection_typed(self, text: str) -> bool:
        """Replace the current selection while preserving the clipboard.

        On macOS, browser address/search fields are much more reliable with a
        real paste command than with synthetic character typing, so paste and
        restore the clipboard. Other platforms keep the old direct-typing path.
        """
        if not text:
            return False
        if sys.platform == "darwin":
            return self.paste_text(text, restore=True)
        self.delete_selection()
        time.sleep(_KEY_SETTLE)
        self.type_text(text)
        return True

    def paste_or_copy(self, text: str) -> str:
        """Insert into the focused window; copy only if insertion fails.

        If focus is not editable, copy the transcript instead of pasting into
        nowhere. Editable targets must never use the clipboard for dictation;
        they receive synthetic text input directly.
        """
        if not text:
            return "empty"
        if sys.platform == "darwin":
            target = active_text_target()
            if target is False:
                self.set_text(text)
                return "copied"
            try:
                self.type_text(text)
                return "pasted"
            except Exception as exc:  # noqa: BLE001
                logutil.warn(f"mac direct typing failed; not copying without a reliable non-text target: {exc}")
                return "failed"
        if not active_text_target():
            self.set_text(text)
            return "copied"
        try:
            self.type_text(text)
            return "pasted"
        except Exception as exc:  # noqa: BLE001
            logutil.warn(f"type failed; not copying because focus is editable: {exc}")
            return "failed"


def active_text_target() -> bool | None:
    """Best-effort check for whether the foreground focus is text-editable."""
    if sys.platform == "darwin":
        return _active_text_target_macos()
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        return _active_text_target_windows(ctypes)
    except Exception:
        return False


def _active_text_target_macos() -> bool:
    try:
        import ApplicationServices as AS  # noqa: PLC0415

        system = AS.AXUIElementCreateSystemWide()
        err, focused = AS.AXUIElementCopyAttributeValue(
            system,
            AS.kAXFocusedUIElementAttribute,
            None,
        )
        if err != 0 or focused is None:
            return None

        role = _ax_attr(focused, AS.kAXRoleAttribute)
        subrole = _ax_attr(focused, AS.kAXSubroleAttribute)
        editable_roles = {"AXTextArea", "AXTextField", "AXComboBox", "AXSearchField"}
        if role in editable_roles or subrole in editable_roles:
            return True

        editable = _ax_attr(focused, "AXEditable")
        if editable is True:
            return True

        if _ax_attr(focused, AS.kAXSelectedTextRangeAttribute) is not None:
            return True
        non_text_roles = {
            "AXButton",
            "AXCheckBox",
            "AXColorWell",
            "AXImage",
            "AXMenu",
            "AXMenuBar",
            "AXMenuButton",
            "AXMenuItem",
            "AXPopUpButton",
            "AXRadioButton",
            "AXSlider",
            "AXToolbar",
        }
        if role in non_text_roles:
            return False
    except Exception:
        return None
    return None


def _ax_attr(element, name):
    try:
        import ApplicationServices as AS  # noqa: PLC0415

        err, value = AS.AXUIElementCopyAttributeValue(element, name, None)
        return value if err == 0 else None
    except Exception:
        return None


def _applescript_string(value: str) -> str:
    return (value or "").replace("\\", "\\\\").replace('"', '\\"').replace("\r", "\n")


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
