// @vitest-environment node
import { afterEach, describe, expect, it, vi } from 'vitest';

import { Sidecar } from './sidecar';
import type { SpawnFn } from './sidecar';
import {
  FakeChild,
  PORT,
  jsonResponse,
  startReadySidecar,
  stopAllSidecars,
} from './sidecar-test-harness';

afterEach(async () => {
  await stopAllSidecars();
});

describe('Sidecar.request', () => {
  it('sends the bearer token to the fixed loopback URL without following redirects', async () => {
    const ready = await startReadySidecar(() => jsonResponse({ ok: 1 }));

    const result = await ready.sidecar.request('GET', '/v1/projects?limit=5');

    expect(result).toEqual({ ok: true, data: { ok: 1 } });
    const [call] = ready.apiCalls();
    expect(call?.url).toBe(`http://127.0.0.1:${String(PORT)}/v1/projects?limit=5`);
    expect(call?.init.method).toBe('GET');
    expect(call?.init.headers).toEqual({
      Authorization: `Bearer ${ready.token}`,
      Accept: 'application/json',
    });
    expect(call?.init.body).toBeUndefined();
    expect(call?.init.redirect).toBe('error');
  });

  it('sends a JSON body with a content type', async () => {
    const ready = await startReadySidecar(() => jsonResponse({ id: 'x' }, 201));

    const result = await ready.sidecar.request('POST', '/v1/projects', { name: 'Тест' });

    expect(result).toEqual({ ok: true, data: { id: 'x' } });
    const [call] = ready.apiCalls();
    expect(call?.init.method).toBe('POST');
    expect(call?.init.headers).toMatchObject({ 'Content-Type': 'application/json' });
    const sent = call?.init.body;
    expect(typeof sent === 'string' ? (JSON.parse(sent) as unknown) : sent).toEqual({ name: 'Тест' });
  });

  it('treats an empty body as null data', async () => {
    const ready = await startReadySidecar(() => new Response(null, { status: 204 }));

    expect(await ready.sidecar.request('POST', '/v1/jobs/x/cancel', {})).toEqual({
      ok: true,
      data: null,
    });
  });

  it('maps a backend error to code, message and retryable and drops details', async () => {
    const ready = await startReadySidecar(() =>
      jsonResponse(
        {
          error: {
            code: 'CLIP_NOT_APPROVED',
            message: 'Кліп не схвалено',
            details: { clip_id: 'secret-detail' },
            retryable: false,
          },
        },
        409,
      ),
    );

    const result = await ready.sidecar.request('POST', '/v1/export-batches', {});

    expect(result).toEqual({
      ok: false,
      error: { code: 'CLIP_NOT_APPROVED', message: 'Кліп не схвалено', retryable: false },
    });
  });

  it('gives a generic error for an error body of another shape', async () => {
    const bad = await startReadySidecar(
      () => new Response('<html>bad gateway</html>', { status: 502 }),
    );
    expect(await bad.sidecar.request('GET', '/v1/projects')).toMatchObject({
      ok: false,
      error: { code: 'BAD_BACKEND_RESPONSE', retryable: false },
    });

    const odd = await startReadySidecar(() => jsonResponse({ detail: 'x' }, 500));
    expect(await odd.sidecar.request('GET', '/v1/projects')).toEqual({
      ok: false,
      error: { code: 'BACKEND_ERROR', message: 'Локальний рушій повернув помилку', retryable: true },
    });

    const client = await startReadySidecar(() => jsonResponse({ detail: 'x' }, 400));
    expect(await client.sidecar.request('GET', '/v1/projects')).toMatchObject({
      ok: false,
      error: { code: 'BACKEND_ERROR', retryable: false },
    });
  });

  it('removes the session token from data and from error messages', async () => {
    const ready = await startReadySidecar((_url, init) => {
      const authorization = new Headers(init.headers).get('Authorization') ?? '';
      return init.method === 'GET'
        ? jsonResponse({ echoed: authorization })
        : jsonResponse(
            { error: { code: 'ECHO', message: `bad ${authorization}`, retryable: false } },
            401,
          );
    });

    const ok = await ready.sidecar.request('GET', '/v1/projects');
    const failed = await ready.sidecar.request('POST', '/v1/projects', {});

    expect(JSON.stringify([ok, failed])).not.toContain(ready.token);
    expect(JSON.stringify(ok)).toContain('[redacted]');
    expect(JSON.stringify(failed)).toContain('[redacted]');
  });

  it('answers BAD_BACKEND_RESPONSE for a 200 that is not JSON', async () => {
    const ready = await startReadySidecar(() => new Response('not json', { status: 200 }));

    expect(await ready.sidecar.request('GET', '/v1/projects')).toMatchObject({
      ok: false,
      error: { code: 'BAD_BACKEND_RESPONSE' },
    });
  });

  it('reports a transport failure; only a read is marked retryable', async () => {
    const ready = await startReadySidecar(() => {
      throw new TypeError(`fetch failed http://127.0.0.1:${String(PORT)}/v1/x`);
    });

    const read = await ready.sidecar.request('GET', '/v1/projects');
    const write = await ready.sidecar.request('POST', '/v1/projects', { name: 'a' });

    expect(read).toEqual({
      ok: false,
      error: { code: 'BACKEND_UNAVAILABLE', message: 'Локальний рушій недоступний', retryable: true },
    });
    expect(write).toMatchObject({ ok: false, error: { retryable: false } });
    expect(JSON.stringify([read, write])).not.toContain(String(PORT));
  });

  it('times out without retrying and says so', async () => {
    const ready = await startReadySidecar(
      (_url, init) =>
        new Promise<Response>((_resolve, reject) => {
          init.signal?.addEventListener('abort', () => {
            reject(new Error('aborted'));
          });
        }),
      { apiTimeoutMs: 30 },
    );

    const result = await ready.sidecar.request('POST', '/v1/export-batches', {});

    expect(result).toMatchObject({ ok: false, error: { code: 'REQUEST_TIMEOUT', retryable: false } });
    expect(ready.apiCalls()).toHaveLength(1);
  });

  it.each([
    '//evil.example/v1/projects',
    'v1/projects',
    '/v2/projects',
    '/v1',
    '/v1/../health',
    '/v1/%2e%2e/health',
    '/v1\\projects',
    'http://evil.example/v1/projects',
  ])('refuses the path %s without calling the sidecar', async (path) => {
    const ready = await startReadySidecar(() => jsonResponse({}));

    const result = await ready.sidecar.request('GET', path);

    expect(result).toMatchObject({ ok: false, error: { code: 'INVALID_REQUEST' } });
    expect(ready.apiCalls()).toEqual([]);
  });

  it('is not ready before start and unavailable after a failure', async () => {
    const child = new FakeChild();
    const fetchMock = vi.fn<typeof fetch>(() => Promise.resolve(jsonResponse({})));
    const sidecar = new Sidecar({
      mode: 'test',
      dataDir: 'C:\\clipforge-test',
      pythonPath: 'python.exe',
      cwd: 'backend',
      env: {},
      spawn: vi.fn<SpawnFn>(() => child as unknown as ReturnType<SpawnFn>),
      fetch: fetchMock,
      log: () => undefined,
      timing: { startPollMs: 5, livePollMs: 60_000, stopGraceMs: 50 },
    });

    expect(await sidecar.request('GET', '/v1/projects')).toEqual({
      ok: false,
      error: { code: 'BACKEND_NOT_READY', message: 'Локальний рушій ще запускається', retryable: true },
    });

    sidecar.start();
    child.emit('exit', 1, null);
    expect(await sidecar.request('GET', '/v1/projects')).toMatchObject({
      ok: false,
      error: { code: 'BACKEND_UNAVAILABLE', retryable: false },
    });
    expect(fetchMock).not.toHaveBeenCalled();
    await sidecar.stop();
  });
});
