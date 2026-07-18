'use strict';

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

let isRecording = false;
let recordTransitioning = false;
let recTimer = null;
let recStart = 0;
let _settingsCache = null;
let _platform = /mac/i.test(navigator.platform || '') ? 'darwin' : '';
let activeTrainingKind = null;

function defaultHotkeys() {
  return _platform === 'darwin'
    ? {
        push_to_talk: 'Option',
        toggle: 'Option+Space',
        clarify: 'Cmd+Option+K',
        learn_correction: 'Cmd+Option+L',
        code_push_to_talk: 'Option+Shift+Space',
        code_toggle: 'Cmd+Option+Space'
      }
    : {
        push_to_talk: 'Ctrl+Space',
        toggle: 'Ctrl+Shift+Space',
        clarify: 'Ctrl+Alt+K',
        learn_correction: 'Ctrl+Alt+L',
        code_push_to_talk: 'Ctrl+Alt+J',
        code_toggle: 'Ctrl+Shift+J'
      };
}

function hotkeyOptions() {
  return _platform === 'darwin'
    ? [
        'Option',
        'Option+Space',
        'Option+Shift+Space',
        'Cmd+Option+Space',
        'Cmd+Shift+K',
        'Cmd+Shift+L',
        'Cmd+Option+K',
        'Cmd+Option+L',
        'Cmd+Option+D',
        'Option+K',
        'Option+L',
        'Option+D'
      ]
    : [
        'Ctrl+Space',
        'Ctrl+Shift+Space',
        'Ctrl+Alt+Space',
        'Ctrl+Alt+K',
        'Ctrl+Shift+K',
        'Ctrl+Alt+L',
        'Ctrl+Shift+L',
        'Ctrl+Alt+D',
        'Ctrl+Shift+D',
        'Ctrl+Alt+J',
        'Ctrl+Shift+J'
      ];
}

function codeLanguageOptions(selected) {
  const options = [
    ['auto', 'Auto'],
    ['python', 'Python'],
    ['javascript', 'JavaScript'],
    ['typescript', 'TypeScript'],
    ['swift', 'Swift'],
    ['java', 'Java'],
    ['cpp', 'C++'],
    ['shell', 'Shell']
  ];
  return options.map(([value, label]) =>
    `<option value="${escapeHtml(value)}" ${selected === value ? 'selected' : ''}>${escapeHtml(label)}</option>`
  ).join('');
}

function escapeHtml(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function setText(sel, value) {
  const el = $(sel);
  if (el) el.textContent = value;
}

function selectedOptionLabel(select) {
  const option = select.options[select.selectedIndex];
  return option ? option.textContent : '';
}

function syncSelectControl(select) {
  const shell = select.closest('.select-shell');
  if (!shell) return;
  const button = shell.querySelector('.select-button');
  const menu = shell.querySelector('.select-menu');
  if (button) button.querySelector('span').textContent = selectedOptionLabel(select);
  if (!menu) return;
  Array.from(menu.children).forEach((item) => {
    const active = item.dataset.value === select.value;
    item.classList.toggle('active', active);
    item.setAttribute('aria-selected', active ? 'true' : 'false');
  });
}

function closeSelects(exceptShell) {
  $$('.select-shell.open').forEach((shell) => {
    if (shell === exceptShell) return;
    shell.classList.remove('open');
    const button = shell.querySelector('.select-button');
    if (button) button.setAttribute('aria-expanded', 'false');
  });
}

function rebuildSelectMenu(select) {
  const shell = select.closest('.select-shell');
  if (!shell) return;
  const menu = shell.querySelector('.select-menu');
  if (!menu) return;
  menu.innerHTML = Array.from(select.options).map((option) =>
    `<button type="button" class="select-option" role="option" data-value="${escapeHtml(option.value)}"><span>${escapeHtml(option.textContent)}</span><i aria-hidden="true"></i></button>`
  ).join('');
  syncSelectControl(select);
}

function positionSelectMenu(shell) {
  const button = shell.querySelector('.select-button');
  const menu = shell.querySelector('.select-menu');
  if (!button || !menu) return;
  shell.classList.remove('open-up', 'align-right');
  const rect = button.getBoundingClientRect();
  const margin = 16;
  const below = Math.max(0, window.innerHeight - rect.bottom - margin);
  const above = Math.max(0, rect.top - margin);
  const openUp = below < 190 && above > below;
  const available = openUp ? above : below;
  shell.classList.toggle('open-up', openUp);
  menu.style.maxHeight = `${Math.max(120, Math.min(320, available - 8))}px`;
  if (menu.getBoundingClientRect().right > window.innerWidth - margin) {
    shell.classList.add('align-right');
  }
}

function enhanceSelect(select) {
  if (!select || select.dataset.enhanced === 'true') {
    if (select) rebuildSelectMenu(select);
    return;
  }
  select.dataset.enhanced = 'true';
  select.classList.add('native-select');
  const shell = document.createElement('div');
  shell.className = 'select-shell';
  select.parentNode.insertBefore(shell, select);
  shell.appendChild(select);
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'select-button';
  button.setAttribute('aria-haspopup', 'listbox');
  button.setAttribute('aria-expanded', 'false');
  button.innerHTML = `<span>${escapeHtml(selectedOptionLabel(select))}</span><i aria-hidden="true"></i>`;
  const menu = document.createElement('div');
  menu.className = 'select-menu';
  menu.setAttribute('role', 'listbox');
  shell.appendChild(button);
  shell.appendChild(menu);
  rebuildSelectMenu(select);

  button.addEventListener('click', () => {
    const open = !shell.classList.contains('open');
    closeSelects(shell);
    shell.classList.toggle('open', open);
    button.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) positionSelectMenu(shell);
  });

  button.addEventListener('keydown', (event) => {
    if (!['ArrowDown', 'ArrowUp', 'Enter', ' ', 'Escape'].includes(event.key)) return;
    event.preventDefault();
    if (event.key === 'Escape') {
      closeSelects();
      return;
    }
    if (!shell.classList.contains('open')) {
      shell.classList.add('open');
      button.setAttribute('aria-expanded', 'true');
      positionSelectMenu(shell);
      return;
    }
    const options = Array.from(select.options);
    const dir = event.key === 'ArrowUp' ? -1 : 1;
    const next = Math.max(0, Math.min(options.length - 1, select.selectedIndex + dir));
    select.value = options[next].value;
    select.dispatchEvent(new Event('change', { bubbles: true }));
    syncSelectControl(select);
  });

  menu.addEventListener('click', (event) => {
    const item = event.target.closest('.select-option');
    if (!item) return;
    select.value = item.dataset.value;
    select.dispatchEvent(new Event('change', { bubbles: true }));
    closeSelects();
    syncSelectControl(select);
    button.focus();
  });

  select.addEventListener('change', () => syncSelectControl(select));
}

function enhanceSelects(root = document) {
  root.querySelectorAll('select').forEach(enhanceSelect);
}

document.addEventListener('click', (event) => {
  if (!event.target.closest('.select-shell')) closeSelects();
});

// ---------- Navigation ----------
function initNav() {
  $$('.nav-item').forEach((btn) => {
    btn.addEventListener('click', () => {
      const page = btn.dataset.page;
      $$('.nav-item').forEach((b) => b.classList.toggle('active', b === btn));
      $$('.page').forEach((p) => p.classList.toggle('active', p.id === `page-${page}`));
      if (page === 'home') refreshHomeStats();
      if (page === 'statistics') refreshStatistics();
      if (page === 'train') refreshTrain();
      if (page === 'settings') refreshSettings();
    });
  });
}

// ---------- Backend status ----------
function setBackendStatus(ready) {
  const dot = $('#backendDot');
  const label = $('#backendLabel');
  if (dot) dot.className = ready ? 'dot dot-ok' : 'dot dot-pending';
  if (label) label.textContent = ready ? 'Backend ready' : 'Starting backend';
}

async function initAbout() {
  try {
    const info = await window.afk.app.getInfo();
    setText('#aboutVersion', info.version);
    setText('#aboutElectron', info.electron);
    setText('#aboutNode', info.node);
    _platform = info.platform || '';
  } catch (e) {
    // Backend shell can still be starting.
  }
}

async function applySavedTheme() {
  try {
    const cfg = await window.afk.call('get_settings', {});
    _settingsCache = cfg;
    applyTheme(cfg.theme);
  } catch (e) {
    applyTheme('dark');
  }
}

async function refreshBackendInfo() {
  try {
    const ready = await window.afk.backendReady();
    setBackendStatus(ready);
    if (!ready) return;
    const info = await window.afk.call('get_info', {});
    setText('#aboutBackend', `${info.backend} (py ${info.python})`);
    setText('#aboutModels', info.models_status || 'not loaded');
    setText('#activeModel', 'Parakeet + Gemma');
  } catch (e) {
    setBackendStatus(false);
  }
}

// ---------- Microphones + ASR ----------
async function refreshMicrophones() {
  try {
    const { devices } = await window.afk.call('list_microphones', {});
    const sel = $('#micSelect');
    const current = sel.value;
    sel.innerHTML = '<option value="">System default</option>';
    (devices || []).forEach((d) => {
      const opt = document.createElement('option');
      opt.value = d.name;
      opt.textContent = d.default ? `${d.name} (default)` : d.name;
      sel.appendChild(opt);
    });
    const cfg = await window.afk.call('get_settings', {});
    _settingsCache = cfg;
    sel.value = cfg.microphone || current || '';
    enhanceSelect(sel);
  } catch (e) {
    // Backend may still be booting.
  }
}

async function refreshAsrStatus() {
  try {
    const { status, engine } = await window.afk.call('asr_status', {});
    const map = {
      loaded: 'ready',
      loading: 'loading',
      'not loaded': 'idle'
    };
    setText('#asrStatus', `Speech: ${engine || 'auto'} / ${map[status] || status}`);
  } catch (e) {
    setText('#asrStatus', 'Speech: starting');
  }
}

async function loadAsrModel() {
  const btn = $('#loadAsrBtn');
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Loading...';
  }
  setText('#asrStatus', 'Speech: loading');
  try {
    const res = await window.afk.call('load_asr', {});
    setText('#asrStatus', `Speech: ${res.status || 'ready'}`);
  } catch (e) {
    setText('#asrStatus', 'Speech: error');
    showTranscription(`ASR load failed: ${e.message || e}`);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = 'Load speech model';
    }
  }
}

async function toggleRecord() {
  const btn = $('#recordBtn');
  if (recordTransitioning) return;
  recordTransitioning = true;
  if (btn) btn.disabled = true;
  try {
    if (!isRecording) {
      const device = $('#micSelect').value || null;
      setRecordButtonLabel('Starting');
      await window.afk.call('start_recording', { device });
      setRecording(true);
      return;
    }

    setRecordButtonLabel('Transcribing');
    const res = await window.afk.call('finish_recording', {});
    setRecording(false);
    if (res && res.text) {
      const action = res.action === 'pasted' ? 'Pasted.' : 'Copied to clipboard.';
      showTranscription(res.text, action);
      setText('#recordStatus', res.action === 'pasted' ? 'Pasted' : 'Copied');
    } else if (res && res.message) {
      showTranscription('', res.message);
    }
  } catch (e) {
    setRecording(false);
    showTranscription(`Recording failed: ${e.message || e}`);
  } finally {
    recordTransitioning = false;
    if (btn) btn.disabled = false;
    setRecordButtonLabel(isRecording ? 'Stop and transcribe' : 'Start recording');
    refreshAsrStatus();
  }
}

async function onMicChange() {
  const value = $('#micSelect').value || null;
  try {
    await window.afk.call('update_settings', { patch: { microphone: value } });
  } catch (e) {
    // Ignore transient backend startup failures.
  }
}

// ---------- Clarify ----------
async function refreshClarifyStatus() {
  try {
    const s = await window.afk.call('clarify_status', {});
    const clean = (value) => value === 'loaded' ? 'ready' : value;
    setText('#clarifyModels', `Clarify: ${clean(s.short)} / ${clean(s.long)}`);
  } catch (e) {
    setText('#clarifyModels', 'Clarify: starting');
  }
}

async function clarifyText() {
  const btn = $('#clarifyBtn');
  const inputEl = $('#clarifyInput');
  const input = inputEl ? inputEl.value.trim() : '';
  if (!input) return;
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Clarifying...';
  }
  setText('#clarifyMeta', '');
  try {
    const res = await window.afk.call('clarify', { text: input });
    setText('#clarifyOutput', res.text || '(no output)');
    const model = res.model && res.model !== 'none' ? res.model : 'no model';
    setText(
      '#clarifyMeta',
      `${res.words} words` + (res.latency_ms ? ` / ${res.latency_ms} ms` : '')
    );
  } catch (e) {
    setText('#clarifyOutput', `Clarify failed: ${e.message || e}`);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = 'Clarify';
    }
    refreshClarifyStatus();
  }
}

// ---------- Hotkeys ----------
async function refreshHotkeys() {
  try {
    const cfg = await window.afk.call('get_settings', {});
    _settingsCache = cfg;
    const hk = cfg.hotkeys || {};
    const defaults = defaultHotkeys();
    $('#pttHotkey').textContent = hk.push_to_talk || defaults.push_to_talk;
    $('#toggleHotkey').textContent = hk.toggle || defaults.toggle;
    $('#clarifyHotkey').textContent = hk.clarify || defaults.clarify;
    $('#learnHotkey').textContent = hk.learn_correction || defaults.learn_correction;
    if ($('#codeHoldHotkey')) $('#codeHoldHotkey').textContent = hk.code_push_to_talk || defaults.code_push_to_talk;
    if ($('#codeToggleHotkey')) $('#codeToggleHotkey').textContent = hk.code_toggle || defaults.code_toggle;
    refreshHotkeyStatus();
  } catch (e) {
    const defaults = defaultHotkeys();
    $('#pttHotkey').textContent = defaults.push_to_talk;
    $('#toggleHotkey').textContent = defaults.toggle;
    $('#clarifyHotkey').textContent = defaults.clarify;
    $('#learnHotkey').textContent = defaults.learn_correction;
    if ($('#codeHoldHotkey')) $('#codeHoldHotkey').textContent = defaults.code_push_to_talk;
    if ($('#codeToggleHotkey')) $('#codeToggleHotkey').textContent = defaults.code_toggle;
    setHotkeyHealth('Unavailable', 'bad');
  }
}

async function refreshHotkeyStatus() {
  try {
    const status = await window.afk.call('hotkeys_status', {});
    if (!status.available) {
      setHotkeyHealth('Unavailable', 'bad');
    } else if (_platform === 'darwin' && status.mac_input_monitoring_trusted === false) {
      setHotkeyHealth('Input Monitoring needed', 'bad');
    } else if (_platform === 'darwin' && status.mac_accessibility_trusted === false) {
      setHotkeyHealth('Accessibility needed', 'bad');
    } else if (status.error) {
      setHotkeyHealth(status.error, 'bad');
    } else {
      setHotkeyHealth(status.listening ? 'Ready' : 'Starting', status.listening ? 'ok' : 'pending');
    }
  } catch (e) {
    setHotkeyHealth('Unavailable', 'bad');
  }
}

function setHotkeyHealth(label, kind) {
  setText('#hotkeyStatus', label);
  const dot = $('#hotkeyHealthDot');
  if (dot) dot.className = `dot dot-${kind}`;
}

function setRestartControls(busy) {
  const inline = $('#restartHotkeysInlineBtn');
  if (inline) {
    inline.disabled = busy;
    inline.textContent = busy ? 'Restarting' : 'Restart';
  }
  const sidebar = $('#restartHotkeysBtn');
  if (sidebar) {
    sidebar.disabled = busy;
    const label = sidebar.querySelector('span:last-child');
    if (label) label.textContent = busy ? 'Restarting' : 'Restart shortcuts';
  }
  const settings = $('#settingsRestartHotkeysBtn');
  if (settings) {
    settings.disabled = busy;
    settings.textContent = busy ? 'Restarting' : 'Restart shortcuts';
  }
}

async function restartHotkeys() {
  setRestartControls(true);
  setHotkeyHealth('Restarting', 'pending');
  try {
    await window.afk.app.restartHotkeys();
    setTimeout(() => {
      setRestartControls(false);
      refreshHotkeyStatus();
    }, 1100);
  } catch (e) {
    setRestartControls(false);
    setHotkeyHealth('Restart failed', 'bad');
  }
}

function isEditableTarget(target) {
  if (!target) return false;
  if (target.isContentEditable) return true;
  const tag = String(target.tagName || '').toLowerCase();
  return tag === 'input' || tag === 'textarea' || tag === 'select';
}

function comboFromEvent(event) {
  const key = event.key;
  const lower = String(key || '').toLowerCase();
  if (['control', 'shift', 'alt', 'meta'].includes(lower)) {
    if (lower === 'alt' && event.altKey && !event.ctrlKey && !event.shiftKey && !event.metaKey) {
      return 'Option';
    }
    return '';
  }

  const parts = [];
  if (_platform === 'darwin' && event.ctrlKey) return '';
  if (event.ctrlKey) parts.push('Ctrl');
  if (event.shiftKey) parts.push('Shift');
  if (event.altKey) parts.push(_platform === 'darwin' ? 'Option' : 'Alt');
  if (event.metaKey) parts.push(_platform === 'darwin' ? 'Cmd' : 'Win');

  let main = '';
  if (event.code === 'Space' || lower === ' ') main = 'Space';
  else if (/^Key[A-Z]$/.test(event.code)) main = event.code.slice(3);
  else if (/^Digit[0-9]$/.test(event.code)) main = event.code.slice(5);
  else if (/^F[0-9]{1,2}$/.test(event.key)) main = event.key.toUpperCase();
  else if (lower === 'escape') main = 'Esc';
  else if (lower === 'arrowup') main = 'Up';
  else if (lower === 'arrowdown') main = 'Down';
  else if (lower === 'arrowleft') main = 'Left';
  else if (lower === 'arrowright') main = 'Right';
  else if (key && key.length === 1) main = key.toUpperCase();
  else if (key) main = key.charAt(0).toUpperCase() + key.slice(1);

  if (!main) return '';
  parts.push(main);
  return parts.join('+');
}

function normalizeCombo(combo) {
  if (!combo) return '';
  const mods = [];
  let main = '';
  String(combo).split('+').forEach((part) => {
    const p = part.trim().toLowerCase();
    if (!p) return;
    if (['ctrl', 'control', 'ctl'].includes(p)) mods.push('ctrl');
    else if (p === 'shift') mods.push('shift');
    else if (['alt', 'option', 'altgr'].includes(p)) mods.push('alt');
    else if (['win', 'cmd', 'super', 'meta', 'windows'].includes(p)) mods.push('win');
    else if (['space', 'spacebar'].includes(p)) main = 'space';
    else if (p === 'esc') main = 'escape';
    else main = p;
  });
  const order = ['ctrl', 'shift', 'alt', 'win'];
  return order.filter((m) => mods.includes(m)).concat(main ? [main] : []).join('+');
}

function eventMatchesCombo(event, combo) {
  return normalizeCombo(comboFromEvent(event)) === normalizeCombo(combo);
}

function configuredHotkeys() {
  const hk = (_settingsCache && _settingsCache.hotkeys) || {};
  const defaults = defaultHotkeys();
  return [
    hk.push_to_talk || defaults.push_to_talk,
    hk.toggle || defaults.toggle,
    hk.clarify || defaults.clarify,
    hk.learn_correction || defaults.learn_correction,
    hk.code_push_to_talk || defaults.code_push_to_talk,
    hk.code_toggle || defaults.code_toggle
  ];
}

function initEditableHotkeyHandling() {
  document.addEventListener('keydown', (event) => {
    const target = event.target;
    if (target && target.classList && target.classList.contains('hotkey-input')) {
      event.preventDefault();
      event.stopPropagation();
      if (event.key === 'Backspace' || event.key === 'Delete') {
        target.value = '';
        saveHotkeys();
        return;
      }
      const combo = comboFromEvent(event);
      if (combo) {
        target.value = combo;
        saveHotkeys();
      }
      return;
    }

    if (!isEditableTarget(target)) return;
    if (configuredHotkeys().some((combo) => eventMatchesCombo(event, combo))) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  }, true);

  document.addEventListener('keyup', (event) => {
    if (!isEditableTarget(event.target)) return;
    if (configuredHotkeys().some((combo) => eventMatchesCombo(event, combo))) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  }, true);
}

// ---------- Statistics ----------
function fmtDuration(sec) {
  sec = Math.round(sec || 0);
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  if (m < 60) return s ? `${m}m ${s}s` : `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

function metric(value, label, detail, color) {
  return `<div class="home-metric" style="--metric-color:${color}">
    <strong>${escapeHtml(value)}</strong>
    <span>${escapeHtml(label)}</span>
    <small>${escapeHtml(detail)}</small>
  </div>`;
}

function activitySeries(stats) {
  if (Array.isArray(stats.activity) && stats.activity.length) return stats.activity;
  const today = new Date();
  return Array.from({ length: 14 }, (_, index) => {
    const current = new Date(today);
    current.setDate(today.getDate() - (13 - index));
    return {
      date: current.toISOString().slice(0, 10),
      words: index === 13 ? Number(stats.words.today || 0) : 0,
      recordings: 0,
      recording_seconds: 0
    };
  });
}

function shortDate(value) {
  const parsed = new Date(`${value}T12:00:00`);
  return parsed.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

function areaChartSvg(activity, options = {}) {
  const width = 760;
  const height = options.height || 240;
  const pad = { top: 22, right: 18, bottom: 34, left: 44 };
  const values = activity.map((item) => Number(item.words || 0));
  const max = Math.max(1, ...values);
  const innerW = width - pad.left - pad.right;
  const innerH = height - pad.top - pad.bottom;
  const points = values.map((value, index) => ({
    x: pad.left + (index / Math.max(1, values.length - 1)) * innerW,
    y: pad.top + innerH - (value / max) * innerH,
    value
  }));
  const line = points.map((point, index) => `${index ? 'L' : 'M'}${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(' ');
  const area = `${line} L${points[points.length - 1].x.toFixed(1)},${(pad.top + innerH).toFixed(1)} L${points[0].x.toFixed(1)},${(pad.top + innerH).toFixed(1)} Z`;
  const grids = [0, 0.5, 1].map((ratio) => {
    const y = pad.top + innerH - ratio * innerH;
    const label = Math.round(max * ratio).toLocaleString();
    return `<line class="chart-grid" x1="${pad.left}" y1="${y}" x2="${width - pad.right}" y2="${y}"></line>
      <text class="chart-axis-label" x="${pad.left - 9}" y="${y + 3}" text-anchor="end">${label}</text>`;
  }).join('');
  const labelIndexes = Array.from(new Set([0, 3, 6, 9, activity.length - 1])).filter((index) => index < activity.length);
  const labels = labelIndexes.map((index) =>
    `<text class="chart-axis-label" x="${points[index].x}" y="${height - 8}" text-anchor="middle">${escapeHtml(shortDate(activity[index].date))}</text>`
  ).join('');
  const peak = values.indexOf(Math.max(...values));
  const dots = points.map((point, index) => {
    if (point.value <= 0 || (index !== peak && index !== points.length - 1)) return '';
    return `<circle class="chart-point${index === points.length - 1 ? ' today' : ''}" cx="${point.x}" cy="${point.y}" r="5">
      <title>${escapeHtml(shortDate(activity[index].date))}: ${point.value.toLocaleString()} words</title>
    </circle>`;
  }).join('');
  return `<svg class="activity-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="Words dictated during the last 14 days">
    ${grids}<path class="chart-area" d="${area}"></path><path class="chart-line" d="${line}"></path>${dots}${labels}
  </svg>`;
}

function recordingBarsSvg(activity) {
  const width = 720;
  const height = 230;
  const pad = { top: 12, right: 10, bottom: 32, left: 10 };
  const values = activity.map((item) => Number(item.recordings || 0));
  const max = Math.max(1, ...values);
  const slot = (width - pad.left - pad.right) / values.length;
  const bars = values.map((value, index) => {
    const barHeight = value ? Math.max(4, (value / max) * (height - pad.top - pad.bottom)) : 2;
    const x = pad.left + index * slot + slot * 0.18;
    const y = height - pad.bottom - barHeight;
    return `<rect class="bar-chart-bar${index === values.length - 1 ? ' today' : ''}" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${(slot * 0.64).toFixed(1)}" height="${barHeight.toFixed(1)}" rx="2">
      <title>${escapeHtml(shortDate(activity[index].date))}: ${value} recordings</title>
    </rect>`;
  }).join('');
  const labels = [0, 3, 6, 9, activity.length - 1].filter((index, position, all) => index < activity.length && all.indexOf(index) === position).map((index) => {
    const x = pad.left + index * slot + slot / 2;
    return `<text class="bar-chart-label" x="${x.toFixed(1)}" y="${height - 7}" text-anchor="middle">${escapeHtml(shortDate(activity[index].date))}</text>`;
  }).join('');
  return `<svg class="activity-bars" viewBox="0 0 ${width} ${height}" role="img" aria-label="Daily recording count during the last 14 days">${bars}${labels}</svg>`;
}

function paceRingSvg(value) {
  const target = 180;
  const ratio = Math.max(0, Math.min(1, Number(value || 0) / target));
  const circumference = 2 * Math.PI * 54;
  return `<svg class="pace-ring" viewBox="0 0 150 150" role="img" aria-label="Average speaking pace ${escapeHtml(value)} words per minute">
    <circle class="ring-track" cx="75" cy="75" r="54"></circle>
    <circle class="ring-value" cx="75" cy="75" r="54" stroke-dasharray="${circumference.toFixed(1)}" stroke-dashoffset="${(circumference * (1 - ratio)).toFixed(1)}"></circle>
    <text class="ring-number" x="75" y="72">${escapeHtml(value)}</text>
    <text class="ring-label" x="75" y="91">WORDS / MIN</text>
  </svg>`;
}

async function refreshHomeStats() {
  const box = $('#homeStats');
  const chart = $('#homeChart');
  if (!box) return;
  try {
    const s = await window.afk.call('get_statistics', {});
    const activity = activitySeries(s);
    const total14 = activity.reduce((total, item) => total + Number(item.words || 0), 0);
    box.innerHTML =
      metric(s.words.today.toLocaleString(), 'Words today', `${s.words.lifetime.toLocaleString()} all time`, '#35b8d4') +
      metric(s.words.week.toLocaleString(), 'Words this week', `${total14.toLocaleString()} in 14 days`, '#ff9a3d') +
      metric(fmtDuration(s.typing_minutes_saved * 60), 'Time reclaimed', `${s.wpm_avg} words per minute`, '#3ccb7f') +
      metric(`${s.streak_current}d`, 'Current streak', `${Math.max(s.streak_longest, s.streak_current)}d personal best`, '#1b4b72');
    if (chart) {
      chart.innerHTML =
        `<div class="voice-trail">
          <div class="chart-heading">
            <div><span class="section-kicker section-kicker-orange">Last 14 days</span><h3>Voice trail</h3></div>
            <span class="chart-total">${total14.toLocaleString()} WORDS</span>
          </div>
          ${areaChartSvg(activity, { height: 250 })}
        </div>`;
    }
  } catch (e) {
    box.innerHTML = '<div class="empty-hint">Statistics are not ready yet.</div>';
    if (chart) chart.innerHTML = '<div class="empty-hint">Activity will appear here.</div>';
  }
}

function historyItem(item) {
  const text = item.text || '';
  const action = item.action === 'pasted' ? 'Pasted' : (item.action === 'copied' ? 'Copied' : 'Saved');
  const date = item.created_at ? new Date(item.created_at).toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit'
  }) : '';
  return `<div class="history-item" data-history-id="${escapeHtml(item.id || '')}">
    <div class="history-text">
      <strong>${escapeHtml(action)}${date ? ` | ${escapeHtml(date)}` : ''}</strong>
      <span>${escapeHtml(text)}</span>
    </div>
    <div class="history-actions">
      <button class="icon-btn" data-copy-history="${escapeHtml(item.id || '')}" aria-label="Copy transcription">Copy</button>
      <button class="icon-btn danger" data-delete-history="${escapeHtml(item.id || '')}" aria-label="Delete transcription">Delete</button>
    </div>
  </div>`;
}

async function refreshHistory() {
  const list = $('#historyList');
  if (!list) return;
  try {
    const history = await window.afk.call('get_transcription_history', { limit: 20 });
    const items = history.items || [];
    list.innerHTML = items.length
      ? items.map(historyItem).join('')
      : '<div class="empty-hint">No transcriptions yet.</div>';
  } catch (e) {
    list.innerHTML = '<div class="empty-hint">History is not ready yet.</div>';
  }
}

async function copyHistoryItem(id) {
  const item = $(`[data-history-id="${CSS.escape(id)}"] .history-text span`);
  const text = item ? item.textContent : '';
  if (!text) return;
  await window.afk.call('set_clipboard', { text });
}

async function deleteHistoryItem(id) {
  await window.afk.call('delete_transcription_history', { id });
  refreshHistory();
}

async function refreshStatistics() {
  const grid = $('#statsGrid');
  try {
    const s = await window.afk.call('get_statistics', {});
    const activity = activitySeries(s);
    const recent = activity.slice(-7).reduce((total, item) => total + Number(item.words || 0), 0);
    const previous = activity.slice(0, 7).reduce((total, item) => total + Number(item.words || 0), 0);
    const delta = previous ? Math.round(((recent - previous) / previous) * 100) : (recent ? 100 : 0);
    const deltaLabel = delta === 0 ? 'Even with the previous week' : `${delta > 0 ? '+' : ''}${delta}% from the previous week`;
    const ringTarget = Math.max(1, s.streak_longest || s.streak_current || 1);
    grid.innerHTML =
      `<section class="insight-hero">
        <div class="insight-primary">
          <span class="section-kicker">All-time voice output</span>
          <strong>${s.words.lifetime.toLocaleString()}</strong>
          <span>words dictated on this Mac</span>
          <div class="insight-change"><i></i><span>${escapeHtml(deltaLabel)}</span></div>
        </div>
        <div class="insight-chart">
          <div class="chart-heading">
            <div><span class="section-kicker section-kicker-orange">Last 14 days</span><h3>Words spoken</h3></div>
            <span class="chart-total">${recent.toLocaleString()} THIS WEEK</span>
          </div>
          ${areaChartSvg(activity, { height: 245 })}
        </div>
      </section>

      <section class="insight-metrics" aria-label="Key dictation metrics">
        <div class="insight-metric" style="--metric-color:#0c8eae"><strong>${s.wpm_avg}</strong><span>Words per minute</span><small>Average speaking pace</small></div>
        <div class="insight-metric" style="--metric-color:#e77518"><strong>${fmtDuration(s.typing_minutes_saved * 60)}</strong><span>Time reclaimed</span><small>At ${s.typing_wpm_assumed || 40} typing WPM</small></div>
        <div class="insight-metric" style="--metric-color:#16995a"><strong>${s.recordings.toLocaleString()}</strong><span>Recording sessions</span><small>${fmtDuration(s.avg_recording_sec)} average length</small></div>
        <div class="insight-metric" style="--metric-color:#1b4b72"><strong>${s.streak_current}d</strong><span>Current streak</span><small>${s.streak_longest}d personal best</small></div>
      </section>

      <section class="insight-visuals">
        <div class="activity-panel">
          <div class="chart-heading">
            <div><span class="section-kicker section-kicker-cyan">Session cadence</span><h3>Recording rhythm</h3></div>
            <span class="chart-total">${s.recordings.toLocaleString()} TOTAL</span>
          </div>
          ${recordingBarsSvg(activity)}
        </div>
        <div class="pace-panel">
          ${paceRingSvg(s.wpm_avg)}
          <div class="pace-copy">
            <div class="pace-row"><span>Current / best streak</span><strong>${s.streak_current} / ${ringTarget} days</strong></div>
            <div class="pace-row"><span>Longest session</span><strong>${fmtDuration(s.longest_recording_sec)}</strong></div>
            <div class="pace-row"><span>Transcription latency</span><strong>${s.avg_transcription_latency_ms} ms</strong></div>
            <div class="pace-row"><span>Clarify requests</span><strong>${s.clarifications.toLocaleString()}</strong></div>
          </div>
        </div>
      </section>`;
  } catch (e) {
    grid.innerHTML = '<div class="empty-hint">Statistics are not ready yet.</div>';
  }
}

// ---------- Train ----------
function trainingItem(item) {
  const kind = item.kind === 'trigger' ? 'Trigger' : 'Word';
  const mode = item.kind === 'trigger' && item.trigger_type === 'autofill' ? 'Autofill' : 'Autoreplace';
  const heard = item.heard ? `Parakeet heard: ${item.heard}` : 'No audio sample captured';
  return `<div class="training-item">
    <div><strong>${escapeHtml(kind)}</strong><span>${escapeHtml(item.spoken || '')}</span>${item.kind === 'trigger' ? `<small>${escapeHtml(mode)}</small>` : ''}</div>
    <div><b>${escapeHtml(item.output || '')}</b><small>${escapeHtml(heard)}</small></div>
    <button class="icon-btn danger" data-delete-training="${escapeHtml(item.id || '')}" aria-label="Delete training sample">Delete</button>
  </div>`;
}

async function refreshTrain() {
  try {
    const adaptation = await window.afk.call('get_adaptation', {});
    $('#trainSummary').textContent = `${adaptation.training_count || 0} samples`;
    const items = (adaptation.training || []).slice(-8).reverse();
    $('#trainingList').innerHTML = items.length
      ? items.map(trainingItem).join('')
      : '<div class="empty-hint">No training samples yet.</div>';
  } catch (e) {
    $('#trainSummary').textContent = 'starting';
    $('#trainingList').innerHTML = '<div class="empty-hint">Training memory is not ready yet.</div>';
  }
}

async function startTrainSample(kind) {
  const isTrigger = kind === 'trigger';
  const spoken = (isTrigger ? $('#trainTriggerInput') : $('#trainWordInput')).value.trim();
  const output = (isTrigger ? $('#trainOutputInput').value.trim() : spoken);
  const status = isTrigger ? $('#trainTriggerStatus') : $('#trainWordStatus');
  if (!spoken || !output) {
    status.textContent = isTrigger ? 'Add both the spoken trigger and output first.' : 'Type the word or phrase first.';
    return;
  }
  try {
    activeTrainingKind = kind;
    status.textContent = 'Recording... say it naturally once.';
    await window.afk.call('start_training_sample', {
      kind,
      spoken,
      output,
      trigger_type: isTrigger ? ($('#trainTriggerType').value || 'autofill') : 'autoreplace',
      device: $('#micSelect') ? ($('#micSelect').value || null) : null
    });
  } catch (e) {
    activeTrainingKind = null;
    status.textContent = `Training failed to start: ${e.message || e}`;
  }
}

async function finishTrainSample(kind) {
  const status = kind === 'trigger' ? $('#trainTriggerStatus') : $('#trainWordStatus');
  try {
    status.textContent = 'Transcribing sample and saving correction...';
    const res = await window.afk.call('finish_training_sample', {});
    activeTrainingKind = null;
    const heard = (res && res.training && res.training.heard) || (res && res.text) || '';
    status.textContent = heard ? `Saved. Parakeet heard "${heard}".` : 'Saved, but no speech was detected in that sample.';
    refreshTrain();
    refreshHomeStats();
  } catch (e) {
    activeTrainingKind = null;
    status.textContent = `Training failed: ${e.message || e}`;
  }
}

function initTrainControls() {
  $('#trainWordStartBtn').addEventListener('click', () => startTrainSample('word'));
  $('#trainWordFinishBtn').addEventListener('click', () => finishTrainSample('word'));
  $('#trainTriggerStartBtn').addEventListener('click', () => startTrainSample('trigger'));
  $('#trainTriggerFinishBtn').addEventListener('click', () => finishTrainSample('trigger'));
  $('#clearTrainingBtn').addEventListener('click', async () => {
    await window.afk.call('clear_adaptation', {});
    $('#trainWordStatus').textContent = 'Training memory cleared.';
    $('#trainTriggerStatus').textContent = 'Training memory cleared.';
    refreshTrain();
    refreshHomeStats();
  });
  $('#trainingList').addEventListener('click', async (event) => {
    const btn = event.target.closest('[data-delete-training]');
    if (!btn) return;
    await window.afk.call('delete_training_sample', { id: btn.dataset.deleteTraining });
    refreshTrain();
    refreshHomeStats();
  });
}

// ---------- Settings ----------
function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme === 'light' ? 'light' : 'dark');
}

function settingRow(name, desc, controlHtml) {
  return `<div class="setting-row"><div class="setting-text"><span class="setting-name">${escapeHtml(name)}</span>` +
    `<span class="setting-desc">${escapeHtml(desc)}</span></div><div class="setting-control">${controlHtml}</div></div>`;
}

function settingsGroup(title, rows) {
  return `<section class="settings-group"><h2 class="settings-group-title">${escapeHtml(title)}</h2>${rows.join('')}</section>`;
}

function toggleHtml(id, checked) {
  return `<label class="switch" aria-label="${escapeHtml(id)}"><input type="checkbox" id="${id}" ${checked ? 'checked' : ''}><span class="slider"></span></label>`;
}

function optionsHtml(options, selected) {
  const all = options.includes(selected) || !selected ? options : [selected].concat(options);
  return all.map((value) =>
    `<option value="${escapeHtml(value)}" ${value === selected ? 'selected' : ''}>${escapeHtml(value)}</option>`
  ).join('');
}

async function refreshSettings() {
  const list = $('#settingsList');
  try {
    const cfg = await window.afk.call('get_settings', {});
    _settingsCache = cfg;
    const mics = (await window.afk.call('list_microphones', {})).devices || [];
    const micOpts = ['<option value="">System default</option>']
      .concat(mics.map((d) =>
        `<option value="${escapeHtml(d.name)}" ${cfg.microphone === d.name ? 'selected' : ''}>${escapeHtml(d.name)}</option>`
      ))
      .join('');
    const hk = cfg.hotkeys || {};
    const defaults = defaultHotkeys();
    const options = hotkeyOptions();

    list.innerHTML =
      settingsGroup('Audio and startup', [
        settingRow('Microphone', 'Input device', `<select id="set-microphone">${micOpts}</select>`),
        settingRow('Start on login', 'Keep AFK ready after sign-in', toggleHtml('set-startup_on_login', cfg.startup_on_login)),
        settingRow('Launch minimized', 'Start in the menu bar', toggleHtml('set-launch_minimized', cfg.launch_minimized)),
        settingRow('Theme', 'Application appearance', `<select id="set-theme"><option value="dark" ${cfg.theme !== 'light' ? 'selected' : ''}>Dark</option><option value="light" ${cfg.theme === 'light' ? 'selected' : ''}>Light</option></select>`)
      ]) +
      settingsGroup('Dictation', [
        settingRow('Auto-paste', 'Insert into the focused text field', toggleHtml('set-auto_paste', cfg.auto_paste)),
        settingRow('Auto-clarify', 'Polish grammar before insertion', toggleHtml('set-auto_clarify', cfg.auto_clarify)),
        settingRow('Capitalization', 'Capitalize transcripts', toggleHtml('set-auto_capitalization', cfg.auto_capitalization !== false)),
        settingRow('Punctuation', 'Keep recognized punctuation', toggleHtml('set-auto_punctuation', cfg.auto_punctuation !== false)),
        settingRow('Training corrections', 'Apply personal vocabulary', toggleHtml('set-training_corrections', cfg.training_corrections !== false)),
        settingRow('Code language', 'Formatting target for code mode', `<select id="set-code_language">${codeLanguageOptions(cfg.code_language || 'auto')}</select>`),
        settingRow('Cleanup length', 'Words before extended cleanup', `<input type="number" id="set-word_count_threshold" min="1" max="500" value="${escapeHtml(cfg.word_count_threshold)}">`)
      ]) +
      settingsGroup('Shortcuts', [
        settingRow('Shortcut listener', 'Native macOS keyboard listener', '<button class="btn btn-quiet" id="settingsRestartHotkeysBtn">Restart shortcuts</button>'),
        settingRow('Push to talk', 'Hold to record', `<select id="hk-push_to_talk">${optionsHtml(options, hk.push_to_talk || defaults.push_to_talk)}</select>`),
        settingRow('Toggle dictation', 'Start or stop', `<select id="hk-toggle">${optionsHtml(options, hk.toggle || defaults.toggle)}</select>`),
        settingRow('Code hold', 'Hold for code mode', `<select id="hk-code_push_to_talk">${optionsHtml(options, hk.code_push_to_talk || defaults.code_push_to_talk)}</select>`),
        settingRow('Code toggle', 'Start or stop code mode', `<select id="hk-code_toggle">${optionsHtml(options, hk.code_toggle || defaults.code_toggle)}</select>`),
        settingRow('Clarify', 'Polish selected text', `<select id="hk-clarify">${optionsHtml(options, hk.clarify || defaults.clarify)}</select>`),
        settingRow('Learn correction', 'Capture corrected selection', `<select id="hk-learn_correction">${optionsHtml(options, hk.learn_correction || defaults.learn_correction)}</select>`)
      ]) +
      settingsGroup('Diagnostics', [
        settingRow('Logging', 'Write local diagnostics', toggleHtml('set-logging', cfg.logging)),
        settingRow('Developer mode', 'Show extended diagnostics', toggleHtml('set-developer_mode', cfg.developer_mode)),
        '<div class="settings-actions"><button class="btn btn-quiet" id="resetStatsBtn">Reset statistics</button></div>'
      ]);

    wireSettingControls();
    enhanceSelects(list);
  } catch (e) {
    list.innerHTML = '<div class="empty-hint">Settings are not ready yet.</div>';
  }
}

function wireSettingControls() {
  const patchToggle = (id, key) => {
    const el = $('#' + id);
    if (el) el.addEventListener('change', () => saveSetting(key, el.checked));
  };

  patchToggle('set-startup_on_login', 'startup_on_login');
  patchToggle('set-launch_minimized', 'launch_minimized');
  patchToggle('set-auto_paste', 'auto_paste');
  patchToggle('set-auto_clarify', 'auto_clarify');
  patchToggle('set-auto_capitalization', 'auto_capitalization');
  patchToggle('set-auto_punctuation', 'auto_punctuation');
  patchToggle('set-training_corrections', 'training_corrections');
  patchToggle('set-logging', 'logging');
  patchToggle('set-developer_mode', 'developer_mode');

  $('#set-microphone').addEventListener('change', (e) => saveSetting('microphone', e.target.value || null));
  $('#set-theme').addEventListener('change', (e) => {
    applyTheme(e.target.value);
    saveSetting('theme', e.target.value);
  });
  $('#set-code_language').addEventListener('change', (e) => saveSetting('code_language', e.target.value || 'auto'));
  $('#settingsRestartHotkeysBtn').addEventListener('click', restartHotkeys);

  const threshold = $('#set-word_count_threshold');
  threshold.addEventListener('change', () => {
    saveSetting('word_count_threshold', parseInt(threshold.value, 10) || 100);
  });

  ['push_to_talk', 'toggle', 'code_push_to_talk', 'code_toggle', 'clarify', 'learn_correction'].forEach((k) => {
    const el = $('#hk-' + k);
    el.addEventListener('change', saveHotkeys);
  });

  $('#resetStatsBtn').addEventListener('click', async () => {
    await window.afk.call('reset_statistics', {});
    refreshStatistics();
  });
}

async function saveSetting(key, value) {
  try {
    const updated = await window.afk.call('update_settings', { patch: { [key]: value } });
    _settingsCache = updated;
    refreshHotkeys();
  } catch (e) {
    // Settings writes are best-effort during backend startup.
  }
}

async function saveHotkeys() {
  const hotkeys = {
    push_to_talk: $('#hk-push_to_talk').value.trim(),
    toggle: $('#hk-toggle').value.trim(),
    code_push_to_talk: $('#hk-code_push_to_talk').value.trim(),
    code_toggle: $('#hk-code_toggle').value.trim(),
    clarify: $('#hk-clarify').value.trim(),
    learn_correction: $('#hk-learn_correction').value.trim()
  };
  try {
    const updated = await window.afk.call('set_hotkeys', { hotkeys });
    _settingsCache = { ...(_settingsCache || {}), hotkeys: updated };
    refreshHotkeys();
  } catch (e) {
    setText('#clarifyMeta', 'Hotkey save failed');
  }
}

// ---------- Backend events ----------
function initEvents() {
  window.afk.onBackendStatus(({ ready }) => {
    setBackendStatus(ready);
    if (ready) {
      refreshBackendInfo();
      refreshHotkeys();
      refreshMicrophones();
      refreshAsrStatus();
      refreshClarifyStatus();
    }
  });

  window.afk.onBackendEvent(({ event, data }) => {
    switch (event) {
      case 'recording_started':
        setRecording(true);
        break;
      case 'recording_stopped':
        setRecording(false);
        setText('#recordStatus', 'Transcribing...');
        break;
      case 'transcription':
        showTranscription(data && data.text, data && data.message);
        setText('#recordStatus', 'Idle');
        refreshAsrStatus();
        refreshHomeStats();
        refreshHistory();
        break;
      case 'clarify_done':
        if (data && data.text) {
          setText('#clarifyOutput', data.text);
          setText('#clarifyMeta', data.latency_ms ? `${data.latency_ms} ms` : '');
        }
        refreshClarifyStatus();
        break;
      case 'statistics_updated':
        if ($('#page-statistics').classList.contains('active')) refreshStatistics();
        refreshHomeStats();
        break;
      case 'correction_learned':
        setText('#recordStatus', data && data.ok ? 'Learned correction' : 'Learning skipped');
        refreshTrain();
        refreshHomeStats();
        break;
      case 'training_sample_saved':
        if ($('#page-train').classList.contains('active')) refreshTrain();
        refreshHomeStats();
        break;
      case 'adaptation_updated':
        if ($('#page-train').classList.contains('active')) refreshTrain();
        refreshHomeStats();
        break;
      case 'history_updated':
        refreshHistory();
        break;
      default:
        break;
    }
  });
}

function setRecording(on) {
  const orb = $('#recordOrb');
  const status = $('#recordStatus');
  const btn = $('#recordBtn');
  isRecording = on;
  if (orb) orb.classList.toggle('recording', on);
  const deck = $('#commandDeck');
  if (deck) deck.classList.toggle('recording', on);
  if (status) status.textContent = on ? 'Listening' : 'Ready';
  if (btn) {
    btn.classList.toggle('recording', on);
    setRecordButtonLabel(on ? 'Stop and transcribe' : 'Start recording');
    btn.disabled = false;
  }
  if (!on && activeTrainingKind) {
    const status = activeTrainingKind === 'trigger' ? $('#trainTriggerStatus') : $('#trainWordStatus');
    if (status) status.textContent = 'Recording stopped. Finish to save this sample.';
  }

  if (on) {
    recStart = Date.now();
    clearInterval(recTimer);
    recTimer = setInterval(() => {
      const s = Math.floor((Date.now() - recStart) / 1000);
      const mm = String(Math.floor(s / 60)).padStart(2, '0');
      const ss = String(s % 60).padStart(2, '0');
      setText('#recordTimer', `${mm}:${ss}`);
    }, 250);
  } else {
    clearInterval(recTimer);
    recTimer = null;
  }
}

function setRecordButtonLabel(label) {
  const btn = $('#recordBtn');
  if (!btn) return;
  const icon = label === 'Stop and transcribe' ? '&#9632;' : '&#9679;';
  btn.innerHTML = `<span class="record-button-icon" aria-hidden="true">${icon}</span><span>${escapeHtml(label)}</span>`;
}

async function copyTranscript(text) {
  if (!text) return;
  try {
    await window.afk.call('set_clipboard', { text });
    setText('#recordStatus', 'Copied');
  } catch (e) {
    setText('#recordStatus', 'Idle');
  }
}

function showTranscription(text, message) {
  const el = $('#transcription');
  if (!el) return;
  if (text && message) el.textContent = `${text}\n\n${message}`;
  else if (text) el.textContent = text;
  else if (message) el.textContent = message;
}

// ---------- Boot ----------
window.addEventListener('DOMContentLoaded', async () => {
  initNav();
  initEvents();
  initEditableHotkeyHandling();
  initTrainControls();
  enhanceSelects();
  if ($('#recordBtn')) $('#recordBtn').addEventListener('click', toggleRecord);
  if ($('#restartHotkeysBtn')) $('#restartHotkeysBtn').addEventListener('click', restartHotkeys);
  if ($('#restartHotkeysInlineBtn')) $('#restartHotkeysInlineBtn').addEventListener('click', restartHotkeys);
  if ($('#quitBtn')) $('#quitBtn').addEventListener('click', () => window.afk.app.quit());
  if ($('#copyTranscriptBtn')) {
    $('#copyTranscriptBtn').addEventListener('click', () => {
      const transcript = $('#transcription');
      const text = transcript && !transcript.querySelector('.placeholder') ? transcript.textContent.trim() : '';
      copyTranscript(text);
    });
  }
  if ($('#loadAsrBtn')) $('#loadAsrBtn').addEventListener('click', loadAsrModel);
  if ($('#micSelect')) $('#micSelect').addEventListener('change', onMicChange);
  if ($('#clarifyBtn')) $('#clarifyBtn').addEventListener('click', clarifyText);
  if ($('#historyList')) {
    $('#historyList').addEventListener('click', async (event) => {
      const copy = event.target.closest('[data-copy-history]');
      const del = event.target.closest('[data-delete-history]');
      if (copy) await copyHistoryItem(copy.dataset.copyHistory);
      if (del) await deleteHistoryItem(del.dataset.deleteHistory);
    });
  }
  if ($('#clearHistoryBtn')) {
    $('#clearHistoryBtn').addEventListener('click', async () => {
      await window.afk.call('clear_transcription_history', {});
      refreshHistory();
    });
  }

  await initAbout();
  await applySavedTheme();
  await refreshBackendInfo();
  await refreshHotkeys();
  await refreshMicrophones();
  await refreshAsrStatus();
  await refreshClarifyStatus();
  await refreshHomeStats();
  await refreshHistory();
  await refreshTrain();

  setTimeout(() => {
    refreshBackendInfo();
    refreshAsrStatus();
    refreshClarifyStatus();
  }, 1500);

  const poll = setInterval(() => {
    refreshAsrStatus();
    refreshClarifyStatus();
  }, 2500);
  setTimeout(() => clearInterval(poll), 90000);
});
