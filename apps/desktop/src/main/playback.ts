import { PLAYBACK_REASONS, parseSourceFields } from '../shared/api';
import type { AudioStreamDto, PlaybackDto, SourceDto, VideoDto } from '../shared/api';

/**
 * Direct playback or preview proxy (ADR 021), decided from the probe the
 * renderer never sees. The decision is deliberately conservative: Chromium may
 * silently drop an audio track it cannot decode, and the `currentTime` of a
 * source whose first frame is not at zero has not been verified. Anything that
 * is not known to be safe goes through the proxy, whose timeline starts at the
 * canonical zero (so `currentTime * 1000` is the canonical millisecond on both).
 */

// The codes live in shared/api.ts so that the renderer can word each of them (ADR 023).
export { PLAYBACK_REASONS };

const DIRECT_CONTAINERS: readonly string[] = ['mov', 'mp4'];
const DIRECT_VIDEO_CODEC = 'h264';
const DIRECT_AUDIO_CODECS: readonly string[] = ['aac', 'mp3', 'opus'];

export interface PlaybackInput {
  readonly audioStreams: readonly AudioStreamDto[];
  /** `probe` of the backend answer: `format_name` and `video_codec` are read from it. */
  readonly probe: unknown;
  /** `canonical_origin` of the backend answer: `origin_ms` is read from it. */
  readonly canonicalOrigin: unknown;
}

function fields(value: unknown): Readonly<Record<string, unknown>> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function proxy(reason: string): PlaybackDto {
  return { direct: false, reason, direct_audio_stream_index: null };
}

export function decidePlayback(input: PlaybackInput): PlaybackDto {
  const probe = fields(input.probe);
  const format = probe.format_name;
  const videoCodec = probe.video_codec;
  const originMs = fields(input.canonicalOrigin).origin_ms;
  if (typeof format !== 'string' || typeof videoCodec !== 'string' || typeof originMs !== 'number') {
    return proxy(PLAYBACK_REASONS.missing);
  }

  // ffprobe names a demuxer group ("mov,mp4,m4a,3gp,3g2,mj2"): any member counts.
  const containers = format.split(',').map((name) => name.trim().toLowerCase());
  if (!containers.some((name) => DIRECT_CONTAINERS.includes(name))) {
    return proxy(PLAYBACK_REASONS.container);
  }
  if (videoCodec.toLowerCase() !== DIRECT_VIDEO_CODEC) {
    return proxy(PLAYBACK_REASONS.videoCodec);
  }
  // A direct player plays the first audio stream of the source and nothing else.
  const [first] = [...input.audioStreams].sort((a, b) => a.index - b.index);
  if (first !== undefined && !DIRECT_AUDIO_CODECS.includes(first.codec.toLowerCase())) {
    return proxy(PLAYBACK_REASONS.audioCodec);
  }
  if (originMs !== 0) {
    return proxy(PLAYBACK_REASONS.origin);
  }
  return { direct: true, reason: null, direct_audio_stream_index: first?.index ?? null };
}

/** `frame_rate` as the backend writes it: "num/den", both positive integers. */
const FRAME_RATE = /^[1-9]\d{0,8}\/[1-9]\d{0,8}$/;

function dimension(value: unknown): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0 ? value : null;
}

/**
 * The video facts of the probe that the source inspector shows. A key that is missing or
 * malformed (a source registered before the backend wrote it) gives null for that field only.
 * A picture turned by a quarter (`rotation` 90 or 270 modulo 360) is shown at its size on
 * the screen: width and height trade places. Any other `rotation` changes nothing.
 */
export function summarizeVideo(probe: unknown): VideoDto {
  const { width, height, rotation, frame_rate: frameRate, vfr } = fields(probe);
  const quarterTurn =
    typeof rotation === 'number' && Number.isInteger(rotation) && Math.abs(rotation % 180) === 90;
  return {
    width: dimension(quarterTurn ? height : width),
    height: dimension(quarterTurn ? width : height),
    frame_rate: typeof frameRate === 'string' && FRAME_RATE.test(frameRate) ? frameRate : null,
    vfr: typeof vfr === 'boolean' ? vfr : null,
  };
}

/**
 * The backend answer for a source, reduced to what the renderer may see, plus
 * `playback` and `video`. Throws `TypeError` (naming the field, never its value) when a
 * display field is malformed; the gateway turns that into BAD_BACKEND_RESPONSE.
 */
export function parseBackendSource(value: unknown): SourceDto {
  const source = parseSourceFields(value);
  const raw = fields(value);
  return {
    ...source,
    playback: decidePlayback({
      audioStreams: source.audio_streams,
      probe: raw.probe,
      canonicalOrigin: raw.canonical_origin,
    }),
    video: summarizeVideo(raw.probe),
  };
}
