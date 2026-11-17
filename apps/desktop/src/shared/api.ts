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
  'jobs.resume',
  'previews.create',
  'previews.get',
  'system.checks',
  'settings.get',
  'settings.update',
  'transcription.estimate',
  'transcription.create',
  'transcription.get',
  'transcription.list',
  'transcripts.list',
  'transcripts.get',
  'transcripts.correct',
  'analysis.estimate',
  'analysis.create',
  'analysis.get',
  'analysis.list',
  'credentials.check',
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
  /**
   * Continues an interrupted, failed or cancelled job (ADR 038). `retry_unknown` is the user's
   * explicit decision to send again the requests whose outcome is unknown; they may already have
   * been paid for. `budget_usd` replaces the run's ceiling (ADR 038, доповнення 2): a decimal string
   * under the rule of `run_budget_usd`. Nothing else is accepted.
   */
  readonly 'jobs.resume': {
    readonly id: string;
    readonly retry_unknown?: boolean;
    readonly budget_usd?: string;
  };
  readonly 'previews.create': {
    readonly source_id: string;
    /** Global source stream index of the audio to keep; null for a proxy without sound. */
    readonly audio_stream_index: number | null;
  };
  readonly 'previews.get': { readonly id: string };
  /** Readiness of FFmpeg, FFprobe and the data folder (ADR 023, ADR 024); takes no params. */
  readonly 'system.checks': Readonly<Record<string, never>>;
  /** The four non-secret settings and whether the sidecar holds a key (ADR 033); no params. */
  readonly 'settings.get': Readonly<Record<string, never>>;
  /**
   * Only the keys that are present change; `null` resets a setting to its default, except
   * `stt_model`, which has an explicit default and cannot be null. The values are checked by the
   * backend (422 with a code). The API key is not a setting: it goes through `setApiKey`.
   */
  readonly 'settings.update': {
    readonly stt_model?: string;
    readonly stt_language?: string | null;
    readonly analysis_model?: string | null;
    readonly run_budget_usd?: string | null;
  };
  /**
   * What a speech-recognition run of one audio track would cost (ADR 037, 038). The model, the
   * language and the budget are not params: the backend takes them from the settings. Nothing is
   * written or sent to the provider.
   */
  readonly 'transcription.estimate': {
    readonly source_id: string;
    /** Global source stream index of the audio track. */
    readonly audio_stream_index: number;
  };
  /**
   * Starts a run. `force` confirms a second, paid run over a transcript that already exists
   * (without it that is 409 TRANSCRIPT_ALREADY_EXISTS).
   */
  readonly 'transcription.create': {
    readonly source_id: string;
    readonly audio_stream_index: number;
    readonly force?: boolean;
  };
  readonly 'transcription.get': { readonly id: string };
  /** All runs of one source, the newest first. */
  readonly 'transcription.list': { readonly source_id: string };
  /**
   * The revisions of one source, oldest first (ADR 039). The renderer follows `next_cursor` until
   * it is null; the page size is the backend's.
   */
  readonly 'transcripts.list': { readonly source_id: string; readonly cursor?: string };
  /**
   * One revision with a window of its segments and words (ADR 030, 039). `offset` is 0 or more,
   * `limit` 1 to 1000 (the backend's default is 200).
   */
  readonly 'transcripts.get': {
    readonly id: string;
    readonly offset?: number;
    readonly limit?: number;
  };
  /**
   * A correction of text (FR-020, ADR 039): a new revision with the edits applied. Each edit names
   * one word or one segment of the revision, never both and never neither.
   */
  readonly 'transcripts.correct': { readonly id: string; readonly edits: readonly TranscriptEdit[] };
  /**
   * What an AI selection run over one transcript would cost (ADR 041 §10, §21). The model and the
   * budget are not params: the backend takes them from the settings. Nothing is written or sent.
   */
  readonly 'analysis.estimate': AnalysisRunParams;
  /**
   * Starts a run (202 with its job). The model and the budget come from the settings, as for a
   * transcription run; the budget of a started run is changed later through `jobs.resume`.
   */
  readonly 'analysis.create': AnalysisRunParams;
  readonly 'analysis.get': { readonly id: string };
  /** All runs of one source, the newest first. */
  readonly 'analysis.list': { readonly source_id: string };
  /**
   * Checks the key that the sidecar holds, with OpenRouter (ADR 039). Takes no params: the key
   * never leaves the sidecar and is not in any op.
   */
  readonly 'credentials.check': Readonly<Record<string, never>>;
}

/** One correction: the ID of a word or of a segment, and the text it gets. */
export type TranscriptEdit =
  | { readonly word_id: string; readonly text: string }
  | { readonly segment_id: string; readonly text: string };

/**
 * The search profile of an analysis run (ADR 041 §10): exactly these six keys. The renderer sends
 * them as typed; the backend checks the values and the bounds (422 names `profile.<field>`).
 */
export interface AnalysisProfile {
  /** Categories of the closed set; the backend asks for 1 to 5, unique. */
  readonly categories: readonly string[];
  /** Kept as typed and passed to the model as data only. */
  readonly custom_instruction: string;
  readonly min_duration_ms: number;
  readonly max_duration_ms: number;
  readonly max_candidates: number;
  readonly allow_partial: boolean;
}

/** Params of `analysis.estimate` and `analysis.create`: one transcript of a source, and the profile. */
export interface AnalysisRunParams {
  readonly source_id: string;
  readonly transcript_id: string;
  readonly profile: AnalysisProfile;
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
  /**
   * Derived, never stored (ADR 041 §11): the clip's run read an older transcript revision than the
   * newest one of its source. Required: a clip answer without it is malformed.
   */
  readonly analysis_stale: boolean;
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

/**
 * The settings of GET and PATCH /v1/settings (ADR 033). `has_api_key` only says that the sidecar
 * holds a key; no answer ever carries the key itself.
 */
export interface SettingsDto {
  readonly stt_model: string;
  readonly stt_language: string | null;
  readonly analysis_model: string | null;
  /** A decimal string such as "2.50", kept as the user wrote it; null: no ceiling is set. */
  readonly run_budget_usd: string | null;
  readonly has_api_key: boolean;
}

/** The transcript a run would repeat: an earlier completed run with the same model and options. */
export interface ExistingTranscriptDto {
  readonly transcript_id: string;
  readonly run_id: string;
}

/**
 * The answer of POST /v1/transcription-runs/estimate (ADR 037). The sum is an upper estimate of
 * one attempt per part; retries and splits can add to it, so it is not a bound on the bill.
 */
export interface TranscriptionEstimateDto {
  readonly source_id: string;
  readonly audio_stream_index: number;
  readonly model: string;
  /** null: the language is detected by the provider. */
  readonly language: string | null;
  readonly duration_ms: number;
  readonly chunk_count: number;
  readonly billed_seconds: number;
  /** A decimal string: the highest price of one billed second. */
  readonly usd_per_second_upper: string;
  readonly estimate_micro_usd: number;
  readonly price_basis: string;
  /** A decimal string such as "2.50"; null: no ceiling is set in the settings. */
  readonly budget_usd: string | null;
  /** null when there is no budget to compare with. */
  readonly within_budget: boolean | null;
  readonly existing: ExistingTranscriptDto | null;
}

export interface TranscriptionCreatedDto {
  readonly id: string;
  readonly job_id: string;
  readonly status: string;
}

export interface TranscriptionChunkDto {
  readonly id: string;
  readonly core_start_ms: number;
  readonly core_end_ms: number;
  /**
   * pending, running, succeeded, failed, cancelled, unknown_outcome or superseded (split in two
   * parts); kept as a string, like the other states, so a new one does not break the answer.
   */
  readonly state: string;
  readonly attempts: number;
  readonly error_code: string | null;
}

/** A run in a list: its figures without the parts. */
export interface TranscriptionRunItemDto {
  readonly id: string;
  readonly source_id: string;
  readonly job_id: string | null;
  readonly model: string;
  readonly status: string;
  /** The transcript revision the run produced; null until it is complete. */
  readonly revision_id: string | null;
  readonly error_code: string | null;
  readonly committed_micro_usd: number;
  readonly created_at: string;
}

export interface TranscriptionRunDto {
  readonly id: string;
  readonly source_id: string;
  readonly job_id: string | null;
  readonly model: string;
  readonly status: string;
  readonly revision_id: string | null;
  readonly error_code: string | null;
  readonly committed_micro_usd: number;
  readonly chunks: readonly TranscriptionChunkDto[];
}

/** GET /v1/transcription-runs has no pages: the runs of one source are few. */
export interface TranscriptionRunListDto {
  readonly items: readonly TranscriptionRunItemDto[];
}

/**
 * The answer of POST /v1/analysis-runs/estimate (ADR 041 §10). `estimate_micro_usd` is the sum of
 * the first requests' reserves: an upper estimate, not a bound on the bill. The prices are the
 * decimal strings the backend sends as they are.
 */
export interface AnalysisEstimateDto {
  readonly model: string;
  readonly unit_level: string;
  readonly unit_count: number;
  readonly window_count: number;
  readonly estimate_micro_usd: number;
  readonly prompt_usd_per_mtok: string;
  readonly completion_usd_per_mtok: string;
  readonly max_tokens: number;
  readonly price_basis: string;
  /** A decimal string such as "2.50"; null: no ceiling is set in the settings. */
  readonly budget_usd: string | null;
  /** null when there is no budget to compare with. */
  readonly within_budget: boolean | null;
}

/** The answer of POST /v1/analysis-runs (202): the run and its job. */
export interface AnalysisRunCreatedDto {
  readonly id: string;
  readonly job_id: string;
  readonly status: string;
}

/** The windows of a run by state. `pending` counts the windows that are running or cancelled too. */
export interface AnalysisWindowCountsDto {
  readonly total: number;
  readonly succeeded: number;
  readonly failed: number;
  readonly unknown_outcome: number;
  readonly pending: number;
}

/**
 * One run (ADR 041 §10). `stale`: the run read an older transcript revision than the newest one of
 * its source. `diagnostics_summary`: section, then reason, then count (ADR 041 §17).
 */
export interface AnalysisRunDto {
  readonly id: string;
  readonly source_id: string;
  readonly transcript_id: string;
  readonly transcript_revision: number;
  readonly model: string;
  readonly provider: string;
  readonly prompt_version: string;
  readonly schema_version: number;
  readonly unit_level: string;
  readonly status: string;
  readonly error_code: string | null;
  readonly created_at: string;
  readonly updated_at: string;
  readonly job_id: string | null;
  readonly committed_micro_usd: number;
  readonly windows: AnalysisWindowCountsDto;
  /** The clips the run made, in the order the aggregation inserted them. */
  readonly clip_ids: readonly string[];
  readonly stale: boolean;
  readonly diagnostics_summary: Readonly<Record<string, Readonly<Record<string, number>>>>;
}

/** A run in a list: its figures without the windows, the clips and the diagnostics. */
export interface AnalysisRunItemDto {
  readonly id: string;
  readonly source_id: string;
  readonly transcript_id: string;
  readonly transcript_revision: number;
  readonly model: string;
  readonly status: string;
  readonly error_code: string | null;
  readonly job_id: string | null;
  readonly committed_micro_usd: number;
  readonly created_at: string;
}

/** GET /v1/analysis-runs has no pages: the runs of one source are few. */
export interface AnalysisRunListDto {
  readonly items: readonly AnalysisRunItemDto[];
}

/** A span of the source's time with the coverage state of the transcript (ADR 030). */
export interface TranscriptCoverageSpanDto {
  readonly start_ms: number;
  readonly end_ms: number;
  /** succeeded or another state of the backend; kept as a string. */
  readonly state: string;
}

/**
 * One revision of a source's transcript (ADR 030, 039). `id` names the revision; `revision` counts
 * the source's revisions from 1. The backend's `metadata` is not in this DTO.
 */
export interface TranscriptDto {
  readonly id: string;
  readonly source_id: string;
  readonly revision: number;
  /** import, stt or correction. */
  readonly origin: string;
  readonly run_id: string | null;
  readonly parent_id: string | null;
  /** word or segment: whether the words of the revision carry the timing. */
  readonly timing_level: string;
  readonly language: string;
  readonly duration_ms: number;
  readonly word_count: number;
  readonly segment_count: number;
  readonly coverage: readonly TranscriptCoverageSpanDto[];
  readonly created_at: string;
}

export interface TranscriptSegmentDto {
  readonly id: string;
  readonly ordinal: number;
  readonly start_ms: number;
  readonly end_ms: number;
  readonly text: string;
  /** The IDs of the words the segment is made of; empty for a segment-level revision. */
  readonly word_ids: readonly string[];
  readonly timing_level: string;
}

export interface TranscriptWordDto {
  readonly id: string;
  readonly ordinal: number;
  readonly start_ms: number;
  readonly end_ms: number;
  readonly text: string;
  readonly chunk_id: string | null;
  readonly speaker_local: string | null;
  /** The provider's own figure; not a calibrated probability (CLAUDE.md). */
  readonly confidence: number | null;
}

/**
 * A revision with one window of its segments and words. `next_offset` is the offset of the next
 * window, or null when neither list has more.
 */
export interface TranscriptDetailDto extends TranscriptDto {
  readonly offset: number;
  readonly limit: number;
  readonly next_offset: number | null;
  readonly segments: readonly TranscriptSegmentDto[];
  readonly words: readonly TranscriptWordDto[];
}

/**
 * The answer of the key check (ADR 039). `valid` also says whether the account is on the free tier
 * and what is left of its limit: null when there is no limit. `invalid` means OpenRouter refused
 * the key; nothing else is said.
 */
export type CredentialCheckDto =
  | {
      readonly state: 'valid';
      readonly is_free_tier: boolean;
      readonly limit_remaining_usd: string | null;
    }
  | { readonly state: 'invalid' };

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
  readonly 'jobs.resume': JobDto;
  readonly 'previews.create': PreviewCreatedDto;
  readonly 'previews.get': PreviewDto;
  readonly 'system.checks': SystemChecksDto;
  readonly 'settings.get': SettingsDto;
  readonly 'settings.update': SettingsDto;
  readonly 'transcription.estimate': TranscriptionEstimateDto;
  readonly 'transcription.create': TranscriptionCreatedDto;
  readonly 'transcription.get': TranscriptionRunDto;
  readonly 'transcription.list': TranscriptionRunListDto;
  readonly 'transcripts.list': Page<TranscriptDto>;
  readonly 'transcripts.get': TranscriptDetailDto;
  readonly 'transcripts.correct': TranscriptDto;
  readonly 'analysis.estimate': AnalysisEstimateDto;
  readonly 'analysis.create': AnalysisRunCreatedDto;
  readonly 'analysis.get': AnalysisRunDto;
  readonly 'analysis.list': AnalysisRunListDto;
  readonly 'credentials.check': CredentialCheckDto;
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
    analysis_stale: field(source, 'analysis_stale', bool),
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

/** Exactly the five fields of the settings answer; any other key is dropped. */
export const parseSettings: Parse<SettingsDto> = (value) => {
  const source = record(value);
  return {
    stt_model: field(source, 'stt_model', text),
    stt_language: field(source, 'stt_language', nullable(text)),
    analysis_model: field(source, 'analysis_model', nullable(text)),
    run_budget_usd: field(source, 'run_budget_usd', nullable(text)),
    has_api_key: field(source, 'has_api_key', bool),
  };
};

const parseExistingTranscript: Parse<ExistingTranscriptDto> = (value) => {
  const source = record(value);
  return {
    transcript_id: field(source, 'transcript_id', text),
    run_id: field(source, 'run_id', text),
  };
};

const parseTranscriptionEstimate: Parse<TranscriptionEstimateDto> = (value) => {
  const source = record(value);
  return {
    source_id: field(source, 'source_id', text),
    audio_stream_index: field(source, 'audio_stream_index', integer),
    model: field(source, 'model', text),
    language: field(source, 'language', nullable(text)),
    duration_ms: field(source, 'duration_ms', count),
    chunk_count: field(source, 'chunk_count', count),
    billed_seconds: field(source, 'billed_seconds', count),
    usd_per_second_upper: field(source, 'usd_per_second_upper', text),
    estimate_micro_usd: field(source, 'estimate_micro_usd', count),
    price_basis: field(source, 'price_basis', text),
    budget_usd: field(source, 'budget_usd', nullable(text)),
    within_budget: field(source, 'within_budget', nullable(bool)),
    existing: field(source, 'existing', nullable(parseExistingTranscript)),
  };
};

const parseTranscriptionCreated: Parse<TranscriptionCreatedDto> = (value) => {
  const source = record(value);
  return {
    id: field(source, 'id', text),
    job_id: field(source, 'job_id', text),
    status: field(source, 'status', text),
  };
};

const parseTranscriptionChunk: Parse<TranscriptionChunkDto> = (value) => {
  const source = record(value);
  return {
    id: field(source, 'id', text),
    core_start_ms: field(source, 'core_start_ms', count),
    core_end_ms: field(source, 'core_end_ms', count),
    state: field(source, 'state', text),
    attempts: field(source, 'attempts', count),
    error_code: field(source, 'error_code', nullable(text)),
  };
};

/** The fields a run has in a list and in its own answer. */
function transcriptionRunFields(source: Fields): Omit<TranscriptionRunItemDto, 'created_at'> {
  return {
    id: field(source, 'id', text),
    source_id: field(source, 'source_id', text),
    job_id: field(source, 'job_id', nullable(text)),
    model: field(source, 'model', text),
    status: field(source, 'status', text),
    revision_id: field(source, 'revision_id', nullable(text)),
    error_code: field(source, 'error_code', nullable(text)),
    committed_micro_usd: field(source, 'committed_micro_usd', count),
  };
}

const parseTranscriptionRun: Parse<TranscriptionRunDto> = (value) => {
  const source = record(value);
  return {
    ...transcriptionRunFields(source),
    chunks: field(source, 'chunks', list(parseTranscriptionChunk)),
  };
};

const parseTranscriptionRunItem: Parse<TranscriptionRunItemDto> = (value) => {
  const source = record(value);
  return {
    ...transcriptionRunFields(source),
    created_at: field(source, 'created_at', text),
  };
};

const parseTranscriptionRunList: Parse<TranscriptionRunListDto> = (value) => ({
  items: field(record(value), 'items', list(parseTranscriptionRunItem)),
});

const parseCoverageSpan: Parse<TranscriptCoverageSpanDto> = (value) => {
  const source = record(value);
  return {
    start_ms: field(source, 'start_ms', integer),
    end_ms: field(source, 'end_ms', integer),
    state: field(source, 'state', text),
  };
};

/**
 * A revision as the backend answers. Only the fields of `TranscriptDto` are kept: the backend's
 * `metadata` and any other key are dropped.
 */
export const parseTranscript: Parse<TranscriptDto> = (value) => {
  const source = record(value);
  return {
    id: field(source, 'id', text),
    source_id: field(source, 'source_id', text),
    revision: field(source, 'revision', count),
    origin: field(source, 'origin', text),
    run_id: field(source, 'run_id', nullable(text)),
    parent_id: field(source, 'parent_id', nullable(text)),
    timing_level: field(source, 'timing_level', text),
    language: field(source, 'language', text),
    duration_ms: field(source, 'duration_ms', count),
    word_count: field(source, 'word_count', count),
    segment_count: field(source, 'segment_count', count),
    coverage: field(source, 'coverage', list(parseCoverageSpan)),
    created_at: field(source, 'created_at', text),
  };
};

const parseSegment: Parse<TranscriptSegmentDto> = (value) => {
  const source = record(value);
  return {
    id: field(source, 'id', text),
    ordinal: field(source, 'ordinal', count),
    start_ms: field(source, 'start_ms', integer),
    end_ms: field(source, 'end_ms', integer),
    text: field(source, 'text', text),
    word_ids: field(source, 'word_ids', list(text)),
    timing_level: field(source, 'timing_level', text),
  };
};

const parseConfidence: Parse<number> = (value) =>
  typeof value === 'number' && Number.isFinite(value) ? value : bad();

const parseWord: Parse<TranscriptWordDto> = (value) => {
  const source = record(value);
  return {
    id: field(source, 'id', text),
    ordinal: field(source, 'ordinal', count),
    start_ms: field(source, 'start_ms', integer),
    end_ms: field(source, 'end_ms', integer),
    text: field(source, 'text', text),
    chunk_id: field(source, 'chunk_id', nullable(text)),
    speaker_local: field(source, 'speaker_local', nullable(text)),
    confidence: field(source, 'confidence', nullable(parseConfidence)),
  };
};

/** A window of a revision: the revision fields plus `offset`, `limit`, `next_offset` and the items. */
const parseTranscriptDetail: Parse<TranscriptDetailDto> = (value) => {
  const source = record(value);
  return {
    ...parseTranscript(source),
    offset: field(source, 'offset', count),
    limit: field(source, 'limit', count),
    next_offset: field(source, 'next_offset', nullable(count)),
    segments: field(source, 'segments', list(parseSegment)),
    words: field(source, 'words', list(parseWord)),
  };
};

const parseCredentialState: Parse<'valid' | 'invalid'> = (value) =>
  value === 'valid' || value === 'invalid' ? value : bad();

/** A plain decimal string: an optional minus, digits, an optional fraction. No exponent, plus or spaces. */
const DECIMAL_TEXT = /^-?\d+(\.\d+)?$/;
const decimalText: Parse<string> = (value) =>
  typeof value === 'string' && DECIMAL_TEXT.test(value) ? value : bad();

/**
 * The key check answer. `invalid` keeps no other field; `valid` needs its free-tier flag and its
 * remaining limit (null when there is none, else a plain decimal string). Any other key is dropped.
 */
const parseCredentialCheck: Parse<CredentialCheckDto> = (value) => {
  const source = record(value);
  if (field(source, 'state', parseCredentialState) === 'invalid') {
    return { state: 'invalid' };
  }
  return {
    state: 'valid',
    is_free_tier: field(source, 'is_free_tier', bool),
    limit_remaining_usd: field(source, 'limit_remaining_usd', nullable(decimalText)),
  };
};

const parseAnalysisEstimate: Parse<AnalysisEstimateDto> = (value) => {
  const source = record(value);
  return {
    model: field(source, 'model', text),
    unit_level: field(source, 'unit_level', text),
    unit_count: field(source, 'unit_count', count),
    window_count: field(source, 'window_count', count),
    estimate_micro_usd: field(source, 'estimate_micro_usd', count),
    prompt_usd_per_mtok: field(source, 'prompt_usd_per_mtok', text),
    completion_usd_per_mtok: field(source, 'completion_usd_per_mtok', text),
    max_tokens: field(source, 'max_tokens', count),
    price_basis: field(source, 'price_basis', text),
    budget_usd: field(source, 'budget_usd', nullable(text)),
    within_budget: field(source, 'within_budget', nullable(bool)),
  };
};

const parseAnalysisRunCreated: Parse<AnalysisRunCreatedDto> = (value) => {
  const source = record(value);
  return {
    id: field(source, 'id', text),
    job_id: field(source, 'job_id', text),
    status: field(source, 'status', text),
  };
};

const parseWindowCounts: Parse<AnalysisWindowCountsDto> = (value) => {
  const source = record(value);
  return {
    total: field(source, 'total', count),
    succeeded: field(source, 'succeeded', count),
    failed: field(source, 'failed', count),
    unknown_outcome: field(source, 'unknown_outcome', count),
    pending: field(source, 'pending', count),
  };
};

/** A map of whole, non-negative counts. Built with `fromEntries`, so a key such as `__proto__` stays a key. */
const countsOf: Parse<Readonly<Record<string, number>>> = (value) =>
  Object.fromEntries(
    Object.entries(record(value)).map(([reason, n]): [string, number] => [reason, count(n)]),
  );

/** `diagnostics_summary`: section, then reason, then count (ADR 041 §17). */
const parseDiagnosticsSummary: Parse<
  Readonly<Record<string, Readonly<Record<string, number>>>>
> = (value) =>
  Object.fromEntries(
    Object.entries(record(value)).map(
      ([section, reasons]): [string, Readonly<Record<string, number>>] => [
        section,
        countsOf(reasons),
      ],
    ),
  );

const parseAnalysisRun: Parse<AnalysisRunDto> = (value) => {
  const source = record(value);
  return {
    id: field(source, 'id', text),
    source_id: field(source, 'source_id', text),
    transcript_id: field(source, 'transcript_id', text),
    transcript_revision: field(source, 'transcript_revision', count),
    model: field(source, 'model', text),
    provider: field(source, 'provider', text),
    prompt_version: field(source, 'prompt_version', text),
    schema_version: field(source, 'schema_version', count),
    unit_level: field(source, 'unit_level', text),
    status: field(source, 'status', text),
    error_code: field(source, 'error_code', nullable(text)),
    created_at: field(source, 'created_at', text),
    updated_at: field(source, 'updated_at', text),
    job_id: field(source, 'job_id', nullable(text)),
    committed_micro_usd: field(source, 'committed_micro_usd', count),
    windows: field(source, 'windows', parseWindowCounts),
    clip_ids: field(source, 'clip_ids', list(text)),
    stale: field(source, 'stale', bool),
    diagnostics_summary: field(source, 'diagnostics_summary', parseDiagnosticsSummary),
  };
};

const parseAnalysisRunItem: Parse<AnalysisRunItemDto> = (value) => {
  const source = record(value);
  return {
    id: field(source, 'id', text),
    source_id: field(source, 'source_id', text),
    transcript_id: field(source, 'transcript_id', text),
    transcript_revision: field(source, 'transcript_revision', count),
    model: field(source, 'model', text),
    status: field(source, 'status', text),
    error_code: field(source, 'error_code', nullable(text)),
    job_id: field(source, 'job_id', nullable(text)),
    committed_micro_usd: field(source, 'committed_micro_usd', count),
    created_at: field(source, 'created_at', text),
  };
};

const parseAnalysisRunList: Parse<AnalysisRunListDto> = (value) => ({
  items: field(record(value), 'items', list(parseAnalysisRunItem)),
});

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
  'jobs.resume': parseJob,
  'previews.create': parsePreviewCreated,
  'previews.get': parsePreview,
  'system.checks': parseSystemChecks,
  'settings.get': parseSettings,
  'settings.update': parseSettings,
  'transcription.estimate': parseTranscriptionEstimate,
  'transcription.create': parseTranscriptionCreated,
  'transcription.get': parseTranscriptionRun,
  'transcription.list': parseTranscriptionRunList,
  'transcripts.list': parsePage(parseTranscript),
  'transcripts.get': parseTranscriptDetail,
  'transcripts.correct': parseTranscript,
  'analysis.estimate': parseAnalysisEstimate,
  'analysis.create': parseAnalysisRunCreated,
  'analysis.get': parseAnalysisRun,
  'analysis.list': parseAnalysisRunList,
  'credentials.check': parseCredentialCheck,
};
