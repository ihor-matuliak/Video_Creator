import { mkdir, readFile, rename, rm, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

import type { IpcMainInvokeEvent } from 'electron';

import {
  DEFAULT_LAYOUT,
  VIEW_STATE_GET_CHANNEL,
  VIEW_STATE_SET_CHANNEL,
  apiFailure,
  parseViewState,
} from '../shared/bridge';
import type { SaveViewStateResult, ViewState } from '../shared/bridge';
import { toIpcSender } from './api-gateway';
import type { IpcRegistry } from './api-gateway';
import { isTrustedSender } from './ipc-guard';
import type { TrustedPage } from './ipc-guard';

/**
 * The project, tab and panel widths the app reopens with at start (docs/03 sections 8 and 12).
 * It lives in main, in a small JSON file next to the workspace (`userData` in production,
 * `CLIPFORGE_DATA_DIR` in test mode), not in renderer storage. The file (schema v2) holds the
 * version, one project id, one tab name and two whole numbers: `parseViewState` rejects any
 * other key and any width outside its limits, so no path, token or text can get in, and a file
 * that someone edited into another shape is ignored. A v1 file (no widths) is still read and
 * gets the default widths; the next write turns it into v2.
 */
export const VIEW_STATE_FILE = 'ui-state.json';
const FILE_VERSION = 2;
const LEGACY_FILE_VERSION = 1;

function parseFile(text: string): ViewState | null {
  try {
    const document: unknown = JSON.parse(text);
    if (typeof document !== 'object' || document === null || Array.isArray(document)) {
      return null;
    }
    const { version, ...state } = document as Record<string, unknown>;
    if (version === FILE_VERSION) {
      return parseViewState(state);
    }
    // v1 never had widths: a file that claims some is not a v1 file.
    if (version === LEGACY_FILE_VERSION && !Object.hasOwn(state, 'layout')) {
      return parseViewState({ ...state, layout: { ...DEFAULT_LAYOUT } });
    }
    return null;
  } catch {
    return null;
  }
}

export class ViewStateStore {
  readonly #directory: string | null;
  readonly #file: string | null;
  readonly #temporary: string | null;
  #tail: Promise<unknown> = Promise.resolve();

  /** `null` (test mode without a data folder) turns the store into a no-op. */
  constructor(dataDir: string | null) {
    this.#directory = dataDir;
    this.#file = dataDir === null ? null : join(dataDir, VIEW_STATE_FILE);
    this.#temporary = this.#file === null ? null : `${this.#file}.tmp`;
  }

  /** The remembered state; null when there is none or the file is missing or unusable. */
  read(): Promise<ViewState | null> {
    return this.#enqueue(async () => {
      if (this.#file === null) {
        return null;
      }
      try {
        return parseFile(await readFile(this.#file, 'utf8'));
      } catch {
        return null;
      }
    });
  }

  /** Remembers `state`, or forgets it for null. Rejects when the file cannot be written. */
  write(state: ViewState | null): Promise<void> {
    return this.#enqueue(async () => {
      if (this.#directory === null || this.#file === null || this.#temporary === null) {
        return;
      }
      if (state === null) {
        await rm(this.#file, { force: true });
        return;
      }
      // Built field by field: whatever else the object carries is never written.
      const document = {
        version: FILE_VERSION,
        project_id: state.project_id,
        tab: state.tab,
        layout: { list_px: state.layout.list_px, details_px: state.layout.details_px },
      };
      await mkdir(this.#directory, { recursive: true });
      await writeFile(this.#temporary, `${JSON.stringify(document)}\n`, 'utf8');
      await rename(this.#temporary, this.#file);
    });
  }

  /** Reads and writes run one after another, so the last call always wins. */
  #enqueue<T>(task: () => Promise<T>): Promise<T> {
    const run = this.#tail.then(task);
    this.#tail = run.catch(() => undefined);
    return run;
  }
}

/** Registers `app:get-view-state` and `app:set-view-state`; each checks its sender first. */
export function registerViewStateIpc(
  ipc: IpcRegistry,
  store: ViewStateStore,
  getTrustedPage: () => TrustedPage | null,
): void {
  const assertTrusted = (event: IpcMainInvokeEvent): void => {
    if (!isTrustedSender(toIpcSender(event), getTrustedPage())) {
      throw new Error('Недозволений відправник IPC');
    }
  };

  ipc.handle(VIEW_STATE_GET_CHANNEL, async (event) => {
    assertTrusted(event);
    return await store.read();
  });

  ipc.handle(VIEW_STATE_SET_CHANNEL, async (event, payload): Promise<SaveViewStateResult> => {
    assertTrusted(event);
    let state: ViewState | null;
    try {
      state = payload === null ? null : parseViewState(payload);
    } catch {
      return apiFailure('INVALID_PARAMS', 'Некоректні параметри запиту');
    }
    try {
      await store.write(state);
    } catch {
      // The message carries no file system text: it could name a path.
      return apiFailure('VIEW_STATE_NOT_SAVED', "Не вдалося запам'ятати відкритий проєкт");
    }
    return { ok: true, data: { saved: true } };
  });
}
