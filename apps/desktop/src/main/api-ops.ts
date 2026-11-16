import {
  PROJECT_SEARCH_MAX_LENGTH,
  REVIEW_STATUSES,
  isFolderToken,
  isUuid4,
  isVideoToken,
} from '../shared/api';
import type { ApiOp, ApiParams, ExportProfile, ReviewStatus, TranscriptEdit } from '../shared/api';
import type { ImportTranscriptParams, ImportVideoParams, OpenFolderParams } from '../shared/bridge';
import type { ApiMethod } from './sidecar-protocol';

/**
 * The fixed table behind `api:call`: every allowed op, how its params are
 * shape-checked, and the one method and path it maps to. Nothing here accepts a
 * path, URL, header or `output_dir` from the renderer, and every ID that ends up
 * in a URL has passed the uuid4 check. The op itself is looked up only after
 * `isApiOp`, never as a property of renderer-controlled data.
 *
 * Checks are about shape only (types, IDs, sizes); domain rules stay with the
 * backend, which answers 422 with a code. The numbers named in ADR 038 and 039
 * (window `limit` 1–1000, `offset` ≥ 0, 1–500 edits, the run budget format) are
 * checked here as well, so an out-of-range value never reaches the sidecar.
 */

/** A call the gateway will make on the sidecar. */
export interface ApiPlan {
  readonly method: ApiMethod;
  readonly path: string;
  readonly body?: Readonly<Record<string, unknown>>;
  /** exports.create only: main swaps it for the folder it picked (`output_dir`). */
  readonly folderToken?: string;
  /**
   * exports.retry only: this plan is the first of two steps. Main reads the batch it names
   * and then posts a new batch for the failed and cancelled clips (see `ApiGateway`).
   */
  readonly retry?: true;
}

/** Thrown for any param that is not of the expected shape; carries no input. */
export class ParamError extends Error {
  constructor() {
    super('Некоректні параметри запиту');
    this.name = 'ParamError';
  }
}

const MAX_TEXT_LENGTH = 1_000;
const MAX_LIST_ITEMS = 1_000;
/** GET /v1/sources accepts limit 1..100 (ADR 023). */
const SOURCES_PAGE_SIZE = 100;
/** Window of GET /v1/transcripts/{id}: `offset` 0 or more, `limit` 1..1000 (ADR 030, 039). */
const MAX_WINDOW_OFFSET = 2 ** 31 - 1;
const MAX_WINDOW_LIMIT = 1_000;
/** POST /v1/transcripts/{id}/corrections takes 1..500 edits (FR-020, ADR 039). */
const MAX_TRANSCRIPT_EDITS = 500;
/** A word or segment ID of a transcript is at most 2000 characters (contracts/transcript.schema.json). */
const MAX_TRANSCRIPT_ITEM_ID = 2_000;
/** The run budget as the settings check it (ADR 033): 0.01 to 1000 USD. */
const RUN_BUDGET_MIN_USD = 0.01;
const RUN_BUDGET_MAX_USD = 1_000;
const RUN_BUDGET_PATTERN = /^(?:0|[1-9][0-9]{0,3})(?:\.[0-9]{1,6})?$/;

type Check<T> = (value: unknown) => T;

function invalid(): never {
  throw new ParamError();
}

function plain(value: unknown): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return invalid();
  }
  const prototype: unknown = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null
    ? (value as Record<string, unknown>)
    : invalid();
}

/**
 * A plain object with no key outside `required` + `optional` and every
 * `required` one present. `undefined` counts as absent.
 */
function exact(
  value: unknown,
  required: readonly string[],
  optional: readonly string[] = [],
): Record<string, unknown> {
  const source = plain(value);
  for (const key of Object.keys(source)) {
    if (!required.includes(key) && !optional.includes(key)) {
      return invalid();
    }
  }
  for (const key of required) {
    if (!Object.hasOwn(source, key) || source[key] === undefined) {
      return invalid();
    }
  }
  return source;
}

const uuid: Check<string> = (value) => (isUuid4(value) ? value : invalid());
const folderToken: Check<string> = (value) => (isFolderToken(value) ? value : invalid());
const videoToken: Check<string> = (value) => (isVideoToken(value) ? value : invalid());
const bool: Check<boolean> = (value) => (typeof value === 'boolean' ? value : invalid());
const int: Check<number> = (value) =>
  typeof value === 'number' && Number.isSafeInteger(value) ? value : invalid();
const str: Check<string> = (value) =>
  typeof value === 'string' && value.length <= MAX_TEXT_LENGTH ? value : invalid();
const reviewStatus: Check<ReviewStatus> = (value) =>
  REVIEW_STATUSES.find((status) => status === value) ?? invalid();

/** An integer from `min` to `max`, both included; a float, a numeric string or null is refused. */
function intIn(min: number, max: number): Check<number> {
  return (value) =>
    typeof value === 'number' && Number.isSafeInteger(value) && value >= min && value <= max
      ? value
      : invalid();
}

const windowOffset = intIn(0, MAX_WINDOW_OFFSET);
const windowLimit = intIn(1, MAX_WINDOW_LIMIT);

/** The 32-bit range of `offset_ms` of POST /v1/transcripts/import (ADR 039). */
const MAX_TRANSCRIPT_OFFSET_MS = 2 ** 31 - 1;
const transcriptOffsetMs = intIn(-MAX_TRANSCRIPT_OFFSET_MS, MAX_TRANSCRIPT_OFFSET_MS);

/**
 * A run budget in the form of the settings' `run_budget_usd` (ADR 033, 038): a decimal string with
 * at most six digits after the point, from 0.01 to 1000. Not a number, and not null: an absent
 * budget is a missing key, not a null. The bounds are compared as doubles, which is exact enough
 * here: a string with at most six decimals is never closer to a bound than the rounding error.
 */
const runBudget: Check<string> = (value) => {
  if (typeof value !== 'string' || !RUN_BUDGET_PATTERN.test(value)) {
    return invalid();
  }
  const amount = Number(value);
  return amount >= RUN_BUDGET_MIN_USD && amount <= RUN_BUDGET_MAX_USD ? value : invalid();
};

/**
 * A word or segment ID of a transcript. These are the document's own IDs (a normalized import keeps
 * them, ADR 030), not uuid4s, so the check is only: a non-empty string of at most 2000 characters.
 * The UTF-16 length is checked first, so a huge string is not split into code points.
 */
const transcriptItemId: Check<string> = (value) =>
  typeof value === 'string' &&
  value.length > 0 &&
  value.length <= MAX_TRANSCRIPT_ITEM_ID * 2 &&
  Array.from(value).length <= MAX_TRANSCRIPT_ITEM_ID
    ? value
    : invalid();

/** The text of an edit: a string that is not blank after trim. Its length is the backend's to check. */
const editText: Check<string> = (value) =>
  typeof value === 'string' && value.trim() !== '' ? value : invalid();

/**
 * One edit, exactly one of `word_id` and `segment_id` with its `text`, and no other key. An
 * absent key is `undefined`; a `null` ID is refused like any other non-string.
 */
function transcriptEdit(value: unknown): TranscriptEdit {
  const source = exact(value, ['text'], ['word_id', 'segment_id']);
  const byWord = source.word_id !== undefined;
  if (byWord === (source.segment_id !== undefined)) {
    return invalid();
  }
  const text = editText(source.text);
  return byWord
    ? { word_id: transcriptItemId(source.word_id), text }
    : { segment_id: transcriptItemId(source.segment_id), text };
}

/** 1 to 500 edits, each checked by `transcriptEdit`. A hole in the array is an invalid edit. */
const transcriptEdits: Check<TranscriptEdit[]> = (value) =>
  Array.isArray(value) && value.length >= 1 && value.length <= MAX_TRANSCRIPT_EDITS
    ? Array.from(value, (edit: unknown) => transcriptEdit(edit))
    : invalid();
/**
 * The project search text: at most 200 characters. They are counted as code points, like the
 * backend's `max_length`; the cheap UTF-16 bound first keeps a huge string from being split.
 */
const searchText: Check<string> = (value) =>
  typeof value === 'string' &&
  value.length <= PROJECT_SEARCH_MAX_LENGTH * 2 &&
  Array.from(value).length <= PROJECT_SEARCH_MAX_LENGTH
    ? value
    : invalid();

function listOf<T>(check: Check<T>, minItems = 0): Check<T[]> {
  return (value) =>
    Array.isArray(value) && value.length >= minItems && value.length <= MAX_LIST_ITEMS
      ? value.map((item: unknown) => check(item))
      : invalid();
}

function optional<T>(value: unknown, check: Check<T>): T | undefined {
  return value === undefined ? undefined : check(value);
}

function nullable<T>(check: Check<T>): Check<T | null> {
  return (value) => (value === null ? null : check(value));
}

function defined(fields: Readonly<Record<string, unknown>>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(fields).filter(([, value]) => value !== undefined));
}

/** Query string from validated primitives; values are encoded, never concatenated raw. */
function withQuery(
  path: string,
  query: Readonly<Record<string, string | number | boolean | undefined>>,
): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined) {
      params.set(key, String(value));
    }
  }
  const encoded = params.toString();
  return encoded === '' ? path : `${path}?${encoded}`;
}

/** Also used on the profile of a batch the backend recorded (`exports.retry`). */
export function parseProfile(value: unknown): ExportProfile {
  const source = exact(value, [
    'video_codec',
    'quality',
    'audio_mode',
    'audio_stream_index',
    'write_srt',
  ]);
  return {
    video_codec: str(source.video_codec),
    quality: str(source.quality),
    audio_mode: str(source.audio_mode),
    audio_stream_index: nullable(int)(source.audio_stream_index),
    write_srt: bool(source.write_srt),
  };
}

type Builder = (params: unknown) => ApiPlan;

/** Binds a param parser to a planner; `P` ties the parser to `ApiParams[op]`. */
function define<P>(parse: Check<P>, plan: (params: P) => ApiPlan): Builder {
  return (params) => plan(parse(params));
}

const BUILDERS: Readonly<Record<ApiOp, Builder>> = {
  'projects.list': define<ApiParams['projects.list']>(
    (raw) => {
      const p = exact(raw, [], ['limit', 'cursor', 'include_archived', 'q']);
      return {
        limit: optional(p.limit, int),
        cursor: optional(p.cursor, str),
        include_archived: optional(p.include_archived, bool),
        q: optional(p.q, searchText),
      };
    },
    (p) => ({ method: 'GET', path: withQuery('/v1/projects', p) }),
  ),

  'projects.create': define<ApiParams['projects.create']>(
    (raw) => ({ name: str(exact(raw, ['name']).name) }),
    (p) => ({ method: 'POST', path: '/v1/projects', body: { name: p.name } }),
  ),

  'projects.update': define<ApiParams['projects.update']>(
    (raw) => {
      const p = exact(raw, ['id'], ['name', 'archived']);
      return {
        id: uuid(p.id),
        name: optional(p.name, str),
        archived: optional(p.archived, bool),
      };
    },
    (p) => ({
      method: 'PATCH',
      path: `/v1/projects/${p.id}`,
      body: defined({ name: p.name, archived: p.archived }),
    }),
  ),

  'sources.get': define<ApiParams['sources.get']>(
    (raw) => ({ id: uuid(exact(raw, ['id']).id) }),
    (p) => ({ method: 'GET', path: `/v1/sources/${p.id}` }),
  ),

  // ADR 023: the sources of one project, always the backend's largest page. The renderer
  // cannot choose the size (it is not a param), only follow the opaque cursor.
  'sources.list': define<ApiParams['sources.list']>(
    (raw) => {
      const p = exact(raw, ['project_id'], ['cursor']);
      return { project_id: uuid(p.project_id), cursor: optional(p.cursor, str) };
    },
    (p) => ({
      method: 'GET',
      path: withQuery('/v1/sources', {
        project_id: p.project_id,
        limit: SOURCES_PAGE_SIZE,
        cursor: p.cursor,
      }),
    }),
  ),

  'clips.list': define<ApiParams['clips.list']>(
    (raw) => {
      const p = exact(raw, [], ['source_id', 'review_status', 'category', 'limit', 'cursor']);
      return {
        source_id: optional(p.source_id, uuid),
        review_status: optional(p.review_status, reviewStatus),
        category: optional(p.category, str),
        limit: optional(p.limit, int),
        cursor: optional(p.cursor, str),
      };
    },
    (p) => ({ method: 'GET', path: withQuery('/v1/clips', p) }),
  ),

  'clips.create': define<ApiParams['clips.create']>(
    (raw) => {
      const p = exact(raw, ['source_id', 'title', 'start_ms', 'end_ms', 'categories']);
      return {
        source_id: uuid(p.source_id),
        title: str(p.title),
        start_ms: int(p.start_ms),
        end_ms: int(p.end_ms),
        categories: listOf(str)(p.categories),
      };
    },
    (p) => ({
      method: 'POST',
      path: '/v1/clips',
      // Set here, never taken from the renderer: only manual clips are created this way.
      body: { ...p, evidence_type: 'manual' },
    }),
  ),

  'clips.patch': define<ApiParams['clips.patch']>(
    (raw) => {
      const p = exact(
        raw,
        ['id', 'expected_revision'],
        ['title', 'start_ms', 'end_ms', 'categories'],
      );
      return {
        id: uuid(p.id),
        expected_revision: int(p.expected_revision),
        title: optional(p.title, str),
        start_ms: optional(p.start_ms, int),
        end_ms: optional(p.end_ms, int),
        categories: optional(p.categories, listOf(str)),
      };
    },
    ({ id, ...edit }) => ({
      method: 'PATCH',
      path: `/v1/clips/${id}`,
      body: defined(edit),
    }),
  ),

  'clips.review': define<ApiParams['clips.review']>(
    (raw) => {
      const p = exact(raw, ['id', 'expected_revision', 'status']);
      return {
        id: uuid(p.id),
        expected_revision: int(p.expected_revision),
        status: reviewStatus(p.status),
      };
    },
    (p) => ({
      method: 'POST',
      path: `/v1/clips/${p.id}/review`,
      body: { expected_revision: p.expected_revision, status: p.status },
    }),
  ),

  'exports.create': define<ApiParams['exports.create']>(
    (raw) => {
      const p = exact(raw, ['project_id', 'clip_ids', 'folderToken', 'profile']);
      return {
        project_id: uuid(p.project_id),
        clip_ids: listOf(uuid, 1)(p.clip_ids),
        folderToken: folderToken(p.folderToken),
        profile: parseProfile(p.profile),
      };
    },
    (p) => ({
      method: 'POST',
      path: '/v1/export-batches',
      body: { project_id: p.project_id, clip_ids: p.clip_ids, profile: p.profile },
      folderToken: p.folderToken,
    }),
  ),

  'exports.get': define<ApiParams['exports.get']>(
    (raw) => ({ id: uuid(exact(raw, ['id']).id) }),
    (p) => ({ method: 'GET', path: `/v1/export-batches/${p.id}` }),
  ),

  'exports.retry': define<ApiParams['exports.retry']>(
    (raw) => ({ batch_id: uuid(exact(raw, ['batch_id']).batch_id) }),
    (p) => ({ method: 'GET', path: `/v1/export-batches/${p.batch_id}`, retry: true }),
  ),

  'jobs.get': define<ApiParams['jobs.get']>(
    (raw) => ({ id: uuid(exact(raw, ['id']).id) }),
    (p) => ({ method: 'GET', path: `/v1/jobs/${p.id}` }),
  ),

  'jobs.cancel': define<ApiParams['jobs.cancel']>(
    (raw) => ({ id: uuid(exact(raw, ['id']).id) }),
    (p) => ({ method: 'POST', path: `/v1/jobs/${p.id}/cancel`, body: {} }),
  ),

  // ADR 021: the preview proxy of a source; the audio track is a global stream index.
  'previews.create': define<ApiParams['previews.create']>(
    (raw) => {
      const p = exact(raw, ['source_id', 'audio_stream_index']);
      return {
        source_id: uuid(p.source_id),
        audio_stream_index: nullable(int)(p.audio_stream_index),
      };
    },
    (p) => ({
      method: 'POST',
      path: `/v1/sources/${p.source_id}/previews`,
      body: { audio_stream_index: p.audio_stream_index },
    }),
  ),

  'previews.get': define<ApiParams['previews.get']>(
    (raw) => ({ id: uuid(exact(raw, ['id']).id) }),
    (p) => ({ method: 'GET', path: `/v1/previews/${p.id}` }),
  ),

  // ADR 024: readiness of FFmpeg, FFprobe and the data folder; no params at all.
  'system.checks': define<ApiParams['system.checks']>(
    (raw) => {
      exact(raw, []);
      return {};
    },
    () => ({ method: 'GET', path: '/v1/system/checks' }),
  ),

  // ADR 033: the four non-secret settings. The API key is not one of them and never takes this
  // route (`ApiKeyService`); any other key in the params is refused here, not forwarded.
  'settings.get': define<ApiParams['settings.get']>(
    (raw) => {
      exact(raw, []);
      return {};
    },
    () => ({ method: 'GET', path: '/v1/settings' }),
  ),

  // ADR 038: `jobs.resume` takes the user's decision to send again requests whose outcome is
  // unknown. `budget_usd` (ADR 038, доповнення 2) replaces the run's ceiling, checked like the
  // setting; without it the ceiling stays as it was.
  'jobs.resume': define<ApiParams['jobs.resume']>(
    (raw) => {
      const p = exact(raw, ['id'], ['retry_unknown', 'budget_usd']);
      return {
        id: uuid(p.id),
        retry_unknown: optional(p.retry_unknown, bool),
        budget_usd: optional(p.budget_usd, runBudget),
      };
    },
    (p) => ({
      method: 'POST',
      path: `/v1/jobs/${p.id}/resume`,
      body: defined({ retry_unknown: p.retry_unknown, budget_usd: p.budget_usd }),
    }),
  ),

  // ADR 038: the model, the language and the budget are not params; the backend takes them from
  // the settings, so a run always matches the Settings screen. No key goes through these ops.
  'transcription.estimate': define<ApiParams['transcription.estimate']>(
    (raw) => {
      const p = exact(raw, ['source_id', 'audio_stream_index']);
      return { source_id: uuid(p.source_id), audio_stream_index: int(p.audio_stream_index) };
    },
    (p) => ({
      method: 'POST',
      path: '/v1/transcription-runs/estimate',
      body: { source_id: p.source_id, audio_stream_index: p.audio_stream_index },
    }),
  ),

  'transcription.create': define<ApiParams['transcription.create']>(
    (raw) => {
      const p = exact(raw, ['source_id', 'audio_stream_index'], ['force']);
      return {
        source_id: uuid(p.source_id),
        audio_stream_index: int(p.audio_stream_index),
        force: optional(p.force, bool),
      };
    },
    (p) => ({
      method: 'POST',
      path: '/v1/transcription-runs',
      body: defined({
        source_id: p.source_id,
        audio_stream_index: p.audio_stream_index,
        force: p.force,
      }),
    }),
  ),

  'transcription.get': define<ApiParams['transcription.get']>(
    (raw) => ({ id: uuid(exact(raw, ['id']).id) }),
    (p) => ({ method: 'GET', path: `/v1/transcription-runs/${p.id}` }),
  ),

  'transcription.list': define<ApiParams['transcription.list']>(
    (raw) => ({ source_id: uuid(exact(raw, ['source_id']).source_id) }),
    (p) => ({ method: 'GET', path: withQuery('/v1/transcription-runs', { source_id: p.source_id }) }),
  ),

  // ADR 039: the revisions of one source, oldest first. The page size is the backend's; the
  // renderer follows the opaque cursor only.
  'transcripts.list': define<ApiParams['transcripts.list']>(
    (raw) => {
      const p = exact(raw, ['source_id'], ['cursor']);
      return { source_id: uuid(p.source_id), cursor: optional(p.cursor, str) };
    },
    (p) => ({
      method: 'GET',
      path: withQuery('/v1/transcripts', { source_id: p.source_id, cursor: p.cursor }),
    }),
  ),

  // ADR 039: one revision with a window of its segments and words (ADR 030). Both bounds are the
  // backend's: `offset` 0 or more, `limit` 1 to 1000; the defaults are the backend's too.
  'transcripts.get': define<ApiParams['transcripts.get']>(
    (raw) => {
      const p = exact(raw, ['id'], ['offset', 'limit']);
      return {
        id: uuid(p.id),
        offset: optional(p.offset, windowOffset),
        limit: optional(p.limit, windowLimit),
      };
    },
    (p) => ({
      method: 'GET',
      path: withQuery(`/v1/transcripts/${p.id}`, { offset: p.offset, limit: p.limit }),
    }),
  ),

  // FR-020, ADR 039: one correction, a new revision. The renderer names the words or segments it
  // changes and their new text; the backend checks the IDs against the revision and decides the rest.
  'transcripts.correct': define<ApiParams['transcripts.correct']>(
    (raw) => {
      const p = exact(raw, ['id', 'edits']);
      return { id: uuid(p.id), edits: transcriptEdits(p.edits) };
    },
    (p) => ({
      method: 'POST',
      path: `/v1/transcripts/${p.id}/corrections`,
      body: { edits: p.edits },
    }),
  ),

  // ADR 039: the key check takes no params. The key is held by the sidecar and never sent by
  // the renderer or sent back to it: this op carries neither.
  'credentials.check': define<ApiParams['credentials.check']>(
    (raw) => {
      exact(raw, []);
      return {};
    },
    () => ({ method: 'POST', path: '/v1/runtime/credentials/check' }),
  ),

  'settings.update': define<ApiParams['settings.update']>(
    (raw) => {
      const p = exact(raw, [], ['stt_model', 'stt_language', 'analysis_model', 'run_budget_usd']);
      return {
        // The default of the model is explicit, so it cannot be reset to null (ADR 033).
        stt_model: optional(p.stt_model, str),
        stt_language: optional(p.stt_language, nullable(str)),
        analysis_model: optional(p.analysis_model, nullable(str)),
        run_budget_usd: optional(p.run_budget_usd, nullable(str)),
      };
    },
    (p) => ({ method: 'PATCH', path: '/v1/settings', body: defined(p) }),
  ),
};

/** The `api:call` payload: exactly `{ op, params? }`. `op` is checked by the caller. */
export function parseCall(payload: unknown): { readonly op: unknown; readonly params: unknown } {
  const envelope = exact(payload, ['op'], ['params']);
  return { op: envelope.op, params: envelope.params ?? {} };
}

/**
 * Params of `dialog:import-video`: the project the video is registered in, and optionally the
 * one-time token of a video that was picked or dropped before (ADR 024). Nothing else.
 */
export function parseImportParams(value: unknown): ImportVideoParams {
  const source = exact(value, ['project_id'], ['file_token']);
  const fileToken = optional(source.file_token, videoToken);
  return fileToken === undefined
    ? { project_id: uuid(source.project_id) }
    : { project_id: uuid(source.project_id), file_token: fileToken };
}

/**
 * Params of `dialog:import-transcript`: the source and the offset of the file's times, nothing
 * else. The file is chosen in the dialog, so no path or text is among the params.
 */
export function parseImportTranscriptParams(value: unknown): ImportTranscriptParams {
  const source = exact(value, ['source_id'], ['offset_ms']);
  const sourceId = uuid(source.source_id);
  const offset = optional(source.offset_ms, transcriptOffsetMs);
  return offset === undefined ? { source_id: sourceId } : { source_id: sourceId, offset_ms: offset };
}

/**
 * Payload of `drop:video`: exactly `{ path }`. The path itself is not checked here: what it
 * must be (a string, absolute, a video file on the disk) is decided, with its own error codes,
 * by the gateway.
 */
export function parseDropParams(value: unknown): { readonly path: unknown } {
  return { path: exact(value, ['path']).path };
}

/** Params of `export:open-folder`: the batch whose folder is opened, nothing else. */
export function parseOpenFolderParams(value: unknown): OpenFolderParams {
  return { batch_id: uuid(exact(value, ['batch_id']).batch_id) };
}

/** Plan for an allowlisted op, or a ParamError for params of the wrong shape. */
export function buildPlan(op: ApiOp, params: unknown): ApiPlan {
  return BUILDERS[op](params);
}
