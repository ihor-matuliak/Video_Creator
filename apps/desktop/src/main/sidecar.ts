import { spawn } from 'node:child_process';
import type {
  ChildProcessWithoutNullStreams,
  SpawnOptionsWithoutStdio,
} from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';

import { apiFailure } from '../shared/bridge';
import type { ApiResult, AppMode, AppState, AppStatus } from '../shared/bridge';
import {
  SIDECAR_ARGS,
  buildApiUrl,
  buildChildEnv,
  buildConfigLine,
  createSessionToken,
  parseApiErrorBody,
  parseHealthBody,
  parseReadyLine,
  redact,
} from './sidecar-protocol';
import type { ApiMethod } from './sidecar-protocol';

export type SpawnFn = (
  command: string,
  args: readonly string[],
  options: SpawnOptionsWithoutStdio,
) => ChildProcessWithoutNullStreams;

export interface SidecarTiming {
  /** Ready line and first healthy /v1/health must arrive within this time. */
  readonly startupTimeoutMs: number;
  /** Health poll interval while starting. */
  readonly startPollMs: number;
  /** Health poll interval once ready (liveness check). */
  readonly livePollMs: number;
  readonly requestTimeoutMs: number;
  /** Upper bound of one API call; source registration probes the file synchronously. */
  readonly apiTimeoutMs: number;
  /** After stdin EOF the sidecar gets this long to exit before it is killed. */
  readonly stopGraceMs: number;
}

const DEFAULT_TIMING: SidecarTiming = {
  startupTimeoutMs: 30_000,
  startPollMs: 100,
  livePollMs: 5_000,
  requestTimeoutMs: 2_000,
  apiTimeoutMs: 60_000,
  stopGraceMs: 5_000,
};

/** Consecutive failed health checks after which a ready sidecar counts as failed. */
const LIVENESS_FAILURE_LIMIT = 3;
const KILL_WAIT_MS = 2_000;
const STDOUT_LIMIT = 64 * 1024;
const STDERR_TAIL_LIMIT = 4 * 1024;
/** The only headers of a media answer that `fetchMedia` passes on. */
const MEDIA_HEADERS: readonly string[] = [
  'Content-Range',
  'Content-Length',
  'Accept-Ranges',
  'Content-Type',
];
const MEDIA_ERROR_HEADERS: readonly string[] = ['Content-Range', 'Accept-Ranges'];

export interface SidecarOptions {
  readonly mode: AppMode;
  /** null means the data directory could not be resolved; the sidecar then fails. */
  readonly dataDir: string | null;
  readonly pythonPath: string;
  readonly cwd: string;
  readonly env: NodeJS.ProcessEnv;
  readonly spawn?: SpawnFn;
  readonly fetch?: typeof fetch;
  readonly timing?: Partial<SidecarTiming>;
  readonly log?: (message: string) => void;
}

/**
 * Owns the Python sidecar child process (ADR 013). The session token and the
 * port exist only here: the token goes to the child through stdin and to the
 * sidecar in an Authorization header (health checks and `request`), nowhere
 * else (not argv, not the renderer, not logs, not any returned value).
 */
export class Sidecar {
  readonly #options: SidecarOptions;
  readonly #spawn: SpawnFn;
  readonly #fetch: typeof fetch;
  readonly #timing: SidecarTiming;
  readonly #log: (message: string) => void;
  readonly #abort = new AbortController();
  readonly #exit: Promise<void>;
  #resolveExit: () => void = () => undefined;
  #state: AppState = 'starting';
  #version: string | null = null;
  #child: ChildProcessWithoutNullStreams | null = null;
  #token = '';
  #port: number | null = null;
  #exited = false;
  #stopping = false;
  #stopPromise: Promise<void> | undefined;
  #readySeen = false;
  #stdoutBuffer = '';
  #stderrTail = '';
  #startupTimer: NodeJS.Timeout | undefined;
  readonly #readyListeners: (() => void)[] = [];

  constructor(options: SidecarOptions) {
    this.#options = options;
    this.#spawn = options.spawn ?? spawn;
    this.#fetch = options.fetch ?? fetch;
    this.#timing = { ...DEFAULT_TIMING, ...options.timing };
    this.#log = options.log ?? ((message) => { console.error(message); });
    this.#exit = new Promise<void>((resolve) => {
      this.#resolveExit = resolve;
    });
  }

  getStatus(): AppStatus {
    return { state: this.#state, mode: this.#options.mode, version: this.#version };
  }

  /**
   * Calls `listener` every time the sidecar becomes ready, right after `request` starts to work
   * and before any status answer can show `ready`. A listener that throws does not stop the
   * monitor. Register before `start()`.
   */
  onReady(listener: () => void): void {
    this.#readyListeners.push(listener);
  }

  start(): void {
    if (this.#child !== null || this.#state === 'failed') {
      return;
    }
    const { mode, dataDir, pythonPath, cwd, env } = this.#options;
    if (dataDir === null) {
      this.#fail('у тестовому режимі CLIPFORGE_DATA_DIR має бути абсолютним шляхом');
      return;
    }

    this.#token = createSessionToken();
    let child: ChildProcessWithoutNullStreams;
    try {
      child = this.#spawn(pythonPath, SIDECAR_ARGS, {
        cwd,
        env: buildChildEnv(mode, env),
        shell: false,
        windowsHide: true,
      });
    } catch (error) {
      this.#fail(`не вдалося запустити процес: ${describe(error)}`);
      return;
    }
    this.#child = child;

    child.once('error', (error) => {
      if (child.pid === undefined) {
        this.#markExited(); // spawn failed: 'exit' will not follow
      }
      this.#fail(`помилка процесу: ${error.message}`);
    });
    child.once('exit', (code, signal) => {
      this.#markExited();
      if (!this.#stopping) {
        this.#fail(
          `процес завершився (код ${String(code ?? 'немає')}, сигнал ${signal ?? 'немає'})`,
        );
      }
    });

    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (chunk: string) => {
      this.#onStdout(chunk);
    });
    child.stderr.setEncoding('utf8');
    child.stderr.on('data', (chunk: string) => {
      this.#stderrTail = (this.#stderrTail + chunk).slice(-STDERR_TAIL_LIMIT);
    });

    // A broken pipe also ends the process, which 'exit' reports.
    child.stdin.on('error', () => undefined);
    // stdin stays open on purpose: its EOF is the sidecar's shutdown signal.
    child.stdin.write(buildConfigLine({ mode, dataDir, token: this.#token }));

    this.#startupTimer = setTimeout(() => {
      this.#fail('перевищено час очікування запуску');
    }, this.#timing.startupTimeoutMs);
  }

  /**
   * One authenticated API call. `path` must be a fixed path under /v1/ built by
   * the caller from validated parts; the answer is reduced to an `ApiResult`
   * and the session token is removed from it. Never throws for transport or
   * backend failures. A failed or timed-out call is not retried here: a
   * mutation may have been applied.
   */
  async request(method: ApiMethod, path: string, body?: unknown): Promise<ApiResult<unknown>> {
    const port = this.#port;
    if (this.#state !== 'ready' || port === null) {
      return this.#state === 'failed'
        ? apiFailure('BACKEND_UNAVAILABLE', 'Локальний рушій недоступний')
        : apiFailure('BACKEND_NOT_READY', 'Локальний рушій ще запускається', true);
    }
    const url = buildApiUrl(port, path);
    if (url === null) {
      return apiFailure('INVALID_REQUEST', 'Некоректний запит до локального рушія');
    }

    const timeout = AbortSignal.timeout(this.#timing.apiTimeoutMs);
    let status: number;
    let ok: boolean;
    let raw: string;
    try {
      const response = await this.#fetch(url, {
        method,
        headers: {
          Authorization: `Bearer ${this.#token}`,
          Accept: 'application/json',
          ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
        redirect: 'error',
        signal: AbortSignal.any([this.#abort.signal, timeout]),
      });
      status = response.status;
      ok = response.ok;
      raw = await response.text();
    } catch {
      // The message of a transport error is not passed on: it may name the URL.
      return timeout.aborted
        ? apiFailure('REQUEST_TIMEOUT', 'Локальний рушій не відповів вчасно', method === 'GET')
        : apiFailure('BACKEND_UNAVAILABLE', 'Локальний рушій недоступний', method === 'GET');
    }

    const parsed = parseJsonBody(redact(raw, this.#token));
    if (parsed === undefined) {
      return apiFailure('BAD_BACKEND_RESPONSE', 'Локальний рушій повернув неочікувану відповідь');
    }
    return ok
      ? { ok: true, data: parsed.value }
      : { ok: false, error: parseApiErrorBody(parsed.value, status) };
  }

  /**
   * One authenticated GET of media bytes, streamed. `path` is a fixed path under
   * /v1/ built by the caller from validated parts; `range` is the renderer's Range
   * header, forwarded as is (the backend answers 200, 206 or 416). Never throws:
   * transport trouble becomes a bodyless 502/503/504.
   *
   * Only the status and Content-Range, Content-Length, Accept-Ranges and
   * Content-Type come back, so no other header can carry the session token out.
   * An answer other than 200/206 keeps just the status, Content-Range and
   * Accept-Ranges: its body (a JSON error) is dropped. The time limit covers the
   * wait for the response headers only; a body may stream for as long as it takes,
   * until the caller's `signal` aborts or the sidecar stops. Not retried.
   */
  async fetchMedia(path: string, range: string | null, signal?: AbortSignal): Promise<Response> {
    const port = this.#port;
    if (this.#state !== 'ready' || port === null) {
      return new Response(null, { status: 503 });
    }
    const url = buildApiUrl(port, path);
    if (url === null) {
      return new Response(null, { status: 400 });
    }

    const headersTimeout = new AbortController();
    const timer = setTimeout(() => {
      headersTimeout.abort();
    }, this.#timing.apiTimeoutMs);
    let upstream: Response;
    try {
      upstream = await this.#fetch(url, {
        method: 'GET',
        headers: {
          Authorization: `Bearer ${this.#token}`,
          ...(range === null ? {} : { Range: range }),
        },
        redirect: 'error',
        signal: AbortSignal.any([
          this.#abort.signal,
          headersTimeout.signal,
          ...(signal === undefined ? [] : [signal]),
        ]),
      });
    } catch {
      // The message of a transport error is not passed on: it may name the URL.
      return new Response(null, { status: headersTimeout.signal.aborted ? 504 : 502 });
    } finally {
      clearTimeout(timer);
    }

    const streamed = upstream.status === 200 || upstream.status === 206;
    const headers = new Headers();
    for (const name of streamed ? MEDIA_HEADERS : MEDIA_ERROR_HEADERS) {
      const value = upstream.headers.get(name);
      if (value !== null) {
        headers.set(name, redact(value, this.#token));
      }
    }
    if (!streamed) {
      await upstream.body?.cancel().catch(() => undefined);
      return new Response(null, { status: upstream.status, headers });
    }
    return new Response(upstream.body, { status: upstream.status, headers });
  }

  /** Graceful stop: stdin EOF, then kill of this child only if it does not exit. */
  stop(): Promise<void> {
    this.#stopping = true;
    this.#stopPromise ??= this.#shutdown();
    return this.#stopPromise;
  }

  /** Synchronous last resort for the process 'exit' hook; ends this child only. */
  terminateNow(): void {
    this.#stopping = true;
    this.#kill();
  }

  async #shutdown(): Promise<void> {
    clearTimeout(this.#startupTimer);
    this.#abort.abort();
    const child = this.#child;
    if (child === null || this.#exited) {
      return;
    }
    child.stdin.end();
    if (await this.#waitForExit(this.#timing.stopGraceMs)) {
      return;
    }
    this.#log('Локальний рушій не завершився після закриття stdin; завершуємо власний процес.');
    this.#kill();
    await this.#waitForExit(KILL_WAIT_MS);
  }

  #kill(): void {
    if (this.#child !== null && !this.#exited) {
      // On Windows the venv launcher puts the real interpreter in a job object,
      // so this ends the whole tree of this child and nothing else.
      this.#child.kill();
    }
  }

  async #waitForExit(timeoutMs: number): Promise<boolean> {
    const cancel = new AbortController();
    try {
      return await Promise.race([
        this.#exit.then(() => true),
        sleep(timeoutMs, false, { signal: cancel.signal }),
      ]);
    } finally {
      cancel.abort();
    }
  }

  #markExited(): void {
    this.#exited = true;
    this.#resolveExit();
  }

  #onStdout(chunk: string): void {
    if (this.#readySeen) {
      return; // keep draining the pipe; later output is not part of the contract
    }
    const lines = (this.#stdoutBuffer + chunk).split('\n');
    this.#stdoutBuffer = lines.pop() ?? '';
    if (this.#stdoutBuffer.length > STDOUT_LIMIT) {
      this.#stdoutBuffer = '';
    }
    for (const line of lines) {
      const port = parseReadyLine(line.trim());
      if (port !== null) {
        this.#readySeen = true;
        this.#port = port;
        void this.#monitor(port);
        return;
      }
    }
  }

  /** Polls /v1/health until healthy, then keeps checking liveness. */
  async #monitor(port: number): Promise<void> {
    const { signal } = this.#abort;
    let failures = 0;
    try {
      for (;;) {
        const version = await this.#checkHealth(port);
        if (signal.aborted) {
          return;
        }
        if (this.#state === 'starting') {
          if (version !== null) {
            clearTimeout(this.#startupTimer);
            this.#version = version;
            this.#state = 'ready';
            this.#notifyReady();
          }
        } else if (version === null) {
          failures += 1;
          if (failures >= LIVENESS_FAILURE_LIMIT) {
            this.#fail('локальний рушій перестав відповідати на /v1/health');
            return;
          }
        } else {
          failures = 0;
        }
        const wait = this.#state === 'ready' ? this.#timing.livePollMs : this.#timing.startPollMs;
        await sleep(wait, undefined, { signal });
      }
    } catch {
      // The pause was aborted by stop() or by a failure: nothing left to do.
    }
  }

  #notifyReady(): void {
    for (const listener of this.#readyListeners) {
      try {
        listener();
      } catch {
        // A listener is not allowed to break the health monitor; it reports its own trouble.
      }
    }
  }

  async #checkHealth(port: number): Promise<string | null> {
    try {
      const response = await this.#fetch(`http://127.0.0.1:${String(port)}/v1/health`, {
        headers: { Authorization: `Bearer ${this.#token}` },
        redirect: 'error',
        signal: AbortSignal.any([
          this.#abort.signal,
          AbortSignal.timeout(this.#timing.requestTimeoutMs),
        ]),
      });
      if (!response.ok) {
        return null;
      }
      return parseHealthBody(await response.json(), this.#options.mode);
    } catch {
      return null;
    }
  }

  #fail(reason: string): void {
    if (this.#state === 'failed') {
      return;
    }
    this.#state = 'failed';
    this.#version = null;
    clearTimeout(this.#startupTimer);
    this.#abort.abort();
    const stderr = this.#stderrTail.trim();
    this.#log(
      redact(
        `Локальний рушій не запустився: ${reason}${stderr === '' ? '' : `\nstderr: ${stderr}`}`,
        this.#token,
      ),
    );
    this.#kill();
  }
}

/** An empty body is `null`; undefined means the text is not JSON. */
function parseJsonBody(raw: string): { readonly value: unknown } | undefined {
  const text = raw.trim();
  if (text === '') {
    return { value: null };
  }
  try {
    return { value: JSON.parse(text) };
  } catch {
    return undefined;
  }
}

function describe(error: unknown): string {
  return error instanceof Error ? error.message : 'невідома помилка';
}
