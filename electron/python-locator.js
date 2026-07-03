'use strict';

/**
 * Locates the Python interpreter and backend entry point in both
 * development and packaged (production) environments.
 *
 * Dev:
 *   - Prefer python/.venv (created by `npm run setup:python`)
 *   - Fall back to a system Python 3.11/3.12 interpreter
 *   Backend entry: <repo>/python/main.py
 *
 * Production (packaged):
 *   - Bundled interpreter at resources/python/runtime
 *   Backend entry: resources/python/main.py
 */

const path = require('path');
const fs = require('fs');
const { spawnSync } = require('child_process');

function isPackaged() {
  // app.isPackaged is the source of truth, but this module is also used by
  // tests without electron loaded. Detect via resourcesPath presence.
  try {
    const { app } = require('electron');
    return app.isPackaged;
  } catch (_) {
    return false;
  }
}

function pythonRoot() {
  if (isPackaged()) {
    return path.join(process.resourcesPath, 'python');
  }
  return path.join(__dirname, '..', 'python');
}

function backendEntry() {
  return path.join(pythonRoot(), 'main.py');
}

function exists(p) {
  try {
    return fs.existsSync(p);
  } catch (_) {
    return false;
  }
}

function commandWorks(command, args) {
  try {
    const res = spawnSync(command, [...args, '--version'], {
      encoding: 'utf-8',
      windowsHide: true
    });
    return res.status === 0;
  } catch (_) {
    return false;
  }
}

function findSystemPython() {
  const candidates = process.platform === 'win32'
    ? [['py', ['-3.11']], ['py', ['-3.12']], ['python', []]]
    : [['python3.11', []], ['python3.12', []], ['python3', []]];

  for (const [command, args] of candidates) {
    if (commandWorks(command, args)) return { command, args };
  }
  return process.platform === 'win32'
    ? { command: 'py', args: ['-3.11'] }
    : { command: 'python3', args: [] };
}

/**
 * Resolve the python command + leading args.
 * @returns {{ command: string, args: string[] }}
 */
function resolvePython() {
  const root = pythonRoot();

  // Production installers must use the embedded runtime. A venv created on a
  // developer machine is not portable on Windows because pyvenv.cfg contains
  // absolute base-interpreter paths.
  const runtimeWin = path.join(root, 'runtime', 'python.exe');
  const runtimeNix = path.join(root, 'runtime', 'bin', 'python');
  if (isPackaged()) {
    if (exists(runtimeWin)) return { command: runtimeWin, args: [] };
    if (exists(runtimeNix)) return { command: runtimeNix, args: [] };
    return findSystemPython();
  }

  // 1. Development virtual environment.
  const venvPythonWin = path.join(root, '.venv', 'Scripts', 'python.exe');
  const venvPythonNix = path.join(root, '.venv', 'bin', 'python');
  if (exists(venvPythonWin)) return { command: venvPythonWin, args: [] };
  if (exists(venvPythonNix)) return { command: venvPythonNix, args: [] };

  // 2. Embedded runtime, useful for smoke-testing packaged resources locally.
  if (exists(runtimeWin)) return { command: runtimeWin, args: [] };
  if (exists(runtimeNix)) return { command: runtimeNix, args: [] };

  // 3. System Python fallback (dev machines / non-self-contained packages).
  return findSystemPython();
}

module.exports = { resolvePython, backendEntry, pythonRoot, isPackaged, findSystemPython };
