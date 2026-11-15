import type { SourceDto } from '../shared/api';

/** Playback speeds of docs/03 section 8. */
export const PLAYBACK_SPEEDS: readonly number[] = [0.5, 1, 1.5, 2];

/**
 * The canonical source millisecond of a `<video>` position (ADR 021): on the
 * direct player and on the proxy alike, `currentTime * 1000` is the canonical time.
 */
export function canonicalMs(currentTimeSeconds: number): number {
  return Number.isFinite(currentTimeSeconds) ? Math.max(0, Math.round(currentTimeSeconds * 1000)) : 0;
}

/** `app-media://source/<id>` is the original, `app-media://preview/<id>` the proxy (ADR 022). */
export function mediaUrl(kind: 'source' | 'preview', id: string): string {
  return `app-media://${kind}/${id}`;
}

/** The audio stream selected when a source opens: the one a direct player plays, else the first. */
export function defaultAudioIndex(source: SourceDto): number | null {
  if (source.playback.direct_audio_stream_index !== null) {
    return source.playback.direct_audio_stream_index;
  }
  const first = [...source.audio_streams].sort((a, b) => a.index - b.index)[0];
  return first?.index ?? null;
}

/**
 * A direct player shows the first audio stream of the source and nothing else, so
 * the original is played directly only for exactly that stream (ADR 021, 022).
 */
export function playsDirectly(source: SourceDto, audioIndex: number | null): boolean {
  return source.playback.direct && audioIndex === source.playback.direct_audio_stream_index;
}

/** Wording of each `playback.reason` code; read in a sentence, so lower-case and without a full stop. */
export const PROXY_REASONS: Readonly<Record<string, string>> = {
  CONTAINER_UNSUPPORTED: 'контейнер файлу не підтримується програвачем',
  VIDEO_CODEC_UNSUPPORTED: 'відеокодек не підтримується програвачем',
  AUDIO_CODEC_UNSUPPORTED: 'звуковий кодек першої доріжки не підтримується програвачем',
  NONZERO_ORIGIN: 'відео починається не з нульової позначки часу',
  PLAYBACK_INFO_MISSING: 'не вдалося визначити, чи підтримується оригінал програвачем',
};

const UNKNOWN_REASON = 'оригінал не підходить для прямого відтворення';

/** Why the player shows a preview version instead of the original, in Ukrainian. */
export function proxyReasonText(
  source: SourceDto,
  audioIndex: number | null,
  directFailed: boolean,
): string {
  const { direct, reason } = source.playback;
  let cause: string;
  if (!direct) {
    cause =
      reason !== null && Object.hasOwn(PROXY_REASONS, reason)
        ? (PROXY_REASONS[reason] ?? UNKNOWN_REASON)
        : UNKNOWN_REASON;
  } else if (audioIndex !== source.playback.direct_audio_stream_index) {
    cause = 'обрана звукова доріжка не відтворюється в оригіналі напряму';
  } else if (directFailed) {
    cause = 'оригінал не вдалося відтворити напряму';
  } else {
    cause = UNKNOWN_REASON;
  }
  return `Перегляд іде з окремої версії: ${cause}. Експорт використовує оригінал.`;
}
