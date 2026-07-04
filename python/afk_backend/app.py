"""AFKApp: service container + RPC method registry.

This is the heart of the backend. Services (settings, and in later phases
transcription / clarify / statistics / audio) are constructed here and exposed
to Electron through a flat method table. Keeping the table explicit makes the
backend API easy to audit and version.
"""

import os
import platform
import re
import sys
import threading
import json
import time
from typing import Any, Callable, Dict

from . import config, logutil, __version__
from .rpc import emit_event, RpcError
from .settings import SettingsStore
from .audio.recorder import Recorder, levels as audio_levels, process as process_audio, signal_too_quiet
from .transcription.transcriber import Transcriber
from .clipboard.clipboard import Clipboard
from .hotkeys import HotkeyManager
from .clarify.engine import ClarifyEngine
from .code_mode import CodeProcessor
from .statistics import StatsStore
from .adaptation import AdaptationStore
from .history import HistoryStore

MIN_DICTATION_SECONDS = 0.35
RECORDING_TAIL_SECONDS = 0.25


class AFKApp:
    def __init__(self) -> None:
        self.settings = SettingsStore()
        if not self.settings.get("logging", True):
            logutil.set_level("warn")

        # Phase 2 services.
        self.recorder = Recorder()
        self.transcriber = Transcriber()

        # Phase 3 services.
        self.clipboard = Clipboard()
        self.hotkeys = HotkeyManager(
            {
                "ptt_start": self._hk_ptt_start,
                "ptt_stop": self._hk_ptt_stop,
                "toggle": self._hk_toggle,
                "code_ptt_start": self._hk_code_ptt_start,
                "code_ptt_stop": self._hk_code_ptt_stop,
                "code_toggle": self._hk_code_toggle,
                "clarify": self._hk_clarify,
                "learn_correction": self._hk_learn_correction,
                "cancel": self._hk_cancel,
            },
            event_observer=self._hotkey_event_observed,
        )
        # Set whenever Escape is pressed; cleared at the start of each new
        # dictation/Clarify run. Checked at safe points so an in-flight command
        # stops short of pasting or replacing anything.
        self._abort_event = threading.Event()

        # Phase 4 service.
        self.clarifier = ClarifyEngine()
        self.code_processor = CodeProcessor()

        # Phase 5 service.
        self.statistics = StatsStore()
        self.adaptation = AdaptationStore()
        self.history = HistoryStore()
        self._last_dictation_text = ""
        self._last_inserted_text = ""
        self._calibration_expected = ""
        self._train_pending: Dict[str, Any] = {}

        self._methods: Dict[str, Callable[[Dict[str, Any]], Any]] = {}
        self._register_core()
        self._register_audio()
        self._register_clipboard_hotkeys()
        self._register_clarify()
        self._register_statistics()
        self._register_adaptation()
        self._register_history()

    # ---- lifecycle ----
    def on_started(self) -> None:
        """Called once the RPC loop is live; announce readiness to Electron."""
        emit_event("ready", self.get_info({}))
        # Model loading is intentionally lazy by default. On macOS, background
        # ASR preload can consume enough memory to make the desktop stutter.
        if os.environ.get("AFK_PRELOAD_ASR") == "1" and not os.environ.get("AFK_NO_PRELOAD"):
            self.transcriber.preload_async()
            # Clarify models are loaded lazily. Preloading the long Gemma
            # server at app startup can consume several GB and make macOS lag.
        # Arm global hotkeys from saved settings.
        try:
            self.hotkeys.set_bindings(self.settings.get("hotkeys", {}))
            if os.environ.get("AFK_HOTKEY_RUNTIME") != "electron":
                self.hotkeys.start()
            self._write_hotkey_status({"type": "startup"})
        except Exception as exc:  # noqa: BLE001
            logutil.error(f"Failed to start hotkeys: {exc}")

    def shutdown(self) -> None:
        logutil.info("Shutting down services")
        try:
            self.hotkeys.stop()
        except Exception:
            pass
        try:
            if self.recorder.is_recording:
                self.recorder.stop()
        except Exception:
            pass
        try:
            self.clarifier.shutdown()
        except Exception:
            pass

    # ---- dispatch ----
    def dispatch(self, method: str, params: Dict[str, Any]) -> Any:
        fn = self._methods.get(method)
        if fn is None:
            raise RpcError(f"Unknown method: {method}", code=-32601)
        return fn(params or {})

    def register(self, name: str, fn: Callable[[Dict[str, Any]], Any]) -> None:
        self._methods[name] = fn

    # ---- core methods ----
    def _register_core(self) -> None:
        self.register("ping", lambda p: {"pong": True})
        self.register("get_info", self.get_info)
        self.register("get_settings", lambda p: self.settings.all())
        self.register("update_settings", self.update_settings)
        self.register("list_methods", lambda p: sorted(self._methods.keys()))

    def get_info(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "version": __version__,
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "backend": "afk-backend",
            "models_status": self._models_status(),
            "default_model": "auto (Gemma 3 270M / Gemma 4 E2B)",
            "asr_model": config.PARAKEET_MODEL,
            "data_dir": str(config.data_dir()),
            "models_dir": str(config.models_dir()),
        }

    def _models_status(self) -> str:
        cs = self.clarifier.status()
        return (
            f"asr[{self.transcriber.engine}]: {self.transcriber.status}, "
            f"clarify(short): {cs['short']}, clarify(long): {cs['long']}"
        )

    def update_settings(self, params: Dict[str, Any]) -> Dict[str, Any]:
        patch = params.get("patch") or params
        updated = self.settings.update(patch)
        if not updated.get("logging", True):
            logutil.set_level("warn")
        else:
            logutil.set_level("debug")
        # Live-reload hotkey bindings if they changed.
        try:
            self.hotkeys.set_bindings(updated.get("hotkeys", {}))
        except Exception as exc:  # noqa: BLE001
            logutil.warn(f"hotkey reload failed: {exc}")
        emit_event("settings_updated", updated)
        return updated

    # ---- audio / transcription methods (Phase 2) ----
    def _register_audio(self) -> None:
        self.register("list_microphones", lambda p: {"devices": Recorder.list_devices()})
        self.register(
            "asr_status",
            lambda p: {"status": self.transcriber.status, "engine": self.transcriber.engine},
        )
        self.register("load_asr", self._load_asr)
        self.register("start_recording", self.start_recording)
        self.register("stop_recording", self.stop_recording)
        self.register("finish_recording", self.finish_recording)
        self.register("finish_code_recording", self.finish_code_recording)
        self.register("format_code_text", self._format_code_text_method)
        self.register("transcribe", self.transcribe)

    def _load_asr(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        # Trigger a (possibly long) load/download synchronously and report.
        try:
            self.transcriber.ensure_loaded()
            return {"status": self.transcriber.status}
        except Exception as exc:  # noqa: BLE001
            raise RpcError(f"ASR load failed: {exc}")

    def start_recording(self, params: Dict[str, Any]) -> Dict[str, Any]:
        self._abort_event.clear()
        device = params.get("device", self.settings.get("microphone"))
        self.recorder.start(device=device)
        emit_event("recording_started", {"device": device})
        return {"recording": True}

    def stop_recording(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Stop recording, condition the audio, transcribe, and return text."""
        if getattr(self.recorder, "is_recording", False):
            time.sleep(RECORDING_TAIL_SECONDS)
        captured = self.recorder.stop()
        emit_event("recording_stopped", {"duration": captured["duration"]})

        audio = captured["audio"]
        if audio is None or len(audio) == 0:
            result = _empty_transcription(captured["duration"], "empty_audio")
            emit_event("transcription", result)
            return result

        if captured["duration"] < MIN_DICTATION_SECONDS:
            result = _empty_transcription(
                captured["duration"],
                "too_short",
                "Recording was too short.",
            )
            emit_event("transcription", result)
            return result

        raw_levels = audio_levels(audio)
        if signal_too_quiet(audio):
            logutil.warn(
                "Microphone signal too quiet "
                f"(rms={raw_levels['rms']:.7f}, peak={raw_levels['peak']:.7f})"
            )
            result = _empty_transcription(
                captured["duration"],
                "low_signal",
                "Microphone signal is too quiet. Check system input volume or choose another mic.",
                raw_levels=raw_levels,
            )
            emit_event("transcription", result)
            return result

        s = self.settings.all()
        apply_text_formatting = params.get("apply_text_formatting", True)
        audio = process_audio(
            audio,
            sr=captured["sr"],
            noise_suppression=s.get("noise_suppression", True),
            auto_gain=s.get("auto_gain", True),
            silence_trim=s.get("silence_trim", True),
        )
        processed_levels = audio_levels(audio)

        result = self.transcriber.transcribe(audio, sample_rate=captured["sr"])
        result["duration"] = round(captured["duration"], 2)
        result["raw_levels"] = raw_levels
        result["processed_levels"] = processed_levels
        if _silence_hallucination(result.get("text", ""), raw_levels):
            logutil.warn(
                "Suppressing likely silence hallucination "
                f"'{result.get('text')}' (rms={raw_levels['rms']:.7f}, peak={raw_levels['peak']:.7f})"
            )
            result.update(
                {
                    "text": "",
                    "reason": "low_signal",
                    "message": "Microphone signal is too quiet. Check system input volume or choose another mic.",
                }
            )
        if result.get("text"):
            if apply_text_formatting and s.get("training_corrections", True):
                adapted, changed, applied = self.adaptation.apply(result.get("text", ""))
            else:
                adapted, changed, applied = result.get("text", ""), False, []
            if changed:
                result["raw_text"] = result.get("text", "")
                result["text"] = adapted
                result["adapted"] = True
                result["adaptations"] = applied
                logutil.info(f"Applied {len(applied)} learned correction(s)")
            if apply_text_formatting:
                formatted = _format_transcript_text(
                    result.get("text", ""),
                    capitalization=s.get("auto_capitalization", True),
                    punctuation=s.get("auto_punctuation", True),
                )
                if formatted != result.get("text", ""):
                    result.setdefault("raw_text", result.get("text", ""))
                    result["text"] = formatted
                    result["formatted"] = True
            self._last_dictation_text = result.get("text", "")
        # Record usage stats (words dictated, recording length, transcription latency).
        try:
            words = len((result.get("text") or "").split())
            self.statistics.record_dictation(words, captured["duration"], result.get("latency_ms", 0))
            emit_event("statistics_updated", {})
        except Exception as exc:  # noqa: BLE001
            logutil.warn(f"stats record failed: {exc}")
        emit_event(
            "transcription",
            {
                "text": result.get("text", ""),
                "latency_ms": result.get("latency_ms", 0),
                "reason": result.get("reason"),
                "message": result.get("message"),
                "raw_text": result.get("raw_text"),
                "adapted": result.get("adapted", False),
                "adaptations": result.get("adaptations", []),
                "raw_levels": result.get("raw_levels"),
                "processed_levels": result.get("processed_levels"),
            },
        )
        return result

    def transcribe(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Transcribe a wav file path (used for tests / file import)."""
        path = params.get("path")
        if not path:
            raise RpcError("transcribe requires a 'path'")
        self.transcriber.ensure_loaded()
        import numpy as np
        import soundfile as sf

        audio, sr = sf.read(path, dtype="float32", always_2d=False)
        if getattr(audio, "ndim", 1) > 1:
            audio = audio.mean(axis=1).astype(np.float32)
        return self.transcriber.transcribe(audio, sample_rate=int(sr))

    def finish_recording(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        """Stop, transcribe, optionally clarify, then paste or copy the text."""
        try:
            result = self.stop_recording({})
        except Exception as exc:  # noqa: BLE001
            logutil.error(f"stop/transcribe failed: {exc}")
            raise
        return self._clarify_and_insert(result)

    def finish_code_recording(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        """Stop, transcribe, format as code, then paste or copy the code."""
        try:
            result = self.stop_recording({"apply_text_formatting": False})
        except Exception as exc:  # noqa: BLE001
            logutil.error(f"stop/code-transcribe failed: {exc}")
            raise
        return self._code_and_insert(result)

    def _format_code_text_method(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self.code_processor.process(
            params.get("text", ""),
            language=params.get("language") or self.settings.get("code_language", "auto"),
        )

    # ---- clipboard + hotkeys methods (Phase 3) ----
    def _register_clipboard_hotkeys(self) -> None:
        self.register("get_clipboard", lambda p: {"text": self.clipboard.get_text()})
        self.register("set_clipboard", self._set_clipboard)
        self.register("paste_text", self._paste_text_method)
        self.register("set_hotkeys", self._set_hotkeys)
        self.register("hotkeys_status", self._hotkeys_status)
        self.register("hotkey_cancel", self._hotkey_cancel_method)
        self.register("hotkey_clarify", self._hotkey_clarify_method)
        self.register("hotkey_learn_correction", self._hotkey_learn_correction_method)

    def _set_clipboard(self, params: Dict[str, Any]) -> Dict[str, Any]:
        self.clipboard.set_text(params.get("text", ""))
        return {"ok": True}

    def _paste_text_method(self, params: Dict[str, Any]) -> Dict[str, Any]:
        text = params.get("text", "")
        self._paste(text)
        return {"ok": True, "chars": len(text)}

    def _set_hotkeys(self, params: Dict[str, Any]) -> Dict[str, Any]:
        hk = params.get("hotkeys") or params
        updated = self.settings.update({"hotkeys": hk})
        self.hotkeys.set_bindings(updated.get("hotkeys", {}))
        emit_event("settings_updated", updated)
        return updated["hotkeys"]

    def _hotkeys_status(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        status = self.hotkeys.status()
        if os.environ.get("AFK_HOTKEY_RUNTIME") == "electron":
            status.update({
                "available": True,
                "listening": False,
                "runtime": "electron",
                "mac_accessibility_trusted": None,
                "mac_input_monitoring_trusted": True,
                "error": "Electron hotkey runtime reports status",
            })
        status["hotkeys"] = self.settings.get("hotkeys", {})
        return status

    def _hotkey_cancel_method(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        self._hk_cancel()
        return {"ok": True}

    def _hotkey_clarify_method(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        self._hk_clarify()
        return {"ok": True}

    def _hotkey_learn_correction_method(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        self._hk_learn_correction()
        return {"ok": True}

    def _hotkey_event_observed(self, event: Dict[str, object]) -> None:
        self._write_hotkey_status(event)

    def _write_hotkey_status(self, event: Dict[str, object]) -> None:
        try:
            status = self.hotkeys.status()
            status["hotkeys"] = self.settings.get("hotkeys", {})
            status["last_update"] = time.time()
            status["last_observed"] = event
            path = config.data_dir() / "hotkeys-status.json"
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(status, fh, indent=2)
        except Exception as exc:  # noqa: BLE001
            logutil.warn(f"hotkey status write failed: {exc}")

    # ---- shared dictation flow ----
    def _paste(self, text: str) -> str:
        """Paste into a text field or copy when there is nowhere to paste."""
        if not text:
            return "empty"
        try:
            self.hotkeys.set_injecting(True)
            action = self.clipboard.paste_or_copy(text)
            emit_event(action, {"chars": len(text)})
            return action
        except Exception as exc:  # noqa: BLE001
            logutil.error(f"paste failed: {exc}")
            return "error"
        finally:
            self.hotkeys.set_injecting(False)

    def _start_rec_safe(self) -> None:
        self._abort_event.clear()
        try:
            if not self.recorder.is_recording:
                self.start_recording({})
        except Exception as exc:  # noqa: BLE001
            logutil.error(f"start recording failed: {exc}")

    def _stop_transcribe_paste(self) -> None:
        try:
            result = self.stop_recording({})
        except Exception as exc:  # noqa: BLE001
            logutil.error(f"stop/transcribe failed: {exc}")
            return
        self._clarify_and_insert(result)

    def _stop_transcribe_code_paste(self) -> None:
        try:
            result = self.stop_recording({"apply_text_formatting": False})
        except Exception as exc:  # noqa: BLE001
            logutil.error(f"stop/code-transcribe failed: {exc}")
            return
        self._code_and_insert(result)

    def _clarify_and_insert(self, result: Dict[str, Any]) -> Dict[str, Any]:
        text = (result or {}).get("text", "")
        if not text or self._abort_event.is_set():
            return result or {}
        # Optionally polish the dictation with Clarify before pasting.
        if self.settings.get("auto_clarify", False) and self.clarifier.any_available():
            threshold = self.settings.get("word_count_threshold", config.DEFAULT_WORD_THRESHOLD)
            emit_event("clarify_started", {"source": "dictation"})
            cr = self.clarifier.clarify(text, threshold=threshold)
            self._record_clarify(cr)
            if self._abort_event.is_set():
                emit_event("cancelled", {"source": "dictation"})
                return result or {}
            polished = cr.get("text")
            if polished:
                text = polished
                emit_event("transcription", {"text": text, "clarified": True})
                result["text"] = text
                result["clarified"] = True
        if self._abort_event.is_set():
            emit_event("cancelled", {"source": "dictation"})
            return result or {}
        if self.settings.get("auto_paste", True):
            before = text
            result["action"] = self._paste(before)
            result["inserted"] = True
            self._last_inserted_text = before
        self._record_history(text, result.get("action", ""))
        return result

    def _code_and_insert(self, result: Dict[str, Any]) -> Dict[str, Any]:
        text = (result or {}).get("text", "")
        if not text or self._abort_event.is_set():
            return result or {}
        language = self.settings.get("code_language", "auto")
        emit_event("code_format_started", {"language": language})
        formatted = self.code_processor.process(text, language=language)
        code = formatted.get("text", "") or text
        result["raw_code_text"] = text
        result["text"] = code
        result["code_mode"] = True
        result["code_language"] = formatted.get("language", language)
        result["code_processor"] = formatted.get("processor", "deterministic")
        emit_event(
            "code_formatted",
            {
                "text": code,
                "raw_text": text,
                "language": result["code_language"],
                "processor": result["code_processor"],
            },
        )
        if self._abort_event.is_set():
            emit_event("cancelled", {"source": "code"})
            return result
        if self.settings.get("auto_paste", True):
            result["action"] = self._paste(code)
            result["inserted"] = True
            self._last_inserted_text = code
        self._last_dictation_text = code
        self._record_history(code, result.get("action", ""))
        return result

    # ---- hotkey callbacks (run on the listener thread; offload heavy work) ----
    def _hk_ptt_start(self) -> None:
        threading.Thread(target=self._start_rec_safe, daemon=True).start()

    def _hk_ptt_stop(self) -> None:
        threading.Thread(target=self._stop_transcribe_paste, daemon=True).start()

    def _hk_toggle(self) -> None:
        if self.recorder.is_recording:
            threading.Thread(target=self._stop_transcribe_paste, daemon=True).start()
        else:
            threading.Thread(target=self._start_rec_safe, daemon=True).start()

    def _hk_code_ptt_start(self) -> None:
        threading.Thread(target=self._start_rec_safe, daemon=True).start()

    def _hk_code_ptt_stop(self) -> None:
        threading.Thread(target=self._stop_transcribe_code_paste, daemon=True).start()

    def _hk_code_toggle(self) -> None:
        if self.recorder.is_recording:
            threading.Thread(target=self._stop_transcribe_code_paste, daemon=True).start()
        else:
            threading.Thread(target=self._start_rec_safe, daemon=True).start()

    def _hk_clarify(self) -> None:
        threading.Thread(target=self._clarify_flow, daemon=True).start()

    def _hk_cancel(self) -> None:
        """Escape: abort dictation or Clarify without inserting anything."""
        self._abort_event.set()
        if self.recorder.is_recording:
            threading.Thread(target=self._cancel_recording, daemon=True).start()
        else:
            emit_event("cancelled", {})

    def _cancel_recording(self) -> None:
        try:
            self.recorder.stop()  # discard captured audio
        except Exception as exc:  # noqa: BLE001
            logutil.error(f"cancel recording failed: {exc}")
        emit_event("cancelled", {"source": "dictation"})

    def _hk_learn_correction(self) -> None:
        threading.Thread(target=self._learn_correction_flow, daemon=True).start()

    # ---- statistics methods (Phase 5) ----
    def _register_statistics(self) -> None:
        self.register("get_statistics", lambda p: self.statistics.snapshot())
        self.register("reset_statistics", self._reset_statistics)

    def _reset_statistics(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        self.statistics.reset()
        emit_event("statistics_updated", {})
        return self.statistics.snapshot()

    # ---- clarify methods (Phase 4) ----
    def _register_clarify(self) -> None:
        self.register("clarify", self._clarify_method)
        self.register("clarify_status", lambda p: self.clarifier.status())

    def _clarify_method(self, params: Dict[str, Any]) -> Dict[str, Any]:
        text = params.get("text", "")
        threshold = params.get("threshold", self.settings.get("word_count_threshold"))
        emit_event("clarify_started", {"source": "manual"})
        result = self.clarifier.clarify(text, threshold=threshold)
        self._record_clarify(result)
        emit_event(
            "clarify_done",
            {"text": result.get("text", ""), "model": result.get("model"), "latency_ms": result.get("latency_ms")},
        )
        return result

    def _record_clarify(self, result: Dict[str, Any]) -> None:
        if result.get("model") not in (None, "none", "error") and result.get("latency_ms"):
            try:
                self.statistics.record_clarification(result["latency_ms"])
                emit_event("statistics_updated", {})
            except Exception as exc:  # noqa: BLE001
                logutil.warn(f"stats record failed: {exc}")

    def _clarify_flow(self) -> None:
        """Hotkey Clarify: take the selection (or clipboard), polish, put it back.

        1. Capture selected text with the platform copy shortcut. If empty, fall back to clipboard.
        2. Route by word count and clarify.
        3. Replace the selection by pasting (or update the clipboard if we used
           the clipboard fallback).
        """
        if not self.clarifier.any_available():
            logutil.warn("Clarify requested but no model available")
            emit_event("clarify_unavailable", {"reason": "No Clarify model installed"})
            return

        self._abort_event.clear()
        emit_event("clarify_started", {"source": "hotkey"})

        # Capture selection with the listener suppressed while we synthesize copy.
        try:
            self.hotkeys.set_injecting(True)
            selection = self.clipboard.capture_selection()
        finally:
            self.hotkeys.set_injecting(False)

        had_selection = bool(selection.strip())
        text = selection.strip() or self.clipboard.get_text().strip()
        if text.startswith("__AFK_NO_SELECTION_") and text.endswith("__"):
            # Leftover sentinel from a clipboard restore that failed; never
            # treat it as real text.
            text = ""
        if not text:
            emit_event("clarify_done", {"text": "", "model": "none", "empty": True})
            return

        threshold = self.settings.get("word_count_threshold", config.DEFAULT_WORD_THRESHOLD)
        result = self.clarifier.clarify(text, threshold=threshold)
        self._record_clarify(result)
        if self._abort_event.is_set():
            emit_event("cancelled", {"source": "clarify"})
            return
        out = result.get("text", "") or text

        if had_selection:
            # Replace the highlighted text in place by deleting the
            # selection and typing the replacement directly — never touches
            # the clipboard, so the user's clipboard is untouched.
            try:
                self.hotkeys.set_injecting(True)
                self.clipboard.replace_selection_typed(out)
            finally:
                self.hotkeys.set_injecting(False)
        else:
            # No selection — clarify whatever's currently on the clipboard
            # and leave the clarified version there in its place.
            self.clipboard.set_text(out)

        emit_event(
            "clarify_done",
            {"text": out, "model": result.get("model"), "latency_ms": result.get("latency_ms")},
        )

    # ---- voice adaptation methods ----
    def _register_adaptation(self) -> None:
        self.register("get_adaptation", lambda p: self.adaptation.snapshot())
        self.register("learn_correction", self._learn_correction_method)
        self.register("clear_adaptation", self._clear_adaptation)
        self.register("calibration_phrases", lambda p: {"phrases": self.adaptation.snapshot()["calibration_phrases"]})
        self.register("start_calibration", self._start_calibration)
        self.register("finish_calibration", self._finish_calibration)
        self.register("start_training_sample", self._start_training_sample)
        self.register("finish_training_sample", self._finish_training_sample)
        self.register("delete_training_sample", self._delete_training_sample)

    def _learn_correction_method(self, params: Dict[str, Any]) -> Dict[str, Any]:
        intended = params.get("intended", "")
        heard = params.get("heard") or self._last_inserted_text or self._last_dictation_text
        result = self.adaptation.learn_correction(heard, intended, source=params.get("source", "manual"))
        emit_event("correction_learned", result)
        return result

    def _clear_adaptation(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        snapshot = self.adaptation.clear()
        emit_event("adaptation_updated", snapshot)
        return snapshot

    def _delete_training_sample(self, params: Dict[str, Any]) -> Dict[str, Any]:
        snapshot = self.adaptation.delete_training(params.get("id", ""))
        emit_event("adaptation_updated", snapshot)
        return snapshot

    def _start_calibration(self, params: Dict[str, Any]) -> Dict[str, Any]:
        expected = (params.get("expected") or "").strip()
        if not expected:
            raise RpcError("start_calibration requires 'expected'")
        self._calibration_expected = expected
        return self.start_recording(params)

    def _finish_calibration(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        result = self.stop_recording({})
        expected = self._calibration_expected
        self._calibration_expected = ""
        learned = self.adaptation.record_calibration(expected, result.get("raw_text") or result.get("text", ""))
        out = {**result, "calibration": learned}
        emit_event("adaptation_updated", self.adaptation.snapshot())
        return out

    def _learn_correction_flow(self) -> None:
        heard = self._last_inserted_text or self._last_dictation_text
        if not heard:
            emit_event("learn_unavailable", {"reason": "No previous dictation to learn from"})
            return

        try:
            self.hotkeys.set_injecting(True)
            selection = self.clipboard.capture_selection()
        finally:
            self.hotkeys.set_injecting(False)

        intended = selection.strip() or self.clipboard.get_text().strip()
        if not intended:
            emit_event("learn_unavailable", {"reason": "Select corrected text or copy it first"})
            return

        result = self.adaptation.learn_correction(heard, intended, source="hotkey")
        emit_event("correction_learned", result)

    def _start_training_sample(self, params: Dict[str, Any]) -> Dict[str, Any]:
        kind = params.get("kind") if params.get("kind") in {"word", "trigger"} else "word"
        spoken = (params.get("spoken") or "").strip()
        output = (params.get("output") or spoken).strip()
        if not spoken or not output:
            raise RpcError("start_training_sample requires 'spoken' and 'output'")
        trigger_type = params.get("trigger_type") if params.get("trigger_type") in {"autoreplace", "autofill"} else "autoreplace"
        self._train_pending = {"kind": kind, "spoken": spoken, "output": output, "trigger_type": trigger_type}
        return self.start_recording(params)

    def _finish_training_sample(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._train_pending:
            raise RpcError("No active training sample")
        result = self.stop_recording({})
        pending = self._train_pending
        self._train_pending = {}
        heard = result.get("raw_text") or result.get("text", "")
        learned = self.adaptation.record_training(
            pending.get("kind", "word"),
            pending.get("spoken", ""),
            pending.get("output", ""),
            heard,
            pending.get("trigger_type", "autoreplace"),
        )
        out = {**result, "training": learned}
        emit_event("adaptation_updated", self.adaptation.snapshot())
        emit_event("training_sample_saved", learned)
        return out

    # ---- transcription history methods ----
    def _register_history(self) -> None:
        self.register("get_transcription_history", self._get_transcription_history)
        self.register("delete_transcription_history", self._delete_transcription_history)
        self.register("clear_transcription_history", self._clear_transcription_history)

    def _record_history(self, text: str, action: str = "") -> None:
        try:
            result = self.history.record(text, action=action)
            if result.get("ok"):
                emit_event("history_updated", self.history.snapshot())
        except Exception as exc:  # noqa: BLE001
            logutil.warn(f"history record failed: {exc}")

    def _get_transcription_history(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self.history.snapshot(params.get("limit", 24))

    def _delete_transcription_history(self, params: Dict[str, Any]) -> Dict[str, Any]:
        snapshot = self.history.delete(params.get("id", ""))
        emit_event("history_updated", snapshot)
        return snapshot

    def _clear_transcription_history(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        snapshot = self.history.clear()
        emit_event("history_updated", snapshot)
        return snapshot


def _empty_transcription(duration: float, reason: str, message: str = "", **extra) -> Dict[str, Any]:
    return {
        "text": "",
        "duration": round(duration, 2),
        "latency_ms": 0,
        "reason": reason,
        "message": message,
        **extra,
    }


def _silence_hallucination(text: str, raw_levels: Dict[str, Any]) -> bool:
    token = (text or "").strip().lower().strip(".!? ")
    if token not in {"yeah", "yea", "yes", "yep", "ok", "okay", "uh", "um", "hmm"}:
        return False
    return (
        float(raw_levels.get("rms", 0.0)) < 0.0008
        and float(raw_levels.get("peak", 0.0)) < 0.006
    )


def _format_transcript_text(text: str, *, capitalization: bool = True, punctuation: bool = True) -> str:
    out = re.sub(r"\s+", " ", text or "").strip()
    if not out:
        return ""
    if punctuation:
        out = _apply_spoken_punctuation(out)
    else:
        out = re.sub(r"[.,!?;:]+", "", out)
    if not capitalization:
        return out.lower()
    if out:
        out = out[:1].upper() + out[1:]
    return out


_SPOKEN_PUNCTUATION = (
    ("exclamation point", "!"),
    ("exclamation mark", "!"),
    ("question mark", "?"),
    ("comma", ","),
    ("period", "."),
    ("full stop", "."),
    ("colon", ":"),
    ("semicolon", ";"),
    ("semi colon", ";"),
    ("dash", "-"),
    ("hyphen", "-"),
    ("new paragraph", "\n\n"),
    ("new line", "\n"),
    ("newline", "\n"),
)


def _apply_spoken_punctuation(text: str) -> str:
    out = text
    for phrase, mark in _SPOKEN_PUNCTUATION:
        if mark.startswith("\n"):
            out = re.sub(rf"\s*\b{re.escape(phrase)}\b\s*", mark, out, flags=re.IGNORECASE)
        else:
            out = re.sub(rf"\s+\b{re.escape(phrase)}\b", mark, out, flags=re.IGNORECASE)
            out = re.sub(rf"\b{re.escape(phrase)}\b", mark, out, flags=re.IGNORECASE)
    out = re.sub(r"\s+([,.;:!?])", r"\1", out)
    out = re.sub(r"([,;:!?])(?=\S)", r"\1 ", out)
    out = re.sub(r"\.(?=\S)", ". ", out)
    out = re.sub(r"\s+-\s+", " - ", out)
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n[ \t]+", "\n", out)
    return out.strip()
