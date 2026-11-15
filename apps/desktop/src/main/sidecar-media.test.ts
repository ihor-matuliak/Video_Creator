// @vitest-environment node
import { afterEach, describe, expect, it, vi } from 'vitest';

import { Sidecar } from './sidecar';
import type { SpawnFn } from './sidecar';
import { PORT, startReadySidecar, stopAllSidecars } from './sidecar-test-harness';

const ID = '3f2b8c1e-5d4a-4b7e-9a10-2c6d8e0f1a2b';
const PATH = `/v1/sources/${ID}/media`;

afterEach(async () => {
  await stopAllSidecars();
});

/** A body that delivers `chunks` with `gapMs` between them and fails when `signal` aborts. */
function slowBody(chunks: readonly string[], gapMs: number, signal: AbortSignal | null | undefined) {
  const encoder = new TextEncoder();
  return new ReadableStream<Uint8Array>({
    async start(controller) {
      signal?.addEventListener('abort', () => {
        controller.error(new Error('aborted'));
      });
      for (const chunk of chunks) {
        await new Promise((resolve) => setTimeout(resolve, gapMs));
        controller.enqueue(encoder.encode(chunk));
      }
      controller.close();
    },
  });
}

describe('Sidecar.fetchMedia', () => {
  it('answers 503 without a request while the sidecar is not ready', async () => {
    const fetchMock = vi.fn<typeof fetch>();
    const sidecar = new Sidecar({
      mode: 'test',
      dataDir: 'C:\\clipforge-test',
      pythonPath: 'python.exe',
      cwd: 'backend',
      env: {},
      spawn: vi.fn<SpawnFn>(),
      fetch: fetchMock,
      log: () => undefined,
    });

    const response = await sidecar.fetchMedia(PATH, null);

    expect(response.status).toBe(503);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('answers 400 without a request for a path that is not under /v1/', async () => {
    const ready = await startReadySidecar(() => new Response('x'));

    for (const path of ['/health', '/v1/../health', '\\v1\\x', 'v1/x', 'http://evil.example/v1/x']) {
      expect((await ready.sidecar.fetchMedia(path, null)).status, path).toBe(400);
    }

    expect(ready.apiCalls()).toEqual([]);
  });

  it('sends a plain GET with the token and, when given, the Range header', async () => {
    const ready = await startReadySidecar(() => new Response('x', { status: 200 }));

    await ready.sidecar.fetchMedia(PATH, null);
    await ready.sidecar.fetchMedia(PATH, 'bytes=10-');

    const [first, second] = ready.apiCalls();
    expect(first?.url).toBe(`http://127.0.0.1:${String(PORT)}${PATH}`);
    expect(first?.init.method).toBe('GET');
    expect(first?.init.redirect).toBe('error');
    expect(first?.init.headers).toEqual({ Authorization: `Bearer ${ready.token}` });
    expect(second?.init.headers).toEqual({
      Authorization: `Bearer ${ready.token}`,
      Range: 'bytes=10-',
    });
  });

  it('streams a 200 and a 206 with only the four allowed headers', async () => {
    const ready = await startReadySidecar((_url, init) => {
      const partial = new Headers(init.headers).has('Range');
      return new Response('abcde', {
        status: partial ? 206 : 200,
        headers: {
          'Content-Length': partial ? '3' : '5',
          'Content-Type': 'video/mp4',
          'Accept-Ranges': 'bytes',
          ...(partial ? { 'Content-Range': 'bytes 1-3/5' } : {}),
          'Cache-Control': 'no-store',
          'X-Secret': 'nope',
        },
      });
    });

    const full = await ready.sidecar.fetchMedia(PATH, null);
    const part = await ready.sidecar.fetchMedia(PATH, 'bytes=1-3');

    expect(full.status).toBe(200);
    expect(Object.fromEntries(full.headers)).toEqual({
      'content-length': '5',
      'content-type': 'video/mp4',
      'accept-ranges': 'bytes',
    });
    expect(part.status).toBe(206);
    expect(Object.fromEntries(part.headers)).toEqual({
      'content-length': '3',
      'content-type': 'video/mp4',
      'accept-ranges': 'bytes',
      'content-range': 'bytes 1-3/5',
    });
    expect(await full.text()).toBe('abcde');
  });

  it('keeps only status, Content-Range and Accept-Ranges of any other answer and drops its body', async () => {
    const ready = await startReadySidecar(
      () =>
        new Response('{"error":{"code":"PREVIEW_NOT_READY"}}', {
          status: 409,
          headers: {
            'Content-Type': 'application/json',
            'Content-Length': '40',
            'Content-Range': 'bytes */7',
            'Accept-Ranges': 'bytes',
          },
        }),
    );

    const response = await ready.sidecar.fetchMedia(PATH, null);

    expect(response.status).toBe(409);
    expect(Object.fromEntries(response.headers)).toEqual({
      'content-range': 'bytes */7',
      'accept-ranges': 'bytes',
    });
    expect(await response.text()).toBe('');
  });

  it('turns a transport failure into a bodyless 502 that names nothing', async () => {
    const ready = await startReadySidecar(() => {
      throw new TypeError(`fetch failed http://127.0.0.1:${String(PORT)}${PATH}`);
    });

    const response = await ready.sidecar.fetchMedia(PATH, null);

    expect(response.status).toBe(502);
    expect(await response.text()).toBe('');
    expect(JSON.stringify([...response.headers])).not.toContain(String(PORT));
  });

  it('answers 504 when the response headers do not arrive in time', async () => {
    const ready = await startReadySidecar(
      (_url, init) =>
        new Promise<Response>((_resolve, reject) => {
          init.signal?.addEventListener('abort', () => {
            reject(new Error('aborted'));
          });
        }),
      { apiTimeoutMs: 30 },
    );

    const response = await ready.sidecar.fetchMedia(PATH, null);

    expect(response.status).toBe(504);
  });

  it('does not cut a body that streams for longer than the headers timeout', async () => {
    const ready = await startReadySidecar(
      (_url, init) =>
        new Response(slowBody(['ab', 'cd', 'ef'], 40, init.signal), {
          status: 200,
          headers: { 'Content-Length': '6' },
        }),
      { apiTimeoutMs: 30 },
    );

    const response = await ready.sidecar.fetchMedia(PATH, null);

    expect(response.status).toBe(200);
    expect(await response.text()).toBe('abcdef');
  });

  it('ends the upstream stream when the caller aborts', async () => {
    const ready = await startReadySidecar(
      (_url, init) => new Response(slowBody(['ab', 'cd', 'ef'], 40, init.signal), { status: 200 }),
    );
    const caller = new AbortController();

    const response = await ready.sidecar.fetchMedia(PATH, null, caller.signal);
    caller.abort();

    expect(ready.apiCalls()[0]?.init.signal?.aborted).toBe(true);
    await expect(response.text()).rejects.toThrow();
  });

  it('ends the upstream stream when the sidecar stops', async () => {
    const ready = await startReadySidecar(
      (_url, init) => new Response(slowBody(['ab', 'cd', 'ef'], 40, init.signal), { status: 200 }),
    );

    const response = await ready.sidecar.fetchMedia(PATH, null);
    await ready.sidecar.stop();

    await expect(response.text()).rejects.toThrow();
  });

  it('never puts the session token in a returned header', async () => {
    const ready = await startReadySidecar((_url, init) => {
      const authorization = new Headers(init.headers).get('Authorization') ?? '';
      return new Response('x', {
        status: 206,
        headers: {
          'Content-Range': `bytes 0-0/1 ${authorization}`,
          'Accept-Ranges': authorization,
          'Content-Type': `video/mp4 ${authorization}`,
          'X-Echo': authorization,
        },
      });
    });

    const response = await ready.sidecar.fetchMedia(PATH, 'bytes=0-0');

    expect(JSON.stringify([...response.headers])).not.toContain(ready.token);
    expect(response.headers.get('X-Echo')).toBeNull();
  });
});
