'use strict';

const { uIOhook, UiohookKey } = require('uiohook-napi');

const MOD_KEYS = {
  alt: new Set([UiohookKey.Alt, UiohookKey.AltRight]),
  ctrl: new Set([UiohookKey.Ctrl, UiohookKey.CtrlRight]),
  shift: new Set([UiohookKey.Shift, UiohookKey.ShiftRight]),
  meta: new Set([UiohookKey.Meta, UiohookKey.MetaRight])
};

const MAIN_KEYS = {
  space: UiohookKey.Space,
  k: UiohookKey.K,
  l: UiohookKey.L,
  d: UiohookKey.D,
  j: UiohookKey.J,
  c: UiohookKey.C,
  escape: UiohookKey.Escape,
  esc: UiohookKey.Escape
};

function isModifierKey(keycode) {
  return Object.values(MOD_KEYS).some((codes) => codes.has(keycode));
}

function modNameForKey(keycode) {
  for (const [name, codes] of Object.entries(MOD_KEYS)) {
    if (codes.has(keycode)) return name;
  }
  return null;
}

function parseCombo(combo) {
  const mods = new Set();
  let main = null;
  String(combo || '').split('+').forEach((raw) => {
    const part = raw.trim().toLowerCase();
    if (!part) return;
    if (['alt', 'option', 'altgr'].includes(part)) mods.add('alt');
    else if (['cmd', 'command', 'meta', 'win', 'super'].includes(part)) mods.add('meta');
    else if (['ctrl', 'control', 'ctl'].includes(part)) mods.add('ctrl');
    else if (part === 'shift') mods.add('shift');
    else if (part === 'spacebar') main = MAIN_KEYS.space;
    else if (Object.prototype.hasOwnProperty.call(MAIN_KEYS, part)) main = MAIN_KEYS[part];
  });
  if (mods.size === 0 && main === null) return null;
  return { mods, main };
}

function sameMods(a, b) {
  if (a.size !== b.size) return false;
  for (const value of a) {
    if (!b.has(value)) return false;
  }
  return true;
}

class MacHotkeyManager {
  constructor(callbacks, logger, hook = uIOhook) {
    this.callbacks = callbacks;
    this.logger = logger;
    this.hook = hook;
    this.downKeys = new Set();
    this.pttActive = false;
    this.pttTimer = null;
    this.edgeFiredFor = new Set();
    this.started = false;
    this.listening = false;
    this.bindings = {};
    this.startedAt = 0;
    this.lastEventAt = 0;
    this.eventCount = 0;
  }

  configure(hotkeys = {}) {
    const getHotkey = (name, fallback) => (
      Object.prototype.hasOwnProperty.call(hotkeys, name) ? hotkeys[name] : fallback
    );
    this.bindings = {
      push_to_talk: parseCombo(getHotkey('push_to_talk', 'Option')),
      toggle: parseCombo(getHotkey('toggle', 'Option+Space')),
      code_push_to_talk: parseCombo(getHotkey('code_push_to_talk', 'Option+Shift+Space')),
      code_toggle: parseCombo(getHotkey('code_toggle', 'Cmd+Option+Space')),
      clarify: parseCombo(getHotkey('clarify', 'Cmd+Option+K')),
      learn_correction: parseCombo(getHotkey('learn_correction', 'Cmd+Option+L'))
    };
    this._log('info', `mac hotkeys configured: ${JSON.stringify(hotkeys)}`);
  }

  start() {
    if (this.started) return;
    const onKeyDown = (event) => this._onKeyDown(event);
    const onKeyUp = (event) => this._onKeyUp(event);
    try {
      this.hook.on('keydown', onKeyDown);
      this.hook.on('keyup', onKeyUp);
      this.hook.start();
      this.started = true;
      this.listening = true;
      this.startedAt = Date.now();
      this._log('info', 'mac native hotkey listener started');
    } catch (err) {
      this.started = false;
      this.listening = false;
      try {
        this.hook.removeListener('keydown', onKeyDown);
        this.hook.removeListener('keyup', onKeyUp);
      } catch (_) {
        // ignore listener cleanup races
      }
      throw err;
    }
  }

  stop() {
    const wasStarted = this.started;
    this.started = false;
    this.listening = false;
    this._cancelPttTimer();
    this.downKeys.clear();
    this.edgeFiredFor.clear();
    this.pttActive = false;
    try {
      if (wasStarted) this.hook.stop();
      this.hook.removeAllListeners('keydown');
      this.hook.removeAllListeners('keyup');
    } catch (_) {
      // ignore shutdown races
    }
  }

  isListening() {
    return this.listening;
  }

  status() {
    return {
      listening: this.listening,
      started_at: this.startedAt,
      last_event_at: this.lastEventAt,
      event_count: this.eventCount
    };
  }

  _observeEvent() {
    this.lastEventAt = Date.now();
    this.eventCount += 1;
  }

  _onKeyDown(event) {
    this._observeEvent();
    const keycode = event.keycode;
    const alreadyDown = this.downKeys.has(keycode);
    this.downKeys.add(keycode);

    if (isModifierKey(keycode)) {
      if (!alreadyDown) this._log('debug', `mac hotkey modifier down: ${modNameForKey(keycode)}`);
      this._evaluateModifierOnlyPtt();
      return;
    }

    this._cancelPttTimer();
    if (keycode === UiohookKey.Escape && !alreadyDown) {
      this._fire('cancel');
      return;
    }

    if (alreadyDown) return;
    for (const [action, callbackName] of [
      ['toggle', 'toggle'],
      ['code_push_to_talk', 'codePttStart'],
      ['code_toggle', 'codeToggle'],
      ['clarify', 'clarify'],
      ['learn_correction', 'learnCorrection']
    ]) {
      if (this._matches(this.bindings[action], keycode) && !this.edgeFiredFor.has(action)) {
        this.edgeFiredFor.add(action);
        this._fire(callbackName);
        return;
      }
    }
  }

  _onKeyUp(event) {
    this._observeEvent();
    const keycode = event.keycode;
    this.downKeys.delete(keycode);

    if (isModifierKey(keycode)) {
      this._log('debug', `mac hotkey modifier up: ${modNameForKey(keycode)}`);
      if (this._comboNoLongerHeld('code_push_to_talk')) this._finishCodePtt();
      if (this.pttActive && !this._modifierOnlyPttStillHeld()) {
        this.pttActive = false;
        this._fire('pttStop');
      }
      this._cancelPttTimer();
      if (this._modifierOnlyPttStillHeld()) this._evaluateModifierOnlyPtt();
      return;
    }

    if (this._comboNoLongerHeld('code_push_to_talk')) this._finishCodePtt();
    for (const [action, binding] of Object.entries(this.bindings)) {
      if (binding && binding.main === keycode) this.edgeFiredFor.delete(action);
    }
  }

  _currentMods() {
    const mods = new Set();
    for (const keycode of this.downKeys) {
      const name = modNameForKey(keycode);
      if (name) mods.add(name);
    }
    return mods;
  }

  _matches(binding, keycode) {
    return !!binding && binding.main === keycode && sameMods(binding.mods, this._currentMods());
  }

  _comboStillHeld(action) {
    const binding = this.bindings[action];
    return !!binding && binding.main !== null && this.downKeys.has(binding.main) && sameMods(binding.mods, this._currentMods());
  }

  _comboNoLongerHeld(action) {
    return this.edgeFiredFor.has(action) && !this._comboStillHeld(action);
  }

  _finishCodePtt() {
    this.edgeFiredFor.delete('code_push_to_talk');
    this._fire('codePttStop');
  }

  _modifierOnlyPttStillHeld() {
    const binding = this.bindings.push_to_talk;
    return !!binding && binding.main === null && sameMods(binding.mods, this._currentMods());
  }

  _evaluateModifierOnlyPtt() {
    if (!this._modifierOnlyPttStillHeld() || this.pttActive || this.pttTimer) return;
    this.pttTimer = setTimeout(() => {
      this.pttTimer = null;
      if (!this._modifierOnlyPttStillHeld() || this.pttActive) return;
      this.pttActive = true;
      this._fire('pttStart');
    }, 120);
  }

  _cancelPttTimer() {
    if (!this.pttTimer) return;
    clearTimeout(this.pttTimer);
    this.pttTimer = null;
  }

  _fire(name) {
    this._log('info', `mac hotkey action: ${name}`);
    const callback = this.callbacks[name];
    if (callback) callback();
  }

  _log(level, message) {
    if (this.logger && this.logger[level]) this.logger[level](message);
  }
}

module.exports = { MacHotkeyManager };
