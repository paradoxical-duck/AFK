"""Filesystem paths and shared constants for the AFK backend.

The Electron side passes AFK_DATA_DIR so both processes agree on where
user data lives. When run standalone (tests, dev), we fall back to a local
.afk-data directory.
"""

import os
from pathlib import Path


def data_dir() -> Path:
    env = os.environ.get("AFK_DATA_DIR")
    if env:
        p = Path(env)
    else:
        p = Path(__file__).resolve().parents[2] / ".afk-data"
    p.mkdir(parents=True, exist_ok=True)
    return p


def models_dir() -> Path:
    # Allow override; otherwise sit next to data dir's parent (userData/models).
    env = os.environ.get("AFK_MODELS_DIR")
    if env:
        p = Path(env)
    else:
        p = data_dir().parent / "models"
    p.mkdir(parents=True, exist_ok=True)
    return p


def local_asr_dir() -> Path:
    """Optional pre-downloaded Parakeet model directory.

    If the user places the onnx-asr model files here, we load from disk and
    skip any network download. Checked before the Hugging Face hub.

    Search order:
      1. $AFK_ASR_DIR (explicit override)
      2. <installed resources>/models/parakeet-v3
      3. <repo>/models/parakeet-v3   (handy for development)
      4. <models_dir>/parakeet-v3    (alongside other app models)
      5. Hugging Face cache snapshot from the official ONNX repo
    """
    env = os.environ.get("AFK_ASR_DIR")
    if env:
        return Path(env)
    for candidate in _local_asr_candidates():
        if _has_required_asr_files(candidate):
            return candidate
    return models_dir() / "parakeet-v3"


def _local_asr_candidates() -> tuple[Path, ...]:
    root = Path(__file__).resolve().parents[2]
    model_root = models_dir()
    return (
        root / "models" / "parakeet-v3",
        model_root / "parakeet-v3",
        model_root
        / "hub"
        / "models--istupakov--parakeet-tdt-0.6b-v3-onnx"
        / "snapshots"
        / "8f23f0c03c8761650bdb5b40aaf3e40d2c15f1ce",
    )


# Files onnx-asr needs for the int8 Parakeet model loaded from a local path.
LOCAL_ASR_REQUIRED = (
    "encoder-model.int8.onnx",
    "decoder_joint-model.int8.onnx",
    "vocab.txt",
    "config.json",
)


def _has_required_asr_files(path: Path) -> bool:
    return path.exists() and all((path / name).exists() for name in LOCAL_ASR_REQUIRED)


# Official NVIDIA NeMo checkpoint filename.
NEMO_CHECKPOINT = "parakeet-tdt-0.6b-v3.nemo"


def local_nemo_path() -> Path:
    """Path to a user-provided official NVIDIA .nemo checkpoint, if any.

    Search order mirrors local_asr_dir():
      1. $AFK_NEMO_PATH (explicit override; may point at the file directly)
      2. <local_asr_dir>/parakeet-tdt-0.6b-v3.nemo
    """
    env = os.environ.get("AFK_NEMO_PATH")
    if env:
        return Path(env)
    return local_asr_dir() / NEMO_CHECKPOINT


def select_asr_engine() -> tuple[str, Path | None]:
    """Decide which ASR engine to use based on what the user has provided.

    Preference:
      * If an official .nemo checkpoint is present -> ('nemo', <path>)
      * Else -> ('onnx', None)  (local int8 files or Hugging Face download)

    An explicit $AFK_ASR_ENGINE ('nemo' | 'onnx') overrides auto-detection.
    """
    forced = os.environ.get("AFK_ASR_ENGINE", "").strip().lower()
    nemo_path = local_nemo_path()
    if forced == "nemo":
        return "nemo", nemo_path
    if forced == "onnx":
        return "onnx", None
    if nemo_path.exists() and nemo_path.is_file():
        return "nemo", nemo_path
    return "onnx", None


def settings_path() -> Path:
    return data_dir() / "settings.json"


def stats_path() -> Path:
    return data_dir() / "statistics.json"


def adaptation_path() -> Path:
    return data_dir() / "adaptation.json"


def history_path() -> Path:
    return data_dir() / "transcriptions.json"


# ---- Model identifiers (used from Phase 2/4 onward) ----
PARAKEET_MODEL = "nemo-parakeet-tdt-0.6b-v3"
GEMMA_SHORT_MODEL = "gemma-3-270m-it"
GEMMA_LONG_MODEL = "gemma-4-e2b-it"

# Default word-count threshold for short vs. long clarification model.
# The 270M model is fast but too small for sentence-level grammar cleanup, so
# route only long-form text to Gemma 4 by default.
DEFAULT_WORD_THRESHOLD = 100

# Clarify model GGUF filenames + their Hugging Face source repos.
CLARIFY_SHORT_GGUF = "gemma-3-270m-Q8_0.gguf"
CLARIFY_LONG_GGUF = "gemma-4-E2B-it-Q8_0.gguf"
CLARIFY_SHORT_REPO = "ggml-org/gemma-3-270m-GGUF"
CLARIFY_LONG_REPO = "ggml-org/gemma-4-E2B-it-GGUF"

# The Clarify system instruction (from the project spec).
CLARIFY_PROMPT = (
    "Correct grammar, punctuation, capitalization, and minor wording while "
    "preserving the original meaning, tone, names, and technical terms. "
    "Do not add or remove information. Return only the corrected text."
)

# Few-shot examples used to coax the tiny short model (Gemma 3 270M) into
# actually performing the edit rather than echoing the input. Presented as
# prior user/assistant turns.
CLARIFY_FEWSHOT = (
    ("i dont no where he wnet yesterday", "I do not know where he went yesterday."),
    ("she dont like cofee in the mornin", "She does not like coffee in the morning."),
    ("can u snd me teh file wen ur done plz", "Can you send me the file when you are done, please?"),
)


def llama_server_path() -> Path:
    """Locate the official llama.cpp `llama-server` binary.

    Search: $AFK_LLAMA_SERVER, else <repo>/vendor/llama.cpp, else bundled
    resources/vendor/llama.cpp (packaged app). We use the upstream prebuilt
    Windows CPU binary (runtime CPU dispatch) rather
    than llama-cpp-python, whose wheels require AVX-512.
    """
    exe = "llama-server.exe" if os.name == "nt" else "llama-server"
    env = os.environ.get("AFK_LLAMA_SERVER")
    if env:
        return Path(env)
    repo_local = Path(__file__).resolve().parents[2] / "vendor" / "llama.cpp" / exe
    if repo_local.exists():
        return repo_local
    res = os.environ.get("AFK_RESOURCES")
    if res:
        return Path(res) / "vendor" / "llama.cpp" / exe
    return repo_local


def clarify_dir() -> Path:
    """Directory holding the Clarify GGUF model files.

    Mirrors the ASR layout: $AFK_CLARIFY_DIR, else <repo>/models/clarify,
    else <models_dir>/clarify.
    """
    env = os.environ.get("AFK_CLARIFY_DIR")
    if env:
        return Path(env)
    repo_local = Path(__file__).resolve().parents[2] / "models" / "clarify"
    if repo_local.exists():
        return repo_local
    return models_dir() / "clarify"


def _resolve_gguf(preferred: str, *contains: str) -> Path:
    """Find a GGUF in the clarify dir.

    Prefer the exact expected filename; otherwise match any *.gguf whose name
    contains all the given substrings (case-insensitive), skipping vision
    projector files ('mmproj'). This tolerates the many community naming
    conventions (e.g. 'google_gemma-3-270m-it-Q4_K_M.gguf').
    """
    d = clarify_dir()
    exact = d / preferred
    if exact.exists():
        return exact
    try:
        for p in sorted(d.glob("*.gguf")):
            name = p.name.lower()
            if "mmproj" in name:
                continue
            if all(c.lower() in name for c in contains):
                return p
    except Exception:
        pass
    return exact  # default (may not exist; callers handle 'missing')


def clarify_short_path() -> Path:
    return _resolve_gguf(CLARIFY_SHORT_GGUF, "270m")


def clarify_long_path() -> Path:
    return _resolve_gguf(CLARIFY_LONG_GGUF, "e2b")

# Typing speed assumption for "time saved" stats (words per minute).
TYPING_WPM = 40
