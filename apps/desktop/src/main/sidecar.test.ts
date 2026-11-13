// @vitest-environment node
import { EventEmitter } from 'node:events';
import { PassThrough, Writable } from 'node:stream';

import { afterEach, describe, expect, it, vi } from 'vitest';

import { Sidecar } from './sidecar';
import type { SidecarOptions, SpawnFn } from './sidecar';

const DATA_DIR = process.platform === 'win32' ? 'C:\\clipforge-test' : '/tmp/clipforge-test';
const HEALTHY = { status: 'ok', mode: 'test', version: '0.1.0' };
const FAST = { startPollMs: 5, livePollMs: 5, requestTimeoutMs: 500, stopGraceMs: 50 };

class FakeChild extends EventEmitter {
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
  /** What the child sees on stdin; the first line is the config. */
  stdinText = '';

  emitReady(port: number): void {
    this.stdout.write(`{"event":"ready","port":${String(port)}}\n`);
  }

  /** Like the real sidecar: exits once stdin reaches EOF. */
  exitOnStdinEof(): void {
    this.stdin.on('finish', () => {
      this.emit('exit', 0, null);
    });
  }

  get token(): string {
    const config = JSON.parse(this.stdinText.split('\n')[0] ?? '') as { session_token: string };
    return config.session_token;
  }
}

interface Harness {
  readonly sidecar: Sidecar;
  readonly child: FakeChild;
  readonly spawn: ReturnType<typeof vi.fn<SpawnFn>>;
  readonly fetch: ReturnType<typeof vi.fn<typeof fetch>>;
  readonly log: ReturnType<typeof vi.fn<(message: string) => void>>;
}

const live: Sidecar[] = [];

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function setup(overrides: Partial<SidecarOptions> = {}): Harness {
  const child = new FakeChild();
  const spawn = vi.fn<SpawnFn>(() => child as unknown as ReturnType<SpawnFn>);
  const fetchMock = vi.fn<typeof fetch>(() => Promise.resolve(jsonResponse(HEALTHY)));
  const log = vi.fn<(message: string) => void>();
  const sidecar = new Sidecar({
    mode: 'test',
    dataDir: DATA_DIR,
    pythonPath: 'python.exe',
    cwd: 'backend',
    env: { PATH: 'x', CLIPFORGE_SESSION_TOKEN: 'inherited-secret' },
    spawn,
    fetch: fetchMock,
    log,
    timing: FAST,
    ...overrides,
  });
  live.push(sidecar);
  return { sidecar, child, spawn, fetch: fetchMock, log };
}

afterEach(async () => {
  for (const sidecar of live.splice(0)) {
    await sidecar.stop();
  }
});

describe('Sidecar start', () => {
  it('spawns the fixed argv without a shell and hides the window', () => {
    const { sidecar, spawn } = setup();
    sidecar.start();

    expect(spawn).toHaveBeenCalledTimes(1);
    const [command, args, options] = spawn.mock.calls[0] ?? [];
    expect(command).toBe('python.exe');
    expect(args).toEqual(['-m', 'clipforge.server', '--port', '0', '--config-stdin']);
    expect(options).toMatchObject({ cwd: 'backend', shell: false, windowsHide: true });
  });

  it('sends the config on stdin, keeps stdin open and never exposes the token elsewhere', () => {
    const { sidecar, child, spawn } = setup();
    sidecar.start();

    const [, args, options] = spawn.mock.calls[0] ?? [];
    const config = JSON.parse(child.stdinText) as Record<string, string>;
    expect(Object.keys(config).sort()).toEqual(['data_dir', 'mode', 'session_token']);
    expect(config.mode).toBe('test');
    expect(config.data_dir).toBe(DATA_DIR);
    expect(config.session_token).toMatch(/^[0-9a-f]{64}$/);
    expect(child.stdin.writableEnded).toBe(false);
    expect(JSON.stringify(args)).not.toContain(child.token);
    expect(JSON.stringify(options?.env)).not.toContain(child.token);
    expect(options?.env).not.toHaveProperty('CLIPFORGE_SESSION_TOKEN');
  });

  it('starts in the starting state with the configured mode', () => {
    const { sidecar } = setup();
    sidecar.start();

    expect(sidecar.getStatus()).toEqual({ state: 'starting', mode: 'test', version: null });
  });

  it('fails without spawning when the data directory is unresolved', () => {
    const { sidecar, spawn, log } = setup({ dataDir: null });
    sidecar.start();

    expect(spawn).not.toHaveBeenCalled();
    expect(sidecar.getStatus().state).toBe('failed');
    expect(log).toHaveBeenCalledOnce();
  });

  it('fails when spawning throws', () => {
    const { sidecar } = setup({
      spawn: () => {
        throw new Error('spawn blocked');
      },
    });
    sidecar.start();

    expect(sidecar.getStatus().state).toBe('failed');
  });

  it('fails when the executable is missing (error event, no exit event)', () => {
    const { sidecar, child } = setup();
    sidecar.start();
    Object.assign(child, { pid: undefined });
    child.emit('error', new Error('spawn python.exe ENOENT'));

    expect(sidecar.getStatus().state).toBe('failed');
  });

  it('starts only once', () => {
    const { sidecar, spawn } = setup();
    sidecar.start();
    sidecar.start();

    expect(spawn).toHaveBeenCalledTimes(1);
  });
});

describe('Sidecar readiness', () => {
  it('becomes ready after the ready line and a healthy authenticated /v1/health', async () => {
    const { sidecar, child, fetch: fetchMock } = setup();
    sidecar.start();
    child.emitReady(54321);

    await vi.waitFor(() => {
      expect(sidecar.getStatus()).toEqual({ state: 'ready', mode: 'test', version: '0.1.0' });
    });
    const [url, init] = fetchMock.mock.calls[0] ?? [];
    expect(url).toBe('http://127.0.0.1:54321/v1/health');
    expect(init?.headers).toEqual({ Authorization: `Bearer ${child.token}` });
    expect(init?.redirect).toBe('error');
  });

  it('accepts a ready line split across chunks and surrounded by noise', async () => {
    const { sidecar, child, fetch: fetchMock } = setup();
    sidecar.start();
    child.stdout.write('warming up\r\n{"event":"rea');
    child.stdout.write('dy","port":4000}\r\n');

    await vi.waitFor(() => {
      expect(sidecar.getStatus().state).toBe('ready');
    });
    expect(fetchMock.mock.calls[0]?.[0]).toBe('http://127.0.0.1:4000/v1/health');
  });

  it('stays starting while health is not ok', async () => {
    let healthy = false;
    const { sidecar, child, fetch: fetchMock } = setup();
    fetchMock.mockImplementation(() =>
      Promise.resolve(healthy ? jsonResponse(HEALTHY) : jsonResponse({}, 503)),
    );
    sidecar.start();
    child.emitReady(4000);

    await vi.waitFor(() => {
      expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(3);
    });
    expect(sidecar.getStatus().state).toBe('starting');
    healthy = true;
    await vi.waitFor(() => {
      expect(sidecar.getStatus().state).toBe('ready');
    });
  });

  it('never becomes ready when health reports another mode', async () => {
    const { sidecar, child, fetch: fetchMock } = setup();
    fetchMock.mockImplementation(() =>
      Promise.resolve(jsonResponse({ status: 'ok', mode: 'production', version: '0.1.0' })),
    );
    sidecar.start();
    child.emitReady(4000);

    await vi.waitFor(() => {
      expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(3);
    });
    expect(sidecar.getStatus().state).toBe('starting');
  });

  it('does not poll health before the ready line', async () => {
    const { sidecar, fetch: fetchMock } = setup();
    sidecar.start();
    await new Promise((resolve) => setTimeout(resolve, 40));

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('fails when the startup timeout passes and ends its own child', async () => {
    const { sidecar, child } = setup({ timing: { ...FAST, startupTimeoutMs: 30 } });
    sidecar.start();

    await vi.waitFor(() => {
      expect(sidecar.getStatus().state).toBe('failed');
    });
    expect(child.kill).toHaveBeenCalledOnce();
  });

  it('fails when the child exits before it is ready, logging stderr without the token', async () => {
    const { sidecar, child, log } = setup();
    sidecar.start();
    child.stderr.write(`boom ${child.token} boom\n`);
    await new Promise((resolve) => setImmediate(resolve));
    child.emit('exit', 2, null);

    expect(sidecar.getStatus()).toEqual({ state: 'failed', mode: 'test', version: null });
    const message = log.mock.calls[0]?.[0] ?? '';
    expect(message).toContain('код 2');
    expect(message).toContain('boom [redacted] boom');
    expect(message).not.toContain(child.token);
    expect(child.kill).not.toHaveBeenCalled();
  });

  it('turns failed when the child exits after it was ready', async () => {
    const { sidecar, child } = setup();
    sidecar.start();
    child.emitReady(4000);
    await vi.waitFor(() => {
      expect(sidecar.getStatus().state).toBe('ready');
    });
    child.emit('exit', 1, null);

    expect(sidecar.getStatus().state).toBe('failed');
  });

  it('turns failed after repeated failed liveness checks', async () => {
    const { sidecar, child, fetch: fetchMock } = setup();
    sidecar.start();
    child.emitReady(4000);
    await vi.waitFor(() => {
      expect(sidecar.getStatus().state).toBe('ready');
    });
    fetchMock.mockImplementation(() => Promise.reject(new TypeError('fetch failed')));

    await vi.waitFor(() => {
      expect(sidecar.getStatus().state).toBe('failed');
    });
    expect(child.kill).toHaveBeenCalledOnce();
  });
});

describe('Sidecar stop', () => {
  it('closes stdin and does not kill a child that exits by itself', async () => {
    const { sidecar, child } = setup();
    child.exitOnStdinEof();
    sidecar.start();
    child.emitReady(4000);
    await vi.waitFor(() => {
      expect(sidecar.getStatus().state).toBe('ready');
    });

    await sidecar.stop();

    expect(child.stdin.writableEnded).toBe(true);
    expect(child.kill).not.toHaveBeenCalled();
    expect(sidecar.getStatus().state).toBe('ready');
  });

  it('kills only its own child when it ignores stdin EOF', async () => {
    const { sidecar, child, log } = setup();
    sidecar.start();

    await sidecar.stop();

    expect(child.stdin.writableEnded).toBe(true);
    expect(child.kill).toHaveBeenCalledOnce();
    expect(log).toHaveBeenCalledOnce();
  });

  it('stops polling health once stopped', async () => {
    const { sidecar, child, fetch: fetchMock } = setup();
    child.exitOnStdinEof();
    sidecar.start();
    child.emitReady(4000);
    await vi.waitFor(() => {
      expect(fetchMock).toHaveBeenCalled();
    });

    await sidecar.stop();
    const calls = fetchMock.mock.calls.length;
    await new Promise((resolve) => setTimeout(resolve, 40));

    expect(fetchMock.mock.calls.length).toBe(calls);
  });

  it('is a no-op before start and idempotent afterwards', async () => {
    const { sidecar, child } = setup();
    await sidecar.stop();

    const second = setup();
    second.sidecar.start();
    second.child.exitOnStdinEof();
    await Promise.all([second.sidecar.stop(), second.sidecar.stop()]);

    expect(child.kill).not.toHaveBeenCalled();
    expect(second.child.kill).not.toHaveBeenCalled();
  });

  it('terminateNow ends the running child synchronously', () => {
    const { sidecar, child } = setup();
    sidecar.start();
    sidecar.terminateNow();

    expect(child.kill).toHaveBeenCalledOnce();
    expect(sidecar.getStatus().state).toBe('starting');
  });
});
