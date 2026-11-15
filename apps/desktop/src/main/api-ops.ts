import {
  PROJECT_SEARCH_MAX_LENGTH,
  REVIEW_STATUSES,
  isFolderToken,
  isUuid4,
  isVideoToken,
} from '../shared/api';
import type { ApiOp, ApiParams, ExportProfile, ReviewStatus } from '../shared/api';
import type { ImportVideoParams, OpenFolderParams } from '../shared/bridge';
import type { ApiMethod } from './sidecar-protocol';

/**
 * The fixed table behind `api:call`: every allowed op, how its params are
 * shape-checked, and the one method and path it maps to. Nothing here accepts a
 * path, URL, header or `output_dir` from the renderer, and every ID that ends up
 * in a URL has passed the uuid4 check. The op itself is looked up only after
 * `isApiOp`, never as a property of renderer-controlled data.
 *
 * Checks are about shape only (types, IDs, sizes); domain rules and bounds stay
 * with the backend, which answers 422 with a code.
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
