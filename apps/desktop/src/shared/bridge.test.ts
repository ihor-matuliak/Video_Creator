import { describe, expect, it } from 'vitest';

import {
  DEFAULT_LAYOUT,
  LAYOUT_LIMITS,
  apiFailure,
  isApiKey,
  parseApiCallResult,
  parseApiKeyResult,
  parseApiKeyStatus,
  parseApiResult,
  parseAppStatus,
  parseChooseFolderResult,
  parseGetViewStateResult,
  parseImportTranscriptResult,
  parseImportVideoResult,
  parseLayout,
  parseOpenFolderResult,
  parsePickVideoResult,
  parseSaveViewStateResult,
  parseViewState,
} from './bridge';

const ID = '3f2b8c1e-5d4a-4b7e-9a10-2c6d8e0f1a2b';
const PROJECT = {
  id: ID,
  name: 'Перевірка',
  created_at: '2026-11-14T10:00:00Z',
  updated_at: '2026-11-14T10:00:00Z',
  archived_at: null,
};
const SOURCE = {
  id: ID,
  project_id: ID,
  display_name: 'color_gop.mp4',
  size: 10,
  duration_ms: 12000,
  video_stream_index: 0,
  audio_streams: [],
  availability: 'available',
  hash_status: 'pending',
  hash_job_id: null,
  created_at: '2026-11-14T10:00:00Z',
  playback: { direct: true, reason: null, direct_audio_stream_index: null },
  video: { width: 320, height: 180, frame_rate: '30/1', vfr: false },
};
const TOKEN = 'ab'.repeat(32);

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

describe('parseApiResult', () => {
  const identity = (value: unknown): unknown => value;

  it('accepts a success and an error, dropping unknown keys', () => {
    expect(parseApiResult({ ok: true, data: { a: 1 }, token: 'x' }, identity)).toEqual({
      ok: true,
      data: { a: 1 },
    });
    expect(
      parseApiResult(
        { ok: false, error: { code: 'E', message: 'm', retryable: true, details: { x: 1 } } },
        identity,
      ),
    ).toEqual({ ok: false, error: { code: 'E', message: 'm', retryable: true } });
  });

  it('runs the data parser on a success only', () => {
    expect(() =>
      parseApiResult({ ok: true, data: 1 }, () => {
        throw new TypeError('bad data');
      }),
    ).toThrow('bad data');
    expect(parseApiResult(apiFailure('E', 'm'), identity)).toEqual({
      ok: false,
      error: { code: 'E', message: 'm', retryable: false },
    });
  });

  it.each([
    undefined,
    null,
    'ok',
    [],
    {},
    { ok: 'yes', data: 1 },
    { ok: false },
    { ok: false, error: null },
    { ok: false, error: { code: '', message: 'm', retryable: false } },
    { ok: false, error: { code: 'E', message: 1, retryable: false } },
    { ok: false, error: { code: 'E', message: 'm', retryable: 'no' } },
  ])('rejects %j', (value) => {
    expect(() => parseApiResult(value, identity)).toThrow(TypeError);
  });
});

describe('parseApiCallResult', () => {
  it('validates the data shape of the op that was called', () => {
    expect(parseApiCallResult('projects.create', { ok: true, data: PROJECT })).toEqual({
      ok: true,
      data: PROJECT,
    });
    expect(() => parseApiCallResult('projects.create', { ok: true, data: { id: ID } })).toThrow(
      TypeError,
    );
    expect(() => parseApiCallResult('jobs.get', { ok: true, data: PROJECT })).toThrow(TypeError);
  });

  it('validates every item of a sources.list page and drops the fields the renderer must not see', () => {
    const page = {
      items: [{ ...SOURCE, path: 'C:\\x.mp4', probe: { format_name: 'mov' } }],
      next_cursor: 'c',
    };

    expect(parseApiCallResult('sources.list', { ok: true, data: page })).toEqual({
      ok: true,
      data: { items: [SOURCE], next_cursor: 'c' },
    });
    expect(() =>
      parseApiCallResult('sources.list', { ok: true, data: { items: [{ id: ID }], next_cursor: null } }),
    ).toThrow(TypeError);
    expect(() => parseApiCallResult('sources.list', { ok: true, data: { items: [SOURCE] } })).toThrow(
      TypeError,
    );
    expect(() => parseApiCallResult('sources.list', { ok: true, data: SOURCE })).toThrow(TypeError);
  });

  it('passes an error through for any op', () => {
    expect(parseApiCallResult('jobs.get', apiFailure('NOT_FOUND', 'Не знайдено'))).toMatchObject({
      ok: false,
      error: { code: 'NOT_FOUND' },
    });
  });

  it('rejects a success for an op that is not allowlisted', () => {
    const op = 'constructor' as unknown as 'jobs.get';

    expect(() => parseApiCallResult(op, { ok: true, data: {} })).toThrow(TypeError);
  });
});

describe('parseImportVideoResult', () => {
  it('accepts cancel, a source and an error', () => {
    expect(parseImportVideoResult({ canceled: true })).toEqual({ canceled: true });
    expect(parseImportVideoResult({ ok: true, data: { ...SOURCE, path: 'C:\\x.mp4' } })).toEqual({
      ok: true,
      data: SOURCE,
    });
    expect(parseImportVideoResult(apiFailure('NOT_A_VIDEO', 'Файл не є відео'))).toMatchObject({
      ok: false,
    });
  });

  it.each([
    undefined,
    null,
    'canceled',
    { canceled: false },
    { canceled: 'true' },
    { ok: true, data: { id: ID } },
    { ok: true },
  ])('rejects %j', (value) => {
    expect(() => parseImportVideoResult(value)).toThrow(TypeError);
  });
});

describe('parseOpenFolderResult', () => {
  it('accepts success and an error, dropping other keys', () => {
    expect(parseOpenFolderResult({ ok: true, data: { opened: true, path: 'C:\\Exports' } })).toEqual({
      ok: true,
      data: { opened: true },
    });
    expect(
      parseOpenFolderResult(apiFailure('EXPORT_FOLDER_MISSING', 'Теки експорту більше немає')),
    ).toEqual({
      ok: false,
      error: { code: 'EXPORT_FOLDER_MISSING', message: 'Теки експорту більше немає', retryable: false },
    });
  });

  it.each([
    undefined,
    null,
    'ok',
    {},
    { canceled: true },
    { ok: true },
    { ok: true, data: null },
    { ok: true, data: {} },
    { ok: true, data: { opened: false } },
    { ok: true, data: { opened: 'yes' } },
    { ok: false },
  ])('rejects %j', (value) => {
    expect(() => parseOpenFolderResult(value)).toThrow(TypeError);
  });
});

describe('parseChooseFolderResult', () => {
  it('accepts cancel and a token with a display name, dropping other keys', () => {
    expect(parseChooseFolderResult({ canceled: true })).toEqual({ canceled: true });
    expect(
      parseChooseFolderResult({ token: TOKEN, displayName: 'Exports', path: 'C:\\Exports' }),
    ).toEqual({ token: TOKEN, displayName: 'Exports' });
  });

  it.each([
    undefined,
    null,
    {},
    { canceled: false },
    { token: 'abc', displayName: 'Exports' },
    { token: TOKEN },
    { token: TOKEN, displayName: '' },
    { token: TOKEN, displayName: 5 },
    { token: 'C:\\Exports', displayName: 'Exports' },
  ])('rejects %j', (value) => {
    expect(() => parseChooseFolderResult(value)).toThrow(TypeError);
  });
});

describe('parsePickVideoResult', () => {
  it('accepts cancel and a token with a file name, dropping other keys', () => {
    expect(parsePickVideoResult({ canceled: true })).toEqual({ canceled: true });
    expect(
      parsePickVideoResult({ token: TOKEN, fileName: 'clip.mp4', path: 'C:\\Videos\\clip.mp4' }),
    ).toEqual({ token: TOKEN, fileName: 'clip.mp4' });
  });

  it('accepts an error and drops its other keys', () => {
    expect(
      parsePickVideoResult({
        ok: false,
        error: { code: 'UNSUPPORTED_FILE', message: 'Це не відеофайл', retryable: false, details: {} },
        path: 'C:\\x',
      }),
    ).toEqual({
      ok: false,
      error: { code: 'UNSUPPORTED_FILE', message: 'Це не відеофайл', retryable: false },
    });
  });

  it.each([
    undefined,
    null,
    'canceled',
    [],
    {},
    { canceled: false },
    { canceled: 'true' },
    { token: 'abc', fileName: 'clip.mp4' },
    { token: TOKEN },
    { token: TOKEN, fileName: '' },
    { token: TOKEN, fileName: 5 },
    { token: TOKEN.toUpperCase(), fileName: 'clip.mp4' },
    { token: 'C:\\Videos\\clip.mp4', fileName: 'clip.mp4' },
    { ok: true },
    { ok: true, data: { token: TOKEN, fileName: 'clip.mp4' } },
    { ok: false },
    { ok: false, error: { code: '', message: 'm', retryable: false } },
  ])('rejects %j', (value) => {
    expect(() => parsePickVideoResult(value)).toThrow(TypeError);
  });

  it('names the channel of the answer in the error', () => {
    expect(() => parsePickVideoResult(null)).toThrow('dialog:pick-video');
    expect(() => parsePickVideoResult(null, 'drop:video')).toThrow('drop:video');
  });
});

const LAYOUT = { list_px: 320, details_px: 320 };

describe('parseLayout', () => {
  it('accepts exactly two whole widths inside their limits, edges included', () => {
    expect(parseLayout(LAYOUT)).toEqual(LAYOUT);
    expect(parseLayout({ list_px: 240, details_px: 280 })).toEqual({ list_px: 240, details_px: 280 });
    expect(parseLayout({ list_px: 480, details_px: 480 })).toEqual({ list_px: 480, details_px: 480 });
  });

  it('shares the limits and the default with the renderer', () => {
    expect(LAYOUT_LIMITS).toEqual({ list_px: { min: 240, max: 480 }, details_px: { min: 280, max: 480 } });
    expect(parseLayout(DEFAULT_LAYOUT)).toEqual(DEFAULT_LAYOUT);
  });

  it.each([
    undefined,
    null,
    320,
    '320',
    [],
    [320, 320],
    {},
    { list_px: 320 },
    { details_px: 320 },
    { list_px: 239, details_px: 320 },
    { list_px: 481, details_px: 320 },
    { list_px: 320, details_px: 279 },
    { list_px: 320, details_px: 481 },
    { list_px: -320, details_px: 320 },
    { list_px: 0, details_px: 320 },
    { list_px: 320.5, details_px: 320 },
    { list_px: '320', details_px: 320 },
    { list_px: null, details_px: 320 },
    { list_px: Number.NaN, details_px: 320 },
    { list_px: 320, details_px: Number.POSITIVE_INFINITY },
    { list_px: 320, details_px: 320, extra_px: 1 },
    { list_px: 320, details_px: 320, path: 'C:\\x' },
    { list_px: 320, details_px: undefined },
  ])('rejects %j', (value) => {
    expect(() => parseLayout(value)).toThrow(TypeError);
  });

  it('rejects an own __proto__ key', () => {
    const value: unknown = JSON.parse('{"list_px":320,"details_px":320,"__proto__":{"x":1}}');

    expect(() => parseLayout(value)).toThrow(TypeError);
  });
});

describe('parseViewState', () => {
  it('accepts exactly a project id, a tab that can be reopened and the panel widths', () => {
    expect(parseViewState({ project_id: ID, tab: 'sources', layout: LAYOUT })).toEqual({
      project_id: ID,
      tab: 'sources',
      layout: LAYOUT,
    });
    expect(
      parseViewState({ project_id: ID, tab: 'clips', layout: { list_px: 260, details_px: 300 } }),
    ).toEqual({ project_id: ID, tab: 'clips', layout: { list_px: 260, details_px: 300 } });
  });

  it('drops nothing and copies the widths instead of handing back the same object', () => {
    const layout = { list_px: 300, details_px: 300 };

    const parsed = parseViewState({ project_id: ID, tab: 'clips', layout });

    expect(parsed.layout).toEqual(layout);
    expect(parsed.layout).not.toBe(layout);
  });

  it.each([
    undefined,
    null,
    'sources',
    {},
    { project_id: ID },
    { tab: 'sources' },
    { project_id: ID, tab: 'sources' },
    { project_id: ID, layout: LAYOUT },
    { tab: 'sources', layout: LAYOUT },
    { project_id: ID, tab: 'export', layout: LAYOUT },
    { project_id: ID, tab: 'Sources', layout: LAYOUT },
    { project_id: ID, tab: 1, layout: LAYOUT },
    { project_id: 'not-a-uuid', tab: 'sources', layout: LAYOUT },
    { project_id: ID.toUpperCase(), tab: 'sources', layout: LAYOUT },
    { project_id: `${ID}/../x`, tab: 'sources', layout: LAYOUT },
    { project_id: ID, tab: 'sources', layout: LAYOUT, path: 'C:\\Videos\\a.mp4' },
    { project_id: ID, tab: 'sources', layout: LAYOUT, token: TOKEN },
    { project_id: ID, tab: 'sources', layout: LAYOUT, version: 2 },
    { project_id: ID, tab: undefined, layout: LAYOUT, extra: 1 },
    { project_id: ID, tab: 'sources', layout: undefined },
    { project_id: ID, tab: 'sources', layout: null },
    { project_id: ID, tab: 'sources', layout: 320 },
    { project_id: ID, tab: 'sources', layout: { list_px: 320 } },
    { project_id: ID, tab: 'sources', layout: { list_px: 10, details_px: 320 } },
    { project_id: ID, tab: 'sources', layout: { list_px: 320, details_px: 9999 } },
    { project_id: ID, tab: 'sources', layout: { ...LAYOUT, extra: 1 } },
  ])('rejects %j', (value) => {
    expect(() => parseViewState(value)).toThrow(TypeError);
  });

  it('rejects an array', () => {
    expect(() => parseViewState([{ project_id: ID, tab: 'sources', layout: LAYOUT }])).toThrow(TypeError);
    expect(() => parseViewState([])).toThrow(TypeError);
  });

  it('rejects an own __proto__ key', () => {
    const value: unknown = JSON.parse(
      `{"project_id":"${ID}","tab":"sources","layout":{"list_px":320,"details_px":320},"__proto__":{"x":1}}`,
    );

    expect(() => parseViewState(value)).toThrow(TypeError);
  });
});

describe('parseGetViewStateResult', () => {
  it('accepts null and a view state', () => {
    expect(parseGetViewStateResult(null)).toBeNull();
    expect(parseGetViewStateResult({ project_id: ID, tab: 'clips', layout: LAYOUT })).toEqual({
      project_id: ID,
      tab: 'clips',
      layout: LAYOUT,
    });
  });

  it.each([
    undefined,
    'null',
    {},
    { project_id: ID, tab: 'clips' },
    { project_id: ID, tab: 'clips', layout: LAYOUT, path: 'C:\\x' },
    { project_id: ID, tab: 'clips', layout: { list_px: 1, details_px: 1 } },
  ])('rejects %j', (value) => {
    expect(() => parseGetViewStateResult(value)).toThrow(TypeError);
  });
});

describe('parseSaveViewStateResult', () => {
  it('accepts success and an error, dropping other keys', () => {
    expect(parseSaveViewStateResult({ ok: true, data: { saved: true, path: 'C:\\x' } })).toEqual({
      ok: true,
      data: { saved: true },
    });
    expect(
      parseSaveViewStateResult(apiFailure('INVALID_PARAMS', 'Некоректні параметри')),
    ).toMatchObject({ ok: false, error: { code: 'INVALID_PARAMS' } });
  });

  it.each([
    undefined,
    null,
    {},
    { ok: true },
    { ok: true, data: {} },
    { ok: true, data: { saved: false } },
  ])('rejects %j', (value) => {
    expect(() => parseSaveViewStateResult(value)).toThrow(TypeError);
  });
});

describe('isApiKey', () => {
  it('accepts 20 to 200 printable ASCII characters without a space', () => {
    expect(isApiKey('k'.repeat(20))).toBe(true);
    expect(isApiKey('k'.repeat(200))).toBe(true);
    expect(isApiKey('sk-or-v1-0123456789abcdef!~')).toBe(true);
  });

  it.each([
    ['too short', 'k'.repeat(19)],
    ['too long', 'k'.repeat(201)],
    ['a space inside', `${'k'.repeat(10)} ${'k'.repeat(10)}`],
    ['a trailing newline', `${'k'.repeat(20)}\n`],
    ['a tab', `${'k'.repeat(20)}\t`],
    ['a non-ASCII letter', `${'k'.repeat(19)}є`],
    ['a number', 1234],
    ['null', null],
    ['an empty string', ''],
  ])('refuses %s', (_name, value) => {
    expect(isApiKey(value)).toBe(false);
  });
});

describe('parseApiKeyStatus', () => {
  it('keeps the state and the message and drops every other key', () => {
    expect(parseApiKeyStatus({ state: 'protected', message: null, key: 'k'.repeat(30) })).toEqual({
      state: 'protected',
      message: null,
    });
    expect(parseApiKeyStatus({ state: 'none', message: 'Не вдалося прочитати' })).toEqual({
      state: 'none',
      message: 'Не вдалося прочитати',
    });
  });

  it.each([
    undefined,
    null,
    [],
    {},
    { state: 'protected' },
    { state: 'unknown', message: null },
    { state: 'session', message: 5 },
    { state: 'session', message: 'x'.repeat(501) },
  ])('rejects %j', (value) => {
    expect(() => parseApiKeyStatus(value)).toThrow(TypeError);
  });
});

describe('parseImportTranscriptResult (ADR 039)', () => {
  const REVISION = {
    id: ID,
    source_id: ID,
    revision: 1,
    origin: 'import',
    run_id: null,
    parent_id: null,
    timing_level: 'segment',
    language: 'uk',
    duration_ms: 12000,
    word_count: 0,
    segment_count: 1,
    coverage: [{ start_ms: 0, end_ms: 12000, state: 'succeeded' }],
    created_at: '2026-11-16T10:00:00Z',
  };

  it('accepts a cancel, a revision and an error, and drops every other key', () => {
    expect(parseImportTranscriptResult({ canceled: true, path: 'C:\\x.srt' })).toEqual({
      canceled: true,
    });
    expect(
      parseImportTranscriptResult({
        ok: true,
        data: { ...REVISION, metadata: { provider: 'import' }, text: '1\n', path: 'C:\\x.srt' },
      }),
    ).toEqual({ ok: true, data: REVISION });
    expect(
      parseImportTranscriptResult(
        apiFailure('TRANSCRIPT_FILE_UNREADABLE', 'Не вдалося прочитати файл субтитрів'),
      ),
    ).toEqual({
      ok: false,
      error: {
        code: 'TRANSCRIPT_FILE_UNREADABLE',
        message: 'Не вдалося прочитати файл субтитрів',
        retryable: false,
      },
    });
  });

  it.each([
    undefined,
    null,
    'canceled',
    { canceled: false },
    { canceled: 'true' },
    { ok: true, data: { id: ID } },
    { ok: true, data: { ...REVISION, revision: -1 } },
    { ok: true, data: { ...REVISION, coverage: 'all' } },
    { ok: true },
  ])('rejects %j', (value) => {
    expect(() => parseImportTranscriptResult(value)).toThrow(TypeError);
  });

  it('names its channel when the answer has no shape of its own', () => {
    expect(() => parseImportTranscriptResult(undefined)).toThrow('dialog:import-transcript');
    expect(() => parseImportTranscriptResult({ canceled: 'yes' })).toThrow(
      'dialog:import-transcript',
    );
  });
});

describe('parseApiKeyResult', () => {
  it('accepts a status and an error, and names the channel it was asked for', () => {
    expect(
      parseApiKeyResult({ ok: true, data: { state: 'session', message: null, x: 1 } }, 'ch'),
    ).toEqual({ ok: true, data: { state: 'session', message: null } });
    expect(parseApiKeyResult(apiFailure('INVALID_API_KEY', 'Ключ має…'), 'ch')).toMatchObject({
      ok: false,
      error: { code: 'INVALID_API_KEY' },
    });
    expect(() => parseApiKeyResult({ ok: true, data: { state: 'x' } }, 'settings:set-api-key')).toThrow(
      'settings:set-api-key',
    );
  });
});
