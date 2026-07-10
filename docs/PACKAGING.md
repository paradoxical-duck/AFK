# Packaging AFK

AFK ships with platform installers built by `electron-builder`: **NSIS** on
Windows and a **DMG** on macOS.

```bash
npm run dist     # -> installer/dist/AFK Setup <version>.exe
npm run dist:mac # -> installer/dist/AFK-<version>-arm64.dmg
npm run pack     # -> installer/dist/win-unpacked/ (unpacked, for debugging)
```

Config lives in the `build` field of [`package.json`](../package.json).

## macOS local signing

macOS Accessibility grants are tied to the app's code-signing requirement. The
mac pack step therefore signs with `AFK Stable Local Code Signing` when that
identity exists, using `~/Library/Application Support/AFK/signing/afk-local-signing.keychain-db`
by default. Set `AFK_MAC_CODESIGN_IDENTITY` and `AFK_MAC_CODESIGN_KEYCHAIN` to
override those values.

If no stable identity is available, packaging falls back to ad-hoc signing and
macOS may require Accessibility permission to be removed and re-added after a
rebuild.

## What the installer contains

| Component | Bundled? | Location in install |
|-----------|----------|---------------------|
| Electron runtime + UI | ✅ | `resources/app.asar` |
| Python backend **source** | ✅ | `resources/python/` |
| llama.cpp `llama-server` (platform build) | ✅ | `resources/vendor/llama.cpp/` |
| App icons | ✅ | bundled |
| Python runtime + backend deps (sounddevice, onnxruntime, etc.) | ✅ | `resources/python/runtime/` |
| macOS Python.framework | ✅ | `Contents/Frameworks/Python.framework/` |
| Optional NeMo/PyTorch stack | ❌ provisioned | install only when using `.nemo` |
| Model weights (Parakeet, Gemma) | ❌ downloaded | user-data `models/` |

The icons are generated from a script (no binary blobs in git):

```bash
python/.venv/Scripts/python scripts/make_icons.py
```

## Why models and ML deps aren't bundled

The full optional NeMo/PyTorch stack is multi-GB, and the models are another
~6 GB. Shipping those inside the installer would make it enormous. AFK bundles
the lightweight Python backend runtime for reliable audio/ONNX startup, while
provisioning large optional assets separately:

- **Models** download (or are placed) into the user-data `models/` directory:
  `models/parakeet-v3/` (ASR) and `models/clarify/` (Gemma GGUF). They are
  resolved at runtime by [`config.py`](../python/afk_backend/config.py) and are
  **never committed to git**.
- **Python deps** install into `python/runtime` via `npm run setup:python` and
  are bundled into packaged builds. On macOS, packaging also embeds
  `Python.framework` and rewrites the runtime launcher paths so the backend does
  not depend on the developer machine's `/Library/Frameworks/Python.framework`.

The backend should still reach "ready" when model weights are absent. Missing
ASR or clarify files should show as missing/not loaded in model status, not as a
permanent "Starting backend" state. The Parakeet ONNX ASR files are downloaded
on first use when internet is available; Gemma clarify models are optional and
must be downloaded or copied into `models/clarify/`.

### Path resolution (dev vs. packaged)

`config.py` and `python-locator.js` check, in order:

1. Environment overrides (`AFK_DATA_DIR`, `AFK_MODELS_DIR`, `AFK_ASR_DIR`,
   `AFK_CLARIFY_DIR`, `AFK_LLAMA_SERVER`, `AFK_RESOURCES`).
2. The repo's `models/` and `vendor/` (development).
3. The packaged `resources/` and the OS user-data dir (production).

Electron passes `AFK_RESOURCES = process.resourcesPath` so the backend finds the
bundled `llama-server` when packaged.

## Producing a fully self-contained installer

To ship an installer that runs transcription and clarify with **zero network or
post-install downloads** on the target machine, bundle or preinstall model
weights as well. Two supported approaches:

1. **Bundle model weights** — add a release-only model payload under
   `resources/models/` or seed the user-data model directory during install.
   This makes the DMG several GB larger.
2. **First-run model download** — keep the DMG smaller and download ASR/clarify
   assets after launch. This requires network on the target Mac before offline
   use.

The packaged app prefers `resources/python/runtime` and only falls back to a
system Python 3.11/3.12 if a custom build omits the bundled runtime.

## Sharing outside this Mac

The local development DMG is signed with the stable local AFK identity so
Accessibility grants survive rebuilds on this machine. It is **not**
Apple-notarized unless the release is signed with a Developer ID certificate and
submitted to Apple. AirDrop or browser downloads can attach quarantine metadata,
so a recipient may need to right-click **Open** the app the first time, or a
release engineer must notarize the DMG for a smoother install.

## No console windows

Every child process (the Python backend and each `llama-server`) is spawned
with `windowsHide` / `CREATE_NO_WINDOW`, so no terminal ever appears — a hard
requirement from the project spec.
