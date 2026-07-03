# Packaging AFK

AFK ships with platform installers built by `electron-builder`: **NSIS** on
Windows and a **DMG** on macOS.

```bash
npm run dist     # -> installer/dist/AFK Setup <version>.exe
npm run dist:mac # -> installer/dist/AFK-<version>-arm64.dmg
npm run pack     # -> installer/dist/win-unpacked/ (unpacked, for debugging)
```

Config lives in the `build` field of [`package.json`](../package.json).

## What the installer contains

| Component | Bundled? | Location in install |
|-----------|----------|---------------------|
| Electron runtime + UI | ✅ | `resources/app.asar` |
| Python backend **source** | ✅ | `resources/python/` |
| llama.cpp `llama-server` (platform build) | ✅ | `resources/vendor/llama.cpp/` |
| App icons | ✅ | bundled |
| Python venv + backend deps (sounddevice, onnxruntime, etc.) | ✅ | `resources/python/.venv/` |
| Optional NeMo/PyTorch stack | ❌ provisioned | install only when using `.nemo` |
| Model weights (Parakeet, Gemma) | ❌ downloaded | user-data `models/` |

The icons are generated from a script (no binary blobs in git):

```bash
python/.venv/Scripts/python scripts/make_icons.py
```

## Why models and ML deps aren't bundled

The full optional NeMo/PyTorch stack is multi-GB, and the models are another
~6 GB. Shipping those inside the installer would make it enormous. AFK bundles
the lightweight Python backend venv for reliable audio/ONNX startup, while
provisioning large optional assets separately:

- **Models** download (or are placed) into the user-data `models/` directory:
  `models/parakeet-v3/` (ASR) and `models/clarify/` (Gemma GGUF). They are
  resolved at runtime by [`config.py`](../python/afk_backend/config.py) and are
  **never committed to git**.
- **Python deps** install into `python/.venv` via `npm run setup:python` and
  are bundled into packaged builds.

### Path resolution (dev vs. packaged)

`config.py` and `python-locator.js` check, in order:

1. Environment overrides (`AFK_DATA_DIR`, `AFK_MODELS_DIR`, `AFK_ASR_DIR`,
   `AFK_CLARIFY_DIR`, `AFK_LLAMA_SERVER`, `AFK_RESOURCES`).
2. The repo's `models/` and `vendor/` (development).
3. The packaged `resources/` and the OS user-data dir (production).

Electron passes `AFK_RESOURCES = process.resourcesPath` so the backend finds the
bundled `llama-server` when packaged.

## Producing a fully self-contained installer

To ship an installer that runs with **zero prerequisites** on the target
machine, bundle the Python runtime as well. Two supported approaches:

1. **Bundle the venv** — add `python/.venv` to `extraResources` (drop the
   `!.venv/**` filter). Simplest, but the installer grows by the size of the
   dependency set (multi-GB with PyTorch). The locator already prefers
   `resources/python/.venv` when present.
2. **Embeddable Python + first-run install** — ship a trimmed embeddable
   CPython and run `setup-python` on first launch. Smaller installer, requires
   network on first run.

The packaged app prefers `resources/python/.venv` and only falls back to a
system Python 3.11/3.12 if a custom build omits the bundled venv.

## No console windows

Every child process (the Python backend and each `llama-server`) is spawned
with `windowsHide` / `CREATE_NO_WINDOW`, so no terminal ever appears — a hard
requirement from the project spec.
