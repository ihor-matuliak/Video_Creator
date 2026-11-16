// @vitest-environment node
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import type { IpcMainInvokeEvent } from 'electron';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { INVALID_API_KEY_MESSAGE, apiFailure } from '../shared/bridge';
import type { ApiResult } from '../shared/bridge';
import {
  API_KEY_FILE,
  API_KEY_NOT_REMOVED_MESSAGE,
  API_KEY_NOT_SAVED_MESSAGE,
  API_KEY_UNREADABLE_MESSAGE,
  ApiKeyService,
  registerApiKeyIpc,
} from './api-key';
import type { SecretStorage } from './api-key';
import type { ApiBackend, IpcRegistry } from './api-gateway';
import type { TrustedPage } from './ipc-guard';

const KEY = 'sk-or-v1-0123456789abcdefghij';
const KEY2 = 'sk-or-v1-zyxwvutsrqponmlkjihg';
const PAGE = 'file:///C:/app/out/renderer/index.html';
const TRUSTED: TrustedPage = { webContentsId: 7, pageUrl: PAGE };
const FRAME = { url: PAGE };

let dataDir = '';

beforeEach(() => {
  dataDir = mkdtempSync(join(tmpdir(), 'clipforge-api-key-'));
});

afterEach(() => {
  rmSync(dataDir, { recursive: true, force: true });
});

/** A stand-in for DPAPI: reversible, and its output does not contain the text. */
class FakeStorage implements SecretStorage {
  available = true;

  isEncryptionAvailable(): boolean {
    return this.available;
  }

  encryptString(plainText: string): Buffer {
    return Buffer.concat([Buffer.from('v10'), Buffer.from(plainText, 'utf8').map((byte) => byte ^ 0x5a)]);
  }

  decryptString(encrypted: Buffer): string {
    if (encrypted.subarray(0, 3).toString() !== 'v10') {
      throw new Error('cannot decrypt');
    }
    return Buffer.from(encrypted.subarray(3).map((byte) => byte ^ 0x5a)).toString('utf8');
  }
}

type Request = ApiBackend['request'];
type Answer = ApiResult<unknown>;

const OK: Answer = { ok: true, data: null };

interface Harness {
  readonly service: ApiKeyService;
  readonly storage: FakeStorage;
  readonly request: ReturnType<typeof vi.fn<Request>>;
  readonly log: ReturnType<typeof vi.fn<(message: string) => void>>;
  /** Whether the sidecar counts as running; a test may change it between calls. */
  readonly backend: { running: boolean };
}

function setup(
  options: {
    readonly dir?: string | null;
    readonly storage?: FakeStorage;
    readonly answer?: Answer;
    /** Default true: a sidecar that is up, as it is when a user presses «Зберегти ключ». */
    readonly running?: boolean;
  } = {},
): Harness {
  const storage = options.storage ?? new FakeStorage();
  const request = vi.fn<Request>(() => Promise.resolve(options.answer ?? OK));
  const log = vi.fn<(message: string) => void>();
  const backend = { running: options.running ?? true };
  const service = new ApiKeyService({
    dataDir: options.dir === undefined ? dataDir : options.dir,
    storage,
    backend: { request },
    backendRunning: () => backend.running,
    log,
  });
  return { service, storage, request, log, backend };
}

function keyFile(): string {
  return join(dataDir, API_KEY_FILE);
}

function temporaryFile(): string {
  return `${keyFile()}.tmp`;
}

/** Every file of the folder, as bytes: none may hold the key in plain form. */
function folderHolds(text: string): boolean {
  return readdirSync(dataDir).some((name) => {
    const bytes = readFileSync(join(dataDir, name));
    return bytes.includes(Buffer.from(text, 'utf8')) || bytes.includes(Buffer.from(text, 'latin1'));
  });
}

const NOT_READY = apiFailure('BACKEND_NOT_READY', 'Локальний рушій ще запускається', true);
/** What `Sidecar.request` says both for a failed sidecar and for a transport failure of a running one. */
const UNAVAILABLE = apiFailure('BACKEND_UNAVAILABLE', 'Локальний рушій недоступний');
const TIMED_OUT = apiFailure('REQUEST_TIMEOUT', 'Локальний рушій не відповів вчасно');
const REFUSED = apiFailure('VALIDATION_ERROR', 'Некоректні дані');

describe('ApiKeyService: reading the state', () => {
  it('has no key before one was saved, and no message', async () => {
    const { service, request } = setup();

    expect(await service.getStatus()).toEqual({ state: 'none', message: null });
    expect(request).not.toHaveBeenCalled();
  });

  it('is `none` for a service without a data folder and keeps a saved key in memory only', async () => {
    const { service } = setup({ dir: null });

    const saved = await service.set(KEY);

    expect(saved).toEqual({ ok: true, data: { state: 'session', message: null } });
    expect(readdirSync(dataDir)).toEqual([]);
  });
});

describe('ApiKeyService.set: with system protection', () => {
  it('validates, gives the key to the backend, writes it encrypted and answers `protected`', async () => {
    const { service, request } = setup();

    const result = await service.set(KEY);

    expect(result).toEqual({ ok: true, data: { state: 'protected', message: null } });
    expect(request.mock.calls).toEqual([
      ['POST', '/v1/runtime/credentials', { openrouter_api_key: KEY }],
    ]);
    expect(existsSync(keyFile())).toBe(true);
    expect(folderHolds(KEY)).toBe(false);
    expect(readdirSync(dataDir)).toEqual([API_KEY_FILE]);
    expect(await service.getStatus()).toEqual({ state: 'protected', message: null });
  });

  it('never returns the key: not in a result, not in a status', async () => {
    const { service } = setup();

    const answers = [await service.set(KEY), await service.getStatus(), await service.forget()];

    expect(JSON.stringify(answers)).not.toContain(KEY);
  });

  it('replaces the stored key with the new one', async () => {
    const { service } = setup();
    await service.set(KEY);

    await service.set(KEY2);

    expect(readdirSync(dataDir)).toEqual([API_KEY_FILE]);
    const next = setup();
    await next.service.syncToBackend();
    expect(next.request.mock.calls).toEqual([
      ['POST', '/v1/runtime/credentials', { openrouter_api_key: KEY2 }],
    ]);
  });

  it('a new service on the same folder finds the key and gives it to the backend on sync', async () => {
    await setup().service.set(KEY);
    const { service, request } = setup();

    expect(await service.getStatus()).toEqual({ state: 'protected', message: null });
    await service.syncToBackend();

    expect(request.mock.calls).toEqual([
      ['POST', '/v1/runtime/credentials', { openrouter_api_key: KEY }],
    ]);
  });

  it.each([
    ['too short', 'k'.repeat(19)],
    ['with a space', `${'k'.repeat(15)} ${'k'.repeat(15)}`],
    ['with a newline', `${KEY}\n`],
    ['non-ASCII', `${'k'.repeat(25)}ї`],
    ['too long', 'k'.repeat(201)],
    ['a number', 5],
    ['null', null],
    ['an object', { key: KEY }],
  ])('refuses a key that is %s with a fixed text, and touches nothing', async (_name, candidate) => {
    const { service, request } = setup();

    const result = await service.set(candidate);

    expect(result).toEqual({
      ok: false,
      error: { code: 'INVALID_API_KEY', message: INVALID_API_KEY_MESSAGE, retryable: false },
    });
    if (typeof candidate === 'string') {
      expect(JSON.stringify(result)).not.toContain(candidate);
    }
    expect(request).not.toHaveBeenCalled();
    expect(readdirSync(dataDir)).toEqual([]);
  });

  it('keeps the key when the sidecar is not running: it is given to it on ready', async () => {
    const { service } = setup({ answer: NOT_READY });

    expect(await service.set(KEY)).toEqual({ ok: true, data: { state: 'protected', message: null } });
    expect(existsSync(keyFile())).toBe(true);
  });

  it('changes nothing when the backend refuses the key, and does not echo it in the error', async () => {
    const { service, request } = setup();
    await service.set(KEY);
    request.mockResolvedValueOnce(
      apiFailure('INVALID_API_KEY', `Ключ ${KEY2} не прийнято`),
    );

    const result = await service.set(KEY2);

    expect(result).toMatchObject({ ok: false, error: { code: 'INVALID_API_KEY' } });
    expect(JSON.stringify(result)).not.toContain(KEY2);
    expect(await service.getStatus()).toEqual({ state: 'protected', message: null });
    const next = setup();
    await next.service.syncToBackend();
    expect(next.request.mock.calls).toEqual([
      ['POST', '/v1/runtime/credentials', { openrouter_api_key: KEY }],
    ]);
  });

  it('reports a failed write, puts the backend back and keeps no key', async () => {
    // The "folder" is a file: it cannot be created or written into.
    const blocker = join(dataDir, 'not-a-folder');
    writeFileSync(blocker, 'x');
    const { service, request } = setup({ dir: blocker });

    const result = await service.set(KEY);

    expect(result).toEqual({
      ok: false,
      error: { code: 'API_KEY_NOT_SAVED', message: API_KEY_NOT_SAVED_MESSAGE, retryable: false },
    });
    expect(request.mock.calls.map(([method]) => method)).toEqual(['POST', 'DELETE']);
    expect(await service.getStatus()).toEqual({ state: 'none', message: null });
  });

  it('leaves no temporary file when the write fails after it was started', async () => {
    const { service } = setup();
    // The place of the key file is a non-empty folder: the temporary file is written, the rename cannot be done.
    mkdirSync(keyFile());
    writeFileSync(join(keyFile(), 'inner'), 'x');

    const result = await service.set(KEY);

    expect(result).toEqual({
      ok: false,
      error: { code: 'API_KEY_NOT_SAVED', message: API_KEY_NOT_SAVED_MESSAGE, retryable: false },
    });
    expect(existsSync(temporaryFile())).toBe(false);
    expect(readdirSync(dataDir)).toEqual([API_KEY_FILE]);
  });

  it('keeps the key when the sidecar has failed: it holds none, and gets it on its next ready', async () => {
    const { service } = setup({ answer: UNAVAILABLE, running: false });

    expect(await service.set(KEY)).toEqual({ ok: true, data: { state: 'protected', message: null } });
    expect(existsSync(keyFile())).toBe(true);
  });

  it.each([
    ['a transport failure', 'BACKEND_UNAVAILABLE', UNAVAILABLE],
    ['a timeout', 'REQUEST_TIMEOUT', TIMED_OUT],
  ])('does not report success when a running sidecar fails the call: %s', async (_name, code, answer) => {
    const { service, request } = setup({ answer });

    const result = await service.set(KEY);

    expect(result).toMatchObject({ ok: false, error: { code } });
    expect(JSON.stringify(result)).not.toContain(KEY);
    expect(request).toHaveBeenCalledTimes(1);
    expect(readdirSync(dataDir)).toEqual([]);
    expect(await service.getStatus()).toEqual({ state: 'none', message: null });
  });

  it('keeps the earlier key when a running sidecar does not get the new one', async () => {
    const { service, request } = setup();
    await service.set(KEY);
    request.mockResolvedValueOnce(UNAVAILABLE);

    const result = await service.set(KEY2);

    expect(result).toMatchObject({ ok: false, error: { code: 'BACKEND_UNAVAILABLE' } });
    expect(await service.getStatus()).toEqual({ state: 'protected', message: null });
    const next = setup();
    await next.service.syncToBackend();
    expect(next.request.mock.calls).toEqual([
      ['POST', '/v1/runtime/credentials', { openrouter_api_key: KEY }],
    ]);
  });
});

describe('ApiKeyService.set: without system protection', () => {
  it('keeps the key in memory only: `session`, no file', async () => {
    const storage = new FakeStorage();
    storage.available = false;
    const { service, request } = setup({ storage });

    const result = await service.set(KEY);

    expect(result).toEqual({ ok: true, data: { state: 'session', message: null } });
    expect(readdirSync(dataDir)).toEqual([]);
    expect(request).toHaveBeenCalledTimes(1);
    await service.syncToBackend();
    expect(request).toHaveBeenCalledTimes(2);
  });

  it('forgets the key with the process: a new service has none', async () => {
    const storage = new FakeStorage();
    storage.available = false;
    await setup({ storage }).service.set(KEY);

    expect(await setup({ storage }).service.getStatus()).toEqual({ state: 'none', message: null });
  });

  it('removes an older encrypted file so that the old key cannot come back', async () => {
    await setup().service.set(KEY);
    const storage = new FakeStorage();
    storage.available = false;

    await setup({ storage }).service.set(KEY2);

    expect(existsSync(keyFile())).toBe(false);
    expect(await setup().service.getStatus()).toEqual({ state: 'none', message: null });
  });

  it('removes a leftover temporary file too', async () => {
    writeFileSync(temporaryFile(), Buffer.from('leftover'));
    const storage = new FakeStorage();
    storage.available = false;

    await setup({ storage }).service.set(KEY);

    expect(readdirSync(dataDir)).toEqual([]);
  });
});

describe('ApiKeyService: a stored key that cannot be used', () => {
  it('garbage in the file: no key and a fixed message, the file is left alone', async () => {
    writeFileSync(keyFile(), Buffer.from('not a key at all'));
    const { service, request } = setup();

    expect(await service.getStatus()).toEqual({ state: 'none', message: API_KEY_UNREADABLE_MESSAGE });
    await service.syncToBackend();
    expect(request).not.toHaveBeenCalled();
    expect(existsSync(keyFile())).toBe(true);
  });

  it('a file that decrypts to something that is not a key counts the same', async () => {
    writeFileSync(keyFile(), new FakeStorage().encryptString('short'));

    expect(await setup().service.getStatus()).toEqual({
      state: 'none',
      message: API_KEY_UNREADABLE_MESSAGE,
    });
  });

  it('a file when the system cannot decrypt now: no key and the message', async () => {
    await setup().service.set(KEY);
    const storage = new FakeStorage();
    storage.available = false;

    expect(await setup({ storage }).service.getStatus()).toEqual({
      state: 'none',
      message: API_KEY_UNREADABLE_MESSAGE,
    });
  });

  it('a file that cannot be read (a folder in its place) is no key either', async () => {
    const { service } = setup();
    // `openrouter-key.bin` is a directory: reading it fails with something other than "missing".
    mkdirSync(keyFile());

    expect(await service.getStatus()).toEqual({ state: 'none', message: API_KEY_UNREADABLE_MESSAGE });
  });

  it('the message goes away when a key is saved, and when the file is forgotten', async () => {
    writeFileSync(keyFile(), Buffer.from('garbage'));
    const first = setup();
    expect((await first.service.getStatus()).message).toBe(API_KEY_UNREADABLE_MESSAGE);
    expect(await first.service.set(KEY)).toEqual({ ok: true, data: { state: 'protected', message: null } });

    writeFileSync(keyFile(), Buffer.from('garbage'));
    const second = setup();
    expect((await second.service.getStatus()).message).toBe(API_KEY_UNREADABLE_MESSAGE);
    expect(await second.service.forget()).toEqual({ ok: true, data: { state: 'none', message: null } });
    expect(existsSync(keyFile())).toBe(false);
  });
});

describe('ApiKeyService.forget', () => {
  it('removes the file and the copy in main, and tells the backend with DELETE', async () => {
    const { service, request } = setup();
    await service.set(KEY);
    request.mockClear();

    const result = await service.forget();

    expect(result).toEqual({ ok: true, data: { state: 'none', message: null } });
    expect(request.mock.calls).toEqual([['DELETE', '/v1/runtime/credentials']]);
    expect(readdirSync(dataDir)).toEqual([]);
    expect(await service.getStatus()).toEqual({ state: 'none', message: null });
    await service.syncToBackend();
    expect(request).toHaveBeenCalledTimes(1);
  });

  it('works for a session key too, and when there is nothing to forget', async () => {
    const storage = new FakeStorage();
    storage.available = false;
    const { service } = setup({ storage });
    await service.set(KEY);

    expect(await service.forget()).toEqual({ ok: true, data: { state: 'none', message: null } });
    expect(await service.forget()).toEqual({ ok: true, data: { state: 'none', message: null } });
  });

  it('is fine when the sidecar is not running', async () => {
    const { service, request } = setup();
    await service.set(KEY);
    request.mockResolvedValue(NOT_READY);

    expect(await service.forget()).toEqual({ ok: true, data: { state: 'none', message: null } });
  });

  it('is fine when the sidecar has failed: it holds no key, and a new process starts without one', async () => {
    const { service, request, backend } = setup();
    await service.set(KEY);
    request.mockResolvedValue(UNAVAILABLE);
    backend.running = false;

    expect(await service.forget()).toEqual({ ok: true, data: { state: 'none', message: null } });
  });

  it('reports the backend when it does not confirm, but the key is gone here', async () => {
    const { service, request } = setup();
    await service.set(KEY);
    request.mockResolvedValueOnce(REFUSED);

    const result = await service.forget();

    expect(result).toMatchObject({ ok: false, error: { code: 'VALIDATION_ERROR' } });
    expect(await service.getStatus()).toEqual({ state: 'none', message: null });
    expect(existsSync(keyFile())).toBe(false);
  });

  it.each([
    ['a transport failure', 'BACKEND_UNAVAILABLE', UNAVAILABLE],
    ['a timeout', 'REQUEST_TIMEOUT', TIMED_OUT],
  ])(
    'does not report success when a running sidecar fails the DELETE: %s; main holds no key',
    async (_name, code, answer) => {
      const { service, request } = setup();
      await service.set(KEY);
      request.mockResolvedValueOnce(answer);

      const result = await service.forget();

      expect(result).toMatchObject({ ok: false, error: { code } });
      expect(await service.getStatus()).toEqual({ state: 'none', message: null });
      expect(readdirSync(dataDir)).toEqual([]);
    },
  );

  it('sends the DELETE again on a retry, though main holds no key any more: the backend may still have it', async () => {
    const { service, request } = setup();
    await service.set(KEY);
    request.mockClear();
    request.mockResolvedValueOnce(UNAVAILABLE);

    const first = await service.forget();
    const second = await service.forget();

    expect(first).toMatchObject({ ok: false, error: { code: 'BACKEND_UNAVAILABLE' } });
    // Nothing is stored in main by now, and the retry still reaches the backend.
    expect(second).toEqual({ ok: true, data: { state: 'none', message: null } });
    expect(request.mock.calls).toEqual([
      ['DELETE', '/v1/runtime/credentials'],
      ['DELETE', '/v1/runtime/credentials'],
    ]);
    expect(readdirSync(dataDir)).toEqual([]);
  });

  it('keeps failing while a running backend keeps refusing the retry', async () => {
    const { service, request } = setup();
    await service.set(KEY);
    request.mockResolvedValue(UNAVAILABLE);

    expect(await service.forget()).toMatchObject({ ok: false, error: { code: 'BACKEND_UNAVAILABLE' } });
    expect(await service.forget()).toMatchObject({ ok: false, error: { code: 'BACKEND_UNAVAILABLE' } });
    expect(await service.getStatus()).toEqual({ state: 'none', message: null });
  });

  it('removes a leftover temporary file too, also when there is no key file', async () => {
    const { service } = setup();
    await service.set(KEY);
    writeFileSync(temporaryFile(), Buffer.from('leftover'));

    expect(await service.forget()).toEqual({ ok: true, data: { state: 'none', message: null } });
    expect(readdirSync(dataDir)).toEqual([]);

    writeFileSync(temporaryFile(), Buffer.from('leftover'));
    expect(await service.forget()).toEqual({ ok: true, data: { state: 'none', message: null } });
    expect(readdirSync(dataDir)).toEqual([]);
  });

  it('changes nothing when the temporary file cannot be removed: the stored key stays', async () => {
    const { service } = setup();
    await service.set(KEY);
    mkdirSync(temporaryFile());
    writeFileSync(join(temporaryFile(), 'inner'), 'x');

    const result = await service.forget();

    expect(result).toEqual({
      ok: false,
      error: { code: 'API_KEY_NOT_REMOVED', message: API_KEY_NOT_REMOVED_MESSAGE, retryable: false },
    });
    expect(existsSync(keyFile())).toBe(true);
    expect(await service.getStatus()).toEqual({ state: 'protected', message: null });
  });

  it('changes nothing when the file cannot be removed', async () => {
    const { service } = setup();
    await service.set(KEY);
    // A key file that is a non-empty folder cannot be removed as a file.
    rmSync(keyFile());
    mkdirSync(keyFile());
    writeFileSync(join(keyFile(), 'inner'), 'x');

    const result = await service.forget();

    expect(result).toEqual({
      ok: false,
      error: { code: 'API_KEY_NOT_REMOVED', message: API_KEY_NOT_REMOVED_MESSAGE, retryable: false },
    });
    expect(await service.getStatus()).toEqual({ state: 'protected', message: null });
  });
});

describe('ApiKeyService.syncToBackend and settled', () => {
  it('does nothing without a key', async () => {
    const { service, request } = setup();

    await service.syncToBackend();

    expect(request).not.toHaveBeenCalled();
  });

  it('logs a refusal by its code and never the key', async () => {
    await setup().service.set(KEY);
    const { service, log } = setup({ answer: apiFailure('INVALID_API_KEY', `Ключ ${KEY} не прийнято`) });

    await service.syncToBackend();

    expect(log).toHaveBeenCalledTimes(1);
    expect(log.mock.calls[0]?.[0]).toContain('INVALID_API_KEY');
    expect(JSON.stringify(log.mock.calls)).not.toContain(KEY);
  });

  it('is quiet when the sidecar is not there', async () => {
    await setup().service.set(KEY);
    const { service, log } = setup({ answer: NOT_READY });

    await service.syncToBackend();

    expect(log).not.toHaveBeenCalled();
  });

  it('is quiet for a sidecar that has failed, and logs a transport failure of a running one by its code', async () => {
    await setup().service.set(KEY);
    const failed = setup({ answer: UNAVAILABLE, running: false });
    const running = setup({ answer: UNAVAILABLE });

    await failed.service.syncToBackend();
    await running.service.syncToBackend();

    expect(failed.log).not.toHaveBeenCalled();
    expect(running.log).toHaveBeenCalledTimes(1);
    expect(running.log.mock.calls[0]?.[0]).toContain('BACKEND_UNAVAILABLE');
    expect(JSON.stringify(running.log.mock.calls)).not.toContain(KEY);
  });

  it('settled waits for a sync that is still on its way', async () => {
    await setup().service.set(KEY);
    const { service, request } = setup();
    let release: (answer: Answer) => void = () => undefined;
    request.mockImplementationOnce(
      () =>
        new Promise<Answer>((resolve) => {
          release = resolve;
        }),
    );
    void service.syncToBackend();
    let done = false;
    void service.settled().then(() => {
      done = true;
    });

    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(done).toBe(false);
    release(OK);
    await service.settled();

    expect(done).toBe(true);
  });

  it('runs calls one after another: a forget queued behind a sync wins', async () => {
    await setup().service.set(KEY);
    const { service, request } = setup();

    const sync = service.syncToBackend();
    const forget = service.forget();
    await Promise.all([sync, forget]);

    expect(request.mock.calls.map(([method]) => method)).toEqual(['POST', 'DELETE']);
    expect(await service.getStatus()).toEqual({ state: 'none', message: null });
  });
});

describe('registerApiKeyIpc', () => {
  type Handler = (event: IpcMainInvokeEvent, ...args: unknown[]) => unknown;

  function event(webContentsId: number): IpcMainInvokeEvent {
    return {
      sender: { id: webContentsId, mainFrame: FRAME },
      senderFrame: FRAME,
    } as unknown as IpcMainInvokeEvent;
  }

  function register(trusted: TrustedPage | null = TRUSTED): {
    readonly handlers: Map<string, Handler>;
    readonly request: Harness['request'];
  } {
    const handlers = new Map<string, Handler>();
    const registry: IpcRegistry = {
      handle: (channel, listener) => {
        handlers.set(channel, listener);
      },
    };
    const { service, request } = setup();
    registerApiKeyIpc(registry, service, () => trusted);
    return { handlers, request };
  }

  function handler(handlers: Map<string, Handler>, channel: string): Handler {
    const found = handlers.get(channel);
    if (found === undefined) {
      throw new Error(`no handler for ${channel}`);
    }
    return found;
  }

  it('registers exactly the three key channels', () => {
    expect([...register().handlers.keys()].sort()).toEqual([
      'settings:forget-api-key',
      'settings:get-api-key-status',
      'settings:set-api-key',
    ]);
  });

  it('saves, reads and forgets the key for the app page, and answers statuses only', async () => {
    const { handlers } = register();
    const set = handler(handlers, 'settings:set-api-key');
    const get = handler(handlers, 'settings:get-api-key-status');
    const forget = handler(handlers, 'settings:forget-api-key');

    expect(await get(event(7))).toEqual({ state: 'none', message: null });
    const saved = await set(event(7), KEY);
    expect(saved).toEqual({ ok: true, data: { state: 'protected', message: null } });
    expect(await get(event(7))).toEqual({ state: 'protected', message: null });
    expect(await forget(event(7))).toEqual({ ok: true, data: { state: 'none', message: null } });
    expect(JSON.stringify(saved)).not.toContain(KEY);
  });

  it('answers a key of the wrong shape with the fixed error, without echoing it', async () => {
    const { handlers, request } = register();

    const result = await handler(handlers, 'settings:set-api-key')(event(7), 'short');

    expect(result).toMatchObject({ ok: false, error: { code: 'INVALID_API_KEY' } });
    expect(JSON.stringify(result)).not.toContain('short');
    expect(request).not.toHaveBeenCalled();
  });

  it.each(['settings:set-api-key', 'settings:forget-api-key', 'settings:get-api-key-status'])(
    '%s is refused for a foreign sender and with no page at all',
    async (channel) => {
      const foreign = register();
      const none = register(null);

      await expect(Promise.resolve().then(() => handler(foreign.handlers, channel)(event(8), KEY))).rejects.toThrow(
        'Недозволений відправник IPC',
      );
      await expect(Promise.resolve().then(() => handler(none.handlers, channel)(event(7), KEY))).rejects.toThrow(
        'Недозволений відправник IPC',
      );
      expect(foreign.request).not.toHaveBeenCalled();
      expect(none.request).not.toHaveBeenCalled();
    },
  );
});
