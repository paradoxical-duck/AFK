"""Local post-ASR adaptation.

This is intentionally not acoustic fine-tuning. AFK keeps a small private
dictionary of phrases and words the user has corrected, then applies those
corrections after Parakeet returns text and before Clarify/paste run.
"""

import difflib
import json
import os
import re
import tempfile
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from .. import config, logutil


CALIBRATION_PHRASES = [
    "AFK should paste this sentence into the active text box.",
    "My microphone should capture quiet words clearly.",
    "Keshav and Pachisia are names I want AFK to remember.",
    "Control Shift Space starts a reliable dictation recording.",
    "The quick brown fox jumps over the lazy dog.",
]


class AdaptationStore:
    def __init__(self) -> None:
        self._path = config.adaptation_path()
        self._lock = threading.Lock()
        self._data = self._load()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            out = deepcopy(self._data)
        out["calibration_phrases"] = list(CALIBRATION_PHRASES)
        out["correction_count"] = len(out.get("corrections", []))
        out["vocabulary_count"] = len(out.get("vocabulary", []))
        out["calibration_count"] = len(out.get("calibration", []))
        out["training_count"] = len(out.get("training", []))
        out["word_training_count"] = len([x for x in out.get("training", []) if x.get("kind") == "word"])
        out["trigger_count"] = len([x for x in out.get("training", []) if x.get("kind") == "trigger"])
        return out

    def apply(self, text: str) -> Tuple[str, bool, List[Dict[str, str]]]:
        """Apply known heard->intended corrections to a transcript."""
        if not text:
            return text or "", False, []
        with self._lock:
            corrections = list(self._data.get("corrections", []))

        out = text
        applied: List[Dict[str, str]] = []
        corrections.sort(
            key=lambda c: (
                len(_words(c.get("heard", ""))),
                len(c.get("heard", "")),
                int(c.get("count", 0)),
            ),
            reverse=True,
        )

        for item in corrections:
            heard = (item.get("heard") or "").strip()
            intended = (item.get("intended") or "").strip()
            if not heard or not intended or heard == intended:
                continue
            pattern = re.compile(rf"(?<!\w){re.escape(heard)}(?!\w)", re.IGNORECASE)

            def repl(match: re.Match[str]) -> str:
                source = item.get("source", "")
                replacement = intended if source.startswith("train_trigger") else _match_case(match.group(0), intended)
                applied.append({"heard": match.group(0), "intended": replacement})
                return replacement

            out = pattern.sub(repl, out)

        return out, out != text, applied

    def learn_correction(self, heard: str, intended: str, source: str = "manual") -> Dict[str, Any]:
        heard = _clean_text(heard)
        intended = _clean_text(intended)
        if not heard or not intended:
            return {"ok": False, "reason": "empty"}
        if heard == intended:
            return {"ok": False, "reason": "unchanged"}

        with self._lock:
            self._upsert_correction(heard, intended, source)
            for bad, good in _word_pairs(heard, intended):
                self._upsert_correction(bad, good, source)
            for term in _important_terms(intended):
                self._upsert_vocabulary(term)
            self._save()

        adapted, changed, applied = self.apply(heard)
        return {
            "ok": True,
            "heard": heard,
            "intended": intended,
            "adapted_preview": adapted,
            "changed_preview": changed,
            "applied": applied,
        }

    def record_calibration(self, expected: str, heard: str) -> Dict[str, Any]:
        expected = _clean_text(expected)
        heard = _clean_text(heard)
        if not expected:
            return {"ok": False, "reason": "missing_expected"}
        result = self.learn_correction(heard, expected, source="calibration") if heard else {
            "ok": False,
            "reason": "missing_heard",
        }
        with self._lock:
            self._data.setdefault("calibration", []).append(
                {
                    "expected": expected,
                    "heard": heard,
                    "created_at": _now(),
                    "learned": bool(result.get("ok")),
                }
            )
            self._data["setup_complete"] = True
            self._save()
        return {"ok": True, "expected": expected, "heard": heard, "learn_result": result}

    def record_training(
        self,
        kind: str,
        spoken: str,
        output: str,
        heard: str,
        trigger_type: str = "autoreplace",
    ) -> Dict[str, Any]:
        kind = kind if kind in {"word", "trigger"} else "word"
        trigger_type = trigger_type if trigger_type in {"autoreplace", "autofill"} else "autoreplace"
        spoken = _clean_text(spoken)
        output = _clean_text(output)
        heard = _clean_text(heard)
        if not spoken or not output:
            return {"ok": False, "reason": "missing_training_text"}

        intended = output
        source = f"train_{kind}"
        if kind == "trigger":
            intended = _clean_text(f"{spoken} {output}") if trigger_type == "autofill" else output
            source = f"train_trigger_{trigger_type}"

        learn_result = self.learn_correction(heard, intended, source=source) if heard else {
            "ok": False,
            "reason": "missing_heard",
        }
        with self._lock:
            item = {
                "id": uuid.uuid4().hex,
                "kind": kind,
                "spoken": spoken,
                "output": output,
                "trigger_type": trigger_type if kind == "trigger" else "",
                "heard": heard,
                "created_at": _now(),
                "learned": bool(learn_result.get("ok")),
            }
            self._data.setdefault("training", []).append(item)
            self._data["setup_complete"] = True
            self._save()
        return {
            "ok": True,
            "id": item["id"],
            "kind": kind,
            "spoken": spoken,
            "output": output,
            "trigger_type": item["trigger_type"],
            "heard": heard,
            "learn_result": learn_result,
        }

    def delete_training(self, item_id: str) -> Dict[str, Any]:
        item_id = str(item_id or "")
        found = False
        with self._lock:
            training = self._data.setdefault("training", [])
            match = None
            remaining = []
            for item in training:
                if item.get("id") == item_id:
                    match = item
                else:
                    remaining.append(item)
            if match is None:
                found = False
            else:
                found = True

                kind = match.get("kind", "word")
                trigger_type = match.get("trigger_type") or "autoreplace"
                sources = {f"train_{kind}"}
                if kind == "trigger":
                    sources.update({"train_trigger_autoreplace", "train_trigger_autofill"})
                source = f"train_trigger_{trigger_type}" if kind == "trigger" else f"train_{kind}"
                heard_key = _norm(match.get("heard", ""))
                spoken_key = _norm(match.get("spoken", ""))
                output = match.get("output", "")
                output_key = _norm(
                    _clean_text(f"{match.get('spoken', '')} {output}") if trigger_type == "autofill" else output
                )
                self._data["training"] = remaining
                self._data["corrections"] = [
                    c for c in self._data.get("corrections", [])
                    if not (
                        c.get("source", source) in sources
                        and _norm(c.get("intended", "")) == output_key
                        and _norm(c.get("heard", "")) in {heard_key, spoken_key}
                    )
                ]
                self._save()
        snapshot = self.snapshot()
        return {"ok": found, **({} if found else {"reason": "not_found"}), **snapshot}

    def clear(self) -> Dict[str, Any]:
        with self._lock:
            self._data = _default_data()
            self._save()
            out = deepcopy(self._data)
        out["calibration_phrases"] = list(CALIBRATION_PHRASES)
        out["correction_count"] = 0
        out["vocabulary_count"] = 0
        out["calibration_count"] = 0
        out["training_count"] = 0
        out["word_training_count"] = 0
        out["trigger_count"] = 0
        return out

    def _upsert_correction(self, heard: str, intended: str, source: str) -> None:
        corrections = self._data.setdefault("corrections", [])
        heard_key = _norm(heard)
        intended_key = _norm(intended)
        for item in corrections:
            if _norm(item.get("heard", "")) == heard_key and _norm(item.get("intended", "")) == intended_key:
                item["count"] = int(item.get("count", 0)) + 1
                item["updated_at"] = _now()
                item["source"] = source
                return
        corrections.append(
            {
                "heard": heard,
                "intended": intended,
                "source": source,
                "count": 1,
                "updated_at": _now(),
            }
        )

    def _upsert_vocabulary(self, term: str) -> None:
        vocabulary = self._data.setdefault("vocabulary", [])
        term_key = _norm(term)
        for item in vocabulary:
            if _norm(item.get("term", "")) == term_key:
                item["count"] = int(item.get("count", 0)) + 1
                item["updated_at"] = _now()
                return
        vocabulary.append({"term": term, "count": 1, "updated_at": _now()})

    def _load(self) -> Dict[str, Any]:
        data = _default_data()
        if not self._path.exists():
            return data
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                user = json.load(fh)
            if isinstance(user, dict):
                data.update({k: user.get(k, v) for k, v in data.items()})
                for key in ("corrections", "vocabulary", "calibration", "training"):
                    if not isinstance(data.get(key), list):
                        data[key] = []
        except Exception as exc:  # noqa: BLE001
            logutil.warn(f"Failed to read adaptation store: {exc}")
        return data

    def _save(self) -> None:
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, self._path)


def _default_data() -> Dict[str, Any]:
    return {
        "version": 1,
        "setup_complete": False,
        "corrections": [],
        "vocabulary": [],
        "calibration": [],
        "training": [],
    }


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _norm(value: str) -> str:
    value = _clean_text(value).casefold()
    return re.sub(r"[^\w\s]", "", value)


def _words(value: str) -> List[str]:
    return re.findall(r"[A-Za-z0-9']+", value or "")


def _word_pairs(heard: str, intended: str) -> List[Tuple[str, str]]:
    heard_words = _words(heard)
    intended_words = _words(intended)
    matcher = difflib.SequenceMatcher(a=[w.casefold() for w in heard_words], b=[w.casefold() for w in intended_words])
    pairs: List[Tuple[str, str]] = []
    for tag, a0, a1, b0, b1 in matcher.get_opcodes():
        if tag != "replace":
            continue
        bad = heard_words[a0:a1]
        good = intended_words[b0:b1]
        if len(bad) == len(good):
            pairs.extend((b, g) for b, g in zip(bad, good) if _norm(b) != _norm(g))
        elif len(bad) == 1 and len(good) == 1:
            pairs.append((bad[0], good[0]))
    return pairs


def _important_terms(text: str) -> List[str]:
    terms = []
    for word in _words(text):
        if len(word) >= 3 and (word[0].isupper() or any(ch.isdigit() for ch in word)):
            terms.append(word)
    return terms


def _match_case(source: str, replacement: str) -> str:
    if source.isupper():
        return replacement.upper()
    if source[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
