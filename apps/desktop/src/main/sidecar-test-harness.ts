import { EventEmitter } from 'node:events';
import { PassThrough, Writable } from 'node:stream';

import { vi } from 'vitest';

import { Sidecar } from './sidecar';
import type { SpawnFn } from './sidecar';

/** Test-only: a ready Sidecar over a fake child process and a fake fetch. */

export const PORT = 54321;
export const HEALTHY = { status: 'ok', mode: 'test', version: '0.1.0' };

export class FakeChild extends EventEmitter {
  readonly stdin = new Writable({
    write: (chunk: Buffer | string, _encoding, callback) => {
      this.stdinText += chunk.toString();
      callback();
    },
  });
  readonly stdout = new PassThrough();
  readonly stderr = new PassThrough();
  readonly pid: number | undefined = 4242;
  readonly kill = vi.fn(() => {
    this.emit('exit', null, 'SIGTERM');
    return true;
  });
  stdinText = '';

  emitReady(port: number): void {
    this.stdout.write(`{"event":"ready","port":${String(port)}}\n`);
  }

  get token(): string {
    const config = JSON.parse(this.stdinText.split('\n')[0] ?? '') as { session_token: string };
    return config.session_token;
  }
}

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

export type ApiHandler = (url: string, init: RequestInit) => Response | Promise<Response>;

export interface ApiCall {
  readonly url: string;
  readonly init: RequestInit;
}

export interface ReadySidecar {
  readonly sidecar: Sidecar;
  readonly child: FakeChild;
  readonly token: string;
  readonly fetch: ReturnType<typeof vi.fn<typeof fetch>>;
  /** Every fetch except the health checks. */
  apiCalls(): ApiCall[];
}

const live: Sidecar[] = [];

/** Call from afterEach: ends every sidecar started by `startReadySidecar`. */
export async function stopAllSidecars(): Promise<void> {
  for (const sidecar of live.splice(0)) {
    await sidecar.stop();
  }
}

/** Starts a sidecar, lets it report ready and returns once its state is `ready`. */
export async function startReadySidecar(
  handler: ApiHandler,
  timing: { readonly apiTimeoutMs?: number } = {},
  /** Registered before `start()`, like `onReady` in main. */
  onReady?: (sidecar: Sidecar) => void,
): Promise<ReadySidecar> {
  const child = new FakeChild();
  const fetchMock = vi.fn<typeof fetch>((input, init) => {
    const url = input instanceof Request ? input.url : String(input);
    if (url.endsWith('/v1/health')) {
      return Promise.resolve(jsonResponse(HEALTHY));
    }
    return Promise.resolve(handler(url, init ?? {}));
  });
  const sidecar = new Sidecar({
    mode: 'test',
    dataDir: process.platform === 'win32' ? 'C:\\clipforge-test' : '/tmp/clipforge-test',
    pythonPath: 'python.exe',
    cwd: 'backend',
    env: { PATH: 'x' },
    spawn: vi.fn<SpawnFn>(() => child as unknown as ReturnType<SpawnFn>),
    fetch: fetchMock,
    log: () => undefined,
    timing: {
      startPollMs: 5,
      livePollMs: 60_000,
      requestTimeoutMs: 500,
      stopGraceMs: 50,
      ...timing,
    },
  });
  live.push(sidecar);
  if (onReady !== undefined) {
    sidecar.onReady(() => {
      onReady(sidecar);
    });
  }
  sidecar.start();
  child.emitReady(PORT);
  await vi.waitFor(() => {
    if (sidecar.getStatus().state !== 'ready') {
      throw new Error('sidecar is not ready yet');
    }
  });
  return {
    sidecar,
    child,
    token: child.token,
    fetch: fetchMock,
    apiCalls: () =>
      fetchMock.mock.calls
        .map(([input, init]) => ({
          url: input instanceof Request ? input.url : String(input),
          init: init ?? {},
        }))
        .filter((call) => !call.url.endsWith('/v1/health')),
  };
}
