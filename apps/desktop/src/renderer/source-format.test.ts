import { describe, expect, it } from 'vitest';

import { PLAYBACK_REASONS } from '../shared/api';
import type { PlaybackDto, VideoDto } from '../shared/api';
import { PROXY_REASONS } from './player-logic';
import { describePlayback, formatFrameRate, formatResolution, vfrBadge } from './source-format';

function video(overrides: Partial<VideoDto> = {}): VideoDto {
  return { width: 1920, height: 1080, frame_rate: '30000/1001', vfr: false, ...overrides };
}

function proxy(reason: string | null): PlaybackDto {
  return { direct: false, reason, direct_audio_stream_index: null };
}

describe('formatResolution', () => {
  it('writes width and height with the multiplication sign', () => {
    expect(formatResolution(video())).toBe('1920×1080');
    expect(formatResolution(video({ width: 320, height: 180 }))).toBe('320×180');
  });

  it('is null unless both sides are known', () => {
    expect(formatResolution(video({ width: null }))).toBeNull();
    expect(formatResolution(video({ height: null }))).toBeNull();
    expect(formatResolution(video({ width: null, height: null }))).toBeNull();
  });
});

describe('formatFrameRate', () => {
  it.each([
    ['30000/1001', '29,97 к/с'],
    ['24000/1001', '23,976 к/с'],
    ['60000/1001', '59,94 к/с'],
    ['30/1', '30 к/с'],
    ['25/1', '25 к/с'],
    ['60/1', '60 к/с'],
    ['100/1', '100 к/с'],
    ['15/2', '7,5 к/с'],
    ['1/3', '0,333 к/с'],
    ['2997/100', '29,97 к/с'],
  ])('%s -> %s (a decimal comma, up to 3 decimals, no trailing zeros)', (rate, text) => {
    expect(formatFrameRate(rate)).toBe(text);
  });

  it.each([
    null,
    '',
    '30',
    '30/0',
    '0/1',
    '0/0',
    '-30/1',
    '29.97/1',
    'abc',
    '30/1 fps',
    '1/100000',
  ])('is null for %j', (rate) => {
    expect(formatFrameRate(rate)).toBeNull();
  });
});

describe('vfrBadge', () => {
  it('words variable frame rate with a warning', () => {
    expect(vfrBadge(true)).toEqual({ tone: 'warning', text: 'Змінна частота кадрів (VFR)' });
  });

  it('words constant frame rate as fine', () => {
    expect(vfrBadge(false)).toEqual({ tone: 'success', text: 'Постійна частота кадрів' });
  });

  it('says what is not known, with a neutral tone', () => {
    expect(vfrBadge(null)).toEqual({ tone: 'neutral', text: 'VFR: невідомо' });
  });
});

describe('describePlayback', () => {
  it('says "directly" when the original is played', () => {
    expect(describePlayback({ direct: true, reason: null, direct_audio_stream_index: 1 })).toBe('Напряму');
  });

  it('words every reason code of main in Ukrainian, none of them as the code itself', () => {
    const codes = Object.values(PLAYBACK_REASONS);

    expect(codes).toHaveLength(5);
    for (const code of codes) {
      const text = describePlayback(proxy(code));

      expect(text, code).toMatch(/^Потрібна копія для перегляду\. Причина: /);
      expect(text, code).toContain(PROXY_REASONS[code]);
      expect(text, code).not.toContain(code);
      expect(text, code).not.toContain('Невідомо');
    }
  });

  it('says what each reason is', () => {
    expect(describePlayback(proxy(PLAYBACK_REASONS.container))).toContain('контейнер файлу');
    expect(describePlayback(proxy(PLAYBACK_REASONS.videoCodec))).toContain('відеокодек');
    expect(describePlayback(proxy(PLAYBACK_REASONS.audioCodec))).toContain('звуковий кодек');
    expect(describePlayback(proxy(PLAYBACK_REASONS.origin))).toContain('не з нульової позначки');
    expect(describePlayback(proxy(PLAYBACK_REASONS.missing))).toContain('не вдалося визначити');
  });

  it.each(['SOMETHING_NEW', 'constructor', '__proto__', 'toString', ''])(
    'says "Невідомо" for the unknown code %j',
    (code) => {
      expect(describePlayback(proxy(code))).toBe('Потрібна копія для перегляду. Причина: Невідомо');
    },
  );

  it('says "Невідомо" for a proxy without a reason', () => {
    expect(describePlayback(proxy(null))).toBe('Потрібна копія для перегляду. Причина: Невідомо');
  });
});
