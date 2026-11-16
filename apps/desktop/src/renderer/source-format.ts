import type { AudioStreamDto, PlaybackDto, VideoDto } from '../shared/api';
import { PROXY_REASONS } from './player-logic';
import type { Tone } from './ui';

/** Shown for a fact the probe did not give. */
export const NOT_KNOWN = 'невідомо';

/** One audio track as the user reads it: its number, then whatever the file says about it. */
export function describeTrack(stream: AudioStreamDto): string {
  const parts = [
    stream.language,
    stream.title,
    stream.channels === null ? null : `${String(stream.channels)} кан.`,
    stream.codec === '' ? null : stream.codec.toUpperCase(),
    stream.default ? 'за замовчуванням' : null,
  ].filter((part): part is string => part !== null && part !== '');
  return parts.length === 0 ? `Доріжка ${String(stream.index)}` : `Доріжка ${String(stream.index)} · ${parts.join(' · ')}`;
}

/** "1920×1080"; null unless both sides are known. */
export function formatResolution(video: VideoDto): string | null {
  const { width, height } = video;
  return width === null || height === null ? null : `${String(width)}×${String(height)}`;
}

/**
 * "29,97 к/с" from the fraction "30000/1001": a decimal comma, at most three decimals and no
 * trailing zeros ("30 к/с"). Null for anything that is not a positive fraction.
 */
export function formatFrameRate(frameRate: string | null): string | null {
  if (frameRate === null) {
    return null;
  }
  const match = /^(\d+)\/(\d+)$/.exec(frameRate);
  if (match === null) {
    return null;
  }
  const fps = Number(match[1]) / Number(match[2]);
  if (!Number.isFinite(fps) || fps <= 0) {
    return null;
  }
  const rounded = fps.toFixed(3).replace(/\.?0+$/, '').replace('.', ',');
  // A rate under half a thousandth of a frame per second rounds to nothing: not a rate to show.
  return rounded === '0' ? null : `${rounded} к/с`;
}

export interface VfrBadge {
  readonly tone: Tone;
  readonly text: string;
}

/**
 * The VFR badge: constant frame rate is fine, variable is worth a warning, unknown is neutral.
 * The unknown text names what is unknown: it stands next to the frame rate, which may be unknown too.
 */
export function vfrBadge(vfr: boolean | null): VfrBadge {
  if (vfr === null) {
    return { tone: 'neutral', text: `VFR: ${NOT_KNOWN}` };
  }
  return vfr
    ? { tone: 'warning', text: 'Змінна частота кадрів (VFR)' }
    : { tone: 'success', text: 'Постійна частота кадрів' };
}

const UNKNOWN_REASON = 'Невідомо';

/** How the source is viewed, and why a preview copy is needed when it is (ADR 021). */
export function describePlayback(playback: PlaybackDto): string {
  if (playback.direct) {
    return 'Напряму';
  }
  const { reason } = playback;
  const cause =
    reason !== null && Object.hasOwn(PROXY_REASONS, reason)
      ? (PROXY_REASONS[reason] ?? UNKNOWN_REASON)
      : UNKNOWN_REASON;
  return `Потрібна копія для перегляду. Причина: ${cause}`;
}
