// @vitest-environment node
import { describe, expect, it } from 'vitest';

import { parseSource } from '../shared/api';
import type { AudioStreamDto } from '../shared/api';
import { PLAYBACK_REASONS, decidePlayback, parseBackendSource, summarizeVideo } from './playback';

const ID = '3f2b8c1e-5d4a-4b7e-9a10-2c6d8e0f1a2b';
const MP4 = 'mov,mp4,m4a,3gp,3g2,mj2';

function audio(index: number, codec: string): AudioStreamDto {
  return {
    index,
    codec,
    channels: 2,
    language: null,
    title: null,
    default: index === 1,
    start_offset_ms: 0,
  };
}

interface Case {
  readonly name: string;
  readonly format?: string;
  readonly video?: string;
  readonly audio?: readonly AudioStreamDto[];
  readonly origin?: number;
}

function decide(c: Case): ReturnType<typeof decidePlayback> {
  return decidePlayback({
    audioStreams: c.audio ?? [audio(1, 'aac')],
    probe: { format_name: c.format ?? MP4, video_codec: c.video ?? 'h264', width: 640, height: 360 },
    canonicalOrigin: { origin_ms: c.origin ?? 0, origin_pts: 0, time_base: '1/15360' },
  });
}

describe('decidePlayback: direct only when the original is known to be safe (ADR 021)', () => {
  it.each<Case>([
    { name: 'mp4 / h264 / aac, origin 0' },
    { name: 'mov / h264 / aac', format: 'mov,mp4,m4a,3gp,3g2,mj2' },
    { name: 'a bare mp4 demuxer name', format: 'mp4' },
    { name: 'mp3 audio', audio: [audio(1, 'mp3')] },
    { name: 'opus audio', audio: [audio(1, 'opus')] },
    { name: 'upper-case names', format: 'MOV,MP4', video: 'H264', audio: [audio(1, 'AAC')] },
  ])('$name -> direct, audio stream 1', (c) => {
    expect(decide(c)).toEqual({ direct: true, reason: null, direct_audio_stream_index: 1 });
  });

  it('a source without audio plays directly and has no direct audio stream', () => {
    expect(decide({ name: '', audio: [] })).toEqual({
      direct: true,
      reason: null,
      direct_audio_stream_index: null,
    });
  });

  it('direct playback is for the first audio stream only, whatever the list order', () => {
    const streams = [audio(3, 'aac'), audio(2, 'aac'), audio(4, 'mp3')];

    expect(decide({ name: '', audio: streams })).toMatchObject({
      direct: true,
      direct_audio_stream_index: 2,
    });
  });

  it('judges the first audio stream: a later unsupported one does not matter, an earlier one does', () => {
    expect(decide({ name: '', audio: [audio(1, 'aac'), audio(2, 'pcm_s16le')] })).toMatchObject({
      direct: true,
      direct_audio_stream_index: 1,
    });
    expect(decide({ name: '', audio: [audio(1, 'pcm_s16le'), audio(2, 'aac')] })).toEqual({
      direct: false,
      reason: PLAYBACK_REASONS.audioCodec,
      direct_audio_stream_index: null,
    });
  });

  it.each<[string, Case, string]>([
    ['matroska', { name: 'mkv', format: 'matroska,webm' }, PLAYBACK_REASONS.container],
    ['mpeg-ts', { name: 'ts', format: 'mpegts' }, PLAYBACK_REASONS.container],
    ['avi', { name: 'avi', format: 'avi' }, PLAYBACK_REASONS.container],
    ['hevc', { name: 'hevc', video: 'hevc' }, PLAYBACK_REASONS.videoCodec],
    ['vp9', { name: 'vp9', video: 'vp9' }, PLAYBACK_REASONS.videoCodec],
    ['pcm_s16le', { name: 'pcm', audio: [audio(1, 'pcm_s16le')] }, PLAYBACK_REASONS.audioCodec],
    ['ac3', { name: 'ac3', audio: [audio(1, 'ac3')] }, PLAYBACK_REASONS.audioCodec],
    ['origin 40 ms', { name: 'origin', origin: 40 }, PLAYBACK_REASONS.origin],
    ['negative origin', { name: 'origin', origin: -33 }, PLAYBACK_REASONS.origin],
  ])('%s -> proxy with a reason', (_label, c, reason) => {
    expect(decide(c)).toEqual({ direct: false, reason, direct_audio_stream_index: null });
  });

  it('names the first failing check: container, then video, audio, origin', () => {
    expect(
      decide({ name: '', format: 'matroska,webm', video: 'hevc', audio: [audio(1, 'pcm_s16le')], origin: 5 }),
    ).toMatchObject({ reason: PLAYBACK_REASONS.container });
    expect(
      decide({ name: '', video: 'hevc', audio: [audio(1, 'pcm_s16le')], origin: 5 }),
    ).toMatchObject({ reason: PLAYBACK_REASONS.videoCodec });
    expect(decide({ name: '', audio: [audio(1, 'pcm_s16le')], origin: 5 })).toMatchObject({
      reason: PLAYBACK_REASONS.audioCodec,
    });
  });

  it.each([
    ['no probe', undefined, { origin_ms: 0 }],
    ['probe of another type', 'mp4', { origin_ms: 0 }],
    ['no format_name', { video_codec: 'h264' }, { origin_ms: 0 }],
    ['no video_codec', { format_name: MP4 }, { origin_ms: 0 }],
    ['no canonical origin', { format_name: MP4, video_codec: 'h264' }, undefined],
    ['origin_ms as text', { format_name: MP4, video_codec: 'h264' }, { origin_ms: '0' }],
    ['no origin_ms', { format_name: MP4, video_codec: 'h264' }, { origin_pts: 0 }],
  ])('%s -> proxy: missing facts never count as safe', (_label, probe, canonicalOrigin) => {
    expect(decidePlayback({ audioStreams: [audio(1, 'aac')], probe, canonicalOrigin })).toEqual({
      direct: false,
      reason: PLAYBACK_REASONS.missing,
      direct_audio_stream_index: null,
    });
  });
});

describe('summarizeVideo: only what the probe really has (ADR 023)', () => {
  const PROBE = {
    format_name: MP4,
    video_codec: 'h264',
    width: 1920,
    height: 1080,
    rotation: 0,
    hdr: false,
    unsupported_reason: null,
    frame_rate: '30000/1001',
    vfr: false,
  };

  it('takes width, height, the frame rate fraction and the VFR flag', () => {
    expect(summarizeVideo(PROBE)).toEqual({
      width: 1920,
      height: 1080,
      frame_rate: '30000/1001',
      vfr: false,
    });
    expect(summarizeVideo({ ...PROBE, vfr: true }).vfr).toBe(true);
  });

  it('gives null for a key that is not there (a source registered before the backend wrote it)', () => {
    const old = { ...PROBE, frame_rate: undefined, vfr: undefined };

    expect(summarizeVideo(old)).toEqual({ width: 1920, height: 1080, frame_rate: null, vfr: null });
    expect(summarizeVideo(JSON.parse(JSON.stringify(old)))).toEqual(summarizeVideo(old));
    expect(summarizeVideo({})).toEqual({ width: null, height: null, frame_rate: null, vfr: null });
  });

  describe('rotation: the size is the one on the screen', () => {
    it.each([90, 270, -90, -270, 450, 810])(
      'rotation %j trades width and height',
      (rotation) => {
        expect(summarizeVideo({ ...PROBE, rotation })).toEqual({
          width: 1080,
          height: 1920,
          frame_rate: '30000/1001',
          vfr: false,
        });
      },
    );

    it.each([
      ['0', 0],
      ['180', 180],
      ['-180', -180],
      ['360', 360],
      ['45', 45],
      ['a fraction', 90.5],
      ['text', '90'],
      ['NaN', Number.NaN],
      ['null', null],
      ['a list', [90]],
    ])('rotation %s changes nothing', (_label, rotation) => {
      expect(summarizeVideo({ ...PROBE, rotation })).toEqual({
        width: 1920,
        height: 1080,
        frame_rate: '30000/1001',
        vfr: false,
      });
    });

    it('changes nothing when the probe has no rotation', () => {
      expect(summarizeVideo({ ...PROBE, rotation: undefined }).width).toBe(1920);
      expect(summarizeVideo({ width: 1920, height: 1080 })).toMatchObject({ width: 1920, height: 1080 });
    });

    it('trades a missing side too: the null goes with it', () => {
      expect(summarizeVideo({ ...PROBE, rotation: 90, height: undefined })).toMatchObject({
        width: null,
        height: 1920,
      });
      expect(summarizeVideo({ ...PROBE, rotation: 90, width: 'x' })).toMatchObject({
        width: 1080,
        height: null,
      });
    });

    it('leaves the frame rate and the VFR flag as they are', () => {
      expect(summarizeVideo({ ...PROBE, rotation: 270, vfr: true })).toMatchObject({
        frame_rate: '30000/1001',
        vfr: true,
      });
    });
  });

  it.each([undefined, null, 'mp4', 7, []])('gives null everywhere for a probe of %j', (probe) => {
    expect(summarizeVideo(probe)).toEqual({ width: null, height: null, frame_rate: null, vfr: null });
  });

  it.each([
    ['width as text', { width: '1920' }, { width: null }],
    ['zero width', { width: 0 }, { width: null }],
    ['negative height', { height: -1 }, { height: null }],
    ['fractional height', { height: 1080.5 }, { height: null }],
    ['NaN width', { width: Number.NaN }, { width: null }],
    ['frame rate as a number', { frame_rate: 29.97 }, { frame_rate: null }],
    ['frame rate without a denominator', { frame_rate: '30' }, { frame_rate: null }],
    ['frame rate with a zero denominator', { frame_rate: '30/0' }, { frame_rate: null }],
    ['frame rate 0/0', { frame_rate: '0/0' }, { frame_rate: null }],
    ['frame rate with a minus', { frame_rate: '-30/1' }, { frame_rate: null }],
    ['frame rate with text around it', { frame_rate: '30/1 fps' }, { frame_rate: null }],
    ['frame rate with a decimal point', { frame_rate: '29.97/1' }, { frame_rate: null }],
    ['frame rate of absurd length', { frame_rate: `${'9'.repeat(30)}/1` }, { frame_rate: null }],
    ['vfr as text', { vfr: 'true' }, { vfr: null }],
    ['vfr as a number', { vfr: 1 }, { vfr: null }],
  ])('%s -> null for that field only', (_label, change, expected) => {
    expect(summarizeVideo({ ...PROBE, ...change })).toEqual({
      width: 1920,
      height: 1080,
      frame_rate: '30000/1001',
      vfr: false,
      ...expected,
    });
  });
});

describe('parseBackendSource', () => {
  const RAW = {
    id: ID,
    project_id: ID,
    path: 'C:\\Videos\\clip.mp4',
    display_name: 'clip.mp4',
    size: 10,
    mtime_ns: 1,
    quick_fingerprint: 'q',
    sha256: null,
    hash_status: 'pending',
    duration_ms: 12000,
    video_stream_index: 0,
    audio_streams: [audio(1, 'aac')],
    canonical_origin: { origin_ms: 0, origin_pts: 0, time_base: '1/15360' },
    probe: {
      format_name: MP4,
      video_codec: 'h264',
      width: 640,
      height: 360,
      hdr: false,
      frame_rate: '30000/1001',
      vfr: false,
    },
    availability: 'available',
    created_at: '2026-11-14T10:00:00Z',
    hash_job_id: null,
  };

  it('adds playback and video and drops the path, probe and canonical origin', () => {
    const source = parseBackendSource(RAW);

    expect(source.playback).toEqual({ direct: true, reason: null, direct_audio_stream_index: 1 });
    expect(source.video).toEqual({ width: 640, height: 360, frame_rate: '30000/1001', vfr: false });
    const serialized = JSON.stringify(source);
    for (const hidden of ['path', 'probe', 'canonical_origin', 'format_name', 'sha256', 'mtime']) {
      expect(serialized).not.toContain(hidden);
    }
  });

  it('is what the preload accepts again', () => {
    expect(parseSource(parseBackendSource(RAW))).toEqual(parseBackendSource(RAW));
  });

  it('puts a Matroska source on the proxy', () => {
    expect(
      parseBackendSource({ ...RAW, probe: { ...RAW.probe, format_name: 'matroska,webm' } }).playback,
    ).toEqual({
      direct: false,
      reason: PLAYBACK_REASONS.container,
      direct_audio_stream_index: null,
    });
  });

  it('shows a turned source at its size on the screen', () => {
    const source = parseBackendSource({ ...RAW, probe: { ...RAW.probe, rotation: 90 } });

    expect(source.video).toMatchObject({ width: 360, height: 640 });
    expect(JSON.stringify(source)).not.toContain('rotation');
  });

  it('a source without frame rate and VFR keys still parses, with null video facts', () => {
    // JSON drops the keys: they are missing, as in the answer for an old source.
    const old: unknown = JSON.parse(JSON.stringify({ ...RAW.probe, frame_rate: undefined, vfr: undefined }));

    const source = parseBackendSource({ ...RAW, probe: old });

    expect(source.video).toEqual({ width: 640, height: 360, frame_rate: null, vfr: null });
    expect(parseSource(source)).toEqual(source);
  });

  it('a source without a probe has null video facts and goes on the proxy', () => {
    const source = parseBackendSource({ ...RAW, probe: undefined });

    expect(source.video).toEqual({ width: null, height: null, frame_rate: null, vfr: null });
    expect(source.playback.reason).toBe(PLAYBACK_REASONS.missing);
  });

  it('rejects a malformed display field instead of guessing', () => {
    expect(() => parseBackendSource({ ...RAW, size: '10' })).toThrow(TypeError);
    expect(() => parseBackendSource(null)).toThrow(TypeError);
  });
});
