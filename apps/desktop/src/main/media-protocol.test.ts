// @vitest-environment node
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  MEDIA_SCHEME_REGISTRATION,
  createMediaHandler,
  mediaPath,
  parseMediaUrl,
  registerMediaProtocol,
} from './media-protocol';
import type { MediaBackend, ProtocolRegistry } from './media-protocol';
import { PORT, startReadySidecar, stopAllSidecars } from './sidecar-test-harness';

const ID = '3f2b8c1e-5d4a-4b7e-9a10-2c6d8e0f1a2b';
const ID2 = '0a9d7e6c-1b2f-4c3d-8e4f-5a6b7c8d9e0f';

afterEach(async () => {
  await stopAllSidecars();
});

/** Raw strings, as a malicious page could build them. */
const BAD_URLS: readonly string[] = [
  'app-media://source/not-a-uuid',
  `app-media://source/${ID.toUpperCase()}`,
  `app-media://source/${ID}x`,
  `app-media://source/${ID.slice(1)}`,
  'app-media://source/3f2b8c1e-5d4a-1b7e-9a10-2c6d8e0f1a2b', // uuid version 1
  'app-media://source/3f2b8c1e-5d4a-4b7e-1a10-2c6d8e0f1a2b', // bad variant
  'app-media://source/',
  'app-media://source',
  'app-media://',
  'app-media:',
  'app-media://clip/' + ID,
  'app-media://sources/' + ID,
  'app-media://previews/' + ID,
  'app-media://SOURCE/' + ID,
  'app-media://' + ID,
  `app-media://source/${ID}/extra`,
  `app-media://source/${ID}/`,
  `app-media://source/${ID}/media`,
  `app-media://preview/${ID}/media`,
  `app-media://source//${ID}`,
  `app-media://source/${ID}/../${ID2}`,
  `app-media://source/./${ID}`,
  `app-media://source/${ID}?x=1`,
  `app-media://source/${ID}?`,
  `app-media://preview/${ID}?range=0-1`,
  `app-media://source/${ID}#frag`,
  `app-media://source/${ID}#`,
  `app-media://source:8080/${ID}`,
  `app-media://user@source/${ID}`,
  `app-media://user:pw@preview/${ID}`,
  `app-media://source/${ID}%2F..`,
  `app-media://source/%2e%2e%2F${ID}`,
  `app-media://source/..%2F..%2Fv1%2Fhealth`,
  `app-media://source/${ID}\\..\\x`,
  `app-media://source/${ID} `,
  ` app-media://source/${ID}`,
  `app-media://source/${ID}\n`,
  `http://127.0.0.1:${String(PORT)}/v1/sources/${ID}/media`,
  `https://source/${ID}`,
  `file:///C:/Videos/${ID}.mp4`,
  'file:///C:/Users/me/Videos/clip.mp4',
  `APP-MEDIA://source/${ID}x`,
  '',
];

function setup(): {
  readonly handler: (request: Request) => Promise<Response>;
  readonly fetchMedia: ReturnType<typeof vi.fn<MediaBackend['fetchMedia']>>;
} {
  const fetchMedia = vi.fn<MediaBackend['fetchMedia']>(() =>
    Promise.resolve(new Response('bytes', { status: 200 })),
  );
  return { handler: createMediaHandler({ fetchMedia }), fetchMedia };
}

describe('MEDIA_SCHEME_REGISTRATION', () => {
  it('is the standard, secure, streaming app-media scheme and nothing more (no fetch)', () => {
    expect(MEDIA_SCHEME_REGISTRATION).toEqual({
      scheme: 'app-media',
      privileges: { standard: true, secure: true, stream: true },
    });
  });
});

describe('parseMediaUrl', () => {
  it.each([
    ['source', ID],
    ['preview', ID2],
  ] as const)('accepts app-media://%s/<uuid4>', (kind, id) => {
    expect(parseMediaUrl(`app-media://${kind}/${id}`)).toEqual({ kind, id });
  });

  it.each(BAD_URLS)('rejects %j', (url) => {
    expect(parseMediaUrl(url)).toBeNull();
  });

  it('maps a target to the fixed media route of the sidecar', () => {
    expect(mediaPath({ kind: 'source', id: ID })).toBe(`/v1/sources/${ID}/media`);
    expect(mediaPath({ kind: 'preview', id: ID2 })).toBe(`/v1/previews/${ID2}/media`);
  });
});

describe('the app-media handler', () => {
  // `Request` normalises its URL (dot segments, spaces), so only URLs that stay bad after
  // that are listed; `parseMediaUrl` above covers the raw forms.
  it('answers 404 without a sidecar call for a bad uuid, unknown kind, extra segments or a query', async () => {
    const { handler, fetchMedia } = setup();

    for (const url of [
      'app-media://source/not-a-uuid',
      `app-media://source/${ID.toUpperCase()}`,
      `app-media://source/${ID}x`,
      'app-media://source/3f2b8c1e-5d4a-1b7e-9a10-2c6d8e0f1a2b',
      'app-media://source/',
      'app-media://source',
      `app-media://clip/${ID}`,
      `app-media://sources/${ID}`,
      `app-media://source/${ID}/extra`,
      `app-media://source/${ID}/media`,
      `app-media://source/${ID}/`,
      `app-media://source//${ID}`,
      `app-media://source/${ID}?x=1`,
      `app-media://source/${ID}?`,
      `app-media://preview/${ID}?range=0-1`,
      `app-media://source/${ID}#frag`,
      `app-media://source:8080/${ID}`,
      `app-media://source/${ID}%2F..`,
      `app-media://source/..%2F..%2Fv1%2Fhealth`,
      `http://127.0.0.1:${String(PORT)}/v1/sources/${ID}/media`,
      'file:///C:/Users/me/Videos/clip.mp4',
    ]) {
      const response = await handler(new Request(url));

      expect(response.status, url).toBe(404);
      expect(await response.text(), url).toBe('');
    }
    expect(fetchMedia).not.toHaveBeenCalled();
  });

  it('forwards a source request to its media route', async () => {
    const { handler, fetchMedia } = setup();
    const request = new Request(`app-media://source/${ID}`);

    const response = await handler(request);

    expect(response.status).toBe(200);
    expect(fetchMedia).toHaveBeenCalledExactlyOnceWith(
      `/v1/sources/${ID}/media`,
      null,
      request.signal,
    );
  });

  it('forwards a preview request to its media route', async () => {
    const { handler, fetchMedia } = setup();

    await handler(new Request(`app-media://preview/${ID2}`));

    expect(fetchMedia).toHaveBeenCalledOnce();
    expect(fetchMedia.mock.calls[0]?.[0]).toBe(`/v1/previews/${ID2}/media`);
  });

  it('forwards the Range header as it came', async () => {
    const { handler, fetchMedia } = setup();

    await handler(new Request(`app-media://source/${ID}`, { headers: { Range: 'bytes=100-199' } }));
    await handler(new Request(`app-media://preview/${ID2}`, { headers: { Range: 'bytes=500-' } }));

    expect(fetchMedia.mock.calls.map(([, range]) => range)).toEqual(['bytes=100-199', 'bytes=500-']);
  });

  it('passes the request signal on, so a cancelled request cancels the stream', async () => {
    const { handler, fetchMedia } = setup();
    const abort = new AbortController();

    await handler(new Request(`app-media://source/${ID}`, { signal: abort.signal }));
    const signal = fetchMedia.mock.calls[0]?.[2];
    abort.abort();

    expect(signal?.aborted).toBe(true);
  });

  it('refuses any method but GET without a sidecar call', async () => {
    const { handler, fetchMedia } = setup();

    for (const method of ['POST', 'PUT', 'DELETE', 'PATCH', 'HEAD']) {
      const response = await handler(
        new Request(`app-media://source/${ID}`, {
          method,
          ...(method === 'HEAD' ? {} : { body: 'x' }),
        }),
      );
      expect(response.status, method).toBe(405);
    }
    expect(fetchMedia).not.toHaveBeenCalled();
  });

  it('checks the URL before the method', async () => {
    const { handler } = setup();

    expect((await handler(new Request('app-media://source/x', { method: 'POST', body: 'x' }))).status).toBe(
      404,
    );
  });

  it('answers 502 when the backend call fails unexpectedly', async () => {
    const fetchMedia = vi.fn<MediaBackend['fetchMedia']>(() =>
      Promise.reject(new Error(`boom http://127.0.0.1:${String(PORT)}`)),
    );
    const handler = createMediaHandler({ fetchMedia });

    const response = await handler(new Request(`app-media://source/${ID}`));

    expect(response.status).toBe(502);
    expect(await response.text()).toBe('');
  });
});

describe('registerMediaProtocol', () => {
  it('handles exactly the app-media scheme', async () => {
    const handle = vi.fn<ProtocolRegistry['handle']>();
    const fetchMedia = vi.fn<MediaBackend['fetchMedia']>(() =>
      Promise.resolve(new Response(null, { status: 200 })),
    );

    registerMediaProtocol({ handle }, { fetchMedia });

    expect(handle).toHaveBeenCalledOnce();
    const [scheme, handler] = handle.mock.calls[0] ?? [];
    expect(scheme).toBe('app-media');
    expect((await handler?.(new Request('app-media://source/nope')))?.status).toBe(404);
    expect(fetchMedia).not.toHaveBeenCalled();
  });
});

const MEDIA_URL = `http://127.0.0.1:${String(PORT)}/v1/sources/${ID}/media`;

describe('app-media through a real Sidecar', () => {
  it('forwards Range, sends the token only to the sidecar and passes 206 headers through', async () => {
    const ready = await startReadySidecar((url, init) => {
      const authorization = new Headers(init.headers).get('Authorization') ?? '';
      return new Response('hello', {
        status: 206,
        headers: {
          'Content-Range': 'bytes 0-4/1234',
          'Content-Length': '5',
          'Accept-Ranges': 'bytes',
          'Content-Type': 'video/mp4',
          // None of these may reach the renderer, least of all one that echoes the token.
          'X-Echo': authorization,
          'WWW-Authenticate': authorization,
          Server: `uvicorn http://127.0.0.1:${String(PORT)}`,
          Location: url,
        },
      });
    });
    const handler = createMediaHandler(ready.sidecar);

    const response = await handler(
      new Request(`app-media://source/${ID}`, { headers: { Range: 'bytes=0-4' } }),
    );

    const [call] = ready.apiCalls();
    expect(call?.url).toBe(MEDIA_URL);
    expect(call?.init.method).toBe('GET');
    expect(call?.init.redirect).toBe('error');
    const sent = new Headers(call?.init.headers);
    expect(sent.get('Range')).toBe('bytes=0-4');
    expect(sent.get('Authorization')).toBe(`Bearer ${ready.token}`);

    expect(response.status).toBe(206);
    expect(Object.fromEntries(response.headers)).toEqual({
      'content-range': 'bytes 0-4/1234',
      'content-length': '5',
      'accept-ranges': 'bytes',
      'content-type': 'video/mp4',
    });
    expect(await response.text()).toBe('hello');
  });

  it('sends no Range header when the renderer sent none, and a preview goes to its own route', async () => {
    const ready = await startReadySidecar(
      () => new Response('x', { status: 200, headers: { 'Content-Type': 'video/mp4' } }),
    );
    const handler = createMediaHandler(ready.sidecar);

    const response = await handler(new Request(`app-media://preview/${ID2}`));

    const [call] = ready.apiCalls();
    expect(call?.url).toBe(`http://127.0.0.1:${String(PORT)}/v1/previews/${ID2}/media`);
    expect(new Headers(call?.init.headers).has('Range')).toBe(false);
    expect(response.status).toBe(200);
  });

  it('returns no token, port or sidecar URL in any header or body', async () => {
    const ready = await startReadySidecar((url, init) => {
      const authorization = new Headers(init.headers).get('Authorization') ?? '';
      return new Response(JSON.stringify({ error: { message: authorization, url } }), {
        status: 416,
        headers: {
          'Content-Range': 'bytes */1234',
          'Accept-Ranges': 'bytes',
          'Content-Type': `application/json; note=${authorization}`,
          'Content-Length': '99',
          'X-Echo': authorization,
        },
      });
    });
    const handler = createMediaHandler(ready.sidecar);

    const response = await handler(
      new Request(`app-media://source/${ID}`, { headers: { Range: 'bytes=9999-' } }),
    );

    const everything = JSON.stringify([[...response.headers], await response.text()]);
    expect(everything).not.toContain(ready.token);
    expect(everything).not.toContain(String(PORT));
    expect(everything).not.toContain('127.0.0.1');
    expect(everything).not.toContain('Bearer');
  });

  it('redacts a token echoed in an allowed header', async () => {
    const ready = await startReadySidecar((_url, init) => {
      const authorization = new Headers(init.headers).get('Authorization') ?? '';
      return new Response('x', {
        status: 200,
        headers: { 'Content-Type': `video/mp4; note=${authorization}` },
      });
    });
    const handler = createMediaHandler(ready.sidecar);

    const response = await handler(new Request(`app-media://source/${ID}`));

    expect(response.headers.get('Content-Type')).toContain('[redacted]');
    expect(JSON.stringify([...response.headers])).not.toContain(ready.token);
  });

  it('keeps status and Content-Range of a 416 but drops its body and type', async () => {
    const ready = await startReadySidecar(
      () =>
        new Response('{"error":{"code":"RANGE_NOT_SATISFIABLE"}}', {
          status: 416,
          headers: {
            'Content-Range': 'bytes */1234',
            'Accept-Ranges': 'bytes',
            'Content-Type': 'application/json',
            'Content-Length': '44',
          },
        }),
    );
    const handler = createMediaHandler(ready.sidecar);

    const response = await handler(new Request(`app-media://source/${ID}`));

    expect(response.status).toBe(416);
    expect(Object.fromEntries(response.headers)).toEqual({
      'content-range': 'bytes */1234',
      'accept-ranges': 'bytes',
    });
    expect(await response.text()).toBe('');
  });

  it.each([404, 409, 503])('passes the status %i of an unknown or unready media on', async (status) => {
    const ready = await startReadySidecar(
      () => new Response('{"error":{"code":"X"}}', { status, headers: { 'Content-Type': 'application/json' } }),
    );
    const handler = createMediaHandler(ready.sidecar);

    const response = await handler(new Request(`app-media://preview/${ID}`));

    expect(response.status).toBe(status);
    expect(await response.text()).toBe('');
  });

  it('never calls the sidecar for a bad URL', async () => {
    const ready = await startReadySidecar(() => new Response('x'));
    const handler = createMediaHandler(ready.sidecar);

    for (const url of [`app-media://source/${ID}?x=1`, 'app-media://source/nope', `app-media://clip/${ID}`]) {
      expect((await handler(new Request(url))).status).toBe(404);
    }

    expect(ready.apiCalls()).toEqual([]);
  });
});
