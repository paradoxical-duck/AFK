"""Clarify: local grammar/polishing via Gemma (GGUF on CPU).

Two models, auto-selected by word count (threshold configurable in Settings):

  * Short  (<= threshold words): Gemma 3 270M IT  — lowest latency
  * Long   (>  threshold words): Gemma 4 E2B IT   — highest quality

Inference runs through the official llama.cpp `llama-server` (see
llama_server.py) so it works on CPUs without AVX-512. Each model gets its own
server, started lazily and kept warm. If a model file (or the server binary)
is missing, Clarify degrades gracefully: long falls back to short, and if
nothing is available the original text is returned unchanged (the dictation
hot path never errors).

Decoding is greedy (temperature 0) so output is deterministic and the model
never invents content.
"""

import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

from .. import config, logutil
from .llama_server import LlamaServer


class ClarifyModel:
    """A single GGUF chat model served by its own llama-server (lazy)."""

    def __init__(self, name: str, gguf_path, n_ctx: int = 4096, fewshot: bool = False):
        self.name = name
        self.path = str(gguf_path)
        self.n_ctx = n_ctx
        self.fewshot = fewshot  # small models need examples to perform the edit
        self._server: Optional[LlamaServer] = None
        self._lock = threading.Lock()
        self._error: Optional[str] = None

    @property
    def available(self) -> bool:
        return os.path.isfile(self.path) and Path(config.llama_server_path()).exists()

    @property
    def is_loaded(self) -> bool:
        return self._server is not None and self._server.is_alive()

    @property
    def status(self) -> str:
        if self.is_loaded:
            return "loaded"
        if self._error:
            return f"error: {self._error}"
        if not os.path.isfile(self.path):
            return "missing"
        if not Path(config.llama_server_path()).exists():
            return "missing (llama-server)"
        return "not loaded"

    def ensure_loaded(self) -> None:
        if self.is_loaded:
            return
        with self._lock:
            if self.is_loaded:
                return
            try:
                t0 = time.time()
                logutil.info(f"Loading Clarify model '{self.name}' from {self.path}…")
                self._server = LlamaServer(self.path, n_ctx=self.n_ctx)
                self._server.start()
                self._error = None
                logutil.info(f"Clarify model '{self.name}' ready in {time.time()-t0:.1f}s")
            except Exception as exc:  # noqa: BLE001
                self._error = str(exc)
                self._server = None
                logutil.error(f"Failed to load Clarify model '{self.name}': {exc}")
                raise

    def _build_messages(self, text: str) -> list:
        if self.fewshot:
            # Multi-turn few-shot: instruction + worked examples, then the target.
            instr = (
                "You are a text corrector. Fix spelling, grammar, punctuation, and "
                "capitalization while preserving the meaning. Output only the corrected text."
            )
            msgs = [{"role": "user", "content": f"{instr}\n\nInput: {config.CLARIFY_FEWSHOT[0][0]}"}]
            msgs.append({"role": "assistant", "content": config.CLARIFY_FEWSHOT[0][1]})
            for src, dst in config.CLARIFY_FEWSHOT[1:]:
                msgs.append({"role": "user", "content": f"Input: {src}"})
                msgs.append({"role": "assistant", "content": dst})
            msgs.append({"role": "user", "content": f"Input: {text}"})
            return msgs
        return [{"role": "user", "content": f"{config.CLARIFY_PROMPT}\n\nText:\n{text}"}]

    def clarify(self, text: str) -> str:
        self.ensure_loaded()
        max_tokens = min(256, self.n_ctx - 256, max(48, int(len(text.split()) * 2.2) + 40))
        corrected = self._server.chat(
            messages=self._build_messages(text),
            temperature=0.0,
            max_tokens=max_tokens,
        )
        return _clean_correction(corrected)

    def stop(self) -> None:
        if self._server is not None:
            self._server.stop()
            self._server = None


class ClarifyEngine:
    def __init__(self) -> None:
        self.short = ClarifyModel(
            config.GEMMA_SHORT_MODEL, config.clarify_short_path(), n_ctx=2048, fewshot=True
        )
        self.long = ClarifyModel(config.GEMMA_LONG_MODEL, config.clarify_long_path(), n_ctx=2048)

    # ---- status ----
    def status(self) -> dict:
        return {
            "available": Path(config.llama_server_path()).exists(),
            "short": self.short.status,
            "long": self.long.status,
        }

    def any_available(self) -> bool:
        return self.short.available or self.long.available

    def preload_short_async(self) -> None:
        if not self.short.available:
            return

        def _run():
            try:
                self.short.ensure_loaded()
            except Exception:
                pass

        threading.Thread(target=_run, name="clarify-preload", daemon=True).start()

    def preload_long_async(self) -> None:
        if not self.long.available:
            return

        def _run():
            try:
                self.long.ensure_loaded()
            except Exception:
                pass

        threading.Thread(target=_run, name="clarify-preload-long", daemon=True).start()

    def preload_preferred_async(self) -> None:
        """Eagerly load both Clarify models at startup so routing never has
        to lazy-load mid-request (short is small/fast; long is preloaded
        too since it's the default for most dictation lengths)."""
        self.preload_short_async()
        self.preload_long_async()

    def shutdown(self) -> None:
        for m in (self.short, self.long):
            try:
                m.stop()
            except Exception:
                pass

    # ---- routing ----
    def _route(self, word_count: int, threshold: int) -> ClarifyModel:
        prefer_long = word_count > threshold
        primary = self.long if prefer_long else self.short
        fallback = self.short if prefer_long else self.long
        if primary.available:
            return primary
        if fallback.available:
            logutil.warn(f"Clarify '{primary.name}' missing; falling back to '{fallback.name}'")
            return fallback
        return primary

    def _fallback_for(self, model: ClarifyModel) -> ClarifyModel:
        return self.short if model is self.long else self.long

    def clarify(self, text: str, threshold: Optional[int] = None) -> dict:
        text = (text or "").strip()
        if not text:
            return {"text": "", "model": "none", "latency_ms": 0, "words": 0}

        threshold = config.DEFAULT_WORD_THRESHOLD if threshold is None else int(threshold)
        words = len(text.split())
        model = self._route(words, threshold)

        if not model.available:
            logutil.warn("No Clarify model available; returning text unchanged")
            return {"text": text, "model": "none", "latency_ms": 0, "words": words}

        t0 = time.time()
        try:
            corrected = model.clarify(text)
        except Exception as exc:  # noqa: BLE001
            logutil.error(f"Clarify failed ({model.name}): {exc}")
            fallback = self._fallback_for(model)
            if fallback.available:
                logutil.warn(f"Retrying Clarify with fallback model '{fallback.name}'")
                try:
                    corrected = fallback.clarify(text)
                    model = fallback
                except Exception as fallback_exc:  # noqa: BLE001
                    logutil.error(f"Clarify fallback failed ({fallback.name}): {fallback_exc}")
                    return {"text": text, "model": "error", "latency_ms": 0, "words": words}
            else:
                return {"text": text, "model": "error", "latency_ms": 0, "words": words}

        return {
            "text": corrected or text,
            "model": model.name,
            "latency_ms": int((time.time() - t0) * 1000),
            "words": words,
        }


def _clean_correction(text: str) -> str:
    """Keep hotkey grammar output paste-ready across model families."""
    text = (text or "").strip()
    if not text:
        return ""
    text = re.split(r"<\|im_(?:start|end)\|>|<\|endoftext\|>", text, maxsplit=1, flags=re.I)[
        0
    ].strip()
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()
    lines = []
    for line in text.splitlines():
        line = re.sub(r"^\s*(?:assistant|user|system)\s*:?\s*", "", line, flags=re.I).strip()
        if line.lower().startswith("input:"):
            break
        if line:
            lines.append(line)
    text = " ".join(lines).strip()
    for prefix in ("Corrected text:", "Correction:", "Output:", "Answer:"):
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix):].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    return text
