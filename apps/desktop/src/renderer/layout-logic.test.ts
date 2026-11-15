import { describe, expect, it } from 'vitest';

import { DEFAULT_LAYOUT, LAYOUT_LIMITS } from '../shared/bridge';
import {
  KEY_STEP_LARGE_PX,
  KEY_STEP_PX,
  PLAYER_MIN_NARROW_PX,
  PLAYER_MIN_WIDE_PX,
  SPLITTER_PX,
  WIDE_MIN_PX,
  WIDE_QUERY,
  clampWidth,
  dragWidth,
  keyboardWidth,
  limitsOf,
  maxWidthFor,
  widthOf,
  withWidth,
} from './layout-logic';

describe('clampWidth', () => {
  it('keeps a width that is inside the limits, as a whole number', () => {
    expect(clampWidth('list', 300)).toBe(300);
    expect(clampWidth('details', 300.6)).toBe(301);
  });

  it.each([
    ['list', 10, 240],
    ['list', 9999, 480],
    ['details', 0, 280],
    ['details', -50, 280],
    ['details', Number.POSITIVE_INFINITY, 480],
    ['list', Number.NEGATIVE_INFINITY, 240],
  ] as const)('puts %s %s inside its limits (%s)', (panel, px, expected) => {
    expect(clampWidth(panel, px)).toBe(expected);
  });

  it('treats a width that is not a number as the smallest', () => {
    expect(clampWidth('list', Number.NaN)).toBe(240);
    expect(clampWidth('details', Number.NaN)).toBe(280);
  });

  it('stops at the room that is left, but never goes under the lower limit', () => {
    expect(clampWidth('list', 400, 350.9)).toBe(350);
    expect(clampWidth('list', 400, 100)).toBe(240);
    expect(clampWidth('details', 300, Number.NaN)).toBe(300);
  });
});

describe('maxWidthFor', () => {
  it('is only the limit of the panel while the row has not been measured', () => {
    expect(maxWidthFor('list', DEFAULT_LAYOUT, 0, true)).toBe(480);
    expect(maxWidthFor('details', DEFAULT_LAYOUT, 0, false)).toBe(480);
  });

  it('leaves the player its 520 px in three columns', () => {
    // 1184: 1440 minus the 208 px sidebar and the 2 x 24 px padding of the page.
    const room = 1184 - DEFAULT_LAYOUT.details_px - 2 * SPLITTER_PX - PLAYER_MIN_WIDE_PX;

    expect(maxWidthFor('list', DEFAULT_LAYOUT, 1184, true)).toBe(room);
    expect(room).toBe(320);
    expect(1184 - room - DEFAULT_LAYOUT.details_px - 2 * SPLITTER_PX).toBe(PLAYER_MIN_WIDE_PX);
  });

  it('counts the other panel at its stored width, and the right panel the same way', () => {
    const layout = { list_px: 260, details_px: 300 };

    expect(maxWidthFor('list', layout, 1184, true)).toBe(1184 - 300 - 2 * SPLITTER_PX - PLAYER_MIN_WIDE_PX);
    expect(maxWidthFor('details', layout, 1184, true)).toBe(1184 - 260 - 2 * SPLITTER_PX - PLAYER_MIN_WIDE_PX);
  });

  it('knows only the list, one splitter and a 400 px player below 1440 px', () => {
    expect(maxWidthFor('list', DEFAULT_LAYOUT, 844, false)).toBe(844 - SPLITTER_PX - PLAYER_MIN_NARROW_PX);
    expect(maxWidthFor('list', DEFAULT_LAYOUT, 1200, false)).toBe(480);
  });

  it('never goes under the lower limit of the panel, however small the row', () => {
    expect(maxWidthFor('list', DEFAULT_LAYOUT, 500, true)).toBe(240);
    expect(maxWidthFor('details', DEFAULT_LAYOUT, 500, false)).toBe(280);
  });
});

describe('keyboardWidth', () => {
  it('grows the list to the right and the right panel to the left', () => {
    expect(keyboardWidth('list', 'ArrowRight', false, 320, 480)).toBe(320 + KEY_STEP_PX);
    expect(keyboardWidth('list', 'ArrowLeft', false, 320, 480)).toBe(320 - KEY_STEP_PX);
    expect(keyboardWidth('details', 'ArrowLeft', false, 320, 480)).toBe(320 + KEY_STEP_PX);
    expect(keyboardWidth('details', 'ArrowRight', false, 320, 480)).toBe(320 - KEY_STEP_PX);
  });

  it('takes a bigger step with Shift', () => {
    expect(keyboardWidth('list', 'ArrowRight', true, 320, 480)).toBe(320 + KEY_STEP_LARGE_PX);
    expect(keyboardWidth('details', 'ArrowRight', true, 400, 480)).toBe(400 - KEY_STEP_LARGE_PX);
  });

  it('goes to the ends with Home and End, End stopping at the room that is left', () => {
    expect(keyboardWidth('list', 'Home', false, 320, 480)).toBe(LAYOUT_LIMITS.list_px.min);
    expect(keyboardWidth('details', 'Home', false, 320, 480)).toBe(LAYOUT_LIMITS.details_px.min);
    expect(keyboardWidth('list', 'End', false, 320, 480)).toBe(LAYOUT_LIMITS.list_px.max);
    expect(keyboardWidth('list', 'End', false, 320, 350)).toBe(350);
  });

  it('is clamped to the limits and to the room that is left', () => {
    expect(keyboardWidth('list', 'ArrowLeft', false, 245, 480)).toBe(240);
    expect(keyboardWidth('list', 'ArrowRight', false, 475, 480)).toBe(480);
    expect(keyboardWidth('list', 'ArrowRight', false, 340, 345)).toBe(345);
    expect(keyboardWidth('details', 'ArrowRight', true, 290, 480)).toBe(280);
  });

  it.each(['a', 'Enter', 'ArrowUp', 'ArrowDown', ' ', 'Tab', 'PageUp', 'Escape'])(
    'ignores %j',
    (key) => {
      expect(keyboardWidth('list', key, false, 320, 480)).toBeNull();
      expect(keyboardWidth('details', key, false, 320, 480)).toBeNull();
    },
  );
});

describe('dragWidth', () => {
  it('moves the list with the pointer and the right panel against it', () => {
    expect(dragWidth('list', 320, 400, 450, 480)).toBe(370);
    expect(dragWidth('list', 320, 400, 380, 480)).toBe(300);
    expect(dragWidth('details', 320, 900, 850, 480)).toBe(370);
    expect(dragWidth('details', 320, 900, 950, 480)).toBe(280);
  });

  it('is clamped to the limits and to the room that is left', () => {
    expect(dragWidth('list', 320, 400, 4000, 480)).toBe(480);
    expect(dragWidth('list', 320, 400, 4000, 400)).toBe(400);
    expect(dragWidth('list', 320, 400, -4000, 480)).toBe(240);
    expect(dragWidth('details', 320, 900, -4000, 480)).toBe(480);
  });

  it('stays where it was when the pointer did not move', () => {
    expect(dragWidth('list', 333, 400, 400, 480)).toBe(333);
  });
});

describe('the rest', () => {
  it('reads and replaces one width without touching the other', () => {
    expect(widthOf({ list_px: 250, details_px: 350 }, 'list')).toBe(250);
    expect(widthOf({ list_px: 250, details_px: 350 }, 'details')).toBe(350);
    expect(withWidth(DEFAULT_LAYOUT, 'list', 260)).toEqual({ list_px: 260, details_px: 320 });
    expect(withWidth(DEFAULT_LAYOUT, 'details', 290)).toEqual({ list_px: 320, details_px: 290 });
    expect(DEFAULT_LAYOUT).toEqual({ list_px: 320, details_px: 320 });
  });

  it('takes the limits from the shared schema', () => {
    expect(limitsOf('list')).toBe(LAYOUT_LIMITS.list_px);
    expect(limitsOf('details')).toBe(LAYOUT_LIMITS.details_px);
  });

  it('switches to three columns at 1440 px', () => {
    expect(WIDE_MIN_PX).toBe(1440);
    expect(WIDE_QUERY).toBe('(min-width: 1440px)');
  });
});
