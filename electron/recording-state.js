'use strict';

class RecordingState {
  constructor() {
    this.reset();
  }

  begin(mode = 'dictation') {
    if (this.active || this.starting || this.finishing) return false;
    this.mode = mode;
    this.starting = true;
    this.stopQueued = false;
    return true;
  }

  markStarted() {
    this.starting = false;
    this.active = true;
    const shouldFinish = this.stopQueued;
    this.stopQueued = false;
    return shouldFinish;
  }

  markStartFailed() {
    this.starting = false;
    this.stopQueued = false;
    if (!this.active) this.mode = 'dictation';
  }

  requestFinish() {
    if (this.starting) {
      this.stopQueued = true;
      return 'queued';
    }
    if (!this.active || this.finishing) return 'ignored';
    this.finishing = true;
    return 'finish';
  }

  markRecordingStopped() {
    this.starting = false;
    this.active = false;
  }

  markFinished() {
    this.reset();
  }

  reset() {
    this.active = false;
    this.starting = false;
    this.finishing = false;
    this.stopQueued = false;
    this.mode = 'dictation';
  }

  get busy() {
    return this.active || this.starting || this.finishing;
  }
}

module.exports = { RecordingState };
