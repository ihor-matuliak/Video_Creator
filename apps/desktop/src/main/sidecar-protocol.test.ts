// @vitest-environment node
import { describe, expect, it } from 'vitest';

import {
  SIDECAR_ARGS,
  buildChildEnv,
  buildConfigLine,
  createSessionToken,
  parseHealthBody,
  parseReadyLine,
  redact,
  resolveDataDir,
  resolveMode,
} from './sidecar-protocol';

describe('SIDECAR_ARGS', () => {
  it('is the fixed ADR 013 argv', () => {
    expect(SIDECAR_ARGS).toEqual(['-m', 'clipforge.server', '--port', '0', '--config-stdin']);
  });
});

describe('resolveMode', () => {
  it('is production by default', () => {
    expect(resolveMode({})).toBe('production');
    expect(resolveMode({ CLIPFORGE_E2E: '0', CLIPFORGE_MODE: 'production' })).toBe('production');
  });

  it('is test only for CLIPFORGE_E2E=1 or CLIPFORGE_MODE=test', () => {
    expect(resolveMode({ CLIPFORGE_E2E: '1' })).toBe('test');
    expect(resolveMode({ CLIPFORGE_MODE: 'test' })).toBe('test');
  });
});

describe('resolveDataDir', () => {
  it('uses the Electron userData directory in production', () => {
    expect(resolveDataDir('production', { CLIPFORGE_DATA_DIR: 'C:\\x' }, 'C:\\user-data')).toBe(
      'C:\\user-data',
    );
  });

  it('uses CLIPFORGE_DATA_DIR in test mode and never falls back to userData', () => {
    const dir = process.platform === 'win32' ? 'C:\\tmp\\data' : '/tmp/data';
    expect(resolveDataDir('test', { CLIPFORGE_DATA_DIR: dir }, 'user-data')).toBe(dir);
    expect(resolveDataDir('test', {}, 'user-data')).toBeNull();
    expect(resolveDataDir('test', { CLIPFORGE_DATA_DIR: 'relative/dir' }, 'user-data')).toBeNull();
  });
});

describe('createSessionToken', () => {
  it('is 32 random bytes as 64 hex characters', () => {
    const token = createSessionToken();
    expect(token).toMatch(/^[0-9a-f]{64}$/);
    expect(createSessionToken()).not.toBe(token);
  });
});

describe('buildConfigLine', () => {
  it('is one JSON line with exactly the three backend keys', () => {
    const line = buildConfigLine({ mode: 'test', dataDir: 'C:\\data', token: 'a'.repeat(64) });
    expect(line.endsWith('\n')).toBe(true);
    expect(line.slice(0, -1)).not.toContain('\n');
    expect(JSON.parse(line)).toEqual({
      mode: 'test',
      data_dir: 'C:\\data',
      session_token: 'a'.repeat(64),
    });
  });
});

describe('buildChildEnv', () => {
  const env = {
    PATH: 'x',
    CLIPFORGE_SESSION_TOKEN: 'leaked',
    CLIPFORGE_MODE: 'test',
    CLIPFORGE_TEST_PROVIDER_BASE_URL: 'http://127.0.0.1:1',
    CLIPFORGE_DATA_DIR: 'C:\\d',
  };

  it('drops every CLIPFORGE_* variable in production', () => {
    expect(buildChildEnv('production', env)).toEqual({ PATH: 'x' });
  });

  it('keeps test switches in test mode but never the session token', () => {
    const result = buildChildEnv('test', env);
    expect(result).not.toHaveProperty('CLIPFORGE_SESSION_TOKEN');
    expect(result.CLIPFORGE_TEST_PROVIDER_BASE_URL).toBe('http://127.0.0.1:1');
    expect(result.PATH).toBe('x');
  });

  it('matches names case-insensitively', () => {
    expect(buildChildEnv('test', { clipforge_session_token: 'leaked' })).toEqual({});
  });

  it('does not modify its input', () => {
    buildChildEnv('production', env);
    expect(env.CLIPFORGE_SESSION_TOKEN).toBe('leaked');
  });
});

describe('parseReadyLine', () => {
  it('returns the port of a ready line', () => {
    expect(parseReadyLine('{"event":"ready","port":54321}')).toBe(54321);
  });

  it.each([
    '',
    'ready',
    '{"event":"ready"}',
    '{"event":"started","port":1}',
    '{"event":"ready","port":"80"}',
    '{"event":"ready","port":0}',
    '{"event":"ready","port":65536}',
    '{"event":"ready","port":80.5}',
    '[1]',
    'null',
  ])('returns null for %j', (line) => {
    expect(parseReadyLine(line)).toBeNull();
  });
});

describe('parseHealthBody', () => {
  it('returns the version of a healthy answer in the expected mode', () => {
    expect(parseHealthBody({ status: 'ok', mode: 'test', version: '0.1.0' }, 'test')).toBe('0.1.0');
  });

  it.each([
    null,
    'ok',
    {},
    { status: 'error', mode: 'test', version: '0.1.0' },
    { status: 'ok', mode: 'production', version: '0.1.0' },
    { status: 'ok', mode: 'test', version: '' },
    { status: 'ok', mode: 'test', version: 1 },
    { status: 'ok', mode: 'test', version: 'x'.repeat(65) },
  ])('returns null for %j', (body) => {
    expect(parseHealthBody(body, 'test')).toBeNull();
  });
});

describe('redact', () => {
  it('removes every occurrence of the token', () => {
    expect(redact('a TOKEN b TOKEN', 'TOKEN')).toBe('a [redacted] b [redacted]');
  });

  it('leaves text alone when the token is empty', () => {
    expect(redact('abc', '')).toBe('abc');
  });
});
