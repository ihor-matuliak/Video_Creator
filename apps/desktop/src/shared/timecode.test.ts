import { describe, expect, it } from 'vitest';

import vectors from '../../../../contracts/time-vectors.json';
import { formatTimecode, parseTimecode } from './timecode';

const TIMECODES = vectors.timecodes.map(([text, ms]) => {
  if (typeof text !== 'string' || typeof ms !== 'number') {
    throw new TypeError('contracts/time-vectors.json: очікувалася пара [рядок, число]');
  }
  return { text, ms };
});

describe('formatTimecode', () => {
  it('has contract vectors to check against', () => {
    expect(TIMECODES.length).toBeGreaterThan(0);
  });

  it.each(TIMECODES)('formats $ms ms as $text (contracts/time-vectors.json)', ({ text, ms }) => {
    expect(formatTimecode(ms)).toBe(text);
  });

  it('formats the 12 s fixture duration used by the UI tests', () => {
    expect(formatTimecode(vectors.intervals[0]?.duration_ms ?? -1)).toBe('00:00:12.000');
  });

  it('keeps millisecond precision at second and minute boundaries', () => {
    expect(formatTimecode(999)).toBe('00:00:00.999');
    expect(formatTimecode(1000)).toBe('00:00:01.000');
    expect(formatTimecode(59_999)).toBe('00:00:59.999');
    expect(formatTimecode(60_000)).toBe('00:01:00.000');
    expect(formatTimecode(3_599_999)).toBe('00:59:59.999');
  });

  it('widens the hours field beyond 99 hours instead of wrapping', () => {
    expect(formatTimecode(360_000_000)).toBe('100:00:00.000');
  });

  it.each([-1, 1.5, Number.NaN, Number.POSITIVE_INFINITY, Number.MAX_SAFE_INTEGER + 1])(
    'rejects %s',
    (value) => {
      expect(() => formatTimecode(value)).toThrow(RangeError);
    },
  );
});

describe('parseTimecode', () => {
  it.each(TIMECODES)('parses $text as $ms ms (contracts/time-vectors.json)', ({ text, ms }) => {
    expect(parseTimecode(text)).toBe(ms);
  });

  it('round-trips what formatTimecode writes', () => {
    for (const ms of [0, 1, 999, 1000, 4120, 7880, 59_999, 3_723_004, 360_000_000]) {
      expect(parseTimecode(formatTimecode(ms))).toBe(ms);
    }
  });

  it('accepts a comma and surrounding whitespace, like typing', () => {
    expect(parseTimecode('00:00:04,120')).toBe(4120);
    expect(parseTimecode('  00:00:04.120 ')).toBe(4120);
  });

  it('accepts more than two hour digits', () => {
    expect(parseTimecode('100:00:00.000')).toBe(360_000_000);
  });

  it.each([
    '',
    '4.12',
    '00:00:04',
    '00:00:04.12',
    '00:00:04.1200',
    '0:00:04.120',
    '00:60:00.000',
    '00:00:60.000',
    '00:00:-1.000',
    '00:00:04.120 extra',
    '٠٠:٠٠:٠٤.١٢٠',
  ])('rejects %j', (text) => {
    expect(parseTimecode(text)).toBeNull();
  });

  it('rejects hours that overflow a safe integer', () => {
    expect(parseTimecode(`${'9'.repeat(30)}:00:00.000`)).toBeNull();
  });
});
