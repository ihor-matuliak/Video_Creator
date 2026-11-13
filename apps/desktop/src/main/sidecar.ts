import { spawn } from 'node:child_process';
import type {
  ChildProcessWithoutNullStreams,
  SpawnOptionsWithoutStdio,
} from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';

import type { AppMode, AppState, AppStatus } from '../shared/bridge';
import {
  SIDECAR_ARGS,
  buildChildEnv,
  buildConfigLine,
  createSessionToken,
  parseHealthBody,
  parseReadyLine,
  redact,
} from './sidecar-protocol';

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
  /** After stdin EOF the sidecar gets this long to exit before it is killed. */
  readonly stopGraceMs: number;
}

const DEFAULT_TIMING: SidecarTiming = {
  startupTimeoutMs: 30_000,
  startPollMs: 100,
  livePollMs: 5_000,
  requestTimeoutMs: 2_000,
  stopGraceMs: 5_000,
};

/** Consecutive failed health checks after which a ready sidecar counts as failed. */
const LIVENESS_FAILURE_LIMIT = 3;
const KILL_WAIT_MS = 2_000;
const STDOUT_LIMIT = 64 * 1024;
const STDERR_TAIL_LIMIT = 4 * 1024;

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
 * Owns the Python sidecar child process (ADR 013). The session token exists
 * only here: it goes to the child through stdin, to /v1/health in an
 * Authorization header and nowhere else (not argv, not the renderer, not logs).
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
  #exited = false;
  #stopping = false;
  #stopPromise: Promise<void> | undefined;
  #readySeen = false;
  #stdoutBuffer = '';
  #stderrTail = '';
  #startupTimer: NodeJS.Timeout | undefined;

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

function describe(error: unknown): string {
  return error instanceof Error ? error.message : 'невідома помилка';
}
