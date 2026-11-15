/**
 * Typed surface that the preload script exposes to the renderer as
 * `window.clipforge`. The renderer never gets Node, the filesystem, the session
 * token, the sidecar port or a raw HTTP client; allowlisted IPC methods are
 * added here as features need them.
 */

import { API_DATA_PARSERS, isFolderToken, isUuid4, isVideoToken, parseSource } from './api';
import type { ApiData, ApiOp, ApiParams, SourceDto } from './api';

/** IPC channel of the backend status query (ipcMain.handle in main). */
export const APP_GET_STATUS_CHANNEL = 'app:get-status';
/** The one channel of the backend API gateway: payload `{ op, params }`. */
export const API_CALL_CHANNEL = 'api:call';
/** Native video picker; main registers the picked file as a source itself. */
export const IMPORT_VIDEO_CHANNEL = 'dialog:import-video';
/** Native video picker; main keeps the picked path behind a one-time token (ADR 024). */
export const PICK_VIDEO_CHANNEL = 'dialog:pick-video';
/** A file dropped on the window: the preload sends its path to main only (ADR 024). */
export const DROP_VIDEO_CHANNEL = 'drop:video';
/** Native folder picker; main keeps the path behind a one-time token. */
export const CHOOSE_FOLDER_CHANNEL = 'export:choose-folder';
/** Opens the output folder of an export batch in the file manager; main finds the path. */
export const EXPORT_OPEN_FOLDER_CHANNEL = 'export:open-folder';
/** The project and tab the app reopens at start; a small file in the app data folder. */
export const VIEW_STATE_GET_CHANNEL = 'app:get-view-state';
export const VIEW_STATE_SET_CHANNEL = 'app:set-view-state';

export type AppState = 'starting' | 'ready' | 'failed';
export type AppMode = 'production' | 'test';

export interface AppStatus {
  readonly state: AppState;
  readonly mode: AppMode;
  /** Backend version reported by /v1/health; null until the backend is ready. */
  readonly version: string | null;
}

/** Error of the shape in docs/04 section 4, without `details`. `message` is Ukrainian. */
export interface ApiErrorInfo {
  readonly code: string;
  readonly message: string;
  readonly retryable: boolean;
}

export type ApiResult<T> =
  | { readonly ok: true; readonly data: T }
  | { readonly ok: false; readonly error: ApiErrorInfo };

/** The failed half of an `ApiResult`. */
export type ApiFailure = Extract<ApiResult<never>, { readonly ok: false }>;

export function apiFailure(code: string, message: string, retryable = false): ApiFailure {
  return { ok: false, error: { code, message, retryable } };
}

/**
 * Two forms (ADR 024). `{ project_id }`: main opens the picker and registers the file it
 * returns. `{ project_id, file_token }`: main registers the video that `pickVideo` or
 * `takeDroppedVideo` named with the token.
 */
export interface ImportVideoParams {
  readonly project_id: string;
  readonly file_token?: string;
}

export type ImportVideoResult = { readonly canceled: true } | ApiResult<SourceDto>;

/** A video picked or dropped by the user. `fileName` is the last path component only. */
export interface VideoChoice {
  readonly token: string;
  readonly fileName: string;
}

/**
 * The answer of `pickVideo` and `takeDroppedVideo`: a choice, a cancelled dialog, or an error
 * with a code and a message that never carries the path (UNSUPPORTED_FILE, FILE_NOT_FOUND).
 */
export type PickVideoResult = { readonly canceled: true } | VideoChoice | ApiFailure;

/** Shown for a dropped item that is not a video file on the disk (ADR 024). */
export const UNSUPPORTED_FILE_MESSAGE = 'Це не відеофайл. Перетягніть файл MP4, MOV, MKV…';
export const FILE_NOT_FOUND_MESSAGE =
  'Файл не знайдено або він недоступний. Перетягніть його ще раз';

/** `displayName` is the last path component only; the full path stays in main. */
export interface FolderChoice {
  readonly token: string;
  readonly displayName: string;
}

export type ChooseFolderResult = { readonly canceled: true } | FolderChoice;

/** Only the batch is named; the folder is whatever the backend recorded for it. */
export interface OpenFolderParams {
  readonly batch_id: string;
}

export interface FolderOpened {
  readonly opened: true;
}

export type OpenFolderResult = ApiResult<FolderOpened>;

/** Tabs of a project that can be reopened; «Експорт» needs a batch of this session. */
export const REMEMBERED_TABS = ['sources', 'clips'] as const;
export type RememberedTab = (typeof REMEMBERED_TABS)[number];

/**
 * Widths, in CSS px, of the two side panels of «Моменти»: the clip list and the right panel
 * (docs/03 section 8). The player takes what is left.
 */
export interface PanelLayout {
  readonly list_px: number;
  readonly details_px: number;
}

/** What a panel may be dragged to; main rejects any width outside it. */
export const LAYOUT_LIMITS = {
  list_px: { min: 240, max: 480 },
  details_px: { min: 280, max: 480 },
} as const;

/** ~320 px each at 1440 px wide (docs/03 section 8). */
export const DEFAULT_LAYOUT: PanelLayout = { list_px: 320, details_px: 320 };

/**
 * What the app shows again after a restart: the open project, its tab and the panel widths,
 * nothing else. It is the whole content of the state file, so it holds no path, token or text
 * of the user.
 */
export interface ViewState {
  readonly project_id: string;
  readonly tab: RememberedTab;
  readonly layout: PanelLayout;
}

export interface ViewStateSaved {
  readonly saved: true;
}

export type SaveViewStateResult = ApiResult<ViewStateSaved>;

/** `window.clipforge` as the preload exposes it (bridge v2). */
export interface ClipforgeBridge {
  readonly bridgeVersion: 2;
  readonly getStatus: () => Promise<AppStatus>;
  readonly call: <O extends ApiOp>(op: O, params: ApiParams[O]) => Promise<ApiResult<ApiData[O]>>;
  readonly importVideo: (params: ImportVideoParams) => Promise<ImportVideoResult>;
  /** The native video picker; the path stays in main behind the returned one-time token. */
  readonly pickVideo: () => Promise<PickVideoResult>;
  /**
   * A file dropped on the window. The preload takes its path and gives it to main only; the
   * renderer gets the same answer as from `pickVideo`.
   */
  readonly takeDroppedVideo: (file: File) => Promise<PickVideoResult>;
  readonly chooseExportFolder: () => Promise<ChooseFolderResult>;
  readonly openExportFolder: (params: OpenFolderParams) => Promise<OpenFolderResult>;
  /** The remembered project and tab; null when nothing is remembered or the file is unusable. */
  readonly getViewState: () => Promise<ViewState | null>;
  /** Remembers the project and tab on screen; null forgets them (the project list is shown). */
  readonly setViewState: (state: ViewState | null) => Promise<SaveViewStateResult>;
}

function isAppState(value: unknown): value is AppState {
  return value === 'starting' || value === 'ready' || value === 'failed';
}

function isAppMode(value: unknown): value is AppMode {
  return value === 'production' || value === 'test';
}

/** Validates an IPC payload; the renderer must not trust unchecked data. */
export function parseAppStatus(value: unknown): AppStatus {
  if (typeof value === 'object' && value !== null) {
    const { state, mode, version } = value as Record<string, unknown>;
    if (isAppState(state) && isAppMode(mode) && (version === null || typeof version === 'string')) {
      return { state, mode, version };
    }
  }
  throw new TypeError('Некоректна відповідь app:get-status');
}

function invalidReply(channel: string): never {
  throw new TypeError(`Некоректна відповідь ${channel}`);
}

function asRecord(value: unknown, channel: string): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : invalidReply(channel);
}

/** Validates the `{ok, data} | {ok:false, error}` envelope; unknown keys are dropped. */
export function parseApiResult<T>(
  value: unknown,
  parseData: (data: unknown) => T,
  channel = API_CALL_CHANNEL,
): ApiResult<T> {
  const envelope = asRecord(value, channel);
  if (envelope.ok === true) {
    return { ok: true, data: parseData(envelope.data) };
  }
  if (envelope.ok === false) {
    const { code, message, retryable } = asRecord(envelope.error, channel);
    if (typeof code === 'string' && code !== '' && typeof message === 'string' && typeof retryable === 'boolean') {
      return { ok: false, error: { code, message, retryable } };
    }
  }
  return invalidReply(channel);
}

/** Validates an `api:call` answer, including the data shape of the op that was called. */
export function parseApiCallResult<O extends ApiOp>(op: O, value: unknown): ApiResult<ApiData[O]> {
  return parseApiResult(value, (data) => {
    const parse = Object.hasOwn(API_DATA_PARSERS, op) ? API_DATA_PARSERS[op] : undefined;
    return parse === undefined ? invalidReply(API_CALL_CHANNEL) : parse(data);
  });
}

export function parseImportVideoResult(value: unknown): ImportVideoResult {
  const reply = asRecord(value, IMPORT_VIDEO_CHANNEL);
  if ('canceled' in reply) {
    return reply.canceled === true ? { canceled: true } : invalidReply(IMPORT_VIDEO_CHANNEL);
  }
  return parseApiResult(reply, parseSource, IMPORT_VIDEO_CHANNEL);
}

/**
 * Validates the answer of `dialog:pick-video` or `drop:video`: cancel, a token with a file name,
 * or an error. A success of the `{ ok: true }` envelope is not a shape of this answer.
 */
export function parsePickVideoResult(
  value: unknown,
  channel: string = PICK_VIDEO_CHANNEL,
): PickVideoResult {
  const reply = asRecord(value, channel);
  if ('canceled' in reply) {
    return reply.canceled === true ? { canceled: true } : invalidReply(channel);
  }
  if ('ok' in reply) {
    const failure = parseApiResult<never>(reply, () => invalidReply(channel), channel);
    return failure.ok ? invalidReply(channel) : failure;
  }
  const { token, fileName } = reply;
  if (isVideoToken(token) && typeof fileName === 'string' && fileName !== '') {
    return { token, fileName };
  }
  return invalidReply(channel);
}

export function parseChooseFolderResult(value: unknown): ChooseFolderResult {
  const reply = asRecord(value, CHOOSE_FOLDER_CHANNEL);
  if ('canceled' in reply) {
    return reply.canceled === true ? { canceled: true } : invalidReply(CHOOSE_FOLDER_CHANNEL);
  }
  const { token, displayName } = reply;
  if (isFolderToken(token) && typeof displayName === 'string' && displayName !== '') {
    return { token, displayName };
  }
  return invalidReply(CHOOSE_FOLDER_CHANNEL);
}

/** A whole number inside the limits of the panel; NaN, fractions and strings are refused. */
function isPanelWidth(value: unknown, limits: { readonly min: number; readonly max: number }): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= limits.min && value <= limits.max;
}

/** Exactly `{ list_px, details_px }`, both whole numbers inside LAYOUT_LIMITS. */
export function parseLayout(value: unknown): PanelLayout {
  const fields = asRecord(value, VIEW_STATE_GET_CHANNEL);
  const { list_px: list, details_px: details } = fields;
  if (
    Object.keys(fields).length === 2 &&
    Object.hasOwn(fields, 'list_px') &&
    Object.hasOwn(fields, 'details_px') &&
    isPanelWidth(list, LAYOUT_LIMITS.list_px) &&
    isPanelWidth(details, LAYOUT_LIMITS.details_px)
  ) {
    return { list_px: list, details_px: details };
  }
  return invalidReply(VIEW_STATE_GET_CHANNEL);
}

/**
 * A view state of exactly `{ project_id, tab, layout }`: any other key, a project id that is
 * not a uuid4, a tab that cannot be reopened or a panel width outside its limits is rejected.
 * Used by main on what the renderer sends and on the file it reads, and by the preload on what
 * main answers.
 */
export function parseViewState(value: unknown): ViewState {
  const fields = asRecord(value, VIEW_STATE_GET_CHANNEL);
  const keys = Object.keys(fields);
  const { project_id: projectId, tab, layout } = fields;
  const known = REMEMBERED_TABS.find((candidate) => candidate === tab);
  if (
    keys.length === 3 &&
    Object.hasOwn(fields, 'project_id') &&
    Object.hasOwn(fields, 'tab') &&
    Object.hasOwn(fields, 'layout') &&
    isUuid4(projectId) &&
    known !== undefined
  ) {
    return { project_id: projectId, tab: known, layout: parseLayout(layout) };
  }
  return invalidReply(VIEW_STATE_GET_CHANNEL);
}

/** The answer of `app:get-view-state`: a view state or null. */
export function parseGetViewStateResult(value: unknown): ViewState | null {
  return value === null ? null : parseViewState(value);
}

export function parseSaveViewStateResult(value: unknown): SaveViewStateResult {
  return parseApiResult(
    value,
    (data): ViewStateSaved =>
      asRecord(data, VIEW_STATE_SET_CHANNEL).saved === true
        ? { saved: true }
        : invalidReply(VIEW_STATE_SET_CHANNEL),
    VIEW_STATE_SET_CHANNEL,
  );
}

export function parseOpenFolderResult(value: unknown): OpenFolderResult {
  return parseApiResult(
    value,
    (data): FolderOpened =>
      asRecord(data, EXPORT_OPEN_FOLDER_CHANNEL).opened === true
        ? { opened: true }
        : invalidReply(EXPORT_OPEN_FOLDER_CHANNEL),
    EXPORT_OPEN_FOLDER_CHANNEL,
  );
}
