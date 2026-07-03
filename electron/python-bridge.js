'use strict';

/**
 * PythonBridge
 * -------------
 * Manages the lifecycle of the AFK Python backend and provides a small
 * JSON-RPC layer over its stdin/stdout.
 *
 * Protocol (newline-delimited JSON):
 *   Request  (Electron -> Python): { "id": <number>, "method": <string>, "params": <object> }
 *   Response (Python -> Electron): { "id": <number>, "result": <any> } | { "id": <number>, "error": { code, message } }
 *   Event    (Python -> Electron): { "event": <string>, "data": <any> }   // no id
 *
 * stderr from Python is treated as structured log output and forwarded.
 */

const { execFileSync, spawn } = require('child_process');
const { EventEmitter } = require('events');
const path = require('path');
const fs = require('fs');
const readline = require('readline');

const { resolvePython, backendEntry } = require('./python-locator');

const RPC_TIMEOUT_MS = 30000;

class PythonBridge extends EventEmitter {
  constructor(options = {}) {
    super();
    this.options = options;
    this.proc = null;
    this.rl = null;
    this._nextId = 1;
    this._pending = new Map();
    this._ready = false;
    this._restarting = false;
    this._stopped = false;
  }

  get isReady() {
    return this._ready;
  }

  start() {
    if (this.proc) return;
    this._stopped = false;

    const { command, args } = resolvePython();
    const entry = backendEntry();
    this._stopStaleBackends(entry);
    const fullArgs = [...args, entry];

    this.emit('log', { level: 'info', msg: `Starting backend: ${command} ${fullArgs.join(' ')}` });

    this.proc = spawn(command, fullArgs, {
      cwd: path.dirname(entry),
      windowsHide: true, // never show a console window
      stdio: ['pipe', 'pipe', 'pipe'],
      env: {
        ...process.env,
        AFK_DATA_DIR: this.options.dataDir || '',
        AFK_MODELS_DIR: this.options.modelsDir || '',
        AFK_RESOURCES: this.options.resourcesPath || '',
        AFK_HOTKEY_RUNTIME: process.platform === 'darwin' ? 'electron' : '',
        PYTHONUNBUFFERED: '1',
        PYTHONIOENCODING: 'utf-8',
        PYTHONDONTWRITEBYTECODE: '1'
      }
    });

    this.proc.on('error', (err) => {
      this.emit('log', { level: 'error', msg: `Failed to start backend: ${err.message}` });
      this.emit('backend-error', err);
    });

    this.proc.on('exit', (code, signal) => {
      this._ready = false;
      this.emit('log', { level: 'warn', msg: `Backend exited (code=${code}, signal=${signal})` });
      this._rejectAllPending(new Error('Backend process exited'));
      this.rl = null;
      this.proc = null;
      this.emit('exit', { code, signal });
      if (!this._stopped && !this._restarting) {
        this._restarting = true;
        setTimeout(() => {
          this._restarting = false;
          if (!this._stopped) this.start();
        }, 1000);
      }
    });

    this.rl = readline.createInterface({ input: this.proc.stdout });
    this.rl.on('line', (line) => this._onLine(line));

    // stderr -> logs
    const errRl = readline.createInterface({ input: this.proc.stderr });
    errRl.on('line', (line) => {
      if (!line.trim()) return;
      this.emit('log', { level: 'debug', msg: `[py] ${line}` });
    });
  }

  _onLine(line) {
    const trimmed = line.trim();
    if (!trimmed) return;
    let msg;
    try {
      msg = JSON.parse(trimmed);
    } catch (e) {
      this.emit('log', { level: 'debug', msg: `[py:raw] ${trimmed}` });
      return;
    }

    if (msg.event) {
      if (msg.event === 'ready') {
        this._ready = true;
        this.emit('ready', msg.data || {});
      }
      this.emit('event', msg.event, msg.data);
      this.emit(`event:${msg.event}`, msg.data);
      return;
    }

    if (typeof msg.id !== 'undefined' && this._pending.has(msg.id)) {
      const { resolve, reject, timer } = this._pending.get(msg.id);
      clearTimeout(timer);
      this._pending.delete(msg.id);
      if (msg.error) {
        const err = new Error(msg.error.message || 'RPC error');
        err.code = msg.error.code;
        reject(err);
      } else {
        resolve(msg.result);
      }
    }
  }

  /**
   * Call a backend method and await its result.
   * @param {string} method
   * @param {object} params
   * @param {number} [timeoutMs]
   */
  call(method, params = {}, timeoutMs = RPC_TIMEOUT_MS) {
    return new Promise((resolve, reject) => {
      if (!this.proc || !this.proc.stdin.writable) {
        reject(new Error('Backend not running'));
        return;
      }
      const id = this._nextId++;
      const timer = setTimeout(() => {
        this._pending.delete(id);
        reject(new Error(`RPC timeout: ${method}`));
      }, timeoutMs);
      this._pending.set(id, { resolve, reject, timer });
      const payload = JSON.stringify({ id, method, params }) + '\n';
      this.proc.stdin.write(payload, 'utf-8');
    });
  }

  _rejectAllPending(err) {
    for (const { reject, timer } of this._pending.values()) {
      clearTimeout(timer);
      reject(err);
    }
    this._pending.clear();
  }

  stop() {
    this._stopped = true;
    if (!this.proc) return;
    try {
      this.call('shutdown', {}, 2000).catch(() => {});
    } catch (_) { /* ignore */ }
    const proc = this.proc;
    setTimeout(() => {
      if (proc && !proc.killed) proc.kill();
    }, 1500);
  }

  _stopStaleBackends(entry) {
    if (process.platform === 'win32') return;
    try {
      const output = execFileSync('pgrep', ['-f', entry], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] });
      for (const rawPid of output.split(/\s+/).filter(Boolean)) {
        const pid = Number(rawPid);
        if (!pid || pid === process.pid) continue;
        try {
          process.kill(pid, 'SIGTERM');
          this.emit('log', { level: 'warn', msg: `Stopped stale backend process ${pid}` });
        } catch (_) {
          // Process may already be gone.
        }
      }
    } catch (_) {
      // No stale backend was found.
    }
  }
}

module.exports = { PythonBridge };
