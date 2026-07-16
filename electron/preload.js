'use strict';

const { contextBridge, ipcRenderer } = require('electron');

/**
 * Secure bridge exposed to the renderer as `window.afk`.
 * contextIsolation is on; the renderer never touches Node or ipcRenderer
 * directly. Everything funnels through these whitelisted calls.
 */
const listeners = new Map();

function on(channel, cb) {
  const wrapped = (_evt, payload) => cb(payload);
  ipcRenderer.on(channel, wrapped);
  listeners.set(cb, { channel, wrapped });
  return () => off(cb);
}

function off(cb) {
  const entry = listeners.get(cb);
  if (entry) {
    ipcRenderer.removeListener(entry.channel, entry.wrapped);
    listeners.delete(cb);
  }
}

contextBridge.exposeInMainWorld('afk', {
  // Call any backend RPC method: afk.call('transcribe', {...})
  call: (method, params) => ipcRenderer.invoke('afk:call', { method, params }),
  backendReady: () => ipcRenderer.invoke('afk:backendReady'),

  app: {
    getInfo: () => ipcRenderer.invoke('app:getInfo'),
    quit: () => ipcRenderer.invoke('app:quit'),
    restartHotkeys: () => ipcRenderer.invoke('hotkeys:restart'),
    openExternal: (url) => ipcRenderer.invoke('shell:openExternal', url)
  },

  window: {
    minimize: () => ipcRenderer.invoke('window:minimize'),
    hide: () => ipcRenderer.invoke('window:hide')
  },

  // Event subscriptions
  onBackendStatus: (cb) => on('backend:status', cb),
  onBackendEvent: (cb) => on('backend:event', cb),
  onOverlayState: (cb) => on('overlay:state', cb),
  off
});
