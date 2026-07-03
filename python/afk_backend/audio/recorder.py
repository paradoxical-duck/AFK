"""Low-latency microphone capture.

Captures mono 16 kHz float32 audio (Parakeet's native rate) into an in-memory
buffer while recording, then returns the trimmed/processed waveform on stop.

Design goals (from the spec): extremely fast startup, low latency, silence
trimming, automatic gain control, and a light noise gate. We deliberately keep
DSP cheap so it adds negligible latency on CPU.
"""

import threading
import time
from typing import List, Optional

import numpy as np

try:
    import sounddevice as sd
except Exception as exc:  # pragma: no cover - import guarded for headless CI
    sd = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None

from .. import logutil

TARGET_SR = 16000
BLOCKSIZE = 1024  # ~64 ms at 16 kHz; small for responsive stop
MIN_SIGNAL_RMS = 0.00015
MIN_SIGNAL_PEAK = 0.001


class AudioUnavailable(RuntimeError):
    pass


class Recorder:
    def __init__(self) -> None:
        self._stream: Optional["sd.InputStream"] = None
        self._frames: List[np.ndarray] = []
        self._lock = threading.Lock()
        self._recording = False
        self._started_at = 0.0
        self._device: Optional[int] = None
        self._sr = TARGET_SR

    # ---- device management ----
    @staticmethod
    def available() -> bool:
        return sd is not None

    @staticmethod
    def list_devices() -> List[dict]:
        if sd is None:
            return []
        out = []
        try:
            default_in = sd.default.device[0] if sd.default.device else None
        except Exception:
            default_in = None
        for idx, dev in enumerate(sd.query_devices()):
            if dev.get("max_input_channels", 0) > 0:
                out.append(
                    {
                        "index": idx,
                        "name": dev.get("name", f"Device {idx}"),
                        "channels": dev.get("max_input_channels", 0),
                        "default": idx == default_in,
                        "default_samplerate": int(dev.get("default_samplerate", 0) or 0),
                    }
                )
        return out

    def _resolve_device(self, name_or_index) -> Optional[int]:
        if name_or_index is None:
            return None
        if isinstance(name_or_index, int):
            return name_or_index
        # match by (partial) name
        for dev in self.list_devices():
            if dev["name"] == name_or_index or name_or_index in dev["name"]:
                return dev["index"]
        logutil.warn(f"Microphone '{name_or_index}' not found; using default")
        return None

    # ---- recording ----
    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def elapsed(self) -> float:
        return time.time() - self._started_at if self._recording else 0.0

    def start(self, device=None) -> None:
        if sd is None:
            raise AudioUnavailable(f"sounddevice unavailable: {_IMPORT_ERROR}")
        if self._recording:
            return

        self._device = self._resolve_device(device)
        self._frames = []
        self._sr = TARGET_SR

        def callback(indata, frames, time_info, status):  # noqa: ANN001
            if status:
                logutil.debug(f"audio status: {status}")
            with self._lock:
                self._frames.append(indata[:, 0].copy())

        try:
            self._stream = sd.InputStream(
                samplerate=self._sr,
                channels=1,
                dtype="float32",
                blocksize=BLOCKSIZE,
                device=self._device,
                callback=callback,
                latency="low",
            )
            self._stream.start()
        except Exception as exc:
            # Fallback: open at device default rate and resample on stop.
            logutil.warn(f"16k capture failed ({exc}); retrying at device default")
            self._sr = 0
            self._stream = sd.InputStream(
                samplerate=None,
                channels=1,
                dtype="float32",
                blocksize=BLOCKSIZE,
                device=self._device,
                callback=callback,
                latency="low",
            )
            self._stream.start()
            self._sr = int(self._stream.samplerate)

        self._recording = True
        self._started_at = time.time()
        logutil.debug(f"Recording started (device={self._device}, sr={self._sr})")

    def stop(self) -> dict:
        """Stop and return {'audio': np.ndarray(float32 @16k), 'duration': sec, 'sr': 16000}."""
        if not self._recording:
            return {"audio": np.zeros(0, dtype=np.float32), "duration": 0.0, "sr": TARGET_SR}

        duration = self.elapsed
        self._recording = False
        try:
            if self._stream:
                self._stream.stop()
                self._stream.close()
        finally:
            self._stream = None

        with self._lock:
            frames = self._frames
            self._frames = []

        if not frames:
            return {"audio": np.zeros(0, dtype=np.float32), "duration": 0.0, "sr": TARGET_SR}

        audio = np.concatenate(frames).astype(np.float32)
        if self._sr != TARGET_SR and self._sr > 0:
            audio = _resample(audio, self._sr, TARGET_SR)

        return {"audio": audio, "duration": duration, "sr": TARGET_SR}


# ---- light-weight DSP helpers ----

def _resample(audio: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    if src_sr == dst_sr or audio.size == 0:
        return audio
    n_dst = int(round(audio.size * dst_sr / src_sr))
    if n_dst <= 0:
        return np.zeros(0, dtype=np.float32)
    x_old = np.linspace(0.0, 1.0, audio.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, n_dst, endpoint=False)
    return np.interp(x_new, x_old, audio).astype(np.float32)


def process(
    audio: np.ndarray,
    sr: int = TARGET_SR,
    *,
    noise_suppression: bool = True,
    auto_gain: bool = True,
    silence_trim: bool = True,
) -> np.ndarray:
    """Apply cheap, latency-friendly conditioning before transcription."""
    if audio.size == 0:
        return audio

    x = audio.astype(np.float32)

    if noise_suppression:
        # Simple noise gate: estimate floor from the quietest 10% of frames.
        frame = max(1, sr // 100)  # 10 ms frames
        n = (x.size // frame) * frame
        if n > 0:
            energies = np.abs(x[:n]).reshape(-1, frame).mean(axis=1)
            floor = np.percentile(energies, 10) if energies.size else 0.0
            gate = floor * 1.5
            if gate > 0:
                mask = np.repeat(energies > gate, frame)
                x[:n] = x[:n] * np.where(mask, 1.0, 0.25)

    if silence_trim:
        x = _trim_silence(x, sr)

    if auto_gain and x.size:
        peak = float(np.max(np.abs(x)))
        if 0 < peak < 0.97:
            x = x * min(0.97 / peak, 8.0)  # cap gain to avoid blowing up noise

    return np.clip(x, -1.0, 1.0).astype(np.float32)


def levels(audio: np.ndarray) -> dict:
    """Return simple amplitude diagnostics for a mono float waveform."""
    if audio is None or audio.size == 0:
        return {"rms": 0.0, "peak": 0.0, "samples": 0}
    x = np.asarray(audio, dtype=np.float32)
    return {
        "rms": float(np.sqrt(np.mean(x * x))),
        "peak": float(np.max(np.abs(x))),
        "samples": int(x.size),
    }


def signal_too_quiet(audio: np.ndarray) -> bool:
    """True when the input is effectively digital silence for dictation."""
    lv = levels(audio)
    return lv["rms"] < MIN_SIGNAL_RMS and lv["peak"] < MIN_SIGNAL_PEAK


def _trim_silence(x: np.ndarray, sr: int, thresh: float = 0.012, pad_ms: int = 180) -> np.ndarray:
    if x.size == 0:
        return x
    abs_x = np.abs(x)
    above = np.where(abs_x > thresh)[0]
    if above.size == 0:
        return x  # all quiet; let the model decide
    pad = int(sr * pad_ms / 1000)
    start = max(0, above[0] - pad)
    end = min(x.size, above[-1] + pad)
    return x[start:end]
