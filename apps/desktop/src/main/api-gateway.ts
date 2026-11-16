import { randomBytes } from 'node:crypto';
import { open, stat } from 'node:fs/promises';
import type { FileHandle } from 'node:fs/promises';
import { basename, extname, isAbsolute } from 'node:path';
import { TextDecoder } from 'node:util';

import type {
  BrowserWindow,
  IpcMainInvokeEvent,
  OpenDialogOptions,
  OpenDialogReturnValue,
} from 'electron';

import {
  API_DATA_PARSERS,
  isApiOp,
  parseBackendExportBatch,
  parsePage,
  parseTranscript,
} from '../shared/api';
import type { ApiData, ApiOp } from '../shared/api';
import {
  API_CALL_CHANNEL,
  CHOOSE_FOLDER_CHANNEL,
  DROP_VIDEO_CHANNEL,
  EXPORT_OPEN_FOLDER_CHANNEL,
  FILE_NOT_FOUND_MESSAGE,
  IMPORT_TRANSCRIPT_CHANNEL,
  IMPORT_VIDEO_CHANNEL,
  PICK_VIDEO_CHANNEL,
  UNSUPPORTED_FILE_MESSAGE,
  apiFailure,
} from '../shared/bridge';
import type {
  ApiFailure,
  ApiResult,
  ChooseFolderResult,
  ImportTranscriptParams,
  ImportTranscriptResult,
  ImportVideoResult,
  OpenFolderResult,
  PickVideoResult,
} from '../shared/bridge';
import {
  ParamError,
  buildPlan,
  parseCall,
  parseDropParams,
  parseImportParams,
  parseImportTranscriptParams,
  parseOpenFolderParams,
} from './api-ops';
import type { ApiPlan } from './api-ops';
import { isOpenBatchStatus, parseOutputDir, parseRetrySource } from './export-batch';
import type { RetrySource } from './export-batch';
import { isTrustedSender } from './ipc-guard';
import type { IpcSender, TrustedPage } from './ipc-guard';
import { parseBackendSource } from './playback';
import type { ApiMethod } from './sidecar-protocol';

/** What the gateway needs from the sidecar: `Sidecar.request` and nothing else. */
export interface ApiBackend {
  request(method: ApiMethod, path: string, body?: unknown): Promise<ApiResult<unknown>>;
}

/** What the gateway needs from Electron's `dialog`; looked up at call time. */
export interface OpenDialogHost {
  showOpenDialog(window: BrowserWindow, options: OpenDialogOptions): Promise<OpenDialogReturnValue>;
}

/** What the gateway needs from Electron's `shell`: `openPath` returns '' on success. */
export interface OpenPathHost {
  openPath(path: string): Promise<string>;
}

export interface ApiGatewayOptions {
  readonly backend: ApiBackend;
  readonly dialog: OpenDialogHost;
  readonly shell: OpenPathHost;
  readonly getWindow: () => BrowserWindow | null;
  readonly getTrustedPage: () => TrustedPage | null;
  /** Overridable for tests; default is 32 random bytes as hex. */
  readonly createFolderToken?: () => string;
  /** Overridable for tests; default is 32 random bytes as hex. */
  readonly createVideoToken?: () => string;
  /** Overridable for tests; default asks the filesystem. Symlinks are followed. */
  readonly isDirectory?: (path: string) => Promise<boolean>;
}

const VIDEO_EXTENSIONS: readonly string[] = [
  'mp4',
  'm4v',
  'mov',
  'mkv',
  'webm',
  'avi',
  'mpg',
  'mpeg',
  'ts',
  'mts',
  'm2ts',
  'wmv',
  'flv',
  'ogv',
  '3gp',
  'mxf',
];

const IMPORT_DIALOG: OpenDialogOptions = {
  title: 'Імпорт відео',
  properties: ['openFile'],
  filters: [{ name: 'Відео', extensions: [...VIDEO_EXTENSIONS] }],
};

const FOLDER_DIALOG: OpenDialogOptions = {
  title: 'Тека для експорту',
  properties: ['openDirectory', 'createDirectory'],
};

/** The subtitle picker of `dialog:import-transcript` (ADR 039): the two formats the backend reads. */
const TRANSCRIPT_DIALOG: OpenDialogOptions = {
  title: 'Імпорт субтитрів',
  properties: ['openFile'],
  filters: [{ name: 'Субтитри', extensions: ['srt', 'vtt'] }],
};

const SUBTITLE_FORMATS = ['srt', 'vtt'] as const;
/** The largest subtitle file that is read: 20 MB (ADR 039). */
const SUBTITLE_MAX_BYTES = 20 * 1024 * 1024;
/** The longest text the backend takes for an import, in code points (its MAX_IMPORT_TEXT_CHARS). */
const MAX_IMPORT_TEXT_CHARS = 5_000_000;

/**
 * What main accepts from the backend per op. Same as the parsers the preload
 * re-applies to main's answer, except for sources: the backend's own answer
 * carries the probe, from which main computes `playback` (ADR 021).
 */
const BACKEND_PARSERS: { readonly [O in ApiOp]: (value: unknown) => ApiData[O] } = {
  ...API_DATA_PARSERS,
  'sources.get': parseBackendSource,
  'sources.list': parsePage(parseBackendSource),
  'exports.get': parseBackendExportBatch,
};

function defaultFolderToken(): string {
  return randomBytes(32).toString('hex');
}

function invalidParams(): ApiFailure {
  return apiFailure('INVALID_PARAMS', 'Некоректні параметри запиту');
}

function badBackendResponse(): ApiFailure {
  return apiFailure('BAD_BACKEND_RESPONSE', 'Локальний рушій повернув неочікувану відповідь');
}

async function isExistingDirectory(path: string): Promise<boolean> {
  try {
    return (await stat(path)).isDirectory();
  } catch {
    return false;
  }
}

function unknownFileToken(): ApiFailure {
  return apiFailure(
    'UNKNOWN_FILE_TOKEN',
    'Відео не обрано або вибір уже використано. Оберіть відео ще раз',
  );
}

/** Whether the path names a file of a video type; the case of the extension does not matter. */
function hasVideoExtension(path: string): boolean {
  return VIDEO_EXTENSIONS.includes(extname(path).slice(1).toLowerCase());
}

/** The format of a subtitle file by its extension, in any letter case; null for any other file. */
function subtitleFormat(path: string): (typeof SUBTITLE_FORMATS)[number] | null {
  const extension = extname(path).slice(1).toLowerCase();
  return SUBTITLE_FORMATS.find((format) => format === extension) ?? null;
}

/** Whether a text has more than MAX_IMPORT_TEXT_CHARS code points; the UTF-16 length goes first. */
function isOverImportLimit(text: string): boolean {
  // A code point is one or two UTF-16 units: a text of no more units than the limit is within it,
  // and one of more than twice the limit in units is over it. Only the texts in between are
  // counted.
  if (text.length <= MAX_IMPORT_TEXT_CHARS) {
    return false;
  }
  return text.length > MAX_IMPORT_TEXT_CHARS * 2 || Array.from(text).length > MAX_IMPORT_TEXT_CHARS;
}

/** Reads from the start of an open file until `limit` bytes or the file ends, whichever is first. */
async function readUpTo(handle: FileHandle, limit: number): Promise<Buffer> {
  // Only the bytes that were read are returned, so the buffer need not be zeroed.
  const buffer = Buffer.allocUnsafe(limit);
  let filled = 0;
  while (filled < limit) {
    const { bytesRead } = await handle.read(buffer, filled, limit - filled, filled);
    if (bytesRead === 0) {
      break;
    }
    filled += bytesRead;
  }
  return buffer.subarray(0, filled);
}

/**
 * Reads a subtitle file as strict UTF-8 without its byte order mark (ADR 039). `stat` decides the
 * size before anything is read, so a file over 20 MB is refused unread. The read itself stops one
 * byte past that limit, so a file that grew after `stat` is refused as well, and never read whole.
 * Every failure is a fixed code and text: no path, no system message and no part of the file
 * reaches the answer.
 */
async function readSubtitleText(path: string): Promise<string | ApiFailure> {
  const unreadable = apiFailure('TRANSCRIPT_FILE_UNREADABLE', 'Не вдалося прочитати файл субтитрів');
  const tooLarge = apiFailure('TRANSCRIPT_FILE_TOO_LARGE', 'Файл субтитрів більший за 20 МБ');
  let size: number;
  try {
    const info = await stat(path);
    if (!info.isFile()) {
      return unreadable;
    }
    size = info.size;
  } catch {
    return unreadable;
  }
  if (size > SUBTITLE_MAX_BYTES) {
    return tooLarge;
  }
  let handle: FileHandle;
  try {
    handle = await open(path, 'r');
  } catch {
    return unreadable;
  }
  let bytes: Buffer;
  try {
    bytes = await readUpTo(handle, SUBTITLE_MAX_BYTES + 1);
  } catch {
    return unreadable;
  } finally {
    await handle.close().catch(() => undefined);
  }
  if (bytes.length > SUBTITLE_MAX_BYTES) {
    return tooLarge;
  }
  let text: string;
  try {
    // `ignoreBOM` keeps the mark in the output, so the one removal below is the only one.
    text = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes);
  } catch {
    return apiFailure('TRANSCRIPT_FILE_ENCODING', 'Файл субтитрів не в кодуванні UTF-8');
  }
  const subtitles = text.startsWith('\uFEFF') ? text.slice(1) : text;
  return isOverImportLimit(subtitles)
    ? apiFailure('TRANSCRIPT_FILE_TOO_LARGE', 'Файл субтитрів містить понад 5 000 000 символів')
    : subtitles;
}

/**
 * What a dropped path must be before it gets a token (ADR 024): a string without NUL, absolute,
 * with the extension of a video, and a regular file on the disk. No answer carries the path.
 */
async function checkDroppedPath(path: unknown): Promise<ApiFailure | string> {
  const unsupported = apiFailure('UNSUPPORTED_FILE', UNSUPPORTED_FILE_MESSAGE);
  if (
    typeof path !== 'string' ||
    path.includes('\0') ||
    !isAbsolute(path) ||
    !hasVideoExtension(path)
  ) {
    return unsupported;
  }
  let isFile: boolean;
  try {
    isFile = (await stat(path)).isFile();
  } catch {
    return apiFailure('FILE_NOT_FOUND', FILE_NOT_FOUND_MESSAGE);
  }
  return isFile ? path : unsupported;
}

/** Reduces a backend answer to what the renderer may see; a malformed one becomes an error. */
function sanitize<T>(result: ApiResult<unknown>, parse: (data: unknown) => T): ApiResult<T> {
  if (!result.ok) {
    return result;
  }
  try {
    return { ok: true, data: parse(result.data) };
  } catch {
    return badBackendResponse();
  }
}

/** Folders the user picked, each behind a random token that works exactly once. */
class FolderTokens {
  readonly #paths = new Map<string, string>();
  readonly #create: () => string;

  constructor(create: () => string) {
    this.#create = create;
  }

  issue(directory: string): string {
    const token = this.#create();
    this.#paths.set(token, directory);
    return token;
  }

  /** Returns the folder and forgets the token; undefined for an unknown or used token. */
  take(token: string): string | undefined {
    const directory = this.#paths.get(token);
    this.#paths.delete(token);
    return directory;
  }
}

/**
 * The video the user picked or dropped last, behind a random token that works exactly once.
 * Only the latest token is valid: a new choice replaces the previous one. A token that is not
 * the current one changes nothing, so a stale call cannot spend the valid choice.
 */
class VideoTokens {
  #current: { readonly token: string; readonly path: string } | null = null;
  readonly #create: () => string;

  constructor(create: () => string) {
    this.#create = create;
  }

  issue(path: string): string {
    const token = this.#create();
    this.#current = { token, path };
    return token;
  }

  /** Returns the path and forgets the token; undefined unless it is the current, unused one. */
  take(token: string): string | undefined {
    const current = this.#current;
    if (current?.token !== token) {
      return undefined;
    }
    this.#current = null;
    return current.path;
  }
}

/**
 * The renderer's only way to the backend (docs/04 section 1). Every entry point
 * first checks the sender, then works from allowlisted, shape-checked input;
 * paths come from native dialogs and stay in main.
 */
export class ApiGateway {
  readonly #options: ApiGatewayOptions;
  readonly #folders: FolderTokens;
  readonly #videos: VideoTokens;
  #dialogOpen = false;

  constructor(options: ApiGatewayOptions) {
    this.#options = options;
    this.#folders = new FolderTokens(options.createFolderToken ?? defaultFolderToken);
    this.#videos = new VideoTokens(options.createVideoToken ?? defaultFolderToken);
  }

  /** `api:call` with `{ op, params }`. Throws for an untrusted sender. */
  async call(sender: IpcSender, payload: unknown): Promise<ApiResult<unknown>> {
    this.#assertTrusted(sender);

    let call: ReturnType<typeof parseCall>;
    try {
      call = parseCall(payload);
    } catch (error) {
      return this.#paramFailure(error, apiFailure('INVALID_REQUEST', 'Некоректний запит'));
    }
    const { op } = call;
    if (!isApiOp(op)) {
      return apiFailure('UNKNOWN_OP', 'Невідома операція');
    }

    let plan: ReturnType<typeof buildPlan>;
    try {
      plan = buildPlan(op, call.params);
    } catch (error) {
      return this.#paramFailure(error, invalidParams());
    }
    if (plan.retry === true) {
      return this.#retryExport(plan);
    }

    let body = plan.body;
    if (plan.folderToken !== undefined) {
      // Taken only after every other param passed, so a bad request keeps the token;
      // taken before the call, so the same folder can never start a second batch.
      const outputDir = this.#folders.take(plan.folderToken);
      if (outputDir === undefined) {
        return apiFailure(
          'UNKNOWN_FOLDER_TOKEN',
          'Теку для експорту не обрано або вибір уже використано. Оберіть теку ще раз',
        );
      }
      body = { ...body, output_dir: outputDir };
    }

    const result = await this.#options.backend.request(plan.method, plan.path, body);
    return sanitize<unknown>(result, BACKEND_PARSERS[op]);
  }

  /**
   * `dialog:import-video`, in two forms (ADR 024). `{ project_id }`: the user picks a file and
   * main registers it as a source. `{ project_id, file_token }`: main registers the video that
   * `pickVideo` or `takeDroppedVideo` stored under the token; the token is taken after the form
   * passed and before the backend is called, so it is spent even when the backend refuses the
   * file, and a malformed request leaves it valid. The path never goes through the renderer.
   */
  async importVideo(sender: IpcSender, payload: unknown): Promise<ImportVideoResult> {
    this.#assertTrusted(sender);

    let params: ReturnType<typeof parseImportParams>;
    try {
      params = parseImportParams(payload);
    } catch (error) {
      return this.#paramFailure(error, invalidParams());
    }

    if (params.file_token !== undefined) {
      const taken = this.#videos.take(params.file_token);
      return taken === undefined
        ? unknownFileToken()
        : this.#registerSource(params.project_id, taken);
    }
    const path = await this.#pick(IMPORT_DIALOG);
    return path === null ? { canceled: true } : this.#registerSource(params.project_id, path);
  }

  async #registerSource(projectId: string, path: string): Promise<ImportVideoResult> {
    const result = await this.#options.backend.request('POST', '/v1/sources', {
      project_id: projectId,
      path,
    });
    return sanitize(result, parseBackendSource);
  }

  /**
   * `dialog:import-transcript` with `{ source_id, offset_ms? }` (ADR 039). The params are checked
   * before the dialog opens. Main shows the subtitle picker, takes the format from the extension,
   * reads the file itself and posts its text to the backend with the source and the offset. The
   * answer is a cancel, the new revision, or a failure (a file check here or the backend's error).
   * The path never leaves main; the raw text goes only to the backend, whose parse error may quote
   * a short fragment of it (ADR 039, доповнення).
   */
  async importTranscript(sender: IpcSender, payload: unknown): Promise<ImportTranscriptResult> {
    this.#assertTrusted(sender);

    let params: ImportTranscriptParams;
    try {
      params = parseImportTranscriptParams(payload);
    } catch (error) {
      return this.#paramFailure(error, invalidParams());
    }
    const path = await this.#pick(TRANSCRIPT_DIALOG);
    if (path === null) {
      return { canceled: true };
    }
    const format = subtitleFormat(path);
    if (format === null) {
      return apiFailure('TRANSCRIPT_FILE_UNSUPPORTED', 'Оберіть файл субтитрів у форматі SRT або VTT');
    }
    const subtitles = await readSubtitleText(path);
    if (typeof subtitles !== 'string') {
      return subtitles;
    }
    const result = await this.#options.backend.request('POST', '/v1/transcripts/import', {
      source_id: params.source_id,
      format,
      text: subtitles,
      offset_ms: params.offset_ms ?? 0,
    });
    return sanitize(result, parseTranscript);
  }

  /**
   * `dialog:pick-video`: the same dialog as `dialog:import-video`; the picked file stays here
   * and the renderer gets a one-time token with the file name (ADR 024).
   */
  async pickVideo(sender: IpcSender): Promise<PickVideoResult> {
    this.#assertTrusted(sender);

    const path = await this.#pick(IMPORT_DIALOG);
    return path === null ? { canceled: true } : this.#videoChoice(path);
  }

  /**
   * `drop:video` with `{ path }`, sent by the preload for a file the user dropped. The path is
   * held to the same rules as a picked one and a few more (`checkDroppedPath`); the answer is
   * the one of `pickVideo`, or an error without the path (ADR 024).
   */
  async takeDroppedVideo(sender: IpcSender, payload: unknown): Promise<PickVideoResult> {
    this.#assertTrusted(sender);

    let dropped: unknown;
    try {
      dropped = parseDropParams(payload).path;
    } catch (error) {
      return this.#paramFailure(error, invalidParams());
    }
    const checked = await checkDroppedPath(dropped);
    return typeof checked === 'string' ? this.#videoChoice(checked) : checked;
  }

  #videoChoice(path: string): PickVideoResult {
    return { token: this.#videos.issue(path), fileName: basename(path) };
  }

  /** `export:choose-folder`: the picked folder stays here; the renderer gets a one-time token. */
  async chooseExportFolder(sender: IpcSender): Promise<ChooseFolderResult> {
    this.#assertTrusted(sender);

    const directory = await this.#pick(FOLDER_DIALOG);
    if (directory === null) {
      return { canceled: true };
    }
    const name = basename(directory);
    return {
      token: this.#folders.issue(directory),
      displayName: name === '' ? directory : name,
    };
  }

  /**
   * `export:open-folder` with `{ batch_id }`: opens the folder the backend recorded for the
   * batch. Only an existing directory is opened (`shell.openPath` would run a file), and a
   * failure of the shell is reported without its text, which may carry the path.
   */
  async openExportFolder(sender: IpcSender, payload: unknown): Promise<OpenFolderResult> {
    this.#assertTrusted(sender);

    let batchId: string;
    try {
      batchId = parseOpenFolderParams(payload).batch_id;
    } catch (error) {
      return this.#paramFailure(error, invalidParams());
    }

    const read = await this.#options.backend.request('GET', `/v1/export-batches/${batchId}`);
    if (!read.ok) {
      return read;
    }
    let directory: string;
    try {
      directory = parseOutputDir(read.data);
    } catch {
      return badBackendResponse();
    }
    const isDirectory = this.#options.isDirectory ?? isExistingDirectory;
    if (!isAbsolute(directory) || !(await isDirectory(directory))) {
      return apiFailure(
        'EXPORT_FOLDER_MISSING',
        'Теки експорту більше немає на цьому місці. Можливо, її перейменовано або видалено',
      );
    }
    if ((await this.#options.shell.openPath(directory)) !== '') {
      return apiFailure('OPEN_FOLDER_FAILED', 'Не вдалося відкрити теку експорту');
    }
    return { ok: true, data: { opened: true } };
  }

  /**
   * `exports.retry`: reads the batch, then posts a new one for its failed and cancelled
   * clips into the same `output_dir` with the same profile. Both come from the backend's own
   * record of the batch, never from the renderer. An unfinished batch is not retried: its
   * queued clips are still going to be exported.
   */
  async #retryExport(plan: ApiPlan): Promise<ApiResult<unknown>> {
    const read = await this.#options.backend.request(plan.method, plan.path);
    if (!read.ok) {
      return read;
    }
    let batch: RetrySource;
    try {
      batch = parseRetrySource(read.data);
    } catch {
      return badBackendResponse();
    }
    if (isOpenBatchStatus(batch.status)) {
      return apiFailure('EXPORT_NOT_FINISHED', 'Експорт ще триває. Дочекайтеся його завершення');
    }
    if (batch.clip_ids.length === 0) {
      return apiFailure('NOTHING_TO_RETRY', 'У цьому експорті немає невдалих або скасованих кліпів');
    }
    const created = await this.#options.backend.request('POST', '/v1/export-batches', {
      project_id: batch.project_id,
      clip_ids: batch.clip_ids,
      output_dir: batch.output_dir,
      profile: batch.profile,
    });
    return sanitize<unknown>(created, BACKEND_PARSERS['exports.retry']);
  }

  #assertTrusted(sender: IpcSender): void {
    if (!isTrustedSender(sender, this.#options.getTrustedPage())) {
      throw new Error('Недозволений відправник IPC');
    }
  }

  #paramFailure(error: unknown, failure: ApiFailure): ApiFailure {
    if (error instanceof ParamError) {
      return failure;
    }
    throw error;
  }

  /** One native dialog at a time; a second request while one is open counts as cancelled. */
  async #pick(options: OpenDialogOptions): Promise<string | null> {
    const window = this.#options.getWindow();
    if (window === null) {
      throw new Error('Вікно застосунку недоступне');
    }
    if (this.#dialogOpen) {
      return null;
    }
    this.#dialogOpen = true;
    try {
      const picked = await this.#options.dialog.showOpenDialog(window, options);
      const [first] = picked.filePaths;
      return picked.canceled || typeof first !== 'string' || first === '' ? null : first;
    } finally {
      this.#dialogOpen = false;
    }
  }
}

/** The part of `ipcMain` used here. */
export interface IpcRegistry {
  handle(
    channel: string,
    listener: (event: IpcMainInvokeEvent, ...args: unknown[]) => unknown,
  ): void;
}

export function toIpcSender(event: IpcMainInvokeEvent): IpcSender {
  const frame = event.senderFrame;
  return {
    webContentsId: event.sender.id,
    frameUrl: frame?.url ?? null,
    isMainFrame: frame !== null && frame === event.sender.mainFrame,
  };
}

/** Registers the seven gateway channels; each handler checks its sender first. */
export function registerApiIpc(ipc: IpcRegistry, gateway: ApiGateway): void {
  ipc.handle(API_CALL_CHANNEL, (event, payload) => gateway.call(toIpcSender(event), payload));
  ipc.handle(IMPORT_VIDEO_CHANNEL, (event, payload) =>
    gateway.importVideo(toIpcSender(event), payload),
  );
  ipc.handle(IMPORT_TRANSCRIPT_CHANNEL, (event, payload) =>
    gateway.importTranscript(toIpcSender(event), payload),
  );
  ipc.handle(PICK_VIDEO_CHANNEL, (event) => gateway.pickVideo(toIpcSender(event)));
  ipc.handle(DROP_VIDEO_CHANNEL, (event, payload) =>
    gateway.takeDroppedVideo(toIpcSender(event), payload),
  );
  ipc.handle(CHOOSE_FOLDER_CHANNEL, (event) => gateway.chooseExportFolder(toIpcSender(event)));
  ipc.handle(EXPORT_OPEN_FOLDER_CHANNEL, (event, payload) =>
    gateway.openExportFolder(toIpcSender(event), payload),
  );
}
