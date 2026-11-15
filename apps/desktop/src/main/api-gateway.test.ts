// @vitest-environment node
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

import type { BrowserWindow, IpcMainInvokeEvent } from 'electron';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { API_OPS, parsePage, parseSource } from '../shared/api';
import type { ApiOp } from '../shared/api';
import { ApiGateway, registerApiIpc, toIpcSender } from './api-gateway';
import type { ApiBackend, IpcRegistry, OpenDialogHost, OpenPathHost } from './api-gateway';
import type { IpcSender, TrustedPage } from './ipc-guard';
import { jsonResponse, startReadySidecar, stopAllSidecars, PORT } from './sidecar-test-harness';

const PAGE = 'file:///C:/app/out/renderer/index.html';
const TRUSTED: TrustedPage = { webContentsId: 7, pageUrl: PAGE };
const SENDER: IpcSender = { webContentsId: 7, frameUrl: PAGE, isMainFrame: true };
const WINDOW = { id: 1 } as unknown as BrowserWindow;

const ID = '3f2b8c1e-5d4a-4b7e-9a10-2c6d8e0f1a2b';
const ID2 = '0a9d7e6c-1b2f-4c3d-8e4f-5a6b7c8d9e0f';
const PICKED_VIDEO = resolve('clipforge-test', 'videos', 'color_gop.mp4');
const PICKED_FOLDER = resolve('clipforge-test', 'Exports');
const PROFILE = {
  video_codec: 'h264',
  quality: 'high',
  audio_mode: 'selected',
  audio_stream_index: 1,
  write_srt: false,
};
const PROJECT = {
  id: ID,
  name: 'Перевірка',
  created_at: '2026-11-14T10:00:00Z',
  updated_at: '2026-11-14T10:00:00Z',
  archived_at: null,
  archived: false,
};
/** What GET /v1/projects answers per project: the project plus the figures of the list. */
const PROJECT_LIST_ITEM = {
  ...PROJECT,
  source_count: 2,
  clip_count: 3,
  last_activity_at: '2026-11-14T11:30:00Z',
};
/** What POST /v1/sources really answers: includes the path and probe dump. */
const SOURCE_RAW = {
  id: ID2,
  project_id: ID,
  path: PICKED_VIDEO,
  display_name: 'color_gop.mp4',
  size: 1234,
  mtime_ns: 1,
  quick_fingerprint: 'q',
  sha256: null,
  hash_status: 'pending',
  duration_ms: 12000,
  video_stream_index: 0,
  audio_streams: [
    {
      index: 1,
      codec: 'aac',
      channels: 2,
      language: null,
      title: null,
      default: true,
      start_offset_ms: 0,
    },
  ],
  canonical_origin: { origin_ms: 0, origin_pts: 0, time_base: '1/15360' },
  probe: {
    format_name: 'mov,mp4,m4a,3gp,3g2,mj2',
    video_codec: 'h264',
    width: 640,
    height: 360,
    rotation: 0,
    hdr: false,
    unsupported_reason: null,
    frame_rate: '30/1',
    vfr: false,
  },
  availability: 'available',
  created_at: '2026-11-14T10:00:00Z',
  hash_job_id: ID,
};
const CREATED = { id: ID, job_id: ID2, status: 'queued' };
const PREVIEW = {
  id: ID,
  source_id: ID2,
  audio_stream_index: 1,
  status: 'ready',
  job_id: ID,
  error: null,
};
const CHECKS = {
  ffmpeg: { ok: true, version: '8.0.1' },
  ffprobe: { ok: true, version: '8.0.1' },
  data_dir: { ok: true, free_bytes: 123_456_789_012 },
};

afterEach(async () => {
  await stopAllSidecars();
});

function deferred<T>(): {
  readonly promise: Promise<T>;
  readonly resolve: (value: T) => void;
} {
  let resolvePromise: (value: T) => void = () => undefined;
  const promise = new Promise<T>((resolveFn) => {
    resolvePromise = resolveFn;
  });
  return { promise, resolve: resolvePromise };
}

interface Harness {
  readonly gateway: ApiGateway;
  readonly request: ReturnType<typeof vi.fn<ApiBackend['request']>>;
  readonly showOpenDialog: ReturnType<typeof vi.fn<OpenDialogHost['showOpenDialog']>>;
  readonly openPath: ReturnType<typeof vi.fn<OpenPathHost['openPath']>>;
}

/** Plausible backend answer for the calls that tests do not script themselves. */
function defaultAnswer(method: string, path: string): unknown {
  if (path === '/v1/sources') {
    return SOURCE_RAW;
  }
  return method === 'GET' && path === '/v1/projects' ? { items: [], next_cursor: null } : PROJECT;
}

function setup(trusted: TrustedPage | null = TRUSTED): Harness {
  const request = vi.fn<ApiBackend['request']>((method, path) =>
    Promise.resolve({ ok: true, data: defaultAnswer(method, path) }),
  );
  const showOpenDialog = vi.fn<OpenDialogHost['showOpenDialog']>(() =>
    Promise.resolve({ canceled: false, filePaths: [PICKED_VIDEO] }),
  );
  const openPath = vi.fn<OpenPathHost['openPath']>(() => Promise.resolve(''));
  const gateway = new ApiGateway({
    backend: { request },
    dialog: { showOpenDialog },
    shell: { openPath },
    getWindow: () => WINDOW,
    getTrustedPage: () => trusted,
  });
  return { gateway, request, showOpenDialog, openPath };
}

describe('api:call allowlist', () => {
  it('rejects an unknown op without calling the sidecar', async () => {
    const { gateway, request } = setup();

    for (const op of [
      'projects.delete',
      'sources.create',
      'settings.get',
      'runtime.credentials',
      'projects.list ',
      'PROJECTS.LIST',
      '/v1/health',
      'constructor',
      '__proto__',
      'toString',
      'hasOwnProperty',
      '',
      42,
      null,
      ['projects.list'],
    ]) {
      expect(await gateway.call(SENDER, { op, params: {} })).toMatchObject({
        ok: false,
        error: { code: 'UNKNOWN_OP', retryable: false },
      });
    }
    expect(request).not.toHaveBeenCalled();
  });

  it.each([
    undefined,
    null,
    'projects.list',
    42,
    [],
    {},
    { params: {} },
    { op: 'projects.list', params: {}, url: 'http://evil.example' },
    { op: 'projects.list', params: {}, headers: { Authorization: 'x' } },
  ])('rejects the malformed envelope %j', async (payload) => {
    const { gateway, request } = setup();

    expect(await gateway.call(SENDER, payload)).toMatchObject({
      ok: false,
      error: { code: 'INVALID_REQUEST' },
    });
    expect(request).not.toHaveBeenCalled();
  });

  it('accepts every allowlisted op with valid params and maps it to its fixed call', async () => {
    const answers: Readonly<Record<ApiOp, unknown>> = {
      'projects.list': { items: [PROJECT_LIST_ITEM], next_cursor: null },
      'projects.create': PROJECT,
      'projects.update': PROJECT,
      'sources.get': SOURCE_RAW,
      'sources.list': { items: [SOURCE_RAW], next_cursor: null },
      'clips.list': { items: [], next_cursor: null },
      'clips.create': CLIP,
      'clips.patch': CLIP,
      'clips.review': CLIP,
      'exports.create': CREATED,
      'exports.get': EXPORT_BATCH,
      'exports.retry': CREATED,
      'jobs.get': JOB,
      'jobs.cancel': JOB,
      'previews.create': CREATED,
      'previews.get': PREVIEW,
      'system.checks': { ...CHECKS, extra: 'x' },
    };
    const params: Readonly<Record<ApiOp, unknown>> = {
      'projects.list': {},
      'projects.create': { name: 'a' },
      'projects.update': { id: ID, name: 'b' },
      'sources.get': { id: ID },
      'sources.list': { project_id: ID },
      'clips.list': { source_id: ID },
      'clips.create': { source_id: ID, title: 't', start_ms: 0, end_ms: 1, categories: ['funny'] },
      'clips.patch': { id: ID, expected_revision: 1, title: 'x' },
      'clips.review': { id: ID, expected_revision: 1, status: 'approved' },
      'exports.create': { project_id: ID, clip_ids: [ID], folderToken: '', profile: PROFILE },
      'exports.get': { id: ID },
      'exports.retry': { batch_id: ID },
      'jobs.get': { id: ID },
      'jobs.cancel': { id: ID },
      'previews.create': { source_id: ID, audio_stream_index: 1 },
      'previews.get': { id: ID },
      'system.checks': {},
    };
    const { gateway, request, showOpenDialog } = setup();
    const folder = await (async () => {
      showOpenDialog.mockResolvedValueOnce({ canceled: false, filePaths: [PICKED_FOLDER] });
      return gateway.chooseExportFolder(SENDER);
    })();
    const token = 'token' in folder ? folder.token : '';

    for (const op of API_OPS) {
      if (op === 'exports.retry') {
        // The first step reads the batch: the retry needs one with a failed item.
        request.mockResolvedValueOnce({ ok: true, data: FAILED_BATCH });
      }
      request.mockResolvedValueOnce({ ok: true, data: answers[op] });
      const given =
        op === 'exports.create' ? { ...(params[op] as object), folderToken: token } : params[op];

      const result = await gateway.call(SENDER, { op, params: given });

      expect(result, op).toMatchObject({ ok: true });
    }
    expect(request.mock.calls.map(([method, path]) => `${method} ${path}`)).toEqual([
      'GET /v1/projects',
      'POST /v1/projects',
      `PATCH /v1/projects/${ID}`,
      `GET /v1/sources/${ID}`,
      `GET /v1/sources?project_id=${ID}&limit=100`,
      `GET /v1/clips?source_id=${ID}`,
      'POST /v1/clips',
      `PATCH /v1/clips/${ID}`,
      `POST /v1/clips/${ID}/review`,
      'POST /v1/export-batches',
      `GET /v1/export-batches/${ID}`,
      `GET /v1/export-batches/${ID}`,
      'POST /v1/export-batches',
      `GET /v1/jobs/${ID}`,
      `POST /v1/jobs/${ID}/cancel`,
      `POST /v1/sources/${ID}/previews`,
      `GET /v1/previews/${ID}`,
      'GET /v1/system/checks',
    ]);
  });

  it('forwards a backend error as code, message and retryable', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({
      ok: false,
      error: { code: 'STALE_REVISION', message: 'Кліп змінено', retryable: false },
    });

    expect(
      await gateway.call(SENDER, {
        op: 'clips.review',
        params: { id: ID, expected_revision: 1, status: 'approved' },
      }),
    ).toEqual({
      ok: false,
      error: { code: 'STALE_REVISION', message: 'Кліп змінено', retryable: false },
    });
  });

  it('sanitises the answer: unknown fields are dropped, a malformed answer becomes an error', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({ ok: true, data: { ...PROJECT, secret: 'x' } });
    request.mockResolvedValueOnce({ ok: true, data: { id: ID } });

    const good = await gateway.call(SENDER, { op: 'projects.create', params: { name: 'a' } });
    const broken = await gateway.call(SENDER, { op: 'projects.create', params: { name: 'a' } });

    expect(good).toEqual({
      ok: true,
      data: {
        id: ID,
        name: 'Перевірка',
        created_at: PROJECT.created_at,
        updated_at: PROJECT.updated_at,
        archived_at: null,
      },
    });
    expect(broken).toMatchObject({ ok: false, error: { code: 'BAD_BACKEND_RESPONSE' } });
  });

  it('projects.list: sends the search text encoded and keeps the figures of each project', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({
      ok: true,
      data: { items: [{ ...PROJECT_LIST_ITEM, secret: 'x' }], next_cursor: null },
    });

    const result = await gateway.call(SENDER, {
      op: 'projects.list',
      params: { q: 'Кирилиця & ТЕСТ', include_archived: true },
    });

    expect(request.mock.calls[0]?.[1]).toBe(
      `/v1/projects?include_archived=true&q=${encodeURIComponent('Кирилиця & ТЕСТ').replaceAll('%20', '+')}`,
    );
    expect(result).toEqual({
      ok: true,
      data: {
        items: [
          {
            id: ID,
            name: 'Перевірка',
            created_at: PROJECT.created_at,
            updated_at: PROJECT.updated_at,
            archived_at: null,
            source_count: 2,
            clip_count: 3,
            last_activity_at: '2026-11-14T11:30:00Z',
          },
        ],
        next_cursor: null,
      },
    });
  });

  it('projects.list: an item without the figures is a malformed answer', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({ ok: true, data: { items: [PROJECT], next_cursor: null } });

    expect(await gateway.call(SENDER, { op: 'projects.list', params: {} })).toMatchObject({
      ok: false,
      error: { code: 'BAD_BACKEND_RESPONSE' },
    });
  });
});

describe('api:call params', () => {
  const NOT_UUIDS = ['', 'abc', '../health', `${ID}/cancel`, `${ID}?x=1`, ID.toUpperCase(), 7];
  const withId: readonly (readonly [ApiOp, Record<string, unknown>])[] = [
    ['projects.update', {}],
    ['sources.get', {}],
    ['clips.patch', { expected_revision: 1 }],
    ['clips.review', { expected_revision: 1, status: 'approved' }],
    ['exports.get', {}],
    ['jobs.get', {}],
    ['jobs.cancel', {}],
    ['previews.get', {}],
  ];

  it.each(withId)('%s: a non-uuid id is rejected without a sidecar call', async (op, rest) => {
    const { gateway, request } = setup();

    for (const bad of NOT_UUIDS) {
      expect(await gateway.call(SENDER, { op, params: { ...rest, id: bad } })).toMatchObject({
        ok: false,
        error: { code: 'INVALID_PARAMS' },
      });
    }
    expect(request).not.toHaveBeenCalled();
  });

  it('rejects a non-uuid id in a query, body list or project reference', async () => {
    const { gateway, request } = setup();
    const calls: readonly [ApiOp, unknown][] = [
      ['clips.list', { source_id: 'x' }],
      ['sources.list', { project_id: 'x' }],
      ['sources.list', { project_id: `${ID}&limit=1` }],
      ['sources.list', { project_id: `${ID}/../health` }],
      ['clips.create', { source_id: 'x', title: 't', start_ms: 0, end_ms: 1, categories: [] }],
      ['exports.create', { project_id: 'x', clip_ids: [ID], folderToken: 'a'.repeat(64), profile: PROFILE }],
      ['exports.create', { project_id: ID, clip_ids: ['x'], folderToken: 'a'.repeat(64), profile: PROFILE }],
      ['previews.create', { source_id: 'x', audio_stream_index: null }],
      ['previews.create', { source_id: `${ID}/previews`, audio_stream_index: 1 }],
    ];

    for (const [op, params] of calls) {
      expect(await gateway.call(SENDER, { op, params })).toMatchObject({
        ok: false,
        error: { code: 'INVALID_PARAMS' },
      });
    }
    expect(request).not.toHaveBeenCalled();
  });

  it('shape-checks params (wrong types, missing and extra keys)', async () => {
    const { gateway, request } = setup();

    for (const [op, params] of [
      ['projects.create', {}],
      ['projects.create', { name: 5 }],
      ['clips.create', { source_id: ID, title: 't', start_ms: '0', end_ms: 1, categories: [] }],
      ['clips.review', { id: ID, expected_revision: 1, status: 'deleted' }],
      ['projects.list', { limit: 'many' }],
      ['projects.list', { q: 5 }],
      ['projects.list', { q: 'а'.repeat(201) }],
      ['projects.list', { q: '🎬'.repeat(201) }],
      ['jobs.get', { id: ID, extra: true }],
      ['sources.list', {}],
      ['sources.list', { project_id: ID, limit: 5 }],
      ['sources.list', { project_id: ID, path: 'C:\\Videos' }],
      ['sources.list', { project_id: ID, cursor: 7 }],
      ['previews.create', { source_id: ID }],
      ['previews.create', { source_id: ID, audio_stream_index: '1' }],
      ['previews.get', { id: ID, extra: true }],
    ] as const) {
      expect(await gateway.call(SENDER, { op, params })).toMatchObject({
        ok: false,
        error: { code: 'INVALID_PARAMS' },
      });
    }
    expect(request).not.toHaveBeenCalled();
  });

  it('adds evidence_type=manual itself and refuses it from the renderer', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({ ok: true, data: CLIP });
    const params = { source_id: ID, title: 't', start_ms: 0, end_ms: 1, categories: ['funny'] };

    await gateway.call(SENDER, { op: 'clips.create', params });
    const refused = await gateway.call(SENDER, {
      op: 'clips.create',
      params: { ...params, evidence_type: 'transcript' },
    });

    expect(request).toHaveBeenCalledTimes(1);
    expect(request.mock.calls[0]?.[2]).toEqual({ ...params, evidence_type: 'manual' });
    expect(refused).toMatchObject({ ok: false, error: { code: 'INVALID_PARAMS' } });
  });
});

describe('source playback (ADR 021)', () => {
  const MKV = {
    ...SOURCE_RAW,
    probe: { ...SOURCE_RAW.probe, format_name: 'matroska,webm' },
  };
  const DIRECT = { direct: true, reason: null, direct_audio_stream_index: 1 };

  it('sources.get adds playback computed from the probe and still hides the probe', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({ ok: true, data: SOURCE_RAW });
    request.mockResolvedValueOnce({ ok: true, data: MKV });

    const direct = await gateway.call(SENDER, { op: 'sources.get', params: { id: ID2 } });
    const proxy = await gateway.call(SENDER, { op: 'sources.get', params: { id: ID2 } });

    expect(direct).toMatchObject({ ok: true, data: { id: ID2, playback: DIRECT } });
    expect(proxy).toMatchObject({
      ok: true,
      data: {
        playback: {
          direct: false,
          reason: 'CONTAINER_UNSUPPORTED',
          direct_audio_stream_index: null,
        },
      },
    });
    for (const result of [direct, proxy]) {
      const serialized = JSON.stringify(result);
      expect(serialized).not.toContain('"probe"');
      expect(serialized).not.toContain('canonical_origin');
      expect(serialized).not.toContain('format_name');
      expect(serialized).not.toContain('"path"');
    }
  });

  it('a non-zero origin puts the source on the proxy', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({
      ok: true,
      data: { ...SOURCE_RAW, canonical_origin: { ...SOURCE_RAW.canonical_origin, origin_ms: 40 } },
    });

    expect(await gateway.call(SENDER, { op: 'sources.get', params: { id: ID2 } })).toMatchObject({
      ok: true,
      data: { playback: { direct: false, reason: 'NONZERO_ORIGIN' } },
    });
  });

  it('sources.get, sources.list and the import add video from the probe (ADR 023)', async () => {
    const { gateway, request } = setup();
    const vfr = { ...MKV, id: ID, probe: { ...MKV.probe, frame_rate: '30000/1001', vfr: true } };
    request.mockResolvedValueOnce({ ok: true, data: SOURCE_RAW });
    request.mockResolvedValueOnce({
      ok: true,
      data: { items: [SOURCE_RAW, vfr], next_cursor: null },
    });
    request.mockResolvedValueOnce({ ok: true, data: vfr });
    const constant = { width: 640, height: 360, frame_rate: '30/1', vfr: false };
    const variable = { width: 640, height: 360, frame_rate: '30000/1001', vfr: true };

    const got = await gateway.call(SENDER, { op: 'sources.get', params: { id: ID2 } });
    const listed = await gateway.call(SENDER, { op: 'sources.list', params: { project_id: ID } });
    const imported = await gateway.importVideo(SENDER, { project_id: ID });

    expect(got).toMatchObject({ ok: true, data: { video: constant } });
    expect(listed).toMatchObject({
      ok: true,
      data: { items: [{ video: constant }, { video: variable }] },
    });
    expect(imported).toMatchObject({ ok: true, data: { video: variable } });
    for (const result of [got, listed, imported]) {
      expect(JSON.stringify(result)).not.toContain('"probe"');
    }
  });

  it('a probe without frame rate and VFR keys answers video with null for them', async () => {
    const { gateway, request } = setup();
    // JSON drops the keys: they are missing, as in the answer for an old source.
    const oldProbe: unknown = JSON.parse(
      JSON.stringify({ ...SOURCE_RAW.probe, frame_rate: undefined, vfr: undefined }),
    );
    request.mockResolvedValueOnce({ ok: true, data: { ...SOURCE_RAW, probe: oldProbe } });

    const result = await gateway.call(SENDER, { op: 'sources.get', params: { id: ID2 } });

    expect(result).toMatchObject({
      ok: true,
      data: { video: { width: 640, height: 360, frame_rate: null, vfr: null } },
    });
  });

  it('dialog:import-video answers with playback too', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({ ok: true, data: SOURCE_RAW });

    expect(await gateway.importVideo(SENDER, { project_id: ID })).toMatchObject({
      ok: true,
      data: { playback: DIRECT },
    });
  });

  it('every source answer passes the preload parser again', async () => {
    const { gateway } = setup();

    const result = await gateway.importVideo(SENDER, { project_id: ID });

    if (!('ok' in result) || !result.ok) {
      throw new Error('the import did not succeed');
    }
    expect(parseSource(result.data)).toEqual(result.data);
  });

  it('sources.list answers a page with playback per source and without path, probe or origin', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({
      ok: true,
      data: { items: [SOURCE_RAW, { ...MKV, id: ID }], next_cursor: 'next-page' },
    });

    const result = await gateway.call(SENDER, { op: 'sources.list', params: { project_id: ID } });

    expect(request.mock.calls).toEqual([['GET', `/v1/sources?project_id=${ID}&limit=100`, undefined]]);
    expect(result).toMatchObject({
      ok: true,
      data: {
        items: [
          { id: ID2, project_id: ID, display_name: 'color_gop.mp4', playback: DIRECT },
          { id: ID, playback: { direct: false, reason: 'CONTAINER_UNSUPPORTED' } },
        ],
        next_cursor: 'next-page',
      },
    });
    const serialized = JSON.stringify(result);
    for (const hidden of [
      '"probe"',
      'canonical_origin',
      'format_name',
      '"path"',
      'mtime_ns',
      'quick_fingerprint',
      PICKED_VIDEO.replaceAll('\\', '\\\\'),
    ]) {
      expect(serialized).not.toContain(hidden);
    }
  });

  it('sources.list passes the cursor on and every item passes the preload parser again', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({ ok: true, data: { items: [SOURCE_RAW], next_cursor: null } });

    const result = await gateway.call(SENDER, {
      op: 'sources.list',
      params: { project_id: ID, cursor: 'a b&c' },
    });

    expect(request.mock.calls[0]?.[1]).toBe(`/v1/sources?project_id=${ID}&limit=100&cursor=a+b%26c`);
    if (!result.ok) {
      throw new Error('the list did not succeed');
    }
    expect(parsePage(parseSource)(result.data)).toEqual(result.data);
  });

  it('sources.list returns an empty page as it is', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({ ok: true, data: { items: [], next_cursor: null } });

    expect(await gateway.call(SENDER, { op: 'sources.list', params: { project_id: ID } })).toEqual({
      ok: true,
      data: { items: [], next_cursor: null },
    });
  });

  it('sources.list forwards a backend error and rejects a malformed page', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({
      ok: false,
      error: { code: 'PROJECT_NOT_FOUND', message: 'Проєкт не знайдено', retryable: false },
    });
    request.mockResolvedValueOnce({ ok: true, data: { items: [{ id: ID2 }], next_cursor: null } });
    request.mockResolvedValueOnce({ ok: true, data: { items: [SOURCE_RAW] } });
    request.mockResolvedValueOnce({ ok: true, data: [SOURCE_RAW] });

    expect(await gateway.call(SENDER, { op: 'sources.list', params: { project_id: ID } })).toEqual({
      ok: false,
      error: { code: 'PROJECT_NOT_FOUND', message: 'Проєкт не знайдено', retryable: false },
    });
    for (let attempt = 0; attempt < 3; attempt += 1) {
      expect(await gateway.call(SENDER, { op: 'sources.list', params: { project_id: ID } })).toMatchObject({
        ok: false,
        error: { code: 'BAD_BACKEND_RESPONSE' },
      });
    }
  });

  it('previews.create posts the audio stream and previews.get reads one preview', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({ ok: true, data: { ...CREATED, extra: 'x' } });
    request.mockResolvedValueOnce({ ok: true, data: { ...PREVIEW, path: 'C:\\cache\\p.mp4' } });

    const created = await gateway.call(SENDER, {
      op: 'previews.create',
      params: { source_id: ID2, audio_stream_index: null },
    });
    const read = await gateway.call(SENDER, { op: 'previews.get', params: { id: ID } });

    expect(request.mock.calls).toEqual([
      ['POST', `/v1/sources/${ID2}/previews`, { audio_stream_index: null }],
      ['GET', `/v1/previews/${ID}`, undefined],
    ]);
    expect(created).toEqual({ ok: true, data: CREATED });
    expect(read).toEqual({ ok: true, data: PREVIEW });
  });

  it('forwards a preview error and rejects a malformed preview answer', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({
      ok: false,
      error: { code: 'PREVIEW_NOT_READY', message: 'Попередній перегляд ще не готовий', retryable: false },
    });
    request.mockResolvedValueOnce({ ok: true, data: { id: ID } });

    expect(await gateway.call(SENDER, { op: 'previews.get', params: { id: ID } })).toMatchObject({
      ok: false,
      error: { code: 'PREVIEW_NOT_READY' },
    });
    expect(await gateway.call(SENDER, { op: 'previews.get', params: { id: ID } })).toMatchObject({
      ok: false,
      error: { code: 'BAD_BACKEND_RESPONSE' },
    });
  });
});

describe('untrusted senders', () => {
  const foreign: readonly (readonly [string, IpcSender])[] = [
    ['another web contents', { ...SENDER, webContentsId: 8 }],
    ['a sub frame', { ...SENDER, isMainFrame: false }],
    ['a destroyed frame', { ...SENDER, frameUrl: null }],
    ['a foreign page', { ...SENDER, frameUrl: 'https://example.com/' }],
  ];

  it.each(foreign)('rejects every channel for %s', async (_name, sender) => {
    const { gateway, request, showOpenDialog, openPath } = setup();

    await expect(gateway.call(sender, { op: 'projects.list', params: {} })).rejects.toThrow(
      'Недозволений відправник IPC',
    );
    await expect(
      gateway.call(sender, { op: 'sources.list', params: { project_id: ID } }),
    ).rejects.toThrow('Недозволений відправник IPC');
    await expect(gateway.call(sender, { op: 'exports.retry', params: { batch_id: ID } })).rejects.toThrow(
      'Недозволений відправник IPC',
    );
    await expect(gateway.importVideo(sender, { project_id: ID })).rejects.toThrow(
      'Недозволений відправник IPC',
    );
    await expect(
      gateway.importVideo(sender, { project_id: ID, file_token: 'ab'.repeat(32) }),
    ).rejects.toThrow('Недозволений відправник IPC');
    await expect(gateway.pickVideo(sender)).rejects.toThrow('Недозволений відправник IPC');
    await expect(gateway.takeDroppedVideo(sender, { path: PICKED_VIDEO })).rejects.toThrow(
      'Недозволений відправник IPC',
    );
    await expect(gateway.chooseExportFolder(sender)).rejects.toThrow('Недозволений відправник IPC');
    await expect(gateway.openExportFolder(sender, { batch_id: ID })).rejects.toThrow(
      'Недозволений відправник IPC',
    );
    expect(request).not.toHaveBeenCalled();
    expect(showOpenDialog).not.toHaveBeenCalled();
    expect(openPath).not.toHaveBeenCalled();
  });

  it('rejects everything while no window is trusted', async () => {
    const { gateway, request, showOpenDialog, openPath } = setup(null);

    await expect(gateway.call(SENDER, { op: 'projects.list', params: {} })).rejects.toThrow();
    await expect(gateway.importVideo(SENDER, { project_id: ID })).rejects.toThrow();
    await expect(gateway.pickVideo(SENDER)).rejects.toThrow();
    await expect(gateway.takeDroppedVideo(SENDER, { path: PICKED_VIDEO })).rejects.toThrow();
    await expect(gateway.chooseExportFolder(SENDER)).rejects.toThrow();
    await expect(gateway.openExportFolder(SENDER, { batch_id: ID })).rejects.toThrow();
    expect(request).not.toHaveBeenCalled();
    expect(showOpenDialog).not.toHaveBeenCalled();
    expect(openPath).not.toHaveBeenCalled();
  });

  it('checks the sender before it looks at the payload', async () => {
    const { gateway } = setup();
    const sender = { ...SENDER, webContentsId: 8 };

    await expect(gateway.call(sender, 'garbage')).rejects.toThrow('Недозволений відправник IPC');
    await expect(gateway.importVideo(sender, 'garbage')).rejects.toThrow(
      'Недозволений відправник IPC',
    );
    await expect(gateway.takeDroppedVideo(sender, 'garbage')).rejects.toThrow(
      'Недозволений відправник IPC',
    );
    await expect(gateway.openExportFolder(sender, 'garbage')).rejects.toThrow(
      'Недозволений відправник IPC',
    );
  });

  it('does not touch the disk for an untrusted drop, even a path that exists', async () => {
    const sandbox = mkdtempSync(join(tmpdir(), 'clipforge-untrusted-'));
    try {
      const file = join(sandbox, 'clip.mp4');
      writeFileSync(file, 'x');
      const { gateway, request } = setup();
      const sender = { ...SENDER, isMainFrame: false };

      await expect(gateway.takeDroppedVideo(sender, { path: file })).rejects.toThrow(
        'Недозволений відправник IPC',
      );
      // Nothing was issued: the same path from the trusted page gets a fresh, working token.
      const choice = await gateway.takeDroppedVideo(SENDER, { path: file });
      expect(choice).toHaveProperty('token');
      expect(request).not.toHaveBeenCalled();
    } finally {
      rmSync(sandbox, { recursive: true, force: true });
    }
  });
});

describe('registerApiIpc', () => {
  const FRAME = { url: PAGE };

  function event(webContentsId: number): IpcMainInvokeEvent {
    return {
      sender: { id: webContentsId, mainFrame: FRAME },
      senderFrame: FRAME,
    } as unknown as IpcMainInvokeEvent;
  }

  function register(): {
    readonly handlers: Map<string, (event: IpcMainInvokeEvent, ...args: unknown[]) => unknown>;
    readonly request: Harness['request'];
  } {
    const { gateway, request } = setup();
    const handlers = new Map<string, (event: IpcMainInvokeEvent, ...args: unknown[]) => unknown>();
    const registry: IpcRegistry = {
      handle: (channel, listener) => {
        handlers.set(channel, listener);
      },
    };
    registerApiIpc(registry, gateway);
    return { handlers, request };
  }

  it('registers exactly the six gateway channels', () => {
    expect([...register().handlers.keys()].sort()).toEqual([
      'api:call',
      'dialog:import-video',
      'dialog:pick-video',
      'drop:video',
      'export:choose-folder',
      'export:open-folder',
    ]);
  });

  it('serves the app page and refuses any other web contents on every channel', async () => {
    const { handlers, request } = register();

    expect(
      await handlers.get('api:call')?.(event(7), { op: 'projects.list', params: {} }),
    ).toMatchObject({ ok: true });
    expect(request).toHaveBeenCalledTimes(1);
    for (const [channel, payload] of [
      ['api:call', { op: 'projects.list', params: {} }],
      ['dialog:import-video', { project_id: ID }],
      ['dialog:import-video', { project_id: ID, file_token: 'ab'.repeat(32) }],
      ['dialog:pick-video', undefined],
      ['drop:video', { path: PICKED_VIDEO }],
      ['export:choose-folder', undefined],
      ['export:open-folder', { batch_id: ID }],
    ] as const) {
      await expect(Promise.resolve().then(() => handlers.get(channel)?.(event(99), payload))).rejects.toThrow(
        'Недозволений відправник IPC',
      );
    }
    expect(request).toHaveBeenCalledTimes(1);
  });

  it('toIpcSender marks only the top frame as main', () => {
    const sub = { url: PAGE };
    const subEvent = { sender: { id: 7, mainFrame: FRAME }, senderFrame: sub } as unknown as IpcMainInvokeEvent;
    const gone = { sender: { id: 7, mainFrame: FRAME }, senderFrame: null } as unknown as IpcMainInvokeEvent;

    expect(toIpcSender(event(7))).toEqual(SENDER);
    expect(toIpcSender(subEvent)).toEqual({ ...SENDER, isMainFrame: false });
    expect(toIpcSender(gone)).toEqual({ ...SENDER, frameUrl: null, isMainFrame: false });
  });
});

describe('dialog:import-video', () => {
  it('opens a video picker on the window, then registers the picked file itself', async () => {
    const { gateway, request, showOpenDialog } = setup();
    request.mockResolvedValueOnce({ ok: true, data: SOURCE_RAW });

    const result = await gateway.importVideo(SENDER, { project_id: ID });

    expect(showOpenDialog).toHaveBeenCalledTimes(1);
    const [window, options] = showOpenDialog.mock.calls[0] ?? [];
    expect(window).toBe(WINDOW);
    expect(options?.properties).toEqual(['openFile']);
    expect(options?.filters?.[0]?.extensions).toEqual(
      expect.arrayContaining(['mp4', 'mov', 'mkv', 'webm', 'avi']),
    );
    expect(request).toHaveBeenCalledWith('POST', '/v1/sources', {
      project_id: ID,
      path: PICKED_VIDEO,
    });
    expect(result).toMatchObject({
      ok: true,
      data: { id: ID2, project_id: ID, display_name: 'color_gop.mp4', duration_ms: 12000 },
    });
  });

  it('returns the source without its path, probe dump or canonical origin', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({ ok: true, data: SOURCE_RAW });

    const serialized = JSON.stringify(await gateway.importVideo(SENDER, { project_id: ID }));

    expect(serialized).not.toContain(PICKED_VIDEO.replaceAll('\\', '\\\\'));
    expect(serialized).not.toContain('probe');
    expect(serialized).not.toContain('canonical_origin');
    expect(serialized).not.toContain('"path"');
  });

  it('returns {canceled:true} and registers nothing when the user cancels', async () => {
    const { gateway, request, showOpenDialog } = setup();
    showOpenDialog.mockResolvedValueOnce({ canceled: true, filePaths: [] });
    showOpenDialog.mockResolvedValueOnce({ canceled: false, filePaths: [] });

    expect(await gateway.importVideo(SENDER, { project_id: ID })).toEqual({ canceled: true });
    expect(await gateway.importVideo(SENDER, { project_id: ID })).toEqual({ canceled: true });
    expect(request).not.toHaveBeenCalled();
  });

  it('forwards a registration error', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({
      ok: false,
      error: { code: 'NOT_A_VIDEO', message: 'Файл не є відео', retryable: false },
    });

    expect(await gateway.importVideo(SENDER, { project_id: ID })).toEqual({
      ok: false,
      error: { code: 'NOT_A_VIDEO', message: 'Файл не є відео', retryable: false },
    });
  });

  it('refuses a path or any extra key from the renderer before the dialog opens', async () => {
    const { gateway, request, showOpenDialog } = setup();

    for (const payload of [
      { project_id: ID, path: 'C:\\Windows\\System32\\x.mp4' },
      { project_id: ID, filePath: 'C:\\x.mp4' },
      { project_id: 'x' },
      { project_id: `${ID}/../x` },
      {},
      'C:\\x.mp4',
      undefined,
    ]) {
      expect(await gateway.importVideo(SENDER, payload)).toMatchObject({
        ok: false,
        error: { code: 'INVALID_PARAMS' },
      });
    }
    expect(showOpenDialog).not.toHaveBeenCalled();
    expect(request).not.toHaveBeenCalled();
  });

  it('shows one dialog at a time', async () => {
    const { gateway, request, showOpenDialog } = setup();
    const first = deferred<{ canceled: boolean; filePaths: string[] }>();
    showOpenDialog.mockReturnValueOnce(first.promise);
    request.mockResolvedValue({ ok: true, data: SOURCE_RAW });

    const pending = gateway.importVideo(SENDER, { project_id: ID });
    await vi.waitFor(() => {
      expect(showOpenDialog).toHaveBeenCalledTimes(1);
    });
    expect(await gateway.importVideo(SENDER, { project_id: ID })).toEqual({ canceled: true });
    expect(await gateway.chooseExportFolder(SENDER)).toEqual({ canceled: true });
    first.resolve({ canceled: false, filePaths: [PICKED_VIDEO] });

    expect(await pending).toMatchObject({ ok: true });
    expect(showOpenDialog).toHaveBeenCalledTimes(1);
    // Closed again: the next request opens a dialog.
    await gateway.importVideo(SENDER, { project_id: ID });
    expect(showOpenDialog).toHaveBeenCalledTimes(2);
  });

  it('frees the dialog when it fails', async () => {
    const { gateway, showOpenDialog } = setup();
    showOpenDialog.mockRejectedValueOnce(new Error('dialog failed'));

    await expect(gateway.importVideo(SENDER, { project_id: ID })).rejects.toThrow('dialog failed');
    expect(await gateway.importVideo(SENDER, { project_id: ID })).toMatchObject({ ok: true });
  });
});

describe('export:choose-folder and folder tokens', () => {
  async function choose(h: Harness, directory = PICKED_FOLDER): Promise<string> {
    h.showOpenDialog.mockResolvedValueOnce({ canceled: false, filePaths: [directory] });
    const choice = await h.gateway.chooseExportFolder(SENDER);
    if (!('token' in choice)) {
      throw new Error('the folder was not chosen');
    }
    return choice.token;
  }

  const createParams = (folderToken: string): object => ({
    op: 'exports.create',
    params: { project_id: ID, clip_ids: [ID2], folderToken, profile: PROFILE },
  });

  it('opens a directory picker and returns only a token and the folder name', async () => {
    const h = setup();
    h.showOpenDialog.mockResolvedValueOnce({ canceled: false, filePaths: [PICKED_FOLDER] });

    const choice = await h.gateway.chooseExportFolder(SENDER);

    const [window, options] = h.showOpenDialog.mock.calls[0] ?? [];
    expect(window).toBe(WINDOW);
    expect(options?.properties).toContain('openDirectory');
    expect(Object.keys(choice).sort()).toEqual(['displayName', 'token']);
    expect(choice).toHaveProperty('displayName', 'Exports');
    expect('token' in choice && /^[0-9a-f]{64}$/.test(choice.token)).toBe(true);
    expect(JSON.stringify(choice)).not.toContain('clipforge-test');
  });

  it('returns {canceled:true} and issues no token when the user cancels', async () => {
    const h = setup();
    h.showOpenDialog.mockResolvedValueOnce({ canceled: true, filePaths: [] });

    expect(await h.gateway.chooseExportFolder(SENDER)).toEqual({ canceled: true });
  });

  it('issues a different token for every choice', async () => {
    const h = setup();

    expect(await choose(h)).not.toBe(await choose(h));
  });

  it('substitutes the picked folder as output_dir', async () => {
    const h = setup();
    const token = await choose(h);
    h.request.mockResolvedValueOnce({ ok: true, data: CREATED });

    const result = await h.gateway.call(SENDER, createParams(token));

    expect(result).toEqual({ ok: true, data: CREATED });
    expect(h.request).toHaveBeenCalledWith('POST', '/v1/export-batches', {
      project_id: ID,
      clip_ids: [ID2],
      profile: PROFILE,
      output_dir: PICKED_FOLDER,
    });
  });

  it('works only once: the second use of the same token is rejected without a sidecar call', async () => {
    const h = setup();
    const token = await choose(h);
    h.request.mockResolvedValue({ ok: true, data: CREATED });

    expect(await h.gateway.call(SENDER, createParams(token))).toMatchObject({ ok: true });
    expect(await h.gateway.call(SENDER, createParams(token))).toMatchObject({
      ok: false,
      error: { code: 'UNKNOWN_FOLDER_TOKEN', retryable: false },
    });
    expect(h.request).toHaveBeenCalledTimes(1);
  });

  it('is used up even when the backend refuses the batch', async () => {
    const h = setup();
    const token = await choose(h);
    h.request.mockResolvedValueOnce({
      ok: false,
      error: { code: 'CLIP_NOT_APPROVED', message: 'Кліп не схвалено', retryable: false },
    });

    expect(await h.gateway.call(SENDER, createParams(token))).toMatchObject({
      ok: false,
      error: { code: 'CLIP_NOT_APPROVED' },
    });
    expect(await h.gateway.call(SENDER, createParams(token))).toMatchObject({
      ok: false,
      error: { code: 'UNKNOWN_FOLDER_TOKEN' },
    });
    expect(h.request).toHaveBeenCalledTimes(1);
  });

  it('rejects a well-formed token main never issued', async () => {
    const h = setup();
    await choose(h);

    expect(await h.gateway.call(SENDER, createParams('cd'.repeat(32)))).toMatchObject({
      ok: false,
      error: { code: 'UNKNOWN_FOLDER_TOKEN' },
    });
    expect(h.request).not.toHaveBeenCalled();
  });

  it('rejects a malformed token or a path in its place', async () => {
    const h = setup();
    await choose(h);

    for (const bad of [PICKED_FOLDER, 'abc', '', 'CD'.repeat(32), 12]) {
      expect(await h.gateway.call(SENDER, createParams(bad as string))).toMatchObject({
        ok: false,
        error: { code: 'INVALID_PARAMS' },
      });
    }
    expect(h.request).not.toHaveBeenCalled();
  });

  it('keeps the token when the request itself is malformed', async () => {
    const h = setup();
    const token = await choose(h);
    h.request.mockResolvedValueOnce({ ok: true, data: CREATED });

    expect(
      await h.gateway.call(SENDER, {
        op: 'exports.create',
        params: { project_id: 'x', clip_ids: [ID2], folderToken: token, profile: PROFILE },
      }),
    ).toMatchObject({ ok: false, error: { code: 'INVALID_PARAMS' } });
    expect(await h.gateway.call(SENDER, createParams(token))).toMatchObject({ ok: true });
  });

  it('keeps tokens apart: each one maps to its own folder', async () => {
    const h = setup();
    const other = resolve('clipforge-test', 'Other');
    const first = await choose(h);
    const second = await choose(h, other);
    h.request.mockResolvedValue({ ok: true, data: CREATED });

    await h.gateway.call(SENDER, createParams(second));
    await h.gateway.call(SENDER, createParams(first));

    expect(h.request.mock.calls.map(([, , body]) => (body as { output_dir: string }).output_dir)).toEqual([
      other,
      PICKED_FOLDER,
    ]);
  });

  it('uses the whole path as the name of a drive or share root', async () => {
    const h = setup();
    const root = resolve('/');
    h.showOpenDialog.mockResolvedValueOnce({ canceled: false, filePaths: [root] });

    const choice = await h.gateway.chooseExportFolder(SENDER);

    expect('displayName' in choice && choice.displayName.trim() !== '').toBe(true);
  });
});

describe('the renderer cannot pass output_dir or a path directly', () => {
  it.each([
    { output_dir: 'C:\\Windows\\System32' },
    { outputDir: 'C:\\Windows\\System32' },
    { path: 'C:\\Windows\\System32' },
    { directory: 'C:\\Windows\\System32' },
  ])('exports.create with %j is refused and its token survives', async (extra) => {
    const h = setup();
    h.showOpenDialog.mockResolvedValueOnce({ canceled: false, filePaths: [PICKED_FOLDER] });
    const choice = await h.gateway.chooseExportFolder(SENDER);
    const token = 'token' in choice ? choice.token : '';

    const refused = await h.gateway.call(SENDER, {
      op: 'exports.create',
      params: { project_id: ID, clip_ids: [ID], folderToken: token, profile: PROFILE, ...extra },
    });
    const withoutToken = await h.gateway.call(SENDER, {
      op: 'exports.create',
      params: { project_id: ID, clip_ids: [ID], profile: PROFILE, ...extra },
    });

    expect(refused).toMatchObject({ ok: false, error: { code: 'INVALID_PARAMS' } });
    expect(withoutToken).toMatchObject({ ok: false, error: { code: 'INVALID_PARAMS' } });
    expect(h.request).not.toHaveBeenCalled();

    h.request.mockResolvedValueOnce({ ok: true, data: CREATED });
    await h.gateway.call(SENDER, {
      op: 'exports.create',
      params: { project_id: ID, clip_ids: [ID], folderToken: token, profile: PROFILE },
    });
    expect(h.request.mock.calls[0]?.[2]).toMatchObject({ output_dir: PICKED_FOLDER });
  });

  it('no op takes a path: sources cannot be registered through api:call', async () => {
    const { gateway, request } = setup();

    for (const op of ['sources.create', 'sources.register', 'sources.relink', 'sources.import']) {
      expect(
        await gateway.call(SENDER, { op, params: { project_id: ID, path: 'C:\\x.mp4' } }),
      ).toMatchObject({ ok: false, error: { code: 'UNKNOWN_OP' } });
    }
    expect(request).not.toHaveBeenCalled();
  });
});

describe('the session token and port stay inside Sidecar', () => {
  it('appear in no value returned to the renderer, even when the backend echoes them', async () => {
    const ready = await startReadySidecar((url, init) => {
      const echo = new Headers(init.headers).get('Authorization') ?? '';
      if (url.endsWith('/v1/projects') && init.method === 'POST') {
        return jsonResponse({ ...PROJECT, name: echo }, 201);
      }
      if (url.endsWith('/v1/sources') && init.method === 'POST') {
        return jsonResponse({ ...SOURCE_RAW, display_name: echo }, 201);
      }
      if (url.includes('/v1/jobs/')) {
        return jsonResponse(
          { error: { code: 'ECHO', message: `bad ${echo}`, retryable: false } },
          401,
        );
      }
      return jsonResponse({ items: [{ ...CLIP, title: echo }], next_cursor: null });
    });
    const dialogs = {
      showOpenDialog: vi.fn<OpenDialogHost['showOpenDialog']>(() =>
        Promise.resolve({ canceled: false, filePaths: [PICKED_VIDEO] }),
      ),
    };
    const gateway = new ApiGateway({
      backend: ready.sidecar,
      dialog: dialogs,
      shell: { openPath: vi.fn<OpenPathHost['openPath']>(() => Promise.resolve('')) },
      getWindow: () => WINDOW,
      getTrustedPage: () => TRUSTED,
    });

    const results = [
      await gateway.call(SENDER, { op: 'projects.create', params: { name: 'a' } }),
      await gateway.call(SENDER, { op: 'jobs.get', params: { id: ID } }),
      await gateway.call(SENDER, { op: 'clips.list', params: {} }),
      await gateway.importVideo(SENDER, { project_id: ID }),
      await gateway.chooseExportFolder(SENDER),
      await gateway.call(SENDER, { op: 'nope', params: {} }),
      await gateway.call(SENDER, { op: 'sources.get', params: { id: 'bad' } }),
    ];
    const serialized = JSON.stringify(results);

    expect(ready.apiCalls().length).toBeGreaterThan(0);
    expect(serialized).not.toContain(ready.token);
    expect(serialized).not.toContain(String(PORT));
    expect(serialized).not.toContain('127.0.0.1');
    expect(serialized).toContain('[redacted]');
    // The token really was sent to the sidecar, by Sidecar and only there.
    const [first] = ready.apiCalls();
    expect(new Headers(first?.init.headers).get('Authorization')).toBe(`Bearer ${ready.token}`);
  });

  it('does not let the renderer reach the sidecar through the gateway with its own URL or header', async () => {
    const ready = await startReadySidecar(() => jsonResponse(PROJECT));
    const gateway = new ApiGateway({
      backend: ready.sidecar,
      dialog: { showOpenDialog: vi.fn<OpenDialogHost['showOpenDialog']>() },
      shell: { openPath: vi.fn<OpenPathHost['openPath']>() },
      getWindow: () => WINDOW,
      getTrustedPage: () => TRUSTED,
    });

    await gateway.call(SENDER, { op: 'projects.list', params: { cursor: '/../health' } });

    const [call] = ready.apiCalls();
    expect(new URL(call?.url ?? '').pathname).toBe('/v1/projects');
    expect(new URL(call?.url ?? '').host).toBe(`127.0.0.1:${String(PORT)}`);
  });
});

describe('exports.get answer', () => {
  it('keeps the measured duration of an item and drops the rest of its observed dump', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({
      ok: true,
      data: {
        ...EXPORT_BATCH,
        items: [
          {
            ...EXPORT_BATCH.items[0],
            observed: { observed_duration_ms: 3760, ffmpeg_version: '8.0', seek_mode: 'preseek' },
          },
          { ...EXPORT_BATCH.items[0], id: ID2, state: 'queued', final_path: null, observed: null },
        ],
      },
    });

    const result = await gateway.call(SENDER, { op: 'exports.get', params: { id: ID } });

    expect(result).toMatchObject({
      ok: true,
      data: { items: [{ state: 'verified', duration_ms: 3760 }, { state: 'queued', duration_ms: null }] },
    });
    expect(JSON.stringify(result)).not.toMatch(/observed|ffmpeg|seek_mode|"profile"/);
  });

  it('refuses a batch whose items have no observed field', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({
      ok: true,
      data: { ...EXPORT_BATCH, items: [{ ...EXPORT_BATCH.items[0], observed: undefined }] },
    });

    expect(await gateway.call(SENDER, { op: 'exports.get', params: { id: ID } })).toMatchObject({
      ok: false,
      error: { code: 'BAD_BACKEND_RESPONSE' },
    });
  });
});

describe('exports.retry', () => {
  const CLIP_A = '11111111-1111-4111-8111-111111111111';
  const CLIP_B = '22222222-2222-4222-8222-222222222222';
  const CLIP_C = '33333333-3333-4333-8333-333333333333';
  const BACKEND_DIR = resolve('clipforge-test', 'BackendExports');

  function batchWith(items: readonly [string, string][], overrides: object = {}): object {
    return {
      ...EXPORT_BATCH,
      status: 'partial_failed',
      output_dir: BACKEND_DIR,
      items: items.map(([clip_id, state]) => ({
        ...EXPORT_BATCH.items[0],
        id: clip_id,
        clip_id,
        state,
      })),
      ...overrides,
    };
  }

  it('posts a new batch of the failed and cancelled clips with the output_dir and profile of the old one', async () => {
    const { gateway, request } = setup();
    const profile = { ...PROFILE, audio_mode: 'all', audio_stream_index: null };
    request.mockResolvedValueOnce({
      ok: true,
      data: batchWith(
        [
          [CLIP_A, 'verified'],
          [CLIP_B, 'failed'],
          [CLIP_C, 'cancelled'],
        ],
        { profile },
      ),
    });
    request.mockResolvedValueOnce({ ok: true, data: { ...CREATED, extra: 'x' } });

    const result = await gateway.call(SENDER, { op: 'exports.retry', params: { batch_id: ID } });

    expect(request.mock.calls).toEqual([
      ['GET', `/v1/export-batches/${ID}`],
      [
        'POST',
        '/v1/export-batches',
        { project_id: ID2, clip_ids: [CLIP_B, CLIP_C], output_dir: BACKEND_DIR, profile },
      ],
    ]);
    expect(result).toEqual({ ok: true, data: CREATED });
  });

  it('never takes output_dir, profile, clip ids or a path from the renderer', async () => {
    const { gateway, request } = setup();

    for (const params of [
      { batch_id: ID, output_dir: 'C:\\Windows\\System32' },
      { batch_id: ID, outputDir: 'C:\\Windows\\System32' },
      { batch_id: ID, path: 'C:\\Windows\\System32' },
      { batch_id: ID, profile: PROFILE },
      { batch_id: ID, clip_ids: [ID2] },
      { batch_id: ID, folderToken: 'a'.repeat(64) },
      { id: ID },
      {},
      { batch_id: 'x' },
      { batch_id: `${ID}/../health` },
      { batch_id: ID.toUpperCase() },
      { batch_id: 7 },
    ]) {
      expect(await gateway.call(SENDER, { op: 'exports.retry', params })).toMatchObject({
        ok: false,
        error: { code: 'INVALID_PARAMS' },
      });
    }
    expect(request).not.toHaveBeenCalled();
  });

  it('exports each failed clip once even when the batch lists it twice', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({
      ok: true,
      data: batchWith([
        [CLIP_B, 'failed'],
        [CLIP_B, 'cancelled'],
      ]),
    });
    request.mockResolvedValueOnce({ ok: true, data: CREATED });

    await gateway.call(SENDER, { op: 'exports.retry', params: { batch_id: ID } });

    expect(request.mock.calls[1]?.[2]).toMatchObject({ clip_ids: [CLIP_B] });
  });

  it('does not retry a batch that is still running or queued', async () => {
    const { gateway, request } = setup();

    for (const status of ['queued', 'running']) {
      request.mockResolvedValueOnce({ ok: true, data: batchWith([[CLIP_B, 'failed']], { status }) });
      expect(await gateway.call(SENDER, { op: 'exports.retry', params: { batch_id: ID } })).toMatchObject({
        ok: false,
        error: { code: 'EXPORT_NOT_FINISHED' },
      });
    }
    expect(request).toHaveBeenCalledTimes(2);
  });

  it('says there is nothing to retry when every item was verified', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({
      ok: true,
      data: batchWith([[CLIP_A, 'verified']], { status: 'succeeded' }),
    });

    expect(await gateway.call(SENDER, { op: 'exports.retry', params: { batch_id: ID } })).toMatchObject({
      ok: false,
      error: { code: 'NOTHING_TO_RETRY' },
    });
    expect(request).toHaveBeenCalledTimes(1);
  });

  it('forwards a failed read of the batch and posts nothing', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({
      ok: false,
      error: { code: 'EXPORT_BATCH_NOT_FOUND', message: 'Пакет експорту не знайдено', retryable: false },
    });

    expect(await gateway.call(SENDER, { op: 'exports.retry', params: { batch_id: ID } })).toEqual({
      ok: false,
      error: { code: 'EXPORT_BATCH_NOT_FOUND', message: 'Пакет експорту не знайдено', retryable: false },
    });
    expect(request).toHaveBeenCalledTimes(1);
  });

  it.each([
    ['no profile', { profile: undefined }],
    ['a profile with an extra key', { profile: { ...PROFILE, crf: 0 } }],
    ['no output_dir', { output_dir: undefined }],
    ['a project that is not a uuid', { project_id: '../x' }],
    ['items that are not a list', { items: 'x' }],
    ['an item with a clip id that is not a uuid', { items: [{ clip_id: 'x', state: 'failed' }] }],
  ])('refuses a batch with %s without posting anything', async (_name, overrides) => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({
      ok: true,
      data: batchWith([[CLIP_B, 'failed']], overrides),
    });

    expect(await gateway.call(SENDER, { op: 'exports.retry', params: { batch_id: ID } })).toMatchObject({
      ok: false,
      error: { code: 'BAD_BACKEND_RESPONSE' },
    });
    expect(request).toHaveBeenCalledTimes(1);
  });

  it('forwards a refusal of the new batch and sanitises a malformed answer', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({ ok: true, data: batchWith([[CLIP_B, 'failed']]) });
    request.mockResolvedValueOnce({
      ok: false,
      error: { code: 'CLIP_NOT_APPROVED', message: 'Кліп не схвалено', retryable: false },
    });
    request.mockResolvedValueOnce({ ok: true, data: batchWith([[CLIP_B, 'failed']]) });
    request.mockResolvedValueOnce({ ok: true, data: { id: ID } });

    expect(await gateway.call(SENDER, { op: 'exports.retry', params: { batch_id: ID } })).toMatchObject({
      ok: false,
      error: { code: 'CLIP_NOT_APPROVED' },
    });
    expect(await gateway.call(SENDER, { op: 'exports.retry', params: { batch_id: ID } })).toMatchObject({
      ok: false,
      error: { code: 'BAD_BACKEND_RESPONSE' },
    });
  });

  it('leaves the folder tokens alone', async () => {
    const h = setup();
    h.showOpenDialog.mockResolvedValueOnce({ canceled: false, filePaths: [PICKED_FOLDER] });
    const choice = await h.gateway.chooseExportFolder(SENDER);
    const token = 'token' in choice ? choice.token : '';
    h.request.mockResolvedValueOnce({ ok: true, data: batchWith([[CLIP_B, 'failed']]) });
    h.request.mockResolvedValueOnce({ ok: true, data: CREATED });
    h.request.mockResolvedValueOnce({ ok: true, data: CREATED });

    await h.gateway.call(SENDER, { op: 'exports.retry', params: { batch_id: ID } });

    // The token still works for the new export the user may start next.
    expect(
      await h.gateway.call(SENDER, {
        op: 'exports.create',
        params: { project_id: ID, clip_ids: [ID2], folderToken: token, profile: PROFILE },
      }),
    ).toMatchObject({ ok: true });
    expect(h.request.mock.calls[2]?.[2]).toMatchObject({ output_dir: PICKED_FOLDER });
  });
});

describe('export:open-folder', () => {
  const MISSING = resolve('clipforge-test', 'no-such-folder-9f3a');
  let sandbox: string;

  beforeEach(() => {
    sandbox = mkdtempSync(join(tmpdir(), 'clipforge-open-'));
  });

  afterEach(() => {
    rmSync(sandbox, { recursive: true, force: true });
  });

  function batchIn(outputDir: unknown): object {
    return { ...EXPORT_BATCH, output_dir: outputDir };
  }

  it('reads the batch, then opens its folder when it is an existing directory', async () => {
    const { gateway, request, openPath } = setup();
    request.mockResolvedValueOnce({ ok: true, data: batchIn(sandbox) });

    const result = await gateway.openExportFolder(SENDER, { batch_id: ID });

    expect(result).toEqual({ ok: true, data: { opened: true } });
    expect(request.mock.calls).toEqual([['GET', `/v1/export-batches/${ID}`]]);
    expect(openPath).toHaveBeenCalledTimes(1);
    expect(openPath).toHaveBeenCalledWith(sandbox);
  });

  it('does not open a folder that is gone', async () => {
    const { gateway, request, openPath } = setup();
    request.mockResolvedValueOnce({ ok: true, data: batchIn(MISSING) });

    const result = await gateway.openExportFolder(SENDER, { batch_id: ID });

    expect(result).toMatchObject({ ok: false, error: { code: 'EXPORT_FOLDER_MISSING', retryable: false } });
    expect(openPath).not.toHaveBeenCalled();
  });

  it('does not open a file: openPath would run it', async () => {
    const { gateway, request, openPath } = setup();
    const file = join(sandbox, 'clip.mp4');
    writeFileSync(file, 'x');
    request.mockResolvedValueOnce({ ok: true, data: batchIn(file) });

    expect(await gateway.openExportFolder(SENDER, { batch_id: ID })).toMatchObject({
      ok: false,
      error: { code: 'EXPORT_FOLDER_MISSING' },
    });
    expect(openPath).not.toHaveBeenCalled();
  });

  it('does not open a relative path, even if such a directory exists', async () => {
    const { gateway, request, openPath } = setup();
    request.mockResolvedValueOnce({ ok: true, data: batchIn('.') });

    expect(await gateway.openExportFolder(SENDER, { batch_id: ID })).toMatchObject({
      ok: false,
      error: { code: 'EXPORT_FOLDER_MISSING' },
    });
    expect(openPath).not.toHaveBeenCalled();
  });

  it('rejects a bad batch_id or any extra key before the sidecar or the shell is touched', async () => {
    const { gateway, request, openPath } = setup();

    for (const payload of [
      { batch_id: 'x' },
      { batch_id: '' },
      { batch_id: `${ID}/../health` },
      { batch_id: ID.toUpperCase() },
      { batch_id: 5 },
      { batch_id: ID, path: sandbox },
      { batch_id: ID, output_dir: sandbox },
      { id: ID },
      {},
      sandbox,
      null,
      undefined,
    ]) {
      expect(await gateway.openExportFolder(SENDER, payload)).toMatchObject({
        ok: false,
        error: { code: 'INVALID_PARAMS' },
      });
    }
    expect(request).not.toHaveBeenCalled();
    expect(openPath).not.toHaveBeenCalled();
  });

  it('forwards an error of the backend', async () => {
    const { gateway, request, openPath } = setup();
    request.mockResolvedValueOnce({
      ok: false,
      error: { code: 'EXPORT_BATCH_NOT_FOUND', message: 'Пакет експорту не знайдено', retryable: false },
    });

    expect(await gateway.openExportFolder(SENDER, { batch_id: ID })).toMatchObject({
      ok: false,
      error: { code: 'EXPORT_BATCH_NOT_FOUND' },
    });
    expect(openPath).not.toHaveBeenCalled();
  });

  it('refuses a batch answer without a usable output_dir', async () => {
    const { gateway, request, openPath } = setup();

    for (const outputDir of [undefined, null, '', 7, { path: sandbox }]) {
      request.mockResolvedValueOnce({ ok: true, data: batchIn(outputDir) });
      expect(await gateway.openExportFolder(SENDER, { batch_id: ID })).toMatchObject({
        ok: false,
        error: { code: 'BAD_BACKEND_RESPONSE' },
      });
    }
    expect(openPath).not.toHaveBeenCalled();
  });

  it('reports a shell that cannot open the folder without echoing its text', async () => {
    const { gateway, request, openPath } = setup();
    request.mockResolvedValueOnce({ ok: true, data: batchIn(sandbox) });
    openPath.mockResolvedValueOnce(`Failed to open path ${sandbox}`);

    const result = await gateway.openExportFolder(SENDER, { batch_id: ID });

    expect(result).toMatchObject({ ok: false, error: { code: 'OPEN_FOLDER_FAILED' } });
    expect(JSON.stringify(result)).not.toContain(sandbox.replaceAll('\\', '\\\\'));
  });

  it('asks the injected check, not the filesystem, when one is given', async () => {
    const isDirectory = vi.fn<(path: string) => Promise<boolean>>(() => Promise.resolve(true));
    const openPath = vi.fn<OpenPathHost['openPath']>(() => Promise.resolve(''));
    const request = vi.fn<ApiBackend['request']>(() =>
      Promise.resolve({ ok: true, data: batchIn(MISSING) }),
    );
    const gateway = new ApiGateway({
      backend: { request },
      dialog: { showOpenDialog: vi.fn<OpenDialogHost['showOpenDialog']>() },
      shell: { openPath },
      getWindow: () => WINDOW,
      getTrustedPage: () => TRUSTED,
      isDirectory,
    });

    expect(await gateway.openExportFolder(SENDER, { batch_id: ID })).toMatchObject({ ok: true });
    expect(isDirectory).toHaveBeenCalledWith(MISSING);
    expect(openPath).toHaveBeenCalledWith(MISSING);
  });
});

describe('dialog:pick-video and the one-time video token (ADR 024)', () => {
  const TOKEN_PATTERN = /^[0-9a-f]{64}$/;
  const PICKED_OTHER = resolve('clipforge-test', 'videos', 'other.mkv');

  async function pick(h: Harness, path = PICKED_VIDEO): Promise<string> {
    h.showOpenDialog.mockResolvedValueOnce({ canceled: false, filePaths: [path] });
    const choice = await h.gateway.pickVideo(SENDER);
    if (!('token' in choice)) {
      throw new Error('the video was not picked');
    }
    return choice.token;
  }

  const importWith = (h: Harness, token: string, projectId = ID): ReturnType<ApiGateway['importVideo']> =>
    h.gateway.importVideo(SENDER, { project_id: projectId, file_token: token });

  it('opens the same dialog as dialog:import-video and returns only a token and the file name', async () => {
    const h = setup();
    h.showOpenDialog.mockResolvedValueOnce({ canceled: false, filePaths: [PICKED_VIDEO] });
    h.request.mockResolvedValueOnce({ ok: true, data: SOURCE_RAW });

    const choice = await h.gateway.pickVideo(SENDER);
    await h.gateway.importVideo(SENDER, { project_id: ID });

    expect(h.showOpenDialog).toHaveBeenCalledTimes(2);
    const [pickCall, importCall] = h.showOpenDialog.mock.calls;
    expect(pickCall?.[0]).toBe(WINDOW);
    expect(pickCall?.[1]).toEqual(importCall?.[1]);
    expect(pickCall?.[1].properties).toEqual(['openFile']);
    expect(Object.keys(choice).sort()).toEqual(['fileName', 'token']);
    expect(choice).toHaveProperty('fileName', 'color_gop.mp4');
    expect('token' in choice && TOKEN_PATTERN.test(choice.token)).toBe(true);
    expect(JSON.stringify(choice)).not.toContain('clipforge-test');
    expect(h.request).toHaveBeenCalledTimes(1);
  });

  it('returns {canceled:true} and registers nothing when the user cancels', async () => {
    const h = setup();
    h.showOpenDialog.mockResolvedValueOnce({ canceled: true, filePaths: [] });
    h.showOpenDialog.mockResolvedValueOnce({ canceled: false, filePaths: [] });

    expect(await h.gateway.pickVideo(SENDER)).toEqual({ canceled: true });
    expect(await h.gateway.pickVideo(SENDER)).toEqual({ canceled: true });
    expect(h.request).not.toHaveBeenCalled();
  });

  it('registers the picked file by its token, without a second dialog, and answers like the import', async () => {
    const h = setup();
    const token = await pick(h);
    h.request.mockResolvedValueOnce({ ok: true, data: SOURCE_RAW });

    const result = await importWith(h, token);

    expect(h.showOpenDialog).toHaveBeenCalledTimes(1);
    expect(h.request.mock.calls).toEqual([
      ['POST', '/v1/sources', { project_id: ID, path: PICKED_VIDEO }],
    ]);
    expect(result).toMatchObject({
      ok: true,
      data: { id: ID2, project_id: ID, playback: { direct: true } },
    });
    const serialized = JSON.stringify(result);
    expect(serialized).not.toContain('"path"');
    expect(serialized).not.toContain('"probe"');
    expect(serialized).not.toContain('clipforge-test');
  });

  it('works only once', async () => {
    const h = setup();
    const token = await pick(h);

    expect(await importWith(h, token)).toMatchObject({ ok: true });
    expect(await importWith(h, token)).toMatchObject({
      ok: false,
      error: { code: 'UNKNOWN_FILE_TOKEN', retryable: false },
    });
    expect(h.request).toHaveBeenCalledTimes(1);
  });

  it('is valid only for the latest choice: a new pick replaces the previous token', async () => {
    const h = setup();
    const first = await pick(h);
    const second = await pick(h, PICKED_OTHER);

    expect(first).not.toBe(second);
    expect(await importWith(h, first)).toMatchObject({
      ok: false,
      error: { code: 'UNKNOWN_FILE_TOKEN' },
    });
    expect(h.request).not.toHaveBeenCalled();
    // The refused old token did not use up the valid one.
    expect(await importWith(h, second)).toMatchObject({ ok: true });
    expect(h.request.mock.calls).toEqual([
      ['POST', '/v1/sources', { project_id: ID, path: PICKED_OTHER }],
    ]);
  });

  it('is used up even when the backend refuses the file', async () => {
    const h = setup();
    const token = await pick(h);
    h.request.mockResolvedValueOnce({
      ok: false,
      error: { code: 'NOT_A_VIDEO', message: 'Файл не є відео', retryable: false },
    });

    expect(await importWith(h, token)).toMatchObject({ ok: false, error: { code: 'NOT_A_VIDEO' } });
    expect(await importWith(h, token)).toMatchObject({
      ok: false,
      error: { code: 'UNKNOWN_FILE_TOKEN' },
    });
    expect(h.request).toHaveBeenCalledTimes(1);
  });

  it('is used up even when the backend answer is malformed', async () => {
    const h = setup();
    const token = await pick(h);
    h.request.mockResolvedValueOnce({ ok: true, data: { id: ID2 } });

    expect(await importWith(h, token)).toMatchObject({
      ok: false,
      error: { code: 'BAD_BACKEND_RESPONSE' },
    });
    expect(await importWith(h, token)).toMatchObject({
      ok: false,
      error: { code: 'UNKNOWN_FILE_TOKEN' },
    });
    expect(h.request).toHaveBeenCalledTimes(1);
  });

  it('is not used up by a malformed request', async () => {
    const h = setup();
    const token = await pick(h);

    for (const payload of [
      { project_id: 'x', file_token: token },
      { project_id: `${ID}/../x`, file_token: token },
      { file_token: token },
      { project_id: ID, file_token: token, path: PICKED_VIDEO },
      { project_id: ID, file_token: token, filePath: PICKED_VIDEO },
      { project_id: ID, file_token: PICKED_VIDEO },
      { project_id: ID, file_token: token.toUpperCase() },
      { project_id: ID, file_token: token.slice(1) },
      { project_id: ID, file_token: '' },
      { project_id: ID, file_token: 7 },
      { project_id: ID, file_token: null },
    ]) {
      expect(await h.gateway.importVideo(SENDER, payload)).toMatchObject({
        ok: false,
        error: { code: 'INVALID_PARAMS' },
      });
    }
    expect(h.request).not.toHaveBeenCalled();
    expect(h.showOpenDialog).toHaveBeenCalledTimes(1);

    h.request.mockResolvedValueOnce({ ok: true, data: SOURCE_RAW });
    expect(await importWith(h, token)).toMatchObject({ ok: true });
  });

  it('refuses a well-formed token main never issued, without a dialog or a request', async () => {
    const h = setup();
    await pick(h);

    expect(await importWith(h, 'cd'.repeat(32))).toMatchObject({
      ok: false,
      error: { code: 'UNKNOWN_FILE_TOKEN' },
    });
    expect(h.showOpenDialog).toHaveBeenCalledTimes(1);
    expect(h.request).not.toHaveBeenCalled();
  });

  it('is kept apart from the folder token: neither opens the other', async () => {
    const h = setup();
    h.showOpenDialog.mockResolvedValueOnce({ canceled: false, filePaths: [PICKED_FOLDER] });
    const folder = await h.gateway.chooseExportFolder(SENDER);
    const folderToken = 'token' in folder ? folder.token : '';
    const videoToken = await pick(h);

    expect(await importWith(h, folderToken)).toMatchObject({
      ok: false,
      error: { code: 'UNKNOWN_FILE_TOKEN' },
    });
    expect(
      await h.gateway.call(SENDER, {
        op: 'exports.create',
        params: { project_id: ID, clip_ids: [ID2], folderToken: videoToken, profile: PROFILE },
      }),
    ).toMatchObject({ ok: false, error: { code: 'UNKNOWN_FOLDER_TOKEN' } });
    expect(h.request).not.toHaveBeenCalled();
    // Each choice is still there for its own use.
    h.request.mockResolvedValue({ ok: true, data: SOURCE_RAW });
    expect(await importWith(h, videoToken)).toMatchObject({ ok: true });
    h.request.mockResolvedValue({ ok: true, data: CREATED });
    expect(
      await h.gateway.call(SENDER, {
        op: 'exports.create',
        params: { project_id: ID, clip_ids: [ID2], folderToken, profile: PROFILE },
      }),
    ).toMatchObject({ ok: true });
  });

  it('shares the one-dialog rule: a second pick while a dialog is open counts as cancelled', async () => {
    const h = setup();
    const first = deferred<{ canceled: boolean; filePaths: string[] }>();
    h.showOpenDialog.mockReturnValueOnce(first.promise);

    const pending = h.gateway.pickVideo(SENDER);
    await vi.waitFor(() => {
      expect(h.showOpenDialog).toHaveBeenCalledTimes(1);
    });
    expect(await h.gateway.pickVideo(SENDER)).toEqual({ canceled: true });
    first.resolve({ canceled: false, filePaths: [PICKED_VIDEO] });

    expect(await pending).toHaveProperty('token');
    expect(h.showOpenDialog).toHaveBeenCalledTimes(1);
  });
});

describe('drop:video (ADR 024)', () => {
  const UNSUPPORTED = {
    ok: false,
    error: {
      code: 'UNSUPPORTED_FILE',
      message: 'Це не відеофайл. Перетягніть файл MP4, MOV, MKV…',
      retryable: false,
    },
  };
  let sandbox: string;

  beforeEach(() => {
    sandbox = mkdtempSync(join(tmpdir(), 'clipforge-drop-'));
    writeFileSync(join(sandbox, 'clip.mp4'), 'x');
    writeFileSync(join(sandbox, 'UPPER.MKV'), 'x');
    writeFileSync(join(sandbox, 'notes.txt'), 'x');
    writeFileSync(join(sandbox, 'noextension'), 'x');
    mkdirSync(join(sandbox, 'folder.mp4'));
    mkdirSync(join(sandbox, 'plain-folder'));
  });

  afterEach(() => {
    rmSync(sandbox, { recursive: true, force: true });
  });

  const hidesPath = (result: unknown): void => {
    const serialized = JSON.stringify(result);
    expect(serialized).not.toContain(sandbox.replaceAll('\\', '\\\\'));
    expect(serialized).not.toContain(sandbox);
  };

  it('gives a token and the file name for a video file on the disk, without a dialog', async () => {
    const h = setup();

    const choice = await h.gateway.takeDroppedVideo(SENDER, { path: join(sandbox, 'clip.mp4') });

    expect(Object.keys(choice).sort()).toEqual(['fileName', 'token']);
    expect(choice).toHaveProperty('fileName', 'clip.mp4');
    expect('token' in choice && /^[0-9a-f]{64}$/.test(choice.token)).toBe(true);
    hidesPath(choice);
    expect(h.showOpenDialog).not.toHaveBeenCalled();
    expect(h.request).not.toHaveBeenCalled();
  });

  it('registers the dropped file by its token', async () => {
    const h = setup();
    const file = join(sandbox, 'clip.mp4');
    const choice = await h.gateway.takeDroppedVideo(SENDER, { path: file });
    h.request.mockResolvedValueOnce({ ok: true, data: SOURCE_RAW });

    const result = await h.gateway.importVideo(SENDER, {
      project_id: ID,
      file_token: 'token' in choice ? choice.token : '',
    });

    expect(result).toMatchObject({ ok: true, data: { id: ID2 } });
    expect(h.request.mock.calls).toEqual([['POST', '/v1/sources', { project_id: ID, path: file }]]);
    expect(h.showOpenDialog).not.toHaveBeenCalled();
    hidesPath(result);
  });

  it('ignores the case of the extension', async () => {
    const { gateway } = setup();

    expect(await gateway.takeDroppedVideo(SENDER, { path: join(sandbox, 'UPPER.MKV') })).toMatchObject({
      fileName: 'UPPER.MKV',
    });
  });

  it('has one token for picked and dropped videos: the latest choice wins', async () => {
    const h = setup();
    h.showOpenDialog.mockResolvedValueOnce({ canceled: false, filePaths: [PICKED_VIDEO] });
    const picked = await h.gateway.pickVideo(SENDER);
    const dropped = await h.gateway.takeDroppedVideo(SENDER, { path: join(sandbox, 'clip.mp4') });
    const pickedToken = 'token' in picked ? picked.token : '';
    const droppedToken = 'token' in dropped ? dropped.token : '';

    expect(await h.gateway.importVideo(SENDER, { project_id: ID, file_token: pickedToken })).toMatchObject({
      ok: false,
      error: { code: 'UNKNOWN_FILE_TOKEN' },
    });
    h.request.mockResolvedValueOnce({ ok: true, data: SOURCE_RAW });
    expect(await h.gateway.importVideo(SENDER, { project_id: ID, file_token: droppedToken })).toMatchObject({
      ok: true,
    });
    expect(h.request).toHaveBeenCalledTimes(1);
  });

  it.each([
    ['a relative path', 'clip.mp4'],
    ['a relative path with a parent', '..\\clip.mp4'],
    ['a dot-relative path', './clip.mp4'],
    ['an empty path', ''],
    ['a number', 5],
    ['null', null],
    ['a list', ['x.mp4']],
    ['an object', { toString: () => 'x.mp4' }],
  ])('refuses %s', async (_name, path) => {
    const h = setup();

    const result = await h.gateway.takeDroppedVideo(SENDER, { path });

    expect(result).toEqual(UNSUPPORTED);
    expect(h.request).not.toHaveBeenCalled();
  });

  it('refuses a path with a NUL character, even one that ends like a video', async () => {
    const { gateway } = setup();

    for (const path of [
      `${join(sandbox, 'cl')}\0ip.mp4`,
      `${join(sandbox, 'clip.mp4')}\0`,
      `\0${join(sandbox, 'clip.mp4')}`,
    ]) {
      const result = await gateway.takeDroppedVideo(SENDER, { path });

      expect(result).toEqual(UNSUPPORTED);
      hidesPath(result);
    }
  });

  it('refuses a folder, whatever it is called', async () => {
    const { gateway } = setup();

    for (const folder of ['folder.mp4', 'plain-folder']) {
      const result = await gateway.takeDroppedVideo(SENDER, { path: join(sandbox, folder) });

      expect(result).toEqual(UNSUPPORTED);
      hidesPath(result);
    }
  });

  it('refuses a file that is not of a video type', async () => {
    const { gateway } = setup();

    for (const name of ['notes.txt', 'noextension', 'clip.mp4.txt', 'clip.mp4 ', '.mp4', 'clip.mp4:stream']) {
      const result = await gateway.takeDroppedVideo(SENDER, { path: join(sandbox, name) });

      expect(result, name).toEqual(UNSUPPORTED);
      hidesPath(result);
    }
  });

  it('says that a video file that does not exist is not found, without its path', async () => {
    const { gateway } = setup();

    const result = await gateway.takeDroppedVideo(SENDER, { path: join(sandbox, 'missing.mp4') });

    expect(result).toMatchObject({
      ok: false,
      error: { code: 'FILE_NOT_FOUND', retryable: false },
    });
    expect(result).toEqual({
      ok: false,
      error: {
        code: 'FILE_NOT_FOUND',
        message: 'Файл не знайдено або він недоступний. Перетягніть його ще раз',
        retryable: false,
      },
    });
    hidesPath(result);
  });

  it('issues no token for a refused drop', async () => {
    const h = setup();

    for (const path of [join(sandbox, 'notes.txt'), join(sandbox, 'missing.mp4'), 'clip.mp4']) {
      expect(await h.gateway.takeDroppedVideo(SENDER, { path })).not.toHaveProperty('token');
    }
    expect(await h.gateway.importVideo(SENDER, { project_id: ID, file_token: 'ab'.repeat(32) })).toMatchObject({
      ok: false,
      error: { code: 'UNKNOWN_FILE_TOKEN' },
    });
    expect(h.request).not.toHaveBeenCalled();
  });

  it('refuses a payload that is not exactly { path }', async () => {
    const h = setup();
    const file = join(sandbox, 'clip.mp4');

    for (const payload of [
      {},
      { path: undefined },
      { path: file, extra: true },
      { path: file, project_id: ID },
      { file },
      file,
      null,
      undefined,
      [file],
    ]) {
      expect(await h.gateway.takeDroppedVideo(SENDER, payload)).toMatchObject({
        ok: false,
        error: { code: 'INVALID_PARAMS' },
      });
    }
    expect(h.request).not.toHaveBeenCalled();
  });
});

describe('system.checks (ADR 024)', () => {
  it('reads GET /v1/system/checks with no body and answers the three checks', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({ ok: true, data: CHECKS });

    const result = await gateway.call(SENDER, { op: 'system.checks', params: {} });

    expect(request.mock.calls).toEqual([['GET', '/v1/system/checks', undefined]]);
    expect(result).toEqual({ ok: true, data: CHECKS });
  });

  it('accepts the call without params, like every op', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({ ok: true, data: CHECKS });

    expect(await gateway.call(SENDER, { op: 'system.checks' })).toEqual({ ok: true, data: CHECKS });
  });

  it('keeps the unknown version and free space as null and drops every other key', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({
      ok: true,
      data: {
        ffmpeg: { ok: false, version: null, path: 'C:\\ffmpeg\\ffmpeg.exe' },
        ffprobe: { ok: false, version: null },
        data_dir: { ok: false, free_bytes: null, path: 'C:\\Data' },
        path: 'C:\\Data',
      },
    });

    const result = await gateway.call(SENDER, { op: 'system.checks', params: {} });

    expect(result).toEqual({
      ok: true,
      data: {
        ffmpeg: { ok: false, version: null },
        ffprobe: { ok: false, version: null },
        data_dir: { ok: false, free_bytes: null },
      },
    });
    expect(JSON.stringify(result)).not.toContain('C:');
  });

  it.each([
    ['an extra key', { extra: 1 }],
    ['a path', { path: 'C:\\Windows' }],
    ['an output_dir', { output_dir: 'C:\\Windows' }],
    ['a list', []],
    ['a string', 'checks'],
    ['a number', 5],
  ])('refuses params with %s without calling the sidecar', async (_name, params) => {
    const { gateway, request } = setup();

    expect(await gateway.call(SENDER, { op: 'system.checks', params })).toMatchObject({
      ok: false,
      error: { code: 'INVALID_PARAMS' },
    });
    expect(request).not.toHaveBeenCalled();
  });

  it.each([
    ['no data_dir', { ffmpeg: CHECKS.ffmpeg, ffprobe: CHECKS.ffprobe }],
    ['a tool whose ok is not a boolean', { ...CHECKS, ffmpeg: { ok: 'yes', version: '8.0' } }],
    ['a version that is a number', { ...CHECKS, ffprobe: { ok: true, version: 8 } }],
    ['a negative free space', { ...CHECKS, data_dir: { ok: true, free_bytes: -1 } }],
    ['a free space that is a fraction', { ...CHECKS, data_dir: { ok: true, free_bytes: 1.5 } }],
    ['a list', []],
  ])('turns a backend answer with %s into an error', async (_name, data) => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({ ok: true, data });

    expect(await gateway.call(SENDER, { op: 'system.checks', params: {} })).toMatchObject({
      ok: false,
      error: { code: 'BAD_BACKEND_RESPONSE' },
    });
  });

  it('forwards an error of the backend', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({
      ok: false,
      error: { code: 'BACKEND_UNAVAILABLE', message: 'Локальний рушій недоступний', retryable: true },
    });

    expect(await gateway.call(SENDER, { op: 'system.checks', params: {} })).toEqual({
      ok: false,
      error: { code: 'BACKEND_UNAVAILABLE', message: 'Локальний рушій недоступний', retryable: true },
    });
  });

  it('is refused for a foreign sender', async () => {
    const { gateway, request } = setup();

    await expect(
      gateway.call({ ...SENDER, webContentsId: 8 }, { op: 'system.checks', params: {} }),
    ).rejects.toThrow('Недозволений відправник IPC');
    expect(request).not.toHaveBeenCalled();
  });
});

const CLIP = {
  id: ID,
  source_id: ID2,
  analysis_run_id: null,
  revision: 1,
  title: 'Мій фрагмент',
  start_ms: 4120,
  end_ms: 7880,
  recommended_start_ms: null,
  recommended_end_ms: null,
  categories: ['funny'],
  evidence_type: 'manual',
  evidence_ids: [],
  reason: null,
  scores: null,
  rank: null,
  visual_review_required: false,
  review_status: 'suggested',
  export_state: 'none',
  created_at: '2026-11-14T10:00:00Z',
  updated_at: '2026-11-14T10:00:00Z',
};
const EXPORT_BATCH = {
  id: ID,
  project_id: ID2,
  job_id: ID,
  status: 'succeeded',
  output_dir: PICKED_FOLDER,
  profile: PROFILE,
  items: [
    {
      id: ID,
      clip_id: ID2,
      clip_revision: 1,
      state: 'verified',
      final_path: resolve(PICKED_FOLDER, 'a.mp4'),
      observed: {},
      error: null,
    },
  ],
  manifest_path: null,
  catalog_path: null,
  created_at: '2026-11-14T10:00:00Z',
};
const FAILED_BATCH = {
  ...EXPORT_BATCH,
  status: 'partial_failed',
  items: [
    {
      ...EXPORT_BATCH.items[0],
      state: 'failed',
      final_path: null,
      observed: null,
      error: { code: 'OUTPUT_INVALID', message: 'Створений кліп не пройшов перевірку' },
    },
  ],
};
const JOB = { id: ID, type: 'export', state: 'running', progress: { done: 1 }, error: null };
