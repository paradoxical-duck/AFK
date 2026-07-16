'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');

const { MacHotkeyManager } = require('../../electron/mac-hotkeys');

class FakeHook extends EventEmitter {
  constructor() {
    super();
    this.starts = 0;
    this.stops = 0;
  }

  start() {
    this.starts += 1;
  }

  stop() {
    this.stops += 1;
  }
}

test('native listener can be stopped and re-armed after wake', () => {
  const hook = new FakeHook();
  const manager = new MacHotkeyManager({}, null, hook);
  manager.configure({ push_to_talk: 'Option', toggle: 'Option+Space' });

  manager.start();
  manager.downKeys.add(56);
  manager.edgeFiredFor.add('toggle');
  manager.pttActive = true;
  manager.stop();

  assert.equal(manager.isListening(), false);
  assert.equal(manager.downKeys.size, 0);
  assert.equal(manager.edgeFiredFor.size, 0);
  assert.equal(manager.pttActive, false);

  manager.start();
  assert.equal(manager.isListening(), true);
  assert.equal(hook.starts, 2);
  assert.equal(hook.stops, 1);
});
