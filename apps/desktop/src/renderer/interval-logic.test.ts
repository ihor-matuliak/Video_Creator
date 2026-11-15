import { describe, expect, it } from 'vitest';

import vectors from '../../../../contracts/time-vectors.json';
import { formatTimecode } from '../shared/timecode';
import { checkInterval, shiftTimecode } from './interval-logic';

const DURATION = 12_000;

describe('checkInterval', () => {
  it('accepts the contract interval 4120-7880 and returns milliseconds', () => {
    expect(checkInterval('00:00:04.120', '00:00:07.880', DURATION)).toEqual({
      ok: true,
      startMs: 4120,
      endMs: 7880,
    });
  });

  it('accepts the whole source, [0, duration)', () => {
    expect(checkInterval('00:00:00.000', '00:00:12.000', DURATION)).toMatchObject({ ok: true });
  });

  it('agrees with the contract vectors for the 12 s source', () => {
    const inRange = vectors.intervals.filter((entry) => entry.start_ms >= 0);
    expect(inRange.length).toBeGreaterThan(0);
    for (const item of inRange) {
      const result = checkInterval(
        formatTimecode(item.start_ms),
        formatTimecode(item.end_ms),
        item.duration_ms,
      );
      expect(result.ok).toBe(item.valid);
    }
  });

  it('refuses an end that is not after the start', () => {
    for (const [start, end] of [
      ['00:00:08.000', '00:00:04.000'],
      ['00:00:02.000', '00:00:02.000'],
    ] as const) {
      expect(checkInterval(start, end, DURATION)).toEqual({
        ok: false,
        message: 'Кінець має бути пізніше за початок',
      });
    }
  });

  it('refuses an end past the source duration and says where the source ends', () => {
    expect(checkInterval('00:00:01.000', '00:00:12.001', DURATION)).toEqual({
      ok: false,
      message: 'Фрагмент має вміщатися у відео: від 00:00:00.000 до 00:00:12.000',
    });
  });

  it('names the field that is not a time', () => {
    expect(checkInterval('', '00:00:04.000', DURATION)).toMatchObject({ ok: false });
    const start = checkInterval('abc', '00:00:04.000', DURATION);
    const end = checkInterval('00:00:01.000', '4', DURATION);
    expect(!start.ok && start.message).toContain('Початок');
    expect(!end.ok && end.message).toContain('Кінець');
  });
});

describe('shiftTimecode', () => {
  it('moves the time by the step', () => {
    expect(shiftTimecode('00:00:04.120', 1000, DURATION)).toBe('00:00:05.120');
    expect(shiftTimecode('00:00:04.120', -1000, DURATION)).toBe('00:00:03.120');
  });

  it('stays inside [0, duration]', () => {
    expect(shiftTimecode('00:00:00.500', -1000, DURATION)).toBe('00:00:00.000');
    expect(shiftTimecode('00:00:11.500', 1000, DURATION)).toBe('00:00:12.000');
  });

  it('has nothing to move while the text is not a time', () => {
    expect(shiftTimecode('', 1000, DURATION)).toBeNull();
    expect(shiftTimecode('00:00', 1000, DURATION)).toBeNull();
  });
});
