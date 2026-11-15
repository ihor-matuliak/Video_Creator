import { describe, expect, it } from 'vitest';

import type { PlaybackDto, SourceDto } from '../shared/api';
import {
  PLAYBACK_SPEEDS,
  canonicalMs,
  defaultAudioIndex,
  mediaUrl,
  playsDirectly,
  proxyReasonText,
} from './player-logic';
import { makeSource } from './test-bridge';

function proxy(reason: string | null): PlaybackDto {
  return { direct: false, reason, direct_audio_stream_index: null };
}

const TWO_TRACKS: SourceDto['audio_streams'] = [
  { index: 2, codec: 'opus', channels: 2, language: null, title: 'Мікрофон', default: false, start_offset_ms: 0 },
  { index: 1, codec: 'aac', channels: 2, language: 'uk', title: 'Гра', default: true, start_offset_ms: 0 },
];

describe('canonicalMs', () => {
  it('is round(currentTime * 1000), ADR 021', () => {
    expect(canonicalMs(4.12)).toBe(4120);
    expect(canonicalMs(7.8804)).toBe(7880);
    expect(canonicalMs(7.8806)).toBe(7881);
    expect(canonicalMs(0)).toBe(0);
  });

  it('never returns a negative or non-integer time', () => {
    expect(canonicalMs(-0.2)).toBe(0);
    expect(canonicalMs(Number.NaN)).toBe(0);
    expect(canonicalMs(Number.POSITIVE_INFINITY)).toBe(0);
  });
});

describe('direct playback choice', () => {
  it('plays directly only for the audio stream a direct player plays', () => {
    const source = makeSource({ audio_streams: TWO_TRACKS });
    expect(playsDirectly(source, 1)).toBe(true);
    expect(playsDirectly(source, 2)).toBe(false);
  });

  it('never plays a source directly when main said it cannot', () => {
    const source = makeSource({ playback: proxy('NONZERO_ORIGIN') });
    expect(playsDirectly(source, 1)).toBe(false);
    expect(playsDirectly(source, null)).toBe(false);
  });

  it('plays a source without audio directly when it is direct', () => {
    const source = makeSource({
      audio_streams: [],
      playback: { direct: true, reason: null, direct_audio_stream_index: null },
    });
    expect(defaultAudioIndex(source)).toBeNull();
    expect(playsDirectly(source, null)).toBe(true);
  });

  it('starts on the direct stream, else on the lowest audio index', () => {
    expect(defaultAudioIndex(makeSource({ audio_streams: TWO_TRACKS }))).toBe(1);
    expect(
      defaultAudioIndex(
        makeSource({ audio_streams: TWO_TRACKS, playback: proxy('CONTAINER_UNSUPPORTED') }),
      ),
    ).toBe(1);
  });
});

describe('proxyReasonText', () => {
  it.each([
    ['CONTAINER_UNSUPPORTED', 'контейнер файлу'],
    ['VIDEO_CODEC_UNSUPPORTED', 'відеокодек'],
    ['AUDIO_CODEC_UNSUPPORTED', 'звуковий кодек'],
    ['NONZERO_ORIGIN', 'не з нульової позначки'],
    ['PLAYBACK_INFO_MISSING', 'не вдалося визначити'],
  ])('words %s in Ukrainian', (reason, fragment) => {
    const text = proxyReasonText(makeSource({ playback: proxy(reason) }), 1, false);
    expect(text).toContain(fragment);
    expect(text).toContain('Експорт використовує оригінал');
    expect(text).not.toContain(reason);
  });

  it('has a generic text for a reason code it does not know', () => {
    expect(proxyReasonText(makeSource({ playback: proxy('constructor') }), 1, false)).toContain(
      'не підходить для прямого відтворення',
    );
    expect(proxyReasonText(makeSource({ playback: proxy(null) }), 1, false)).toContain(
      'не підходить для прямого відтворення',
    );
  });

  it('explains a stream other than the direct one and a failed direct player', () => {
    const source = makeSource({ audio_streams: TWO_TRACKS });
    expect(proxyReasonText(source, 2, false)).toContain('обрана звукова доріжка');
    expect(proxyReasonText(source, 1, true)).toContain('оригінал не вдалося відтворити напряму');
  });
});

describe('media URLs and speeds', () => {
  it('builds the app-media URL of the original and of a preview', () => {
    expect(mediaUrl('source', 'abc')).toBe('app-media://source/abc');
    expect(mediaUrl('preview', 'abc')).toBe('app-media://preview/abc');
  });

  it('offers the speeds of docs/03 section 8', () => {
    expect(PLAYBACK_SPEEDS).toEqual([0.5, 1, 1.5, 2]);
  });
});
