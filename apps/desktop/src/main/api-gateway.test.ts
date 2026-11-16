// @vitest-environment node
import { mkdirSync, mkdtempSync, rmSync, truncateSync, writeFileSync } from 'node:fs';
import type { Stats } from 'node:fs';
import { open, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

import type * as fsPromises from 'node:fs/promises';
import type { BrowserWindow, IpcMainInvokeEvent } from 'electron';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { API_OPS, parsePage, parseSource } from '../shared/api';
import type { ApiOp } from '../shared/api';
import { ApiGateway, registerApiIpc, toIpcSender } from './api-gateway';
import type { ApiBackend, IpcRegistry, OpenDialogHost, OpenPathHost } from './api-gateway';
import type { IpcSender, TrustedPage } from './ipc-guard';
import { jsonResponse, startReadySidecar, stopAllSidecars, PORT } from './sidecar-test-harness';

// The real module, with `open` and `stat` as spies: the subtitle tests see whether a file was
// opened at all, and can hand back a file handle or a `stat` answer of their own.
vi.mock('node:fs/promises', async (importOriginal) => {
  const actual = await importOriginal<typeof fsPromises>();
  return { ...actual, open: vi.fn(actual.open), stat: vi.fn(actual.stat) };
});

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
/** What GET /v1/transcripts answers per revision (ADR 039): `metadata` is the backend's own dump. */
const TRANSCRIPT = {
  id: ID,
  source_id: ID2,
  revision: 2,
  origin: 'correction',
  run_id: null,
  parent_id: ID2,
  timing_level: 'word',
  language: 'uk',
  duration_ms: 125_000,
  word_count: 2,
  segment_count: 1,
  coverage: [{ start_ms: 0, end_ms: 125_000, state: 'succeeded' }],
  metadata: { provider: 'openrouter', options_hash: 'x' },
  created_at: '2026-11-16T10:00:00Z',
};
/** The same revision as main answers it: `metadata` is gone. */
const TRANSCRIPT_DTO = {
  id: ID,
  source_id: ID2,
  revision: 2,
  origin: 'correction',
  run_id: null,
  parent_id: ID2,
  timing_level: 'word',
  language: 'uk',
  duration_ms: 125_000,
  word_count: 2,
  segment_count: 1,
  coverage: [{ start_ms: 0, end_ms: 125_000, state: 'succeeded' }],
  created_at: '2026-11-16T10:00:00Z',
};
const TRANSCRIPT_WORDS = [
  {
    id: 'w1',
    ordinal: 0,
    start_ms: 0,
    end_ms: 400,
    text: 'Привіт',
    chunk_id: ID,
    speaker_local: null,
    confidence: 0.91,
  },
  {
    id: 'w2',
    ordinal: 1,
    start_ms: 400,
    end_ms: 900,
    text: 'світ',
    chunk_id: ID,
    speaker_local: null,
    confidence: null,
  },
];
const TRANSCRIPT_SEGMENT = {
  id: 's1',
  ordinal: 0,
  start_ms: 0,
  end_ms: 900,
  text: 'Привіт світ',
  word_ids: ['w1', 'w2'],
  timing_level: 'word',
};
/** What GET /v1/transcripts/{id} answers: the revision, a window, and `metadata` again. */
const TRANSCRIPT_WINDOW = {
  ...TRANSCRIPT,
  offset: 0,
  limit: 200,
  next_offset: null,
  segments: [TRANSCRIPT_SEGMENT],
  words: TRANSCRIPT_WORDS,
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
const SETTINGS = {
  stt_model: 'microsoft/mai-transcribe-2',
  stt_language: null,
  analysis_model: null,
  run_budget_usd: '2.50',
  has_api_key: true,
};
/** What POST /v1/transcription-runs/estimate answers (ADR 037). */
const ESTIMATE = {
  source_id: ID,
  audio_stream_index: 1,
  model: 'microsoft/mai-transcribe-2',
  language: null,
  duration_ms: 125_000,
  chunk_count: 1,
  billed_seconds: 125,
  usd_per_second_upper: '0.0001',
  estimate_micro_usd: 12_500,
  price_basis: 'upper_bound_per_second',
  budget_usd: '2.50',
  within_budget: true,
  existing: null,
};
const TRANSCRIPTION_ITEM = {
  id: ID,
  source_id: ID2,
  job_id: ID,
  model: 'microsoft/mai-transcribe-2',
  status: 'partial',
  revision_id: null,
  error_code: 'TRANSCRIPTION_PARTIAL',
  committed_micro_usd: 12_500,
  created_at: '2026-11-16T10:00:00Z',
};
/** What GET /v1/transcription-runs/{id} answers: the item without `created_at`, plus the parts. */
const TRANSCRIPTION_RUN = {
  id: ID,
  source_id: ID2,
  job_id: ID,
  model: 'microsoft/mai-transcribe-2',
  status: 'partial',
  revision_id: null,
  error_code: 'TRANSCRIPTION_PARTIAL',
  committed_micro_usd: 12_500,
  chunks: [
    {
      id: ID2,
      core_start_ms: 0,
      core_end_ms: 125_000,
      state: 'unknown_outcome',
      attempts: 1,
      error_code: 'STT_UNKNOWN_OUTCOME',
    },
  ],
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
      'settings.delete',
      'settings.credentials',
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
      'jobs.resume': JOB,
      'previews.create': CREATED,
      'previews.get': PREVIEW,
      'system.checks': { ...CHECKS, extra: 'x' },
      'settings.get': SETTINGS,
      'settings.update': SETTINGS,
      'transcription.estimate': ESTIMATE,
      'transcription.create': CREATED,
      'transcription.get': TRANSCRIPTION_RUN,
      'transcription.list': { items: [TRANSCRIPTION_ITEM] },
      'transcripts.list': { items: [TRANSCRIPT], next_cursor: null },
      'transcripts.get': TRANSCRIPT_WINDOW,
      'transcripts.correct': TRANSCRIPT,
      'credentials.check': { state: 'valid', is_free_tier: false, limit_remaining_usd: '4.25' },
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
      'jobs.resume': { id: ID },
      'previews.create': { source_id: ID, audio_stream_index: 1 },
      'previews.get': { id: ID },
      'system.checks': {},
      'settings.get': {},
      'settings.update': { run_budget_usd: '2.50' },
      'transcription.estimate': { source_id: ID, audio_stream_index: 1 },
      'transcription.create': { source_id: ID, audio_stream_index: 1 },
      'transcription.get': { id: ID },
      'transcription.list': { source_id: ID },
      'transcripts.list': { source_id: ID2 },
      'transcripts.get': { id: ID, offset: 0, limit: 200 },
      'transcripts.correct': { id: ID, edits: [{ word_id: 'w1', text: 'Привіт' }] },
      'credentials.check': {},
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
      `POST /v1/jobs/${ID}/resume`,
      `POST /v1/sources/${ID}/previews`,
      `GET /v1/previews/${ID}`,
      'GET /v1/system/checks',
      'GET /v1/settings',
      'PATCH /v1/settings',
      'POST /v1/transcription-runs/estimate',
      'POST /v1/transcription-runs',
      `GET /v1/transcription-runs/${ID}`,
      `GET /v1/transcription-runs?source_id=${ID}`,
      `GET /v1/transcripts?source_id=${ID2}`,
      `GET /v1/transcripts/${ID}?offset=0&limit=200`,
      `POST /v1/transcripts/${ID}/corrections`,
      'POST /v1/runtime/credentials/check',
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
    ['jobs.resume', {}],
    ['previews.get', {}],
    ['transcription.get', {}],
    ['transcripts.get', {}],
    ['transcripts.correct', { edits: [{ word_id: 'w1', text: 'a' }] }],
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
    await expect(gateway.importTranscript(sender, { source_id: ID2 })).rejects.toThrow(
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
    await expect(gateway.importTranscript(SENDER, { source_id: ID2 })).rejects.toThrow();
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
    await expect(gateway.importTranscript(sender, 'garbage')).rejects.toThrow(
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

  it('registers exactly the seven gateway channels', () => {
    expect([...register().handlers.keys()].sort()).toEqual([
      'api:call',
      'dialog:import-transcript',
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
      ['dialog:import-transcript', { source_id: ID2 }],
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

  it('routes dialog:import-transcript to the gateway, which checks the sender itself', async () => {
    const { handlers, request } = register();

    // The test dialog picks a .mp4: the gateway answers the format before any file is read.
    expect(await handlers.get('dialog:import-transcript')?.(event(7), { source_id: ID2 })).toMatchObject({
      ok: false,
      error: { code: 'TRANSCRIPT_FILE_UNSUPPORTED' },
    });
    await expect(
      Promise.resolve().then(() => handlers.get('dialog:import-transcript')?.(event(99), { source_id: ID2 })),
    ).rejects.toThrow('Недозволений відправник IPC');
    expect(request).not.toHaveBeenCalled();
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

describe('dialog:import-transcript (ADR 039)', () => {
  const MAX_SUBTITLE_BYTES = 20 * 1024 * 1024;
  const UNREADABLE = {
    ok: false,
    error: {
      code: 'TRANSCRIPT_FILE_UNREADABLE',
      message: 'Не вдалося прочитати файл субтитрів',
      retryable: false,
    },
  };
  let sandbox: string;

  beforeEach(() => {
    sandbox = mkdtempSync(join(tmpdir(), 'clipforge-subtitles-'));
    vi.mocked(open).mockClear();
    vi.mocked(stat).mockClear();
  });

  afterEach(() => {
    rmSync(sandbox, { recursive: true, force: true });
  });

  /** Writes a file of the sandbox and makes the next dialog pick it; returns its path. */
  function pick(h: Harness, name: string, content: string | Buffer = 'x'): string {
    const file = join(sandbox, name);
    writeFileSync(file, content);
    h.showOpenDialog.mockResolvedValueOnce({ canceled: false, filePaths: [file] });
    return file;
  }

  const hidesPath = (result: unknown, file: string): void => {
    const serialized = JSON.stringify(result);
    expect(serialized).not.toContain(file.replaceAll('\\', '\\\\'));
    expect(serialized).not.toContain(sandbox.replaceAll('\\', '\\\\'));
  };

  it('opens the subtitle picker, then posts the text with the source and the offset', async () => {
    const h = setup();
    const text = '1\n00:00:00,000 --> 00:00:01,000\nПривіт\n';
    pick(h, 'lines.srt', text);
    h.request.mockResolvedValueOnce({ ok: true, data: TRANSCRIPT });

    const result = await h.gateway.importTranscript(SENDER, { source_id: ID2, offset_ms: 1500 });

    expect(h.showOpenDialog).toHaveBeenCalledTimes(1);
    const [window, options] = h.showOpenDialog.mock.calls[0] ?? [];
    expect(window).toBe(WINDOW);
    expect(options).toEqual({
      title: 'Імпорт субтитрів',
      properties: ['openFile'],
      filters: [{ name: 'Субтитри', extensions: ['srt', 'vtt'] }],
    });
    expect(h.request.mock.calls).toEqual([
      [
        'POST',
        '/v1/transcripts/import',
        { source_id: ID2, format: 'srt', text, offset_ms: 1500 },
      ],
    ]);
    expect(result).toEqual({ ok: true, data: TRANSCRIPT_DTO });
  });

  it('takes the format from the extension in any letter case, and offset 0 when none is given', async () => {
    const h = setup();
    h.request.mockResolvedValue({ ok: true, data: TRANSCRIPT });

    for (const [name, format] of [
      ['one.SRT', 'srt'],
      ['two.Vtt', 'vtt'],
      ['three.VTT', 'vtt'],
      ['four.srt', 'srt'],
    ] as const) {
      pick(h, name, 'x');
      await h.gateway.importTranscript(SENDER, { source_id: ID2 });
      expect(h.request).toHaveBeenLastCalledWith('POST', '/v1/transcripts/import', {
        source_id: ID2,
        format,
        text: 'x',
        offset_ms: 0,
      });
    }
  });

  it('answers {canceled:true} and reads nothing when the user cancels', async () => {
    const h = setup();
    h.showOpenDialog.mockResolvedValueOnce({ canceled: true, filePaths: [] });

    expect(await h.gateway.importTranscript(SENDER, { source_id: ID2 })).toEqual({ canceled: true });
    expect(open).not.toHaveBeenCalled();
    expect(h.request).not.toHaveBeenCalled();
  });

  it('refuses a file over 20 MB by its size, before any of it is read', async () => {
    const h = setup();
    const file = join(sandbox, 'long.srt');
    writeFileSync(file, '');
    truncateSync(file, MAX_SUBTITLE_BYTES + 1);
    h.showOpenDialog.mockResolvedValueOnce({ canceled: false, filePaths: [file] });

    const result = await h.gateway.importTranscript(SENDER, { source_id: ID2 });

    expect(result).toEqual({
      ok: false,
      error: {
        code: 'TRANSCRIPT_FILE_TOO_LARGE',
        message: 'Файл субтитрів більший за 20 МБ',
        retryable: false,
      },
    });
    expect(open).not.toHaveBeenCalled();
    expect(h.request).not.toHaveBeenCalled();
    hidesPath(result, file);
  });

  it('refuses a file that grows past 20 MB after stat, and reads no byte past the limit', async () => {
    const h = setup();
    const file = pick(h, 'grows.srt', 'x');
    // `stat` saw a small file; the handle then holds more than the limit.
    vi.mocked(stat).mockResolvedValueOnce({ isFile: () => true, size: 1024 } as unknown as Stats);
    const close = vi.fn().mockResolvedValue(undefined);
    const read = vi.fn((buffer: Buffer, offset: number, length: number) => {
      buffer.fill(0x20, offset, offset + length);
      return Promise.resolve({ bytesRead: length, buffer });
    });
    vi.mocked(open).mockResolvedValueOnce({ read, close } as unknown as fsPromises.FileHandle);

    const result = await h.gateway.importTranscript(SENDER, { source_id: ID2 });

    expect(result).toEqual({
      ok: false,
      error: {
        code: 'TRANSCRIPT_FILE_TOO_LARGE',
        message: 'Файл субтитрів більший за 20 МБ',
        retryable: false,
      },
    });
    expect(read).toHaveBeenCalledTimes(1);
    expect(read).toHaveBeenCalledWith(expect.any(Buffer), 0, MAX_SUBTITLE_BYTES + 1, 0);
    expect(close).toHaveBeenCalledTimes(1);
    expect(h.request).not.toHaveBeenCalled();
    hidesPath(result, file);
  });

  it('reads a file of exactly 20 MB, then refuses its text as over the character limit', async () => {
    const h = setup();
    pick(h, 'exact.srt', Buffer.alloc(MAX_SUBTITLE_BYTES, 0x20));

    // The byte limit is inclusive, so the file is read. Its 20 MB of text has over 5 000 000
    // characters, so the text limit refuses it and nothing is posted.
    expect(await h.gateway.importTranscript(SENDER, { source_id: ID2 })).toEqual({
      ok: false,
      error: {
        code: 'TRANSCRIPT_FILE_TOO_LARGE',
        message: 'Файл субтитрів містить понад 5 000 000 символів',
        retryable: false,
      },
    });
    expect(open).toHaveBeenCalledTimes(1);
    expect(h.request).not.toHaveBeenCalled();
  });

  it.each([
    ['5 000 001 characters of one UTF-16 unit each', 'x'.repeat(5_000_001)],
    ['5 000 001 characters of two UTF-16 units each', '😀'.repeat(5_000_001)],
  ])('refuses a text of over 5 000 000 code points (%s), and posts nothing', async (_name, text) => {
    const h = setup();
    const file = pick(h, 'long.srt', text);

    const result = await h.gateway.importTranscript(SENDER, { source_id: ID2 });

    expect(result).toEqual({
      ok: false,
      error: {
        code: 'TRANSCRIPT_FILE_TOO_LARGE',
        message: 'Файл субтитрів містить понад 5 000 000 символів',
        retryable: false,
      },
    });
    expect(h.request).not.toHaveBeenCalled();
    hidesPath(result, file);
  });

  it('accepts exactly 5 000 000 code points, a two-unit character counting as one', async () => {
    const h = setup();
    const text = '😀'.repeat(5_000_000);
    pick(h, 'limit.srt', text);
    h.request.mockResolvedValueOnce({ ok: true, data: TRANSCRIPT });

    expect(await h.gateway.importTranscript(SENDER, { source_id: ID2 })).toEqual({
      ok: true,
      data: TRANSCRIPT_DTO,
    });
    expect(h.request).toHaveBeenCalledWith('POST', '/v1/transcripts/import', {
      source_id: ID2,
      format: 'srt',
      text,
      offset_ms: 0,
    });
  });

  it('strips the byte order mark before the limit is counted, so a mark and 5 000 000 characters pass', async () => {
    const h = setup();
    const text = 'x'.repeat(5_000_000);
    pick(h, 'mark-limit.srt', Buffer.concat([Buffer.from([0xef, 0xbb, 0xbf]), Buffer.from(text)]));
    h.request.mockResolvedValueOnce({ ok: true, data: TRANSCRIPT });

    await h.gateway.importTranscript(SENDER, { source_id: ID2 });

    expect(h.request).toHaveBeenCalledWith('POST', '/v1/transcripts/import', {
      source_id: ID2,
      format: 'srt',
      text,
      offset_ms: 0,
    });
  });

  it.each([
    ['Windows-1251', Buffer.from([0x31, 0x0a, 0xcf, 0xf0, 0xe8, 0xe2, 0xb3, 0xf2])],
    ['UTF-16 with a byte order mark', Buffer.from('\ufeff1\n', 'utf16le')],
  ])('refuses a %s file without its path, and posts nothing', async (_name, bytes) => {
    const h = setup();
    const file = pick(h, 'bad.srt', bytes);

    const result = await h.gateway.importTranscript(SENDER, { source_id: ID2 });

    expect(result).toEqual({
      ok: false,
      error: {
        code: 'TRANSCRIPT_FILE_ENCODING',
        message: 'Файл субтитрів не в кодуванні UTF-8',
        retryable: false,
      },
    });
    expect(h.request).not.toHaveBeenCalled();
    hidesPath(result, file);
  });

  it('strips the byte order mark of a UTF-8 file before the text is posted', async () => {
    const h = setup();
    const text = 'WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello\n';
    pick(h, 'bom.vtt', Buffer.concat([Buffer.from([0xef, 0xbb, 0xbf]), Buffer.from(text, 'utf8')]));
    h.request.mockResolvedValueOnce({ ok: true, data: TRANSCRIPT });

    await h.gateway.importTranscript(SENDER, { source_id: ID2 });

    expect(h.request).toHaveBeenCalledWith('POST', '/v1/transcripts/import', {
      source_id: ID2,
      format: 'vtt',
      text,
      offset_ms: 0,
    });
  });

  it('strips only the first byte order mark: a file that starts with two posts the second', async () => {
    const h = setup();
    const text = 'WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello\n';
    const mark = Buffer.from([0xef, 0xbb, 0xbf]);
    pick(h, 'two-marks.vtt', Buffer.concat([mark, mark, Buffer.from(text, 'utf8')]));
    h.request.mockResolvedValueOnce({ ok: true, data: TRANSCRIPT });

    await h.gateway.importTranscript(SENDER, { source_id: ID2 });

    expect(h.request).toHaveBeenCalledWith('POST', '/v1/transcripts/import', {
      source_id: ID2,
      format: 'vtt',
      text: `\uFEFF${text}`,
      offset_ms: 0,
    });
  });

  it('closes the file handle after a successful read, and posts the text it read', async () => {
    const h = setup();
    const text = '1\n00:00:00,000 --> 00:00:01,000\nДескриптор\n';
    const bytes = Buffer.from(text, 'utf8');
    pick(h, 'handle.srt', bytes);
    // The handle serves the bytes from the position asked for, and nothing past their end.
    const read = vi.fn((buffer: Buffer, offset: number, length: number, position: number) => {
      const chunk = bytes.subarray(position, position + length);
      chunk.copy(buffer, offset);
      return Promise.resolve({ bytesRead: chunk.length, buffer });
    });
    const close = vi.fn().mockResolvedValue(undefined);
    vi.mocked(open).mockResolvedValueOnce({ read, close } as unknown as fsPromises.FileHandle);
    h.request.mockResolvedValueOnce({ ok: true, data: TRANSCRIPT });

    await h.gateway.importTranscript(SENDER, { source_id: ID2 });

    expect(close).toHaveBeenCalledTimes(1);
    expect(h.request).toHaveBeenCalledWith('POST', '/v1/transcripts/import', {
      source_id: ID2,
      format: 'srt',
      text,
      offset_ms: 0,
    });
  });

  it('posts a UTF-8 file without a byte order mark as it is', async () => {
    const h = setup();
    const text = '1\n00:00:00,000 --> 00:00:01,000\nБез мітки\n';
    pick(h, 'plain.srt', text);
    h.request.mockResolvedValueOnce({ ok: true, data: TRANSCRIPT });

    await h.gateway.importTranscript(SENDER, { source_id: ID2 });

    expect(h.request).toHaveBeenCalledWith(
      'POST',
      '/v1/transcripts/import',
      expect.objectContaining({ text }),
    );
  });

  it('answers a file that is a folder or is gone with the fixed unreadable error', async () => {
    const h = setup();
    mkdirSync(join(sandbox, 'folder.srt'));
    const folder = join(sandbox, 'folder.srt');
    const gone = join(sandbox, 'gone.srt');
    h.showOpenDialog.mockResolvedValueOnce({ canceled: false, filePaths: [folder] });
    h.showOpenDialog.mockResolvedValueOnce({ canceled: false, filePaths: [gone] });

    const folderResult = await h.gateway.importTranscript(SENDER, { source_id: ID2 });
    const goneResult = await h.gateway.importTranscript(SENDER, { source_id: ID2 });

    expect(folderResult).toEqual(UNREADABLE);
    expect(goneResult).toEqual(UNREADABLE);
    hidesPath(folderResult, folder);
    hidesPath(goneResult, gone);
    expect(h.request).not.toHaveBeenCalled();
  });

  it('does not pass on an open or read error, whose system message names the path', async () => {
    const h = setup();
    const file = pick(h, 'locked.srt', 'x');
    vi.mocked(open).mockRejectedValueOnce(new Error(`EACCES: permission denied, open '${file}'`));

    const refused = await h.gateway.importTranscript(SENDER, { source_id: ID2 });

    expect(refused).toEqual(UNREADABLE);
    hidesPath(refused, file);

    // A read that fails: the message is dropped, and the handle is closed all the same.
    const close = vi.fn().mockResolvedValue(undefined);
    const read = vi.fn().mockRejectedValue(new Error(`EIO: i/o error, read '${file}'`));
    h.showOpenDialog.mockResolvedValueOnce({ canceled: false, filePaths: [file] });
    vi.mocked(open).mockResolvedValueOnce({ read, close } as unknown as fsPromises.FileHandle);

    const result = await h.gateway.importTranscript(SENDER, { source_id: ID2 });

    expect(result).toEqual(UNREADABLE);
    hidesPath(result, file);
    expect(close).toHaveBeenCalledTimes(1);
    expect(h.request).not.toHaveBeenCalled();
  });

  it.each(['notes.txt', 'movie.mp4', 'subs.srt.bak', 'noextension', '.srt'])(
    'refuses %s by its extension, without reading it',
    async (name) => {
      const h = setup();
      pick(h, name, 'x');

      expect(await h.gateway.importTranscript(SENDER, { source_id: ID2 })).toEqual({
        ok: false,
        error: {
          code: 'TRANSCRIPT_FILE_UNSUPPORTED',
          message: 'Оберіть файл субтитрів у форматі SRT або VTT',
          retryable: false,
        },
      });
      expect(open).not.toHaveBeenCalled();
      expect(h.request).not.toHaveBeenCalled();
    },
  );

  it('refuses bad params before the dialog opens', async () => {
    const h = setup();

    for (const payload of [
      { source_id: '' },
      { source_id: 'x'.repeat(201) },
      { source_id: 'x'.repeat(200) },
      { source_id: 'not-a-uuid' },
      { source_id: 7 },
      // Not uuid4: a version digit other than 4, and upper case.
      { source_id: '0a9d7e6c-1b2f-1c3d-8e4f-5a6b7c8d9e0f' },
      { source_id: ID2.toUpperCase() },
      { source_id: ID2, offset_ms: 2 ** 31 },
      { source_id: ID2, offset_ms: -(2 ** 31) },
      { source_id: ID2, offset_ms: 1.5 },
      { source_id: ID2, offset_ms: null },
      { source_id: ID2, offset_ms: '1000' },
      { source_id: ID2, path: 'C:\\x.srt' },
      { source_id: ID2, text: '1\n' },
      { source_id: ID2, format: 'srt' },
      { offset_ms: 0 },
      {},
      'C:\\x.srt',
      undefined,
    ]) {
      expect(await h.gateway.importTranscript(SENDER, payload)).toMatchObject({
        ok: false,
        error: { code: 'INVALID_PARAMS' },
      });
    }
    expect(h.showOpenDialog).not.toHaveBeenCalled();
    expect(h.request).not.toHaveBeenCalled();
  });

  it('accepts a uuid4 source ID and offsets of plus or minus 2^31-1', async () => {
    const h = setup();
    h.showOpenDialog.mockResolvedValue({ canceled: true, filePaths: [] });

    for (const payload of [
      { source_id: ID2 },
      { source_id: ID2, offset_ms: 2 ** 31 - 1 },
      { source_id: ID2, offset_ms: -(2 ** 31 - 1) },
    ]) {
      expect(await h.gateway.importTranscript(SENDER, payload)).toEqual({ canceled: true });
    }
    expect(h.showOpenDialog).toHaveBeenCalledTimes(3);
  });

  it('keeps the path and the text of the file out of a successful answer', async () => {
    const h = setup();
    const file = pick(h, 'secret-lines.srt', '1\n00:00:00,000 --> 00:00:01,000\nТаємний текст\n');
    h.request.mockResolvedValueOnce({
      ok: true,
      data: { ...TRANSCRIPT, path: file, text: 'Таємний текст' },
    });

    const result = await h.gateway.importTranscript(SENDER, { source_id: ID2 });

    expect(result).toEqual({ ok: true, data: TRANSCRIPT_DTO });
    const serialized = JSON.stringify(result);
    expect(serialized).not.toContain('Таємний');
    expect(serialized).not.toContain('secret-lines');
    hidesPath(result, file);
    expect(serialized).not.toContain('"metadata"');
  });

  it('forwards a backend refusal, and answers a reply of another shape as a bad backend response', async () => {
    const h = setup();
    pick(h, 'refused.srt', 'x');
    h.request.mockResolvedValueOnce({
      ok: false,
      error: { code: 'SOURCE_NOT_FOUND', message: 'Джерело не знайдено', retryable: false },
    });
    expect(await h.gateway.importTranscript(SENDER, { source_id: ID2 })).toEqual({
      ok: false,
      error: { code: 'SOURCE_NOT_FOUND', message: 'Джерело не знайдено', retryable: false },
    });

    pick(h, 'odd.srt', 'x');
    h.request.mockResolvedValueOnce({ ok: true, data: { id: ID } });
    expect(await h.gateway.importTranscript(SENDER, { source_id: ID2 })).toEqual({
      ok: false,
      error: {
        code: 'BAD_BACKEND_RESPONSE',
        message: 'Локальний рушій повернув неочікувану відповідь',
        retryable: false,
      },
    });
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

describe('settings.get and settings.update (ADR 033)', () => {
  it('settings.get reads GET /v1/settings and answers exactly the five fields', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({ ok: true, data: { ...SETTINGS, openrouter_api_key: 'x'.repeat(30) } });

    const result = await gateway.call(SENDER, { op: 'settings.get', params: {} });

    expect(request.mock.calls).toEqual([['GET', '/v1/settings', undefined]]);
    expect(result).toEqual({ ok: true, data: SETTINGS });
    expect(JSON.stringify(result)).not.toContain('xxxxxxxx');
  });

  it('settings.update patches only the keys that were given, null included', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValue({ ok: true, data: SETTINGS });

    await gateway.call(SENDER, { op: 'settings.update', params: { run_budget_usd: null } });
    await gateway.call(SENDER, {
      op: 'settings.update',
      params: {
        stt_model: 'google/gemini-3.5-transcribe',
        stt_language: 'es',
        analysis_model: 'vendor/model',
        run_budget_usd: '1.5',
      },
    });
    await gateway.call(SENDER, { op: 'settings.update', params: {} });

    expect(request.mock.calls).toEqual([
      ['PATCH', '/v1/settings', { run_budget_usd: null }],
      [
        'PATCH',
        '/v1/settings',
        {
          stt_model: 'google/gemini-3.5-transcribe',
          stt_language: 'es',
          analysis_model: 'vendor/model',
          run_budget_usd: '1.5',
        },
      ],
      ['PATCH', '/v1/settings', {}],
    ]);
  });

  it.each([
    ['the API key', { openrouter_api_key: 'k'.repeat(30) }],
    ['a has_api_key flag', { has_api_key: true }],
    ['an unknown setting', { theme: 'dark' }],
    ['a null model of recognition', { stt_model: null }],
    ['a number as the budget', { run_budget_usd: 2.5 }],
    ['a number as the language', { stt_language: 5 }],
    ['a list', []],
    ['a string', 'x'],
  ])('settings.update refuses %s without calling the sidecar', async (_name, params) => {
    const { gateway, request } = setup();

    expect(await gateway.call(SENDER, { op: 'settings.update', params })).toMatchObject({
      ok: false,
      error: { code: 'INVALID_PARAMS' },
    });
    expect(request).not.toHaveBeenCalled();
  });

  it('settings.get refuses params', async () => {
    const { gateway, request } = setup();

    expect(await gateway.call(SENDER, { op: 'settings.get', params: { x: 1 } })).toMatchObject({
      ok: false,
      error: { code: 'INVALID_PARAMS' },
    });
    expect(request).not.toHaveBeenCalled();
  });

  it('forwards a 422 of the backend with its code', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({
      ok: false,
      error: { code: 'INVALID_RUN_BUDGET', message: 'Ліміт витрат має бути…', retryable: false },
    });

    expect(
      await gateway.call(SENDER, { op: 'settings.update', params: { run_budget_usd: 'abc' } }),
    ).toEqual({
      ok: false,
      error: { code: 'INVALID_RUN_BUDGET', message: 'Ліміт витрат має бути…', retryable: false },
    });
  });

  it('turns a settings answer of another shape into an error', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({ ok: true, data: { ...SETTINGS, has_api_key: 'yes' } });

    expect(await gateway.call(SENDER, { op: 'settings.get', params: {} })).toMatchObject({
      ok: false,
      error: { code: 'BAD_BACKEND_RESPONSE' },
    });
  });
});

describe('transcription ops and jobs.resume (ADR 038)', () => {
  it('estimate posts only the source and the track, and answers exactly the thirteen fields', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({
      ok: true,
      data: { ...ESTIMATE, existing: { transcript_id: ID, run_id: ID2, path: 'C:\\x' }, extra: 1 },
    });

    const result = await gateway.call(SENDER, {
      op: 'transcription.estimate',
      params: { source_id: ID, audio_stream_index: 1 },
    });

    expect(request.mock.calls).toEqual([
      ['POST', '/v1/transcription-runs/estimate', { source_id: ID, audio_stream_index: 1 }],
    ]);
    expect(result).toEqual({
      ok: true,
      data: { ...ESTIMATE, existing: { transcript_id: ID, run_id: ID2 } },
    });
  });

  it('create sends force only when it was given, and answers the run and its job', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValue({ ok: true, data: { ...CREATED, extra: 1 } });

    const plain = await gateway.call(SENDER, {
      op: 'transcription.create',
      params: { source_id: ID, audio_stream_index: 2 },
    });
    await gateway.call(SENDER, {
      op: 'transcription.create',
      params: { source_id: ID, audio_stream_index: 2, force: true },
    });

    expect(request.mock.calls).toEqual([
      ['POST', '/v1/transcription-runs', { source_id: ID, audio_stream_index: 2 }],
      ['POST', '/v1/transcription-runs', { source_id: ID, audio_stream_index: 2, force: true }],
    ]);
    expect(plain).toEqual({ ok: true, data: CREATED });
  });

  it('get and list read the run and the runs of a source; the answers lose unknown keys', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({
      ok: true,
      data: {
        ...TRANSCRIPTION_RUN,
        chunks: [{ ...TRANSCRIPTION_RUN.chunks[0], response_path: 'C:\\x' }],
        extra: 1,
      },
    });
    request.mockResolvedValueOnce({
      ok: true,
      data: { items: [{ ...TRANSCRIPTION_ITEM, extra: 1 }] },
    });

    const run = await gateway.call(SENDER, { op: 'transcription.get', params: { id: ID } });
    const runs = await gateway.call(SENDER, {
      op: 'transcription.list',
      params: { source_id: ID2 },
    });

    expect(request.mock.calls).toEqual([
      ['GET', `/v1/transcription-runs/${ID}`, undefined],
      ['GET', `/v1/transcription-runs?source_id=${ID2}`, undefined],
    ]);
    expect(run).toEqual({ ok: true, data: TRANSCRIPTION_RUN });
    expect(runs).toEqual({ ok: true, data: { items: [TRANSCRIPTION_ITEM] } });
  });

  it('resume posts an empty body, or only retry_unknown, and answers the job', async () => {
    const { gateway, request } = setup();
    const job = { ...JOB, type: 'transcription', state: 'queued' };
    request.mockResolvedValue({ ok: true, data: { ...job, lease_owner: 'x' } });

    const first = await gateway.call(SENDER, { op: 'jobs.resume', params: { id: ID } });
    await gateway.call(SENDER, { op: 'jobs.resume', params: { id: ID, retry_unknown: true } });

    expect(request.mock.calls).toEqual([
      ['POST', `/v1/jobs/${ID}/resume`, {}],
      ['POST', `/v1/jobs/${ID}/resume`, { retry_unknown: true }],
    ]);
    expect(first).toEqual({ ok: true, data: job });
  });

  it.each([
    ['transcription.estimate', { source_id: ID, audio_stream_index: 1, model: 'vendor/model' }],
    ['transcription.estimate', { source_id: ID, audio_stream_index: 1, budget_usd: '1.00' }],
    ['transcription.estimate', { source_id: ID, audio_stream_index: '1' }],
    ['transcription.create', { source_id: ID, audio_stream_index: 1, language: 'uk' }],
    ['transcription.create', { source_id: ID, audio_stream_index: 1, openrouter_api_key: 'k' }],
    ['transcription.create', { source_id: ID, audio_stream_index: 1, force: 'yes' }],
    ['transcription.create', { source_id: 'x', audio_stream_index: 1 }],
    ['transcription.get', { id: `${ID}/../health` }],
    ['transcription.list', { source_id: `${ID}&x=1` }],
    ['transcription.list', { source_id: ID, limit: 5 }],
    ['jobs.resume', { id: ID, budget_usd: '0.00' }],
    ['jobs.resume', { id: ID, budget_usd: 9 }],
    ['jobs.resume', { id: ID, retry_unknown: 'true' }],
  ] as const)('%s refuses %j without calling the sidecar', async (op, params) => {
    const { gateway, request } = setup();

    expect(await gateway.call(SENDER, { op, params })).toMatchObject({
      ok: false,
      error: { code: 'INVALID_PARAMS' },
    });
    expect(request).not.toHaveBeenCalled();
  });

  it('forwards a refusal of the backend with its code and message, and nothing else', async () => {
    const { gateway, request } = setup();
    const refusal = {
      ok: false,
      error: { code: 'TRANSCRIPT_ALREADY_EXISTS', message: 'Транскрипт уже є', retryable: false },
    } as const;
    request.mockResolvedValueOnce(refusal);

    expect(
      await gateway.call(SENDER, {
        op: 'transcription.create',
        params: { source_id: ID, audio_stream_index: 1 },
      }),
    ).toEqual(refusal);
  });

  it('turns an answer of another shape into an error, for every transcription op', async () => {
    const { gateway, request } = setup();
    const broken: readonly (readonly [ApiOp, unknown, unknown])[] = [
      ['transcription.estimate', { source_id: ID, audio_stream_index: 1 }, { ...ESTIMATE, chunk_count: '1' }],
      ['transcription.create', { source_id: ID, audio_stream_index: 1 }, { id: ID }],
      ['transcription.get', { id: ID }, { ...TRANSCRIPTION_RUN, chunks: [{ id: ID }] }],
      ['transcription.list', { source_id: ID }, { items: [{ ...TRANSCRIPTION_ITEM, created_at: 5 }] }],
      ['jobs.resume', { id: ID }, { ...JOB, progress: 5 }],
    ];

    for (const [op, params, data] of broken) {
      request.mockResolvedValueOnce({ ok: true, data });

      expect(await gateway.call(SENDER, { op, params }), op).toMatchObject({
        ok: false,
        error: { code: 'BAD_BACKEND_RESPONSE' },
      });
    }
  });
});

describe('transcripts, credentials and the resume budget (ADR 038, 039)', () => {
  it('jobs.resume sends the budget next to the decision, and only the params that were given', async () => {
    const { gateway, request } = setup();
    const job = { ...JOB, type: 'transcription', state: 'queued' };
    request.mockResolvedValue({ ok: true, data: job });

    await gateway.call(SENDER, { op: 'jobs.resume', params: { id: ID, budget_usd: '5.00' } });
    await gateway.call(SENDER, {
      op: 'jobs.resume',
      params: { id: ID, retry_unknown: true, budget_usd: '1000' },
    });

    expect(request.mock.calls).toEqual([
      ['POST', `/v1/jobs/${ID}/resume`, { budget_usd: '5.00' }],
      ['POST', `/v1/jobs/${ID}/resume`, { retry_unknown: true, budget_usd: '1000' }],
    ]);
  });

  it.each([
    ['jobs.resume', { id: ID, budget_usd: null }],
    ['jobs.resume', { id: ID, budget_usd: '0.009999' }],
    ['jobs.resume', { id: ID, budget_usd: '1000.000001' }],
    ['jobs.resume', { id: ID, budget_usd: '1001' }],
    ['jobs.resume', { id: ID, budget_usd: '05' }],
    ['transcripts.get', { id: ID, limit: 0 }],
    ['transcripts.get', { id: ID, limit: 1001 }],
    ['transcripts.get', { id: ID, offset: -1 }],
    ['transcripts.get', { id: ID, openrouter_api_key: 'k' }],
    ['transcripts.list', { source_id: 'x' }],
    ['transcripts.correct', { id: ID, edits: [] }],
    ['transcripts.correct', { id: ID, edits: [{ word_id: 'w1', segment_id: 's1', text: 'a' }] }],
    ['transcripts.correct', { id: ID, edits: [{ word_id: 'w1', text: '   ' }] }],
    ['credentials.check', { openrouter_api_key: 'k' }],
  ] as const)('%s refuses %j without calling the sidecar', async (op, params) => {
    const { gateway, request } = setup();

    expect(await gateway.call(SENDER, { op, params })).toMatchObject({
      ok: false,
      error: { code: 'INVALID_PARAMS' },
    });
    expect(request).not.toHaveBeenCalled();
  });

  it('transcripts.list pages with the opaque cursor, encoded, and keeps no metadata', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({ ok: true, data: { items: [TRANSCRIPT], next_cursor: 'c2' } });

    const result = await gateway.call(SENDER, {
      op: 'transcripts.list',
      params: { source_id: ID2, cursor: 'c1&source_id=x' },
    });

    expect(request.mock.calls).toEqual([
      ['GET', `/v1/transcripts?source_id=${ID2}&cursor=c1%26source_id%3Dx`, undefined],
    ]);
    expect(result).toEqual({ ok: true, data: { items: [TRANSCRIPT_DTO], next_cursor: 'c2' } });
  });

  it('transcripts.get sends the window as a query and answers the window without metadata', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({ ok: true, data: { ...TRANSCRIPT_WINDOW, limit: 1000 } });

    const result = await gateway.call(SENDER, {
      op: 'transcripts.get',
      params: { id: ID, limit: 1000 },
    });

    expect(request.mock.calls).toEqual([['GET', `/v1/transcripts/${ID}?limit=1000`, undefined]]);
    expect(result).toEqual({
      ok: true,
      data: {
        ...TRANSCRIPT_DTO,
        offset: 0,
        limit: 1000,
        next_offset: null,
        segments: [TRANSCRIPT_SEGMENT],
        words: TRANSCRIPT_WORDS,
      },
    });
    expect(JSON.stringify(result)).not.toContain('options_hash');
  });

  it('transcripts.correct posts exactly the edits and answers the new revision', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({ ok: true, data: TRANSCRIPT });

    const result = await gateway.call(SENDER, {
      op: 'transcripts.correct',
      params: { id: ID, edits: [{ segment_id: 's1', text: 'Привіт, світ' }] },
    });

    expect(request.mock.calls).toEqual([
      [
        'POST',
        `/v1/transcripts/${ID}/corrections`,
        { edits: [{ segment_id: 's1', text: 'Привіт, світ' }] },
      ],
    ]);
    expect(result).toEqual({ ok: true, data: TRANSCRIPT_DTO });
  });

  it('credentials.check posts with no body, answers valid or invalid, and refuses a malformed answer', async () => {
    const { gateway, request } = setup();
    request.mockResolvedValueOnce({
      ok: true,
      data: { state: 'valid', is_free_tier: true, limit_remaining_usd: null, label: 'x' },
    });
    request.mockResolvedValueOnce({ ok: true, data: { state: 'invalid' } });
    request.mockResolvedValueOnce({
      ok: true,
      data: { state: 'valid', is_free_tier: 'no', limit_remaining_usd: null },
    });

    const valid = await gateway.call(SENDER, { op: 'credentials.check', params: {} });
    const invalid = await gateway.call(SENDER, { op: 'credentials.check' });
    const broken = await gateway.call(SENDER, { op: 'credentials.check', params: {} });

    expect(request.mock.calls).toEqual([
      ['POST', '/v1/runtime/credentials/check', undefined],
      ['POST', '/v1/runtime/credentials/check', undefined],
      ['POST', '/v1/runtime/credentials/check', undefined],
    ]);
    expect(valid).toEqual({
      ok: true,
      data: { state: 'valid', is_free_tier: true, limit_remaining_usd: null },
    });
    expect(invalid).toEqual({ ok: true, data: { state: 'invalid' } });
    expect(broken).toMatchObject({ ok: false, error: { code: 'BAD_BACKEND_RESPONSE' } });
  });

  it('credentials.check forwards the upstream error with its code, message and retryable', async () => {
    const { gateway, request } = setup();
    const refusal = {
      ok: false,
      error: { code: 'KEY_CHECK_UPSTREAM', message: 'OpenRouter не відповів', retryable: true },
    } as const;
    request.mockResolvedValueOnce(refusal);

    expect(await gateway.call(SENDER, { op: 'credentials.check', params: {} })).toEqual(refusal);
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
