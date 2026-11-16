// @vitest-environment node
import { afterEach, describe, expect, it, vi } from 'vitest';

import { jsonResponse, startReadySidecar, stopAllSidecars } from './sidecar-test-harness';

afterEach(async () => {
  await stopAllSidecars();
});

describe('Sidecar.onReady', () => {
  it('calls the listener once the sidecar is ready, and request already works inside it', async () => {
    const answers: unknown[] = [];
    const ready = await startReadySidecar(
      () => jsonResponse({ pong: 1 }),
      {},
      (sidecar) => {
        expect(sidecar.getStatus().state).toBe('ready');
        void sidecar.request('GET', '/v1/settings').then((result) => answers.push(result));
      },
    );

    await vi.waitFor(() => {
      expect(answers).toEqual([{ ok: true, data: { pong: 1 } }]);
    });
    expect(ready.apiCalls().map((call) => call.url.split('/v1/')[1])).toEqual(['settings']);
  });

  it('keeps the sidecar ready when a listener throws', async () => {
    const listener = vi.fn(() => {
      throw new Error('listener failed');
    });
    const ready = await startReadySidecar(() => jsonResponse({}), {}, listener);

    expect(listener).toHaveBeenCalledTimes(1);
    expect(ready.sidecar.getStatus().state).toBe('ready');
  });
});

describe('Sidecar.request with DELETE', () => {
  it('sends DELETE with the bearer token and no body, and reads a 204 as null data', async () => {
    const ready = await startReadySidecar(() => new Response(null, { status: 204 }));

    const result = await ready.sidecar.request('DELETE', '/v1/runtime/credentials');

    expect(result).toEqual({ ok: true, data: null });
    const [call] = ready.apiCalls();
    expect(call?.init.method).toBe('DELETE');
    expect(call?.init.body).toBeUndefined();
    expect(call?.init.headers).toMatchObject({ Authorization: `Bearer ${ready.token}` });
  });
});
