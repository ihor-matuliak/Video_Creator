/**
 * Allowlisted backend operations behind the single `api:call` IPC channel, with
 * the typed params and result of each one. Shared by main (param validation and
 * sanitising of backend answers), preload (re-validation of every response) and
 * the renderer (types only). Pure: no Node, no Electron.
 */

export const API_OPS = [
  'projects.list',
  'projects.create',
  'projects.update',
  'sources.get',
  'sources.list',
  'clips.list',
  'clips.create',
  'clips.patch',
  'clips.review',
  'exports.create',
  'exports.get',
  'exports.retry',
  'jobs.get',
  'jobs.cancel',
  'previews.create',
  'previews.get',
  'system.checks',
] as const;

export type ApiOp = (typeof API_OPS)[number];

export function isApiOp(value: unknown): value is ApiOp {
  return typeof value === 'string' && (API_OPS as readonly string[]).includes(value);
}

const UUID4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

/** IDs are uuid4 strings (ADR 017); anything else never reaches a URL. */
export function isUuid4(value: unknown): value is string {
  return typeof value === 'string' && UUID4.test(value);
}

/** One-time export-folder token: 32 random bytes as 64 lowercase hex characters. */
export const FOLDER_TOKEN_PATTERN = /^[0-9a-f]{64}$/;

export function isFolderToken(value: unknown): value is string {
  return typeof value === 'string' && FOLDER_TOKEN_PATTERN.test(value);
}

/** One-time video token (`pickVideo`, `takeDroppedVideo`, ADR 024): the shape of a folder token. */
export function isVideoToken(value: unknown): value is string {
  return isFolderToken(value);
}

/** The longest `q` of `projects.list`, in characters (code points, like the backend's `len`). */
export const PROJECT_SEARCH_MAX_LENGTH = 200;

export type ReviewStatus = 'suggested' | 'approved' | 'rejected';
export const REVIEW_STATUSES: readonly ReviewStatus[] = ['suggested', 'approved', 'rejected'];

export interface ExportProfile {
  readonly video_codec: string;
  readonly quality: string;
  readonly audio_mode: string;
  /** Global source stream index; null for no audio or all streams. */
  readonly audio_stream_index: number | null;
  readonly write_srt: boolean;
}

/**
 * Params of each op. They mirror the backend contract (docs/04) except for what
 * the renderer must never control: there is no `output_dir`, no filesystem path
 * and no `evidence_type`. `folderToken` is bridge-only (see `chooseExportFolder`).
 */
export interface ApiParams {
  readonly 'projects.list': {
    readonly limit?: number;
    readonly cursor?: string;
    readonly include_archived?: boolean;
    /** Name search: a substring, case-insensitive for any script; at most PROJECT_SEARCH_MAX_LENGTH. */
    readonly q?: string;
  };
  readonly 'projects.create': { readonly name: string };
  readonly 'projects.update': {
    readonly id: string;
    readonly name?: string;
    readonly archived?: boolean;
  };
  readonly 'sources.get': { readonly id: string };
  /**
   * One page of the sources of a project (ADR 023). The page size is fixed in main; the
   * renderer follows `next_cursor` until it is null.
   */
  readonly 'sources.list': { readonly project_id: string; readonly cursor?: string };
  readonly 'clips.list': {
    readonly source_id?: string;
    readonly review_status?: ReviewStatus;
    readonly category?: string;
    readonly limit?: number;
    readonly cursor?: string;
  };
  readonly 'clips.create': {
    readonly source_id: string;
    readonly title: string;
    readonly start_ms: number;
    readonly end_ms: number;
    readonly categories: readonly string[];
  };
  readonly 'clips.patch': {
    readonly id: string;
    readonly expected_revision: number;
    readonly title?: string;
    readonly start_ms?: number;
    readonly end_ms?: number;
    readonly categories?: readonly string[];
  };
  readonly 'clips.review': {
    readonly id: string;
    readonly expected_revision: number;
    readonly status: ReviewStatus;
  };
  readonly 'exports.create': {
    readonly project_id: string;
    readonly clip_ids: readonly string[];
    readonly folderToken: string;
    readonly profile: ExportProfile;
  };
  readonly 'exports.get': { readonly id: string };
  /**
   * Main reads the batch and posts a new one for its failed and cancelled clips, with the
   * batch's own `output_dir` and profile: the renderer names the batch and nothing else.
   */
  readonly 'exports.retry': { readonly batch_id: string };
  readonly 'jobs.get': { readonly id: string };
  readonly 'jobs.cancel': { readonly id: string };
  readonly 'previews.create': {
    readonly source_id: string;
    /** Global source stream index of the audio to keep; null for a proxy without sound. */
    readonly audio_stream_index: number | null;
  };
  readonly 'previews.get': { readonly id: string };
  /** Readiness of FFmpeg, FFprobe and the data folder (ADR 023, ADR 024); takes no params. */
  readonly 'system.checks': Readonly<Record<string, never>>;
}

export interface Page<T> {
  readonly items: readonly T[];
  readonly next_cursor: string | null;
}

export interface ProjectDto {
  readonly id: string;
  readonly name: string;
  readonly created_at: string;
  readonly updated_at: string;
  readonly archived_at: string | null;
}

/** A project in the project list: the project plus the figures of docs/03 section 4. */
export interface ProjectListItemDto extends ProjectDto {
  readonly source_count: number;
  readonly clip_count: number;
  /** ISO 8601 UTC: the latest change of the project, its sources, clips and export batches. */
  readonly last_activity_at: string;
}

export interface AudioStreamDto {
  /** Global stream index in the source: the stable track ID. */
  readonly index: number;
  readonly codec: string;
  readonly channels: number | null;
  readonly language: string | null;
  readonly title: string | null;
  readonly default: boolean;
  readonly start_offset_ms: number | null;
}

/**
 * Whether the original may be played directly (ADR 021), decided in main from
 * the probe, which the renderer never sees. Direct playback shows the first
 * audio stream of the source only, so it applies to that stream and no other.
 */
export interface PlaybackDto {
  readonly direct: boolean;
  /** Why a preview proxy is needed (a stable code); null when `direct`. */
  readonly reason: string | null;
  /** The audio stream a direct player plays; null for no audio or when not `direct`. */
  readonly direct_audio_stream_index: number | null;
}

/** Stable codes for `playback.reason`; the renderer words them in Ukrainian. */
export const PLAYBACK_REASONS = {
  /** The container is not mov/mp4. */
  container: 'CONTAINER_UNSUPPORTED',
  /** The video is not H.264. */
  videoCodec: 'VIDEO_CODEC_UNSUPPORTED',
  /** The first audio stream is not AAC, MP3 or Opus. */
  audioCodec: 'AUDIO_CODEC_UNSUPPORTED',
  /** The first video frame is not at container time zero. */
  origin: 'NONZERO_ORIGIN',
  /** The probe summary lacks a field the decision needs. */
  missing: 'PLAYBACK_INFO_MISSING',
} as const;

/**
 * The video facts the source inspector shows (ADR 023), taken by main from the probe the
 * renderer never sees. Each field is null when the probe does not have it or it is malformed.
 */
export interface VideoDto {
  /** The size on the screen: width and height trade places for a quarter-turn rotation. */
  readonly width: number | null;
  readonly height: number | null;
  /** The average frame rate as the fraction string "num/den" (for example "30000/1001"). */
  readonly frame_rate: string | null;
  /** true for variable frame rate, false for constant, null when not determined. */
  readonly vfr: boolean | null;
}

/** Deliberately without the filesystem path, probe dump and canonical origin. */
export interface SourceDto {
  readonly id: string;
  readonly project_id: string;
  readonly display_name: string;
  readonly size: number;
  readonly duration_ms: number;
  readonly video_stream_index: number;
  readonly audio_streams: readonly AudioStreamDto[];
  readonly availability: string;
  readonly hash_status: string;
  /** The SHA-256 job queued by a registration; null in every other response. */
  readonly hash_job_id: string | null;
  readonly created_at: string;
  /** Always present in answers of main (the parsers require it). */
  readonly playback: PlaybackDto;
  /** Always present in answers of main (the parsers require it). */
  readonly video: VideoDto;
}

/** A source as the backend describes it, before main adds `playback` and `video`. */
export type SourceFields = Omit<SourceDto, 'playback' | 'video'>;

export interface ClipDto {
  readonly id: string;
  readonly source_id: string;
  readonly analysis_run_id: string | null;
  readonly revision: number;
  readonly title: string;
  readonly start_ms: number;
  readonly end_ms: number;
  readonly recommended_start_ms: number | null;
  readonly recommended_end_ms: number | null;
  readonly categories: readonly string[];
  readonly evidence_type: string;
  readonly evidence_ids: readonly string[];
  readonly reason: string | null;
  readonly scores: Readonly<Record<string, number>> | null;
  readonly rank: number | null;
  readonly visual_review_required: boolean;
  readonly review_status: string;
  readonly export_state: string;
  readonly created_at: string;
  readonly updated_at: string;
}

export interface ExportCreatedDto {
  readonly id: string;
  readonly job_id: string;
  readonly status: string;
}

export interface CodeMessage {
  readonly code: string;
  readonly message: string;
}

export interface ExportItemDto {
  readonly id: string;
  readonly clip_id: string;
  readonly clip_revision: number;
  readonly state: string;
  readonly final_path: string | null;
  /** Duration measured on the verified file; null until the item is verified. */
  readonly duration_ms: number | null;
  readonly error: CodeMessage | null;
}

export interface ExportBatchDto {
  readonly id: string;
  readonly project_id: string;
  readonly job_id: string | null;
  readonly status: string;
  readonly output_dir: string;
  readonly items: readonly ExportItemDto[];
  readonly manifest_path: string | null;
  readonly catalog_path: string | null;
  readonly created_at: string;
}

export interface JobDto {
  readonly id: string;
  readonly type: string;
  readonly state: string;
  readonly progress: Readonly<Record<string, unknown>>;
  readonly error: CodeMessage | null;
}

export interface PreviewCreatedDto {
  readonly id: string;
  readonly job_id: string;
  /** queued, ... ; `ready` when an existing valid preview is reused. */
  readonly status: string;
}

export interface PreviewDto {
  readonly id: string;
  readonly source_id: string;
  readonly audio_stream_index: number | null;
  /** queued, running, ready, failed, cancelled or interrupted. */
  readonly status: string;
  readonly job_id: string;
  readonly error: CodeMessage | null;
}

/** FFmpeg or FFprobe: whether it was found at start-up and its version; never a path. */
export interface ToolCheckDto {
  readonly ok: boolean;
  readonly version: string | null;
}

/** The data folder: whether the app could write in it, and the free space of its disk. */
export interface DataDirCheckDto {
  readonly ok: boolean;
  readonly free_bytes: number | null;
}

export interface SystemChecksDto {
  readonly ffmpeg: ToolCheckDto;
  readonly ffprobe: ToolCheckDto;
  readonly data_dir: DataDirCheckDto;
}

export interface ApiData {
  readonly 'projects.list': Page<ProjectListItemDto>;
  readonly 'projects.create': ProjectDto;
  readonly 'projects.update': ProjectDto;
  readonly 'sources.get': SourceDto;
  readonly 'sources.list': Page<SourceDto>;
  readonly 'clips.list': Page<ClipDto>;
  readonly 'clips.create': ClipDto;
  readonly 'clips.patch': ClipDto;
  readonly 'clips.review': ClipDto;
  readonly 'exports.create': ExportCreatedDto;
  readonly 'exports.get': ExportBatchDto;
  readonly 'exports.retry': ExportCreatedDto;
  readonly 'jobs.get': JobDto;
  readonly 'jobs.cancel': JobDto;
  readonly 'previews.create': PreviewCreatedDto;
  readonly 'previews.get': PreviewDto;
  readonly 'system.checks': SystemChecksDto;
}

type Parse<T> = (value: unknown) => T;
type Fields = Readonly<Record<string, unknown>>;

function bad(): never {
  throw new TypeError('Некоректна відповідь локального рушія');
}

const text: Parse<string> = (value) => (typeof value === 'string' ? value : bad());
const integer: Parse<number> = (value) =>
  typeof value === 'number' && Number.isSafeInteger(value) ? value : bad();
const count: Parse<number> = (value) => {
  const whole = integer(value);
  return whole >= 0 ? whole : bad();
};
const bool: Parse<boolean> = (value) => (typeof value === 'boolean' ? value : bad());
const record: Parse<Fields> = (value) =>
  typeof value === 'object' && value !== null && !Array.isArray(value) ? (value as Fields) : bad();

function nullable<T>(parse: Parse<T>): Parse<T | null> {
  return (value) => (value === null ? null : parse(value));
}

function list<T>(parse: Parse<T>): Parse<T[]> {
  return (value) => (Array.isArray(value) ? value.map((item: unknown) => parse(item)) : bad());
}

/** Reads one field; a failure names the field but never echoes its value. */
function field<T>(source: Fields, key: string, parse: Parse<T>): T {
  try {
    return parse(source[key]);
  } catch {
    throw new TypeError(`Некоректна відповідь локального рушія: поле ${key}`);
  }
}

const parseCodeMessage: Parse<CodeMessage> = (value) => {
  const source = record(value);
  return { code: field(source, 'code', text), message: field(source, 'message', text) };
};

const parseScores: Parse<Readonly<Record<string, number>>> = (value) =>
  Object.fromEntries(Object.entries(record(value)).map(([key, score]) => [key, integer(score)]));

export function parsePage<T>(parseItem: Parse<T>): Parse<Page<T>> {
  return (value) => {
    const source = record(value);
    return {
      items: field(source, 'items', list(parseItem)),
      next_cursor: field(source, 'next_cursor', nullable(text)),
    };
  };
}

export const parseProject: Parse<ProjectDto> = (value) => {
  const source = record(value);
  return {
    id: field(source, 'id', text),
    name: field(source, 'name', text),
    created_at: field(source, 'created_at', text),
    updated_at: field(source, 'updated_at', text),
    archived_at: field(source, 'archived_at', nullable(text)),
  };
};

/** A list item: the project fields and the figures; the PATCH `archived` flag is dropped like in `parseProject`. */
export const parseProjectListItem: Parse<ProjectListItemDto> = (value) => {
  const source = record(value);
  return {
    ...parseProject(source),
    source_count: field(source, 'source_count', count),
    clip_count: field(source, 'clip_count', count),
    last_activity_at: field(source, 'last_activity_at', text),
  };
};

const parseAudioStream: Parse<AudioStreamDto> = (value) => {
  const source = record(value);
  return {
    index: field(source, 'index', integer),
    codec: field(source, 'codec', text),
    channels: field(source, 'channels', nullable(integer)),
    language: field(source, 'language', nullable(text)),
    title: field(source, 'title', nullable(text)),
    default: field(source, 'default', bool),
    start_offset_ms: field(source, 'start_offset_ms', nullable(integer)),
  };
};

/** The display fields of a source; used on the backend answer and on main's own answer. */
export const parseSourceFields: Parse<SourceFields> = (value) => {
  const source = record(value);
  return {
    id: field(source, 'id', text),
    project_id: field(source, 'project_id', text),
    display_name: field(source, 'display_name', text),
    size: field(source, 'size', integer),
    duration_ms: field(source, 'duration_ms', integer),
    video_stream_index: field(source, 'video_stream_index', integer),
    audio_streams: field(source, 'audio_streams', list(parseAudioStream)),
    availability: field(source, 'availability', text),
    hash_status: field(source, 'hash_status', text),
    hash_job_id: field(source, 'hash_job_id', nullable(text)),
    created_at: field(source, 'created_at', text),
  };
};

const parsePlayback: Parse<PlaybackDto> = (value) => {
  const source = record(value);
  return {
    direct: field(source, 'direct', bool),
    reason: field(source, 'reason', nullable(text)),
    direct_audio_stream_index: field(source, 'direct_audio_stream_index', nullable(integer)),
  };
};

const parseVideo: Parse<VideoDto> = (value) => {
  const source = record(value);
  return {
    width: field(source, 'width', nullable(integer)),
    height: field(source, 'height', nullable(integer)),
    frame_rate: field(source, 'frame_rate', nullable(text)),
    vfr: field(source, 'vfr', nullable(bool)),
  };
};

/** A source as main answers: display fields plus the required `playback` and `video`. */
export const parseSource: Parse<SourceDto> = (value) => ({
  ...parseSourceFields(value),
  playback: field(record(value), 'playback', parsePlayback),
  video: field(record(value), 'video', parseVideo),
});

export const parseClip: Parse<ClipDto> = (value) => {
  const source = record(value);
  return {
    id: field(source, 'id', text),
    source_id: field(source, 'source_id', text),
    analysis_run_id: field(source, 'analysis_run_id', nullable(text)),
    revision: field(source, 'revision', integer),
    title: field(source, 'title', text),
    start_ms: field(source, 'start_ms', integer),
    end_ms: field(source, 'end_ms', integer),
    recommended_start_ms: field(source, 'recommended_start_ms', nullable(integer)),
    recommended_end_ms: field(source, 'recommended_end_ms', nullable(integer)),
    categories: field(source, 'categories', list(text)),
    evidence_type: field(source, 'evidence_type', text),
    evidence_ids: field(source, 'evidence_ids', list(text)),
    reason: field(source, 'reason', nullable(text)),
    scores: field(source, 'scores', nullable(parseScores)),
    rank: field(source, 'rank', nullable(integer)),
    visual_review_required: field(source, 'visual_review_required', bool),
    review_status: field(source, 'review_status', text),
    export_state: field(source, 'export_state', text),
    created_at: field(source, 'created_at', text),
    updated_at: field(source, 'updated_at', text),
  };
};

export const parseExportCreated: Parse<ExportCreatedDto> = (value) => {
  const source = record(value);
  return {
    id: field(source, 'id', text),
    job_id: field(source, 'job_id', text),
    status: field(source, 'status', text),
  };
};

function exportItemFields(source: Fields): Omit<ExportItemDto, 'duration_ms'> {
  return {
    id: field(source, 'id', text),
    clip_id: field(source, 'clip_id', text),
    clip_revision: field(source, 'clip_revision', integer),
    state: field(source, 'state', text),
    final_path: field(source, 'final_path', nullable(text)),
    error: field(source, 'error', nullable(parseCodeMessage)),
  };
}

/** An item as main answers: its measured duration is already `duration_ms`. */
const parseExportItem: Parse<ExportItemDto> = (value) => {
  const source = record(value);
  return {
    ...exportItemFields(source),
    duration_ms: field(source, 'duration_ms', nullable(integer)),
  };
};

/** `observed_duration_ms` of the backend's `observed` dump; null when it is not there. */
const parseObservedDuration: Parse<number | null> = (value) => {
  if (value === null) {
    return null;
  }
  const { observed_duration_ms: duration } = record(value);
  return typeof duration === 'number' && Number.isSafeInteger(duration) && duration >= 0
    ? duration
    : null;
};

/** An item as the backend describes it: only the measured duration of `observed` is kept. */
const parseBackendExportItem: Parse<ExportItemDto> = (value) => {
  const source = record(value);
  return {
    ...exportItemFields(source),
    duration_ms: field(source, 'observed', parseObservedDuration),
  };
};

function exportBatchParser(parseItem: Parse<ExportItemDto>): Parse<ExportBatchDto> {
  return (value) => {
    const source = record(value);
    return {
      id: field(source, 'id', text),
      project_id: field(source, 'project_id', text),
      job_id: field(source, 'job_id', nullable(text)),
      status: field(source, 'status', text),
      output_dir: field(source, 'output_dir', text),
      items: field(source, 'items', list(parseItem)),
      manifest_path: field(source, 'manifest_path', nullable(text)),
      catalog_path: field(source, 'catalog_path', nullable(text)),
      created_at: field(source, 'created_at', text),
    };
  };
}

export const parseExportBatch: Parse<ExportBatchDto> = exportBatchParser(parseExportItem);

/** A batch as the backend answers (items carry `observed`); main reduces it to `ExportBatchDto`. */
export const parseBackendExportBatch: Parse<ExportBatchDto> = exportBatchParser(parseBackendExportItem);

export const parseJob: Parse<JobDto> = (value) => {
  const source = record(value);
  return {
    id: field(source, 'id', text),
    type: field(source, 'type', text),
    state: field(source, 'state', text),
    progress: field(source, 'progress', record),
    error: field(source, 'error', nullable(parseCodeMessage)),
  };
};

const parsePreviewCreated: Parse<PreviewCreatedDto> = (value) => {
  const source = record(value);
  return {
    id: field(source, 'id', text),
    job_id: field(source, 'job_id', text),
    status: field(source, 'status', text),
  };
};

const parsePreview: Parse<PreviewDto> = (value) => {
  const source = record(value);
  return {
    id: field(source, 'id', text),
    source_id: field(source, 'source_id', text),
    audio_stream_index: field(source, 'audio_stream_index', nullable(integer)),
    status: field(source, 'status', text),
    job_id: field(source, 'job_id', text),
    error: field(source, 'error', nullable(parseCodeMessage)),
  };
};

const parseToolCheck: Parse<ToolCheckDto> = (value) => {
  const source = record(value);
  return {
    ok: field(source, 'ok', bool),
    version: field(source, 'version', nullable(text)),
  };
};

const parseDataDirCheck: Parse<DataDirCheckDto> = (value) => {
  const source = record(value);
  return {
    ok: field(source, 'ok', bool),
    free_bytes: field(source, 'free_bytes', nullable(count)),
  };
};

const parseSystemChecks: Parse<SystemChecksDto> = (value) => {
  const source = record(value);
  return {
    ffmpeg: field(source, 'ffmpeg', parseToolCheck),
    ffprobe: field(source, 'ffprobe', parseToolCheck),
    data_dir: field(source, 'data_dir', parseDataDirCheck),
  };
};

/** Parser of the `data` of a successful result, per op. */
export const API_DATA_PARSERS: { readonly [O in ApiOp]: Parse<ApiData[O]> } = {
  'projects.list': parsePage(parseProjectListItem),
  'projects.create': parseProject,
  'projects.update': parseProject,
  'sources.get': parseSource,
  'sources.list': parsePage(parseSource),
  'clips.list': parsePage(parseClip),
  'clips.create': parseClip,
  'clips.patch': parseClip,
  'clips.review': parseClip,
  'exports.create': parseExportCreated,
  'exports.get': parseExportBatch,
  'exports.retry': parseExportCreated,
  'jobs.get': parseJob,
  'jobs.cancel': parseJob,
  'previews.create': parsePreviewCreated,
  'previews.get': parsePreview,
  'system.checks': parseSystemChecks,
};
