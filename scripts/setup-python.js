'use strict';

/**
 * Creates the backend virtual environment and installs Python dependencies.
 * Run via:  npm run setup:python
 *
 * Strategy:
 *   1. Find a Python 3.11/3.12 interpreter.
 *   2. Create python/.venv if missing.
 *   3. Upgrade pip and install python/requirements.txt.
 *
 * Models are NOT downloaded here — that happens lazily on first use so the
 * install stays small and offline-friendly until the user actually dictates.
 */

const { spawnSync } = require('child_process');
const path = require('path');
const fs = require('fs');

const repo = path.join(__dirname, '..');
const pyDir = path.join(repo, 'python');
const venvDir = path.join(pyDir, '.venv');
const isWin = process.platform === 'win32';
const venvPython = isWin
  ? path.join(venvDir, 'Scripts', 'python.exe')
  : path.join(venvDir, 'bin', 'python');

function run(cmd, args, opts = {}) {
  console.log(`> ${cmd} ${args.join(' ')}`);
  const res = spawnSync(cmd, args, { stdio: 'inherit', ...opts });
  if (res.error) throw res.error;
  if (res.status !== 0) throw new Error(`Command failed (${res.status}): ${cmd}`);
}

function findBasePython() {
  const candidates = isWin
    ? [['py', ['-3.11']], ['py', ['-3.12']], ['python', []]]
    : [['python3.11', []], ['python3.12', []], ['python3', []]];
  for (const [cmd, pre] of candidates) {
    const res = spawnSync(cmd, [...pre, '--version'], { encoding: 'utf-8' });
    if (res.status === 0) {
      console.log(`Using base Python: ${cmd} ${pre.join(' ')} (${(res.stdout || res.stderr || '').trim()})`);
      return { cmd, pre };
    }
  }
  throw new Error('No suitable Python (3.11/3.12) found. Install Python 3.11 or 3.12.');
}

function main() {
  if (!fs.existsSync(venvPython)) {
    const { cmd, pre } = findBasePython();
    const venvArgs = [...pre, '-m', 'venv'];
    if (process.platform === 'darwin') venvArgs.push('--copies');
    run(cmd, [...venvArgs, venvDir]);
  } else {
    console.log('Virtual environment already exists.');
  }

  run(venvPython, ['-m', 'pip', 'install', '--upgrade', 'pip', 'wheel']);
  run(venvPython, ['-m', 'pip', 'install', '-r', path.join(pyDir, 'requirements.txt')]);

  console.log('\nPython backend environment ready.');
}

main();
