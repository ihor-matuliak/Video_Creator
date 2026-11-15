import { describe, expect, it } from 'vitest';

import { SAVE_STATUS_VIEW, summarizeSaveStates } from './save-status';
import type { SaveState } from './use-clip-editing';

const SAVING: SaveState = { kind: 'saving' };
const SAVED: SaveState = { kind: 'saved' };
const ERROR: SaveState = { kind: 'error', message: 'Не вдалося' };
const CONFLICT: SaveState = { kind: 'conflict', refreshed: true };

describe('summarizeSaveStates', () => {
  it('is «saved» when nothing was edited or everything was stored', () => {
    expect(summarizeSaveStates([])).toBe('saved');
    expect(summarizeSaveStates([SAVED, SAVED])).toBe('saved');
  });

  it('lets the worst state of any clip win: conflict, then error, then saving', () => {
    expect(summarizeSaveStates([SAVING])).toBe('saving');
    expect(summarizeSaveStates([SAVED, SAVING])).toBe('saving');
    expect(summarizeSaveStates([SAVING, ERROR, SAVED])).toBe('error');
    expect(summarizeSaveStates([ERROR, SAVING, CONFLICT])).toBe('conflict');
    expect(summarizeSaveStates([CONFLICT])).toBe('conflict');
  });
});

describe('SAVE_STATUS_VIEW', () => {
  it('gives every status its own text and tone, so that it never rests on colour alone', () => {
    const views = Object.values(SAVE_STATUS_VIEW);
    expect(new Set(views.map((view) => view.label)).size).toBe(views.length);
    expect(new Set(views.map((view) => view.tone)).size).toBe(views.length);
  });
});
