'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const { RecordingState } = require('../../electron/recording-state');

test('queues a push-to-talk release while recording is starting', () => {
  const state = new RecordingState();

  assert.equal(state.begin('dictation'), true);
  assert.equal(state.requestFinish(), 'queued');
  assert.equal(state.markStarted(), true);
  assert.equal(state.active, true);
  assert.equal(state.requestFinish(), 'finish');
});

test('blocks overlapping starts and resets after completion', () => {
  const state = new RecordingState();

  assert.equal(state.begin('code'), true);
  assert.equal(state.begin('dictation'), false);
  assert.equal(state.markStarted(), false);
  assert.equal(state.mode, 'code');
  assert.equal(state.requestFinish(), 'finish');
  assert.equal(state.begin('dictation'), false);

  state.markFinished();
  assert.equal(state.begin('dictation'), true);
  assert.equal(state.mode, 'dictation');
});

test('failed starts do not leave a queued release behind', () => {
  const state = new RecordingState();

  state.begin('dictation');
  state.requestFinish();
  state.markStartFailed();

  assert.equal(state.busy, false);
  assert.equal(state.stopQueued, false);
  assert.equal(state.mode, 'dictation');
});
