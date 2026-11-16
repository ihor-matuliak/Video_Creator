import { mkdir, readFile, rename, rm, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

import type { IpcMainInvokeEvent } from 'electron';

import {
  API_KEY_FORGET_CHANNEL,
  API_KEY_SET_CHANNEL,
  API_KEY_STATUS_CHANNEL,
  INVALID_API_KEY_MESSAGE,
  apiFailure,
  isApiKey,
} from '../shared/bridge';
import type { ApiFailure, ApiKeyState, ApiKeyStatus, ApiResult } from '../shared/bridge';
import { toIpcSender } from './api-gateway';
import type { ApiBackend, IpcRegistry } from './api-gateway';
import { isTrustedSender } from './ipc-guard';
import type { TrustedPage } from './ipc-guard';

/**
 * The OpenRouter key (ADR 034). It lives in main: the renderer hands it over once
 * (`settings:set-api-key`) and never gets it back, and it never goes through `api:call`.
 *
 * Where it is kept depends on the system. When `safeStorage` can encrypt (DPAPI on Windows) the
 * encrypted bytes are written to one file of the data folder, through a temporary file and a
 * rename, and the state is `protected`. Otherwise the key stays only in the memory of main
 * (`session`), nothing is written, and an older file is removed so that it cannot come back. A
 * file that cannot be read or decrypted counts as no key (`none`) with a fixed message; it is left
 * where it is until the user saves or forgets a key.
 *
 * The backend holds its own copy in memory only. Main gives it the key on every `ready` of the
 * sidecar (`syncToBackend`) and removes it there when the user forgets it. The key is never logged:
 * the log lines carry error codes only.
 *
 * "No backend" means a sidecar that is not running (still starting, or failed): it holds nothing,
 * and the next `ready` brings the key. A call that fails on a sidecar that is running (it does not
 * answer, or answers with a refusal) is a real failure and is reported, never counted as success.
 * The temporary file of a write is removed after a failed write and on forget.
 */
export const API_KEY_FILE = 'openrouter-key.bin';

const CREDENTIALS_PATH = '/v1/runtime/credentials';

export const API_KEY_UNREADABLE_MESSAGE =
  'Збережений ключ не вдалося прочитати. Введіть ключ ще раз';
export const API_KEY_NOT_SAVED_MESSAGE =
  "Не вдалося зберегти ключ на цьому комп'ютері. Спробуйте ще раз";
export const API_KEY_NOT_REMOVED_MESSAGE =
  'Не вдалося видалити збережений ключ. Спробуйте ще раз';

/** The part of Electron's `safeStorage` used here; looked up at call time. */
export interface SecretStorage {
  isEncryptionAvailable(): boolean;
  encryptString(plainText: string): Buffer;
  decryptString(encrypted: Buffer): string;
}

export interface ApiKeyServiceOptions {
  /** The workspace folder; null (test mode without one) keeps the key in memory only. */
  readonly dataDir: string | null;
  readonly storage: SecretStorage;
  readonly backend: ApiBackend;
  /**
   * Whether the sidecar is running (`ready`) right now. `Sidecar.request` answers BACKEND_UNAVAILABLE
   * both for a failed sidecar and for a transport failure of a running one; this tells them apart.
   */
  readonly backendRunning: () => boolean;
  readonly log?: (message: string) => void;
}

function isMissing(error: unknown): boolean {
  return typeof error === 'object' && error !== null && 'code' in error && error.code === 'ENOENT';
}

export class ApiKeyService {
  readonly #file: string | null;
  readonly #temporary: string | null;
  readonly #directory: string | null;
  readonly #storage: SecretStorage;
  readonly #backend: ApiBackend;
  readonly #backendRunning: () => boolean;
  readonly #log: (message: string) => void;
  #key: string | null = null;
  /** The key is the one in the encrypted file. */
  #stored = false;
  #problem: string | null = null;
  #loaded = false;
  #tail: Promise<unknown> = Promise.resolve();

  constructor(options: ApiKeyServiceOptions) {
    this.#directory = options.dataDir;
    this.#file = options.dataDir === null ? null : join(options.dataDir, API_KEY_FILE);
    this.#temporary = this.#file === null ? null : `${this.#file}.tmp`;
    this.#storage = options.storage;
    this.#backend = options.backend;
    this.#backendRunning = options.backendRunning;
    this.#log = options.log ?? ((message) => { console.error(message); });
  }

  getStatus(): Promise<ApiKeyStatus> {
    return this.#enqueue(async () => {
      await this.#ensureLoaded();
      return this.#status();
    });
  }

  /**
   * Checks the key, gives it to the backend, then keeps it. A key of the wrong shape is refused
   * with a fixed text. The backend is asked first: when it refuses (anything but "not running"),
   * nothing changes here. When keeping it fails, the backend is put back to what main holds.
   */
  set(candidate: unknown): Promise<ApiResult<ApiKeyStatus>> {
    if (!isApiKey(candidate)) {
      return Promise.resolve(apiFailure('INVALID_API_KEY', INVALID_API_KEY_MESSAGE));
    }
    const key = candidate;
    return this.#enqueue<ApiResult<ApiKeyStatus>>(async () => {
      await this.#ensureLoaded();
      const refused = await this.#push(key);
      if (refused !== null) {
        return refused;
      }
      let stored: boolean;
      try {
        stored = await this.#persist(key);
      } catch {
        await this.#restoreBackend();
        return apiFailure('API_KEY_NOT_SAVED', API_KEY_NOT_SAVED_MESSAGE);
      }
      this.#key = key;
      this.#stored = stored;
      this.#problem = null;
      return { ok: true, data: this.#status() };
    });
  }

  /**
   * Removes the stored file (and the temporary file of a write), the copy in main and the copy in
   * the backend. When a file cannot be removed nothing else changes, so the key is not half
   * forgotten. When the backend is running and does not confirm, the key is gone here and the
   * backend's error is reported: success is only answered when the backend confirmed, or is not
   * running (it then holds no key, and a new process starts without one).
   */
  forget(): Promise<ApiResult<ApiKeyStatus>> {
    return this.#enqueue<ApiResult<ApiKeyStatus>>(async () => {
      await this.#ensureLoaded();
      try {
        await this.#removeFiles();
      } catch {
        return apiFailure('API_KEY_NOT_REMOVED', API_KEY_NOT_REMOVED_MESSAGE);
      }
      this.#key = null;
      this.#stored = false;
      this.#problem = null;
      const result = await this.#backend.request('DELETE', CREDENTIALS_PATH);
      if (!result.ok && !this.#backendAbsent(result)) {
        return apiFailure(result.error.code, result.error.message, result.error.retryable);
      }
      return { ok: true, data: this.#status() };
    });
  }

  /**
   * Gives the key to the backend, if main holds one. Called on every `ready` of the sidecar: its
   * copy is only in its memory, so a new process starts without one. Never throws; a failure is
   * logged by its code.
   */
  syncToBackend(): Promise<void> {
    return this.#enqueue(async () => {
      await this.#ensureLoaded();
      if (this.#key === null) {
        return;
      }
      const refused = await this.#push(this.#key);
      if (refused !== null) {
        this.#log(`Не вдалося передати ключ локальному рушію: ${refused.error.code}`);
      }
    });
  }

  /** Resolves when every call made so far is done, the first sync after `ready` included. */
  settled(): Promise<void> {
    return this.#tail.then(() => undefined);
  }

  #status(): ApiKeyStatus {
    const state: ApiKeyState = this.#key === null ? 'none' : this.#stored ? 'protected' : 'session';
    return { state, message: state === 'none' ? this.#problem : null };
  }

  /**
   * True for a sidecar that is not running (BACKEND_NOT_READY while it starts; BACKEND_UNAVAILABLE
   * once it failed): nothing is lost, the key is given to it when it is ready. BACKEND_UNAVAILABLE
   * from a sidecar that is running is a transport failure of the call, which is not "no backend".
   */
  #backendAbsent(failure: ApiFailure): boolean {
    const { code } = failure.error;
    return code === 'BACKEND_NOT_READY' || (code === 'BACKEND_UNAVAILABLE' && !this.#backendRunning());
  }

  /** null: delivered, or there is no running backend to give it to; else the failure of the call. */
  async #push(key: string): Promise<ApiFailure | null> {
    const result = await this.#backend.request('POST', CREDENTIALS_PATH, { openrouter_api_key: key });
    if (result.ok || this.#backendAbsent(result)) {
      return null;
    }
    // Defence in depth: the backend does not echo the key, but the text that reaches the
    // renderer is made sure of here.
    return apiFailure(
      result.error.code,
      result.error.message.split(key).join('[redacted]'),
      result.error.retryable,
    );
  }

  async #restoreBackend(): Promise<void> {
    if (this.#key === null) {
      await this.#backend.request('DELETE', CREDENTIALS_PATH);
    } else {
      await this.#push(this.#key);
    }
  }

  /**
   * Writes the encrypted key and returns true, or (no system protection) removes the file and
   * returns false. A write that fails leaves no temporary file behind.
   */
  async #persist(key: string): Promise<boolean> {
    if (
      this.#directory === null ||
      this.#file === null ||
      this.#temporary === null ||
      !this.#storage.isEncryptionAvailable()
    ) {
      await this.#removeFiles();
      return false;
    }
    const encrypted = this.#storage.encryptString(key);
    await mkdir(this.#directory, { recursive: true });
    try {
      await writeFile(this.#temporary, encrypted);
      await rename(this.#temporary, this.#file);
    } catch (error) {
      // The encrypted key must not stay in a file nobody reads; the original failure is reported.
      await rm(this.#temporary, { force: true }).catch(() => undefined);
      throw error;
    }
    return true;
  }

  /** The temporary file goes first: when it cannot be removed, the stored key is still untouched. */
  async #removeFiles(): Promise<void> {
    if (this.#temporary !== null) {
      await rm(this.#temporary, { force: true });
    }
    if (this.#file !== null) {
      await rm(this.#file, { force: true });
    }
  }

  /** Reads the stored key once, the first time anything needs the state. */
  async #ensureLoaded(): Promise<void> {
    if (this.#loaded) {
      return;
    }
    this.#loaded = true;
    if (this.#file === null) {
      return;
    }
    let encrypted: Buffer;
    try {
      encrypted = await readFile(this.#file);
    } catch (error) {
      if (!isMissing(error)) {
        this.#problem = API_KEY_UNREADABLE_MESSAGE;
      }
      return;
    }
    const key = this.#decrypt(encrypted);
    if (key === null) {
      this.#problem = API_KEY_UNREADABLE_MESSAGE;
      return;
    }
    this.#key = key;
    this.#stored = true;
  }

  #decrypt(encrypted: Buffer): string | null {
    try {
      if (!this.#storage.isEncryptionAvailable()) {
        return null;
      }
      const plain = this.#storage.decryptString(encrypted);
      return isApiKey(plain) ? plain : null;
    } catch {
      return null;
    }
  }

  /** Calls run one after another, so a sync cannot overtake a save or a forget. */
  #enqueue<T>(task: () => Promise<T>): Promise<T> {
    const run = this.#tail.then(task);
    this.#tail = run.catch(() => undefined);
    return run;
  }
}

/** Registers the three key channels; each handler checks its sender first. */
export function registerApiKeyIpc(
  ipc: IpcRegistry,
  service: ApiKeyService,
  getTrustedPage: () => TrustedPage | null,
): void {
  const assertTrusted = (event: IpcMainInvokeEvent): void => {
    if (!isTrustedSender(toIpcSender(event), getTrustedPage())) {
      throw new Error('Недозволений відправник IPC');
    }
  };

  ipc.handle(API_KEY_SET_CHANNEL, async (event, payload) => {
    assertTrusted(event);
    return await service.set(payload);
  });
  ipc.handle(API_KEY_FORGET_CHANNEL, async (event) => {
    assertTrusted(event);
    return await service.forget();
  });
  ipc.handle(API_KEY_STATUS_CHANNEL, async (event) => {
    assertTrusted(event);
    return await service.getStatus();
  });
}
