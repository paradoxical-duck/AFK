'use strict';

const { app, BrowserWindow, ipcMain, Tray, Menu, nativeImage, shell, screen, systemPreferences, globalShortcut, powerMonitor } = require('electron');
const path = require('path');
const fs = require('fs');

const logger = require('./logger');
const paths = require('./paths');
const { PythonBridge } = require('./python-bridge');
const { MacHotkeyManager } = require('./mac-hotkeys');
const { RecordingState } = require('./recording-state');

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
const recordingState = new RecordingState();
let macHotkeyError = '';
let macHotkeyLastConfig = null;
let macHotkeyPermissionTimer = null;
let macHotkeyRestartTimer = null;
let macHotkeyWatchdogTimer = null;
let macHotkeyWatchdogTick = Date.now();
let macGlobalShortcutAccelerators = [];
let quitCleanupStarted = false;

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
    width: 1120,
    height: 760,
    minWidth: 820,
    minHeight: 560,
    show: false,
    backgroundColor: '#1b4b72',
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

  const launchMinimized = process.argv.includes('--minimized') || process.argv.includes('--hidden');
  mainWindow.loadFile(path.join(__dirname, '..', 'ui', 'index.html'));

  mainWindow.once('ready-to-show', () => {
    if (!launchMinimized) mainWindow.show();
  });

  // A renderer recovering from sleep or cache corruption may miss
  // ready-to-show. Do not leave an explicitly opened window invisible.
  setTimeout(() => {
    if (!launchMinimized && mainWindow && !mainWindow.isDestroyed() && !mainWindow.isVisible()) {
      mainWindow.show();
    }
  }, 1200);

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
  const { x, y, width, height } = display.bounds;
  const [overlayWidth, overlayHeight] = overlayWindow.getSize();
  overlayWindow.setPosition(
    Math.round(x + (width - overlayWidth) / 2),
    Math.round(y + height - overlayHeight - 28),
    false
  );
}

function keepOverlayVisibleAboveFullscreen() {
  if (!overlayWindow || overlayWindow.isDestroyed()) return;
  try {
    overlayWindow.setAlwaysOnTop(true, 'screen-saver', 1);
    if (process.platform === 'darwin') {
      overlayWindow.setFullScreenable(false);
      overlayWindow.setHiddenInMissionControl(true);
      overlayWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
      overlayWindow.moveTop();
    }
  } catch (err) {
    logger.warn(`overlay z-order refresh failed: ${err.message || err}`);
  }
}

function createOverlayWindow() {
  if (overlayWindow && !overlayWindow.isDestroyed()) return;

  overlayReady = false;
  overlayWindow = new BrowserWindow({
    width: 460,
    height: 78,
    show: false,
    frame: false,
    ...(process.platform === 'darwin' ? { type: 'panel', roundedCorners: false } : {}),
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
    fullscreenable: false,
    backgroundColor: '#00000000',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false
    }
  });

  overlayWindow.setIgnoreMouseEvents(true, { forward: true });
  keepOverlayVisibleAboveFullscreen();
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
  keepOverlayVisibleAboveFullscreen();
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
  keepOverlayVisibleAboveFullscreen();
  pendingOverlayPayload = overlayPayload;
  if (state === 'hidden') {
    overlayWindow.hide();
    return;
  }
  overlayWindow.showInactive();
  keepOverlayVisibleAboveFullscreen();
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

function startRecordingFromHotkey(mode = 'dictation') {
  if (!recordingState.begin(mode)) return;
  callBackendForHotkey('start_recording', {}).then((result) => {
    if (result && result.recording && recordingState.starting) {
      const shouldFinish = recordingState.markStarted();
      updateTrayMenu();
      if (shouldFinish) finishRecordingFromHotkey();
    } else if ((!result || !result.recording) && recordingState.starting) {
      recordingState.markStartFailed();
      updateTrayMenu();
    }
  });
}

function finishRecordingFromHotkey() {
  const action = recordingState.requestFinish();
  if (action !== 'finish') return;
  updateTrayMenu();
  const method = recordingState.mode === 'code' ? 'finish_code_recording' : 'finish_recording';
  callBackendForHotkey(method, {}, 10 * 60 * 1000).finally(() => {
    recordingState.markFinished();
    updateTrayMenu();
  });
}

function toggleRecordingFromHotkey() {
  if (recordingState.active || recordingState.starting) finishRecordingFromHotkey();
  else startRecordingFromHotkey();
}

function startCodeRecordingFromHotkey() {
  startRecordingFromHotkey('code');
}

function finishCodeRecordingFromHotkey() {
  if (recordingState.mode !== 'code') return;
  finishRecordingFromHotkey();
}

function toggleCodeRecordingFromHotkey() {
  if (recordingState.active || recordingState.starting) {
    if (recordingState.mode !== 'code') return;
    finishCodeRecordingFromHotkey();
  } else {
    startCodeRecordingFromHotkey();
  }
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
  const listenerStatus = macHotkeys && macHotkeys.status ? macHotkeys.status() : {};
  return {
    ...base,
    ...listenerStatus,
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

function clearMacHotkeyRestartTimer() {
  if (!macHotkeyRestartTimer) return;
  clearTimeout(macHotkeyRestartTimer);
  macHotkeyRestartTimer = null;
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
    if (trusted !== true) return;
    if (attemptStartMacHotkeys(true)) {
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
        codePttStart: startCodeRecordingFromHotkey,
        codePttStop: finishCodeRecordingFromHotkey,
        codeToggle: toggleCodeRecordingFromHotkey,
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
    toggle: (hotkeys && hotkeys.toggle) || 'Option+Space',
    code_push_to_talk: (hotkeys && hotkeys.code_push_to_talk) || 'Option+Shift+Space',
    code_toggle: (hotkeys && hotkeys.code_toggle) || 'Cmd+Option+Space',
    clarify: '',
    learn_correction: ''
  });

  const trusted = macAccessibilityTrusted(false);
  if (trusted === false) {
    macHotkeyError = 'Accessibility permission needed for AFK.app';
    logger.warn('mac accessibility trust query is false; native listener paused until permission is granted');
    waitForMacHotkeyPermission();
    return;
  }

  if (!attemptStartMacHotkeys()) waitForMacHotkeyPermission();
}

function pauseMacHotkeys(reason) {
  if (process.platform !== 'darwin') return;
  clearMacHotkeyRestartTimer();
  clearMacHotkeyPermissionTimer();
  clearMacGlobalShortcuts();
  if (macHotkeys) macHotkeys.stop();
  logger.info(`mac hotkeys paused: ${reason}`);
}

function scheduleMacHotkeyRestart(reason, delayMs = 650) {
  if (process.platform !== 'darwin' || isQuitting || !macHotkeyLastConfig) return false;
  pauseMacHotkeys(reason);
  macHotkeyRestartTimer = setTimeout(() => {
    macHotkeyRestartTimer = null;
    if (isQuitting) return;
    logger.info(`re-arming mac hotkeys: ${reason}`);
    configureMacHotkeys(macHotkeyLastConfig);
    updateTrayMenu();
  }, delayMs);
  return true;
}

function cancelRecordingForSystemTransition(reason) {
  if (!recordingState.busy) return;
  logger.warn(`cancelling active recording for ${reason}`);
  callBackendForHotkey('hotkey_cancel', {});
  recordingState.reset();
  updateTrayMenu();
  setOverlayState('hidden');
}

function setupMacHotkeyRecovery() {
  if (process.platform !== 'darwin') return;

  const pauseFor = (reason) => {
    cancelRecordingForSystemTransition(reason);
    pauseMacHotkeys(reason);
  };
  powerMonitor.on('suspend', () => pauseFor('system sleep'));
  powerMonitor.on('lock-screen', () => pauseFor('screen lock'));
  powerMonitor.on('resume', () => scheduleMacHotkeyRestart('system wake'));
  powerMonitor.on('unlock-screen', () => scheduleMacHotkeyRestart('screen unlock'));

  macHotkeyWatchdogTick = Date.now();
  macHotkeyWatchdogTimer = setInterval(() => {
    const now = Date.now();
    const elapsed = now - macHotkeyWatchdogTick;
    macHotkeyWatchdogTick = now;
    if (elapsed > 75000) {
      scheduleMacHotkeyRestart(`watchdog clock gap (${Math.round(elapsed / 1000)}s)`);
    } else if (macHotkeys && !macHotkeys.isListening()) {
      scheduleMacHotkeyRestart('listener health check');
    }
  }, 30000);
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

function updateTrayMenu() {
  if (!tray) return;
  const menu = Menu.buildFromTemplate([
    { label: 'Open AFK', click: () => createWindow() },
    {
      label: recordingState.active || recordingState.starting ? 'Stop transcription' : 'Start transcription',
      enabled: backendReadyForHotkeys() && !recordingState.finishing,
      click: () => toggleRecordingFromHotkey()
    },
    {
      label: (recordingState.active || recordingState.starting) && recordingState.mode === 'code' ? 'Stop code transcription' : 'Start code transcription',
      enabled: backendReadyForHotkeys() && !recordingState.finishing && (!recordingState.busy || recordingState.mode === 'code'),
      click: () => toggleCodeRecordingFromHotkey()
    },
    { type: 'separator' },
    {
      label: backendReadyForHotkeys() ? 'Backend ready' : 'Backend starting',
      enabled: false,
      id: 'status'
    },
    ...(process.platform === 'darwin' ? [{
      label: 'Restart shortcuts',
      enabled: !!macHotkeyLastConfig,
      click: () => scheduleMacHotkeyRestart('menu command', 150)
    }] : []),
    { type: 'separator' },
    {
      label: 'Quit AFK',
      click: () => requestQuit()
    }
  ]);
  tray.setContextMenu(menu);
}

function createTray() {
  tray = new Tray(createTrayImage());
  tray.setToolTip('AFK — local speech-to-text');
  updateTrayMenu();
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
    recordingState.reset();
    updateTrayMenu();
    broadcast('backend:status', { ready: false });
    setOverlayState('hidden');
  });

  // Forward all backend events to the renderer under a single channel.
  bridge.on('event', (event, data) => {
    broadcast('backend:event', { event, data });
    if (event === 'recording_started') {
      const shouldFinish = recordingState.markStarted();
      updateTrayMenu();
      setOverlayState('recording', { label: recordingState.mode === 'code' ? 'Listening for code' : 'Listening' });
      if (shouldFinish) setImmediate(finishRecordingFromHotkey);
    } else if (event === 'recording_stopped') {
      recordingState.markRecordingStopped();
      updateTrayMenu();
      setOverlayState('processing', { label: 'Transcribing' });
    } else if (event === 'transcription') {
      recordingState.markFinished();
      updateTrayMenu();
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
    } else if (event === 'code_format_started') {
      setOverlayState('clarifying', { label: 'Formatting code', sub: 'Converting spoken syntax' });
    } else if (event === 'code_formatted') {
      setOverlayState('done', { label: 'Code ready' });
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
      recordingState.reset();
      updateTrayMenu();
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
      'finish_code_recording',
      'finish_training_sample',
      'finish_calibration',
      'transcribe',
      'format_code_text',
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
    requestQuit();
    return true;
  });

  ipcMain.handle('hotkeys:restart', () => ({ scheduled: scheduleMacHotkeyRestart('app command', 150) }));

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
    const trusted = macAccessibilityTrusted(true);
    logger.info(`mac accessibility one-shot setup prompt requested; trusted=${trusted}`);
  }

  registerIpc();
  createTray();
  startBackend();
  createWindow();
  setupMacHotkeyRecovery();

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

async function shutdownAndExit() {
  if (quitCleanupStarted) return;
  quitCleanupStarted = true;
  isQuitting = true;
  logger.info('AFK shutdown requested');
  clearMacHotkeyRestartTimer();
  clearMacHotkeyPermissionTimer();
  if (macHotkeyWatchdogTimer) {
    clearInterval(macHotkeyWatchdogTimer);
    macHotkeyWatchdogTimer = null;
  }
  clearMacGlobalShortcuts();
  if (macHotkeys) macHotkeys.stop();
  if (tray) {
    tray.destroy();
    tray = null;
  }
  try {
    if (bridge) await bridge.stop();
  } catch (err) {
    logger.warn(`backend shutdown failed: ${err.message || err}`);
  }
  for (const window of BrowserWindow.getAllWindows()) {
    if (!window.isDestroyed()) window.destroy();
  }
  logger.info('AFK shutdown complete');
  app.exit(0);
}

function requestQuit() {
  if (quitCleanupStarted) return;
  void shutdownAndExit();
}

app.on('before-quit', (event) => {
  if (quitCleanupStarted) return;
  event.preventDefault();
  requestQuit();
});

process.on('uncaughtException', (err) => {
  logger.error(`Uncaught exception: ${err && err.stack ? err.stack : err}`);
});
