import { describe, expect, it } from 'vitest';

import { parseAppStatus } from './bridge';

describe('parseAppStatus', () => {
  it('accepts a valid status and drops unknown keys', () => {
    expect(
      parseAppStatus({ state: 'ready', mode: 'test', version: '0.1.0', token: 'secret' }),
    ).toEqual({ state: 'ready', mode: 'test', version: '0.1.0' });
  });

  it('accepts a null version while starting', () => {
    expect(parseAppStatus({ state: 'starting', mode: 'production', version: null })).toEqual({
      state: 'starting',
      mode: 'production',
      version: null,
    });
  });

  it.each([
    undefined,
    null,
    'ready',
    42,
    {},
    { state: 'running', mode: 'test', version: null },
    { state: 'ready', mode: 'dev', version: null },
    { state: 'ready', mode: 'test', version: 1 },
    { state: 'ready', mode: 'test' },
  ])('rejects %j', (value) => {
    expect(() => parseAppStatus(value)).toThrow(TypeError);
  });
});
