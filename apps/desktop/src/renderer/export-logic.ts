import type {
  AudioStreamDto,
  ClipDto,
  ExportBatchDto,
  ExportItemDto,
  ExportProfile,
  JobDto,
  SourceDto,
} from '../shared/api';

/** The export batch the Експорт tab shows: the batch and the job that renders it. */
export interface ExportRun {
  readonly id: string;
  readonly jobId: string;
}

/** Every clip the project view has listed, by id, in the order they were first seen. */
export type ClipRegistry = ReadonlyMap<string, ClipDto>;

export interface SkippedClip {
  readonly id: string;
  /** Null when the clip is not in the registry. */
  readonly title: string | null;
  readonly sourceId: string | null;
}

/** «Вибрано N · підтверджено M»: what is ticked, and what of it can be exported (FR-033). */
export interface ExportSelection {
  /** N: every ticked clip. */
  readonly selected: number;
  /** M: the ticked clips that are approved; only these are exported. */
  readonly approved: readonly ClipDto[];
  /** Ticked clips that are not approved: shown as skipped, never sent to the backend. */
  readonly skipped: readonly SkippedClip[];
}

export function summarizeSelection(
  selectedIds: ReadonlySet<string>,
  clips: ClipRegistry,
): ExportSelection {
  const approved: ClipDto[] = [];
  const skipped: SkippedClip[] = [];
  for (const clip of clips.values()) {
    if (!selectedIds.has(clip.id)) {
      continue;
    }
    if (clip.review_status === 'approved') {
      approved.push(clip);
    } else {
      skipped.push({ id: clip.id, title: clip.title, sourceId: clip.source_id });
    }
  }
  for (const id of selectedIds) {
    if (!clips.has(id)) {
      skipped.push({ id, title: null, sourceId: null });
    }
  }
  return { selected: selectedIds.size, approved, skipped };
}

// --- audio -------------------------------------------------------------------------------------

export interface AudioOption {
  readonly value: string;
  readonly label: string;
}

export interface AudioChoices {
  readonly options: readonly AudioOption[];
  readonly defaultValue: string;
}

const AUDIO_ALL = 'all';
const AUDIO_NONE = 'none';
const TRACK_PREFIX = 'track:';

function trackLabel(stream: AudioStreamDto): string {
  const parts = [stream.language, stream.title].filter(
    (part): part is string => part !== null && part !== '',
  );
  return parts.length === 0
    ? `Доріжка ${String(stream.index)}`
    : `Доріжка ${String(stream.index)} · ${parts.join(' · ')}`;
}

/**
 * The «Звук у готових кліпах» choices for the sources of the clips being exported (docs/03
 * section 5). The backend's `selected` mode names one global stream index and refuses a source
 * that lacks it, so a single track is offered only when every source has it. «Усі доріжки» and
 * «Без звуку» are always valid. Nothing else is offered: unsupported values are a 422.
 */
export function audioChoices(sources: readonly SourceDto[]): AudioChoices {
  const [first, ...others] = sources;
  const common =
    first === undefined
      ? []
      : first.audio_streams
          .filter((stream) =>
            others.every((source) =>
              source.audio_streams.some((candidate) => candidate.index === stream.index),
            ),
          )
          .sort((a, b) => a.index - b.index);
  const anyAudio = sources.some((source) => source.audio_streams.length > 0);

  const options: AudioOption[] = common.map((stream) => ({
    value: `${TRACK_PREFIX}${String(stream.index)}`,
    label: trackLabel(stream),
  }));
  if (anyAudio) {
    options.push({ value: AUDIO_ALL, label: 'Усі доріжки' });
  }
  options.push({ value: AUDIO_NONE, label: 'Без звуку' });

  const preferred = common.find((stream) => stream.default) ?? common[0];
  let defaultValue = AUDIO_NONE;
  if (preferred !== undefined) {
    defaultValue = `${TRACK_PREFIX}${String(preferred.index)}`;
  } else if (anyAudio) {
    defaultValue = AUDIO_ALL;
  }
  return { options, defaultValue };
}

/** The profile the backend accepts today (docs/04 section 4): h264, high, no SRT yet. */
export function profileFor(audio: string): ExportProfile {
  const base = { video_codec: 'h264', quality: 'high', write_srt: false };
  if (audio.startsWith(TRACK_PREFIX)) {
    return {
      ...base,
      audio_mode: 'selected',
      audio_stream_index: Number(audio.slice(TRACK_PREFIX.length)),
    };
  }
  return { ...base, audio_mode: audio === AUDIO_ALL ? 'all' : 'none', audio_stream_index: null };
}

export function hasSound(audio: string): boolean {
  return audio !== AUDIO_NONE;
}

// --- batch -------------------------------------------------------------------------------------

/** A batch is open while its clips are still queued or rendered; any other status is final. */
export function isBatchFinished(status: string): boolean {
  return status !== 'queued' && status !== 'running';
}

export interface ItemCounts {
  readonly total: number;
  readonly verified: number;
  readonly failed: number;
  readonly cancelled: number;
  /** Queued, running or in a state this app does not know. */
  readonly open: number;
}

export function countItems(items: readonly ExportItemDto[]): ItemCounts {
  let verified = 0;
  let failed = 0;
  let cancelled = 0;
  for (const item of items) {
    if (item.state === 'verified') {
      verified += 1;
    } else if (item.state === 'failed') {
      failed += 1;
    } else if (item.state === 'cancelled') {
      cancelled += 1;
    }
  }
  return {
    total: items.length,
    verified,
    failed,
    cancelled,
    open: items.length - verified - failed - cancelled,
  };
}

export type BatchOutcome = 'running' | 'complete' | 'cancelled' | 'failed';

/**
 * `complete` only when the batch is finished and every item that is not a failure is verified:
 * success is the backend's verification of the file, never its existence (FR-041). A batch the
 * user cancelled, or in which nothing was verified, is not complete.
 */
export function batchOutcome(batch: ExportBatchDto): BatchOutcome {
  if (!isBatchFinished(batch.status)) {
    return 'running';
  }
  if (batch.status === 'cancelled') {
    return 'cancelled';
  }
  const counts = countItems(batch.items);
  const settled = counts.open === 0 && counts.cancelled === 0;
  const succeeded = batch.status === 'succeeded' || batch.status === 'partial_failed';
  return succeeded && settled && counts.verified > 0 ? 'complete' : 'failed';
}

/** The file name of a path (either separator); the folder part is not shown. */
export function fileName(path: string): string {
  const parts = path.split(/[\\/]/).filter((part) => part !== '');
  return parts.at(-1) ?? path;
}

export interface RunningProgress {
  /** `render` or `verify`, as the job reports it; null when it says nothing known. */
  readonly stage: 'render' | 'verify' | null;
  /** 0..100 of the stage of the item being rendered; null when unknown. */
  readonly percent: number | null;
}

/** What the export job says about the item that is rendered right now. */
export function runningProgress(job: JobDto | null): RunningProgress {
  const progress = job?.progress ?? {};
  const { stage, item_percent: percent } = progress;
  return {
    stage: stage === 'render' || stage === 'verify' ? stage : null,
    percent:
      typeof percent === 'number' && Number.isFinite(percent)
        ? Math.min(100, Math.max(0, Math.round(percent)))
        : null,
  };
}
