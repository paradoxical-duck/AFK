'use strict';

const { app, BrowserWindow, ipcMain, Tray, Menu, nativeImage, shell, screen, systemPreferences, globalShortcut } = require('electron');
const path = require('path');
const fs = require('fs');

const logger = require('./logger');
const paths = require('./paths');
const { PythonBridge } = require('./python-bridge');
const { MacHotkeyManager } = require('./mac-hotkeys');

let AutoLaunch = null;
try { AutoLaunch = require('auto-launch'); } catch (_) { /* optional */ }

let autoLauncher = null;
function applyAutoLaunch(enabled) {
  if (!AutoLaunch) return;
  try {
    if (!autoLauncher) {
      autoLauncher = new AutoLaunch({ name: 'AFK', isHidden: true });
    }
    autoLauncher.isEnabled().then((isOn) => {
      if (enabled && !isOn) autoLauncher.enable();
      else if (!enabled && isOn) autoLauncher.disable();
    }).catch(() => {});
  } catch (e) {
    logger.warn(`auto-launch failed: ${e.message}`);
  }
}

// Single-instance lock — AFK is a tray app; never run twice.
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
}

let mainWindow = null;
let overlayWindow = null;
let tray = null;
let bridge = null;
let isQuitting = false;
let overlayHideTimer = null;
let overlayReady = false;
let pendingOverlayPayload = null;
let macHotkeys = null;
let recordingActive = false;
let finishingRecording = false;
let macHotkeyError = '';
let macHotkeyLastConfig = null;
let macHotkeyPermissionTimer = null;
let macGlobalShortcutAccelerators = [];

const DEV = !!process.env.AFK_DEV;
const PROMPT_ACCESSIBILITY = process.argv.includes('--prompt-accessibility');
const APP_USER_MODEL_ID = 'com.afk.app';
const APP_ICON_PATH = path.join(__dirname, '..', 'assets', 'icon.ico');
const TRAY_ICON_PATH = path.join(__dirname, '..', 'assets', 'tray.png');
const TRAY_TEMPLATE_ICON_PATH = path.join(__dirname, '..', 'assets', 'trayTemplate.png');

if (process.platform === 'win32') {
  app.setAppUserModelId(APP_USER_MODEL_ID);
}
app.setName('AFK');
app.disableHardwareAcceleration();
app.commandLine.appendSwitch('disable-gpu');
app.commandLine.appendSwitch('disable-gpu-compositing');

function createWindow() {
  if (mainWindow) {
    mainWindow.show();
    mainWindow.focus();
    return;
  }

  mainWindow = new BrowserWindow({
    width: 980,
    height: 680,
    minWidth: 820,
    minHeight: 560,
    show: false,
    backgroundColor: '#0f1115',
    title: 'AFK',
    icon: APP_ICON_PATH,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false
    }
  });

  mainWindow.loadFile(path.join(__dirname, '..', 'ui', 'index.html'));

  mainWindow.once('ready-to-show', () => {
    const launchMinimized = process.argv.includes('--minimized');
    if (!launchMinimized) mainWindow.show();
  });

  // Close to tray instead of quitting.
  mainWindow.on('close', (e) => {
    if (!isQuitting) {
      e.preventDefault();
      mainWindow.hide();
    }
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  // External links open in the default browser, never in-app.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  if (DEV) mainWindow.webContents.openDevTools({ mode: 'detach' });
}

function positionOverlay() {
  if (!overlayWindow || overlayWindow.isDestroyed()) return;
  const display = screen.getDisplayNearestPoint(screen.getCursorScreenPoint());
  const { x, y, width, height } = display.workArea;
  const [overlayWidth, overlayHeight] = overlayWindow.getSize();
  overlayWindow.setPosition(
    Math.round(x + (width - overlayWidth) / 2),
    Math.round(y + height - overlayHeight - 28),
    false
  );
}

function createOverlayWindow() {
  if (overlayWindow && !overlayWindow.isDestroyed()) return;

  overlayReady = false;
  overlayWindow = new BrowserWindow({
    width: 460,
    height: 78,
    show: false,
    frame: false,
    transparent: true,
    resizable: false,
    movable: false,
    minimizable: false,
    maximizable: false,
    closable: false,
    focusable: false,
    skipTaskbar: true,
    hasShadow: false,
    alwaysOnTop: true,
    backgroundColor: '#00000000',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false
    }
  });

  overlayWindow.setIgnoreMouseEvents(true, { forward: true });
  overlayWindow.setAlwaysOnTop(true, 'screen-saver');
  overlayWindow.webContents.on('did-finish-load', () => {
    overlayReady = true;
    if (pendingOverlayPayload) {
      overlayWindow.webContents.send('overlay:state', pendingOverlayPayload);
    }
  });
  overlayWindow.webContents.on('render-process-gone', (_event, details) => {
    logger.warn(`Overlay renderer gone: ${details && details.reason ? details.reason : 'unknown'}`);
    if (overlayWindow && !overlayWindow.isDestroyed()) overlayWindow.destroy();
  });
  overlayWindow.loadFile(path.join(__dirname, '..', 'ui', 'overlay.html'));
  overlayWindow.on('closed', () => {
    overlayWindow = null;
    overlayReady = false;
  });
  positionOverlay();
}

function setOverlayState(state, payload = {}) {
  const overlayPayload = { state, ...payload };
  if (state === 'hidden' && (!overlayWindow || overlayWindow.isDestroyed())) return;
  if (state !== 'hidden') createOverlayWindow();
  if (!overlayWindow || overlayWindow.isDestroyed()) return;
  if (overlayHideTimer) {
    clearTimeout(overlayHideTimer);
    overlayHideTimer = null;
  }
  positionOverlay();
  pendingOverlayPayload = overlayPayload;
  if (state === 'hidden') {
    overlayWindow.hide();
    return;
  }
  overlayWindow.showInactive();
  if (overlayReady) {
    overlayWindow.webContents.send('overlay:state', overlayPayload);
  }
}

function hideOverlaySoon(delayMs = 1800) {
  if (overlayHideTimer) clearTimeout(overlayHideTimer);
  overlayHideTimer = setTimeout(() => {
    setOverlayState('hidden');
  }, delayMs);
}

function backendReadyForHotkeys() {
  return bridge && bridge.isReady;
}

function callBackendForHotkey(method, params = {}, timeoutMs) {
  if (!backendReadyForHotkeys()) {
    logger.warn(`hotkey ignored; backend not ready (${method})`);
    return Promise.resolve(null);
  }
  return bridge.call(method, params, timeoutMs).catch((err) => {
    logger.error(`hotkey backend call failed (${method}): ${err.message || err}`);
    return null;
  });
}

function startRecordingFromHotkey() {
  if (recordingActive || finishingRecording) return;
  callBackendForHotkey('start_recording', {}).then((result) => {
    if (result && result.recording) recordingActive = true;
  });
}

function finishRecordingFromHotkey() {
  if (!recordingActive || finishingRecording) return;
  finishingRecording = true;
  callBackendForHotkey('finish_recording', {}, 10 * 60 * 1000).finally(() => {
    finishingRecording = false;
  });
}

function toggleRecordingFromHotkey() {
  if (recordingActive) finishRecordingFromHotkey();
  else startRecordingFromHotkey();
}

function comboToAccelerator(combo) {
  const parts = String(combo || '').split('+').map((part) => part.trim()).filter(Boolean);
  if (parts.length < 2) return '';
  return parts.map((part) => {
    const lower = part.toLowerCase();
    if (lower === 'option' || lower === 'alt') return 'Alt';
    if (lower === 'cmd' || lower === 'command' || lower === 'meta') return 'Command';
    if (lower === 'ctrl' || lower === 'control') return 'Control';
    if (lower === 'shift') return 'Shift';
    if (lower === 'space' || lower === 'spacebar') return 'Space';
    if (part.length === 1) return part.toUpperCase();
    return part;
  }).join('+');
}

function clearMacGlobalShortcuts() {
  if (process.platform !== 'darwin') return;
  for (const accelerator of macGlobalShortcutAccelerators) {
    try {
      globalShortcut.unregister(accelerator);
    } catch (_) {
      // ignore unregister races
    }
  }
  macGlobalShortcutAccelerators = [];
}

function registerMacGlobalShortcut(name, combo, callback) {
  const accelerator = comboToAccelerator(combo);
  if (!accelerator) return;
  try {
    globalShortcut.unregister(accelerator);
    const ok = globalShortcut.register(accelerator, () => {
      logger.info(`mac global shortcut action: ${name}`);
      callback();
    });
    if (ok) {
      macGlobalShortcutAccelerators.push(accelerator);
      logger.info(`mac global shortcut registered: ${name} (${accelerator})`);
    } else {
      logger.warn(`mac global shortcut registration failed: ${name} (${accelerator})`);
    }
  } catch (err) {
    logger.error(`mac global shortcut registration error: ${name} (${accelerator}): ${err.message || err}`);
  }
}

function configureMacGlobalShortcuts(hotkeys = {}) {
  if (process.platform !== 'darwin') return;
  clearMacGlobalShortcuts();
  registerMacGlobalShortcut('toggle', hotkeys.toggle || 'Option+Space', toggleRecordingFromHotkey);
  registerMacGlobalShortcut('clarify', hotkeys.clarify || 'Cmd+Option+K', () => callBackendForHotkey('hotkey_clarify', {}, 10 * 60 * 1000));
  registerMacGlobalShortcut('learnCorrection', hotkeys.learn_correction || 'Cmd+Option+L', () => callBackendForHotkey('hotkey_learn_correction', {}, 10 * 60 * 1000));
}

function macAccessibilityTrusted(prompt = false) {
  if (process.platform !== 'darwin') return true;
  try {
    return systemPreferences.isTrustedAccessibilityClient(prompt);
  } catch (err) {
    logger.warn(`mac accessibility trust check failed: ${err.message || err}`);
    return null;
  }
}

function macHotkeyStatus(base = {}) {
  if (process.platform !== 'darwin') return base;
  const trusted = macAccessibilityTrusted(false);
  const listening = !!(macHotkeys && macHotkeys.isListening && macHotkeys.isListening());
  return {
    ...base,
    available: true,
    listening,
    runtime: 'electron',
    mac_accessibility_trusted: trusted,
    mac_input_monitoring_trusted: true,
    error: trusted === false ? 'Accessibility permission needed for AFK.app' : macHotkeyError
  };
}

function clearMacHotkeyPermissionTimer() {
  if (!macHotkeyPermissionTimer) return;
  clearInterval(macHotkeyPermissionTimer);
  macHotkeyPermissionTimer = null;
}

function attemptStartMacHotkeys(logFailure = true) {
  if (!macHotkeys) return false;
  if (macHotkeys.isListening && macHotkeys.isListening()) return true;
  try {
    macHotkeys.start();
    clearMacHotkeyPermissionTimer();
    macHotkeyError = '';
    return true;
  } catch (err) {
    macHotkeyError = err.message || String(err);
    const message = `mac native hotkey listener failed: ${err.message || err}`;
    if (logFailure) logger.error(message);
    else logger.debug(message);
    return false;
  }
}

function waitForMacHotkeyPermission() {
  if (process.platform !== 'darwin' || macHotkeyPermissionTimer) return;
  macHotkeyPermissionTimer = setInterval(() => {
    const trusted = macAccessibilityTrusted(false);
    if (attemptStartMacHotkeys(trusted === true)) {
      logger.info('mac native hotkey listener started after permission retry');
    }
  }, 3000);
}

function configureMacHotkeys(hotkeys) {
  if (process.platform !== 'darwin') return;
  macHotkeyLastConfig = hotkeys || {};
  if (!macHotkeys) {
    macHotkeys = new MacHotkeyManager(
      {
        pttStart: startRecordingFromHotkey,
        pttStop: finishRecordingFromHotkey,
        toggle: toggleRecordingFromHotkey,
        clarify: () => callBackendForHotkey('hotkey_clarify', {}, 10 * 60 * 1000),
        learnCorrection: () => callBackendForHotkey('hotkey_learn_correction', {}, 10 * 60 * 1000),
        cancel: () => callBackendForHotkey('hotkey_cancel', {})
      },
      logger
    );
  }
  configureMacGlobalShortcuts(hotkeys || {});
  macHotkeys.configure({
    push_to_talk: (hotkeys && hotkeys.push_to_talk) || 'Option',
    toggle: '',
    clarify: '',
    learn_correction: ''
  });

  const trusted = macAccessibilityTrusted(false);
  if (trusted === false) {
    macHotkeyError = 'Accessibility permission needed for AFK.app';
    logger.warn('mac accessibility trust query is false; attempting native listener anyway');
  }

  if (!attemptStartMacHotkeys()) waitForMacHotkeyPermission();
}

function createTrayImage() {
  try {
    const primary = process.platform === 'darwin' ? TRAY_TEMPLATE_ICON_PATH : TRAY_ICON_PATH;
    let image = nativeImage.createFromPath(primary);
    if (image.isEmpty()) image = nativeImage.createFromPath(APP_ICON_PATH);
    if (image.isEmpty()) return nativeImage.createEmpty();
    if (process.platform === 'darwin') {
      image = image.resize({ width: 18, height: 18 });
      image.setTemplateImage(true);
    }
    return image;
  } catch (_) {
    return nativeImage.createEmpty();
  }
}

function createTray() {
  tray = new Tray(createTrayImage());
  tray.setToolTip('AFK — local speech-to-text');

  const menu = Menu.buildFromTemplate([
    { label: 'Open AFK', click: () => createWindow() },
    { type: 'separator' },
    {
      label: 'Backend status',
      enabled: false,
      id: 'status'
    },
    { type: 'separator' },
    {
      label: 'Quit AFK',
      click: () => {
        isQuitting = true;
        app.quit();
      }
    }
  ]);
  tray.setContextMenu(menu);
  tray.on('double-click', () => createWindow());
}

function broadcast(channel, payload) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send(channel, payload);
  }
}

function startBackend() {
  bridge = new PythonBridge({
    dataDir: paths.dataDir(),
    modelsDir: paths.modelsDir(),
    resourcesPath: app.isPackaged ? process.resourcesPath : ''
  });

  bridge.on('log', ({ level, msg }) => logger[level] ? logger[level](msg) : logger.info(msg));

  bridge.on('ready', (info) => {
    logger.info(`Backend ready: ${JSON.stringify(info)}`);
    broadcast('backend:status', { ready: true, info });
    // Apply OS-level preferences from saved settings.
    bridge.call('get_settings', {}).then((cfg) => {
      applyAutoLaunch(!!(cfg && cfg.startup_on_login));
      configureMacHotkeys(cfg && cfg.hotkeys);
    }).catch(() => {});
  });

  // React to settings changes for OS-level behaviors (auto-launch).
  bridge.on('event:settings_updated', (cfg) => {
    applyAutoLaunch(!!(cfg && cfg.startup_on_login));
    configureMacHotkeys(cfg && cfg.hotkeys);
  });

  bridge.on('exit', () => {
    broadcast('backend:status', { ready: false });
    setOverlayState('hidden');
  });

  // Forward all backend events to the renderer under a single channel.
  bridge.on('event', (event, data) => {
    broadcast('backend:event', { event, data });
    if (event === 'recording_started') {
      recordingActive = true;
      setOverlayState('recording', { label: 'Listening' });
    } else if (event === 'recording_stopped') {
      recordingActive = false;
      setOverlayState('processing', { label: 'Transcribing' });
    } else if (event === 'transcription') {
      finishingRecording = false;
      const text = data && data.text ? String(data.text) : '';
      const reason = data && data.reason;
      const message = data && data.message;
      setOverlayState('done', {
        label: text ? 'Ready to paste' : (reason === 'low_signal' ? 'Mic too quiet' : 'No speech detected'),
        sub: text ? 'Dictation complete' : (message || 'Try speaking closer to the microphone')
      });
      hideOverlaySoon(text ? 1400 : 1800);
    } else if (event === 'pasted') {
      setOverlayState('done', { label: 'Pasted' });
      hideOverlaySoon(900);
    } else if (event === 'copied') {
      setOverlayState('done', { label: 'Copied to clipboard' });
      hideOverlaySoon(1200);
    } else if (event === 'clarify_started') {
      setOverlayState('clarifying', { label: 'Clarifying', sub: 'Polishing selected text locally' });
    } else if (event === 'clarify_unavailable') {
      setOverlayState('done', { label: 'Clarify unavailable', sub: (data && data.reason) || 'Install local models' });
      hideOverlaySoon(1800);
    } else if (event === 'clarify_done') {
      setOverlayState('done', { label: 'Corrected' });
      hideOverlaySoon(1200);
    } else if (event === 'cancelled') {
      recordingActive = false;
      finishingRecording = false;
      setOverlayState('done', { label: 'Cancelled' });
      hideOverlaySoon(900);
    }
  });

  bridge.start();
}

// ---- IPC: renderer <-> main <-> python ----

function registerIpc() {
  // Generic pass-through to the Python backend.
  ipcMain.handle('afk:call', async (_evt, { method, params }) => {
    if (!bridge) throw new Error('Backend not initialised');
    const longCalls = new Set([
      'load_asr',
      'stop_recording',
      'finish_recording',
      'finish_training_sample',
      'finish_calibration',
      'transcribe',
      'clarify'
    ]);
    const result = await bridge.call(method, params || {}, longCalls.has(method) ? 10 * 60 * 1000 : undefined);
    return method === 'hotkeys_status' ? macHotkeyStatus(result) : result;
  });

  ipcMain.handle('afk:backendReady', () => (bridge ? bridge.isReady : false));

  ipcMain.handle('app:getInfo', () => ({
    version: app.getVersion(),
    name: app.getName(),
    dev: DEV,
    platform: process.platform,
    electron: process.versions.electron,
    node: process.versions.node
  }));

  ipcMain.handle('window:minimize', () => mainWindow && mainWindow.minimize());
  ipcMain.handle('window:hide', () => mainWindow && mainWindow.hide());
  ipcMain.handle('app:quit', () => {
    isQuitting = true;
    app.quit();
  });

  ipcMain.handle('shell:openExternal', (_e, url) => shell.openExternal(url));
}

// ---- App lifecycle ----

app.on('second-instance', () => {
  createWindow();
});

app.whenReady().then(() => {
  logger.init(paths.logsDir());
  logger.info('AFK starting up');
  if (PROMPT_ACCESSIBILITY && process.platform === 'darwin') {
    logger.info('mac accessibility permission prompt requested by setup flag');
    macAccessibilityTrusted(true);
  }

  registerIpc();
  createTray();
  startBackend();
  createWindow();

  screen.on('display-metrics-changed', positionOverlay);
  screen.on('display-added', positionOverlay);
  screen.on('display-removed', positionOverlay);

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

// Keep running in the tray when all windows are closed.
app.on('window-all-closed', (e) => {
  // do not quit — AFK lives in the tray
});

app.on('before-quit', () => {
  isQuitting = true;
  clearMacHotkeyPermissionTimer();
  clearMacGlobalShortcuts();
  if (macHotkeys) macHotkeys.stop();
  if (bridge) bridge.stop();
});

process.on('uncaughtException', (err) => {
  logger.error(`Uncaught exception: ${err && err.stack ? err.stack : err}`);
});
