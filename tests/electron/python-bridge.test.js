'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');

const { parsePythonLogLine } = require('../../electron/python-bridge');

test('forwards Python log severity into the private Electron log', () => {
  assert.deepEqual(parsePythonLogLine('09:21:22 [INFO] Recording captured'), {
    level: 'info',
    msg: '[py] Recording captured'
  });
  assert.deepEqual(parsePythonLogLine('09:21:23 [WARN] Audio callback overflow'), {
    level: 'warn',
    msg: '[py] Audio callback overflow'
  });
});

test('keeps unstructured Python stderr at debug level', () => {
  assert.deepEqual(parsePythonLogLine('native library note'), {
    level: 'debug',
    msg: '[py] native library note'
  });
});
