// @vitest-environment node
import { describe, expect, it } from 'vitest';

import { API_OPS } from '../shared/api';
import type { ApiOp } from '../shared/api';
import {
  ParamError,
  buildPlan,
  parseCall,
  parseDropParams,
  parseImportParams,
  parseOpenFolderParams,
} from './api-ops';

const ID = '3f2b8c1e-5d4a-4b7e-9a10-2c6d8e0f1a2b';
const ID2 = '0a9d7e6c-1b2f-4c3d-8e4f-5a6b7c8d9e0f';
const TOKEN = 'ab'.repeat(32);
const PROFILE = {
  video_codec: 'h264',
  quality: 'high',
  audio_mode: 'selected',
  audio_stream_index: 1,
  write_srt: false,
};

describe('buildPlan: every op maps to one fixed method and path', () => {
  const cases: readonly (readonly [ApiOp, unknown, unknown])[] = [
    ['projects.list', {}, { method: 'GET', path: '/v1/projects' }],
    [
      'projects.list',
      { limit: 20, cursor: 'abc', include_archived: true },
      { method: 'GET', path: '/v1/projects?limit=20&cursor=abc&include_archived=true' },
    ],
    [
      'projects.list',
      { q: 'Проєкт', include_archived: true },
      { method: 'GET', path: '/v1/projects?include_archived=true&q=%D0%9F%D1%80%D0%BE%D1%94%D0%BA%D1%82' },
    ],
    [
      'projects.create',
      { name: 'Перевірка' },
      { method: 'POST', path: '/v1/projects', body: { name: 'Перевірка' } },
    ],
    [
      'projects.update',
      { id: ID, archived: true },
      { method: 'PATCH', path: `/v1/projects/${ID}`, body: { archived: true } },
    ],
    ['sources.get', { id: ID }, { method: 'GET', path: `/v1/sources/${ID}` }],
    [
      'sources.list',
      { project_id: ID },
      { method: 'GET', path: `/v1/sources?project_id=${ID}&limit=100` },
    ],
    [
      'sources.list',
      { project_id: ID, cursor: 'abc' },
      { method: 'GET', path: `/v1/sources?project_id=${ID}&limit=100&cursor=abc` },
    ],
    [
      'clips.list',
      { source_id: ID, review_status: 'approved', category: 'funny', limit: 10 },
      {
        method: 'GET',
        path: `/v1/clips?source_id=${ID}&review_status=approved&category=funny&limit=10`,
      },
    ],
    [
      'clips.create',
      { source_id: ID, title: 'Мій', start_ms: 4120, end_ms: 7880, categories: ['funny'] },
      {
        method: 'POST',
        path: '/v1/clips',
        body: {
          source_id: ID,
          title: 'Мій',
          start_ms: 4120,
          end_ms: 7880,
          categories: ['funny'],
          evidence_type: 'manual',
        },
      },
    ],
    [
      'clips.patch',
      { id: ID, expected_revision: 2, title: 'Новий' },
      {
        method: 'PATCH',
        path: `/v1/clips/${ID}`,
        body: { expected_revision: 2, title: 'Новий' },
      },
    ],
    [
      'clips.review',
      { id: ID, expected_revision: 3, status: 'rejected' },
      {
        method: 'POST',
        path: `/v1/clips/${ID}/review`,
        body: { expected_revision: 3, status: 'rejected' },
      },
    ],
    [
      'exports.create',
      { project_id: ID, clip_ids: [ID2], folderToken: TOKEN, profile: PROFILE },
      {
        method: 'POST',
        path: '/v1/export-batches',
        body: { project_id: ID, clip_ids: [ID2], profile: PROFILE },
        folderToken: TOKEN,
      },
    ],
    ['exports.get', { id: ID }, { method: 'GET', path: `/v1/export-batches/${ID}` }],
    [
      'exports.retry',
      { batch_id: ID },
      { method: 'GET', path: `/v1/export-batches/${ID}`, retry: true },
    ],
    ['jobs.get', { id: ID }, { method: 'GET', path: `/v1/jobs/${ID}` }],
    ['jobs.cancel', { id: ID }, { method: 'POST', path: `/v1/jobs/${ID}/cancel`, body: {} }],
    [
      'previews.create',
      { source_id: ID, audio_stream_index: 2 },
      {
        method: 'POST',
        path: `/v1/sources/${ID}/previews`,
        body: { audio_stream_index: 2 },
      },
    ],
    [
      'previews.create',
      { source_id: ID, audio_stream_index: null },
      {
        method: 'POST',
        path: `/v1/sources/${ID}/previews`,
        body: { audio_stream_index: null },
      },
    ],
    ['previews.get', { id: ID }, { method: 'GET', path: `/v1/previews/${ID}` }],
    ['system.checks', {}, { method: 'GET', path: '/v1/system/checks' }],
    ['settings.get', {}, { method: 'GET', path: '/v1/settings' }],
    [
      'settings.update',
      { stt_model: 'google/gemini-3.5-transcribe', stt_language: null, run_budget_usd: '2.50' },
      {
        method: 'PATCH',
        path: '/v1/settings',
        body: { stt_model: 'google/gemini-3.5-transcribe', stt_language: null, run_budget_usd: '2.50' },
      },
    ],
    ['settings.update', {}, { method: 'PATCH', path: '/v1/settings', body: {} }],
    ['jobs.resume', { id: ID }, { method: 'POST', path: `/v1/jobs/${ID}/resume`, body: {} }],
    [
      'jobs.resume',
      { id: ID, retry_unknown: true },
      { method: 'POST', path: `/v1/jobs/${ID}/resume`, body: { retry_unknown: true } },
    ],
    [
      'jobs.resume',
      { id: ID, retry_unknown: false },
      { method: 'POST', path: `/v1/jobs/${ID}/resume`, body: { retry_unknown: false } },
    ],
    [
      'transcription.estimate',
      { source_id: ID, audio_stream_index: 1 },
      {
        method: 'POST',
        path: '/v1/transcription-runs/estimate',
        body: { source_id: ID, audio_stream_index: 1 },
      },
    ],
    [
      'transcription.create',
      { source_id: ID, audio_stream_index: 2 },
      {
        method: 'POST',
        path: '/v1/transcription-runs',
        body: { source_id: ID, audio_stream_index: 2 },
      },
    ],
    [
      'transcription.create',
      { source_id: ID, audio_stream_index: 2, force: true },
      {
        method: 'POST',
        path: '/v1/transcription-runs',
        body: { source_id: ID, audio_stream_index: 2, force: true },
      },
    ],
    [
      'transcription.create',
      { source_id: ID, audio_stream_index: 0, force: false },
      {
        method: 'POST',
        path: '/v1/transcription-runs',
        body: { source_id: ID, audio_stream_index: 0, force: false },
      },
    ],
    ['transcription.get', { id: ID }, { method: 'GET', path: `/v1/transcription-runs/${ID}` }],
    [
      'transcription.list',
      { source_id: ID },
      { method: 'GET', path: `/v1/transcription-runs?source_id=${ID}` },
    ],
    [
      'jobs.resume',
      { id: ID, budget_usd: '5.00' },
      { method: 'POST', path: `/v1/jobs/${ID}/resume`, body: { budget_usd: '5.00' } },
    ],
    [
      'jobs.resume',
      { id: ID, retry_unknown: true, budget_usd: '1000' },
      {
        method: 'POST',
        path: `/v1/jobs/${ID}/resume`,
        body: { retry_unknown: true, budget_usd: '1000' },
      },
    ],
    [
      'jobs.resume',
      { id: ID, budget_usd: '0.01' },
      { method: 'POST', path: `/v1/jobs/${ID}/resume`, body: { budget_usd: '0.01' } },
    ],
    [
      'transcripts.list',
      { source_id: ID },
      { method: 'GET', path: `/v1/transcripts?source_id=${ID}` },
    ],
    [
      'transcripts.list',
      { source_id: ID, cursor: 'c1' },
      { method: 'GET', path: `/v1/transcripts?source_id=${ID}&cursor=c1` },
    ],
    ['transcripts.get', { id: ID }, { method: 'GET', path: `/v1/transcripts/${ID}` }],
    [
      'transcripts.get',
      { id: ID, offset: 0, limit: 1 },
      { method: 'GET', path: `/v1/transcripts/${ID}?offset=0&limit=1` },
    ],
    [
      'transcripts.get',
      { id: ID, offset: 1000, limit: 1000 },
      { method: 'GET', path: `/v1/transcripts/${ID}?offset=1000&limit=1000` },
    ],
    [
      'transcripts.correct',
      {
        id: ID,
        edits: [
          { word_id: 'w1', text: 'Привіт' },
          { segment_id: 's2', text: '  Світ  ' },
        ],
      },
      {
        method: 'POST',
        path: `/v1/transcripts/${ID}/corrections`,
        body: {
          edits: [
            { word_id: 'w1', text: 'Привіт' },
            { segment_id: 's2', text: '  Світ  ' },
          ],
        },
      },
    ],
    [
      'credentials.check',
      {},
      { method: 'POST', path: '/v1/runtime/credentials/check' },
    ],
  ];

  it.each(cases)('%s %j', (op, params, expected) => {
    expect(buildPlan(op, params)).toEqual(expected);
  });

  it('covers every allowlisted op', () => {
    expect(new Set(cases.map(([op]) => op))).toEqual(new Set(API_OPS));
  });

  it('encodes query values instead of concatenating them', () => {
    const plan = buildPlan('projects.list', { cursor: 'a&include_archived=true#x y' });

    expect(plan.path).toBe('/v1/projects?cursor=a%26include_archived%3Dtrue%23x+y');
  });

  it('projects.list: the search text is encoded, never read as another parameter', () => {
    const plan = buildPlan('projects.list', { q: 'кир&include_archived=true#x y' });

    expect(plan.path).toBe(
      '/v1/projects?q=%D0%BA%D0%B8%D1%80%26include_archived%3Dtrue%23x+y',
    );
    expect(plan.body).toBeUndefined();
  });

  it('projects.list: 200 characters are accepted, counted as code points like the backend', () => {
    const cyrillic = 'ж'.repeat(200);
    const astral = '🎬'.repeat(200);

    expect(buildPlan('projects.list', { q: cyrillic }).path).toBe(
      `/v1/projects?q=${encodeURIComponent(cyrillic)}`,
    );
    // 200 characters, 400 UTF-16 code units.
    expect(buildPlan('projects.list', { q: astral }).path).toBe(
      `/v1/projects?q=${encodeURIComponent(astral)}`,
    );
  });
  it('encodes the cursor of sources.list and keeps the project and the page size fixed', () => {
    const plan = buildPlan('sources.list', { project_id: ID, cursor: 'a&project_id=x&limit=1#y z' });

    expect(plan.path).toBe(
      `/v1/sources?project_id=${ID}&limit=100&cursor=a%26project_id%3Dx%26limit%3D1%23y+z`,
    );
    expect(plan.body).toBeUndefined();
  });

  it('never carries an output_dir or a path in a plan', () => {
    const plan = buildPlan('exports.create', {
      project_id: ID,
      clip_ids: [ID],
      folderToken: TOKEN,
      profile: PROFILE,
    });

    expect(JSON.stringify(plan.body)).not.toContain('output_dir');
    expect(plan.folderToken).toBe(TOKEN);
  });

  it('keeps null audio_stream_index for a no-audio or all-streams profile', () => {
    const plan = buildPlan('exports.create', {
      project_id: ID,
      clip_ids: [ID],
      folderToken: TOKEN,
      profile: { ...PROFILE, audio_mode: 'none', audio_stream_index: null },
    });

    expect(plan.body).toMatchObject({ profile: { audio_stream_index: null } });
  });
});

describe('buildPlan: ids must be uuid4', () => {
  const NOT_UUIDS: readonly unknown[] = [
    '',
    'abc',
    '../health',
    `${ID}/../../health`,
    `${ID}?x=1`,
    `${ID}#x`,
    ` ${ID}`,
    ID.toUpperCase(),
    '3f2b8c1e-5d4a-1b7e-9a10-2c6d8e0f1a2b', // version 1
    '3f2b8c1e-5d4a-4b7e-1a10-2c6d8e0f1a2b', // bad variant
    42,
    null,
    { toString: () => ID },
    [ID],
  ];

  const idOps: readonly (readonly [ApiOp, string])[] = [
    ['projects.update', 'id'],
    ['sources.get', 'id'],
    ['sources.list', 'project_id'],
    ['clips.patch', 'id'],
    ['clips.review', 'id'],
    ['exports.get', 'id'],
    ['exports.retry', 'batch_id'],
    ['jobs.get', 'id'],
    ['jobs.cancel', 'id'],
    ['jobs.resume', 'id'],
    ['previews.create', 'source_id'],
    ['previews.get', 'id'],
    ['transcription.estimate', 'source_id'],
    ['transcription.create', 'source_id'],
    ['transcription.get', 'id'],
    ['transcription.list', 'source_id'],
    ['transcripts.list', 'source_id'],
    ['transcripts.get', 'id'],
    ['transcripts.correct', 'id'],
  ];
  const valid: Readonly<Record<string, unknown>> = {
    'projects.update': { id: ID },
    'sources.get': { id: ID },
    'sources.list': { project_id: ID },
    'clips.patch': { id: ID, expected_revision: 1 },
    'clips.review': { id: ID, expected_revision: 1, status: 'approved' },
    'exports.get': { id: ID },
    'exports.retry': { batch_id: ID },
    'jobs.get': { id: ID },
    'jobs.cancel': { id: ID },
    'jobs.resume': { id: ID },
    'previews.create': { source_id: ID, audio_stream_index: 1 },
    'previews.get': { id: ID },
    'transcription.estimate': { source_id: ID, audio_stream_index: 1 },
    'transcription.create': { source_id: ID, audio_stream_index: 1 },
    'transcription.get': { id: ID },
    'transcription.list': { source_id: ID },
    'transcripts.list': { source_id: ID },
    'transcripts.get': { id: ID },
    'transcripts.correct': { id: ID, edits: [{ word_id: 'w1', text: 'a' }] },
  };

  it.each(idOps)('%s rejects a bad %s', (op, key) => {
    for (const bad of NOT_UUIDS) {
      expect(() => buildPlan(op, { ...(valid[op] as object), [key]: bad })).toThrow(ParamError);
    }
  });

  it('rejects bad ids in body and query parameters', () => {
    for (const bad of NOT_UUIDS) {
      expect(() => buildPlan('clips.list', { source_id: bad })).toThrow(ParamError);
      expect(() =>
        buildPlan('clips.create', {
          source_id: bad,
          title: 't',
          start_ms: 0,
          end_ms: 1,
          categories: [],
        }),
      ).toThrow(ParamError);
      expect(() =>
        buildPlan('exports.create', {
          project_id: bad,
          clip_ids: [ID],
          folderToken: TOKEN,
          profile: PROFILE,
        }),
      ).toThrow(ParamError);
      expect(() =>
        buildPlan('exports.create', {
          project_id: ID,
          clip_ids: [ID, bad],
          folderToken: TOKEN,
          profile: PROFILE,
        }),
      ).toThrow(ParamError);
    }
  });

  it('rejects an empty clip_ids list', () => {
    expect(() =>
      buildPlan('exports.create', {
        project_id: ID,
        clip_ids: [],
        folderToken: TOKEN,
        profile: PROFILE,
      }),
    ).toThrow(ParamError);
  });
});

describe('buildPlan: params of the wrong shape are rejected', () => {
  it.each([
    ['projects.create', {}],
    ['projects.create', { name: 5 }],
    ['projects.create', { name: 'a'.repeat(1001) }],
    ['projects.create', { name: 'a', extra: 1 }],
    ['projects.create', null],
    ['projects.create', 'name'],
    ['projects.create', [{ name: 'a' }]],
    ['projects.list', { limit: '5' }],
    ['projects.list', { limit: 1.5 }],
    ['projects.list', { include_archived: 'true' }],
    ['projects.list', { q: 5 }],
    ['projects.list', { q: null }],
    ['projects.list', { q: 'а'.repeat(201) }],
    ['projects.list', { q: '🎬'.repeat(201) }],
    ['projects.update', { id: ID, name: null }],
    ['clips.list', { review_status: 'deleted' }],
    ['clips.create', { source_id: ID, title: 't', start_ms: 1.5, end_ms: 2, categories: [] }],
    ['clips.create', { source_id: ID, title: 't', start_ms: 1, end_ms: 2, categories: 'funny' }],
    ['clips.create', { source_id: ID, title: 't', start_ms: 1, end_ms: 2, categories: [1] }],
    ['clips.create', { source_id: ID, title: 't', start_ms: 1, end_ms: 2 }],
    ['clips.patch', { id: ID }],
    ['clips.patch', { id: ID, expected_revision: '1' }],
    ['clips.review', { id: ID, expected_revision: 1, status: 'exported' }],
    ['exports.get', {}],
    ['exports.retry', {}],
    ['exports.retry', { id: ID }],
    ['exports.retry', { batch_id: ID, output_dir: 'C:\\Windows' }],
    ['exports.retry', { batch_id: ID, profile: PROFILE }],
    ['exports.retry', { batch_id: ID, clip_ids: [ID2] }],
    ['exports.retry', undefined],
    ['jobs.get', undefined],
    ['sources.list', {}],
    ['sources.list', undefined],
    ['sources.list', null],
    ['sources.list', { cursor: 'abc' }],
    ['sources.list', { project_id: ID, cursor: 5 }],
    ['sources.list', { project_id: ID, cursor: null }],
    ['sources.list', { project_id: ID, cursor: 'a'.repeat(1001) }],
    ['sources.list', { project_id: ID, limit: 5 }],
    ['sources.list', { project_id: ID, id: ID }],
    ['sources.list', { project_id: ID, source_id: ID }],
    ['sources.list', { project_id: ID, extra: true }],
    ['sources.list', [{ project_id: ID }]],
    ['previews.create', { source_id: ID }],
    ['previews.create', { audio_stream_index: 1 }],
    ['previews.create', { source_id: ID, audio_stream_index: undefined }],
    ['previews.create', { source_id: ID, audio_stream_index: '1' }],
    ['previews.create', { source_id: ID, audio_stream_index: 1.5 }],
    ['previews.create', { source_id: ID, audio_stream_index: Number.NaN }],
    ['previews.create', { source_id: ID, audio_stream_index: [1] }],
    ['previews.create', { source_id: ID, audio_stream_index: 1, extra: true }],
    ['previews.create', null],
    ['previews.get', {}],
    ['previews.get', { id: ID, extra: true }],
    ['previews.get', undefined],
    ['system.checks', { extra: 1 }],
    ['system.checks', { path: 'C:\\Windows' }],
    ['system.checks', { output_dir: 'C:\\Windows' }],
    ['system.checks', { id: undefined, extra: 1 }],
    ['system.checks', []],
    ['system.checks', 'checks'],
    ['system.checks', 5],
    ['system.checks', true],
    ['settings.get', { extra: 1 }],
    ['settings.get', []],
    ['settings.update', { openrouter_api_key: 'k'.repeat(30) }],
    ['settings.update', { has_api_key: true }],
    ['settings.update', { theme: 'dark' }],
    ['settings.update', { stt_model: null }],
    ['settings.update', { stt_model: 5 }],
    ['settings.update', { stt_language: 5 }],
    ['settings.update', { analysis_model: false }],
    ['settings.update', { run_budget_usd: 2.5 }],
    ['settings.update', { run_budget_usd: 'a'.repeat(1001) }],
    ['settings.update', { run_budget_usd: '1', extra: true }],
    ['settings.update', []],
    ['settings.update', 'x'],
    ['jobs.resume', {}],
    ['jobs.resume', undefined],
    ['jobs.resume', null],
    ['jobs.resume', { id: ID, retry_unknown: 'true' }],
    ['jobs.resume', { id: ID, retry_unknown: 1 }],
    ['jobs.resume', { id: ID, retry_unknown: null }],
    ['jobs.resume', { id: ID, budget_usd: null }],
    ['jobs.resume', { id: ID, budget_usd: 5 }],
    ['jobs.resume', { id: ID, budget_usd: '' }],
    ['jobs.resume', { id: ID, budget_usd: '0' }],
    ['jobs.resume', { id: ID, budget_usd: '0.00' }],
    ['jobs.resume', { id: ID, budget_usd: '0.009999' }],
    ['jobs.resume', { id: ID, budget_usd: '1000.000001' }],
    ['jobs.resume', { id: ID, budget_usd: '1000.0000001' }],
    ['jobs.resume', { id: ID, budget_usd: '1001' }],
    ['jobs.resume', { id: ID, budget_usd: '10000' }],
    ['jobs.resume', { id: ID, budget_usd: '05' }],
    ['jobs.resume', { id: ID, budget_usd: '1.' }],
    ['jobs.resume', { id: ID, budget_usd: '.5' }],
    ['jobs.resume', { id: ID, budget_usd: '+5' }],
    ['jobs.resume', { id: ID, budget_usd: '-5' }],
    ['jobs.resume', { id: ID, budget_usd: '1e2' }],
    ['jobs.resume', { id: ID, budget_usd: ' 5' }],
    ['jobs.resume', { id: ID, budget_usd: '5,00' }],
    ['jobs.resume', { id: ID, budget_usd: 'NaN' }],
    ['jobs.resume', { id: ID, retry_unknown: true, budget_usd: '0' }],
    ['jobs.resume', { id: ID, budget_usd: '5', ceiling: '5' }],
    ['jobs.resume', { id: ID, extra: true }],
    ['jobs.resume', [{ id: ID }]],
    ['transcription.estimate', {}],
    ['transcription.estimate', undefined],
    ['transcription.estimate', null],
    ['transcription.estimate', { source_id: ID }],
    ['transcription.estimate', { audio_stream_index: 1 }],
    ['transcription.estimate', { source_id: ID, audio_stream_index: undefined }],
    ['transcription.estimate', { source_id: ID, audio_stream_index: null }],
    ['transcription.estimate', { source_id: ID, audio_stream_index: '1' }],
    ['transcription.estimate', { source_id: ID, audio_stream_index: 1.5 }],
    ['transcription.estimate', { source_id: ID, audio_stream_index: Number.NaN }],
    ['transcription.estimate', { source_id: ID, audio_stream_index: Number.POSITIVE_INFINITY }],
    ['transcription.estimate', { source_id: ID, audio_stream_index: [1] }],
    ['transcription.estimate', { source_id: ID, audio_stream_index: 1, model: 'vendor/model' }],
    ['transcription.estimate', { source_id: ID, audio_stream_index: 1, language: 'uk' }],
    ['transcription.estimate', { source_id: ID, audio_stream_index: 1, budget_usd: '1.00' }],
    ['transcription.estimate', { source_id: ID, audio_stream_index: 1, force: true }],
    ['transcription.estimate', { source_id: ID, audio_stream_index: 1, extra: true }],
    ['transcription.estimate', [{ source_id: ID, audio_stream_index: 1 }]],
    ['transcription.create', {}],
    ['transcription.create', undefined],
    ['transcription.create', null],
    ['transcription.create', { source_id: ID }],
    ['transcription.create', { audio_stream_index: 1 }],
    ['transcription.create', { source_id: ID, audio_stream_index: '1' }],
    ['transcription.create', { source_id: ID, audio_stream_index: 1.5 }],
    ['transcription.create', { source_id: ID, audio_stream_index: null }],
    ['transcription.create', { source_id: ID, audio_stream_index: 1, force: 'true' }],
    ['transcription.create', { source_id: ID, audio_stream_index: 1, force: 1 }],
    ['transcription.create', { source_id: ID, audio_stream_index: 1, force: null }],
    ['transcription.create', { source_id: ID, audio_stream_index: 1, model: 'vendor/model' }],
    ['transcription.create', { source_id: ID, audio_stream_index: 1, language: 'uk' }],
    ['transcription.create', { source_id: ID, audio_stream_index: 1, budget_usd: '1.00' }],
    ['transcription.create', { source_id: ID, audio_stream_index: 1, openrouter_api_key: 'k' }],
    ['transcription.create', { source_id: ID, audio_stream_index: 1, extra: true }],
    ['transcription.create', [{ source_id: ID, audio_stream_index: 1 }]],
    ['transcription.get', {}],
    ['transcription.get', undefined],
    ['transcription.get', { source_id: ID }],
    ['transcription.get', { id: ID, extra: true }],
    ['transcription.get', [{ id: ID }]],
    ['transcription.list', {}],
    ['transcription.list', undefined],
    ['transcription.list', { id: ID }],
    ['transcription.list', { source_id: ID, limit: 5 }],
    ['transcription.list', { source_id: ID, cursor: 'abc' }],
    ['transcription.list', { source_id: ID, extra: true }],
    ['transcription.list', [{ source_id: ID }]],
    ['transcripts.list', undefined],
    ['transcripts.list', {}],
    ['transcripts.list', { source_id: ID, limit: 5 }],
    ['transcripts.list', { source_id: ID, cursor: 5 }],
    ['transcripts.list', { source_id: ID, cursor: null }],
    ['transcripts.list', { source_id: ID, cursor: 'a'.repeat(1001) }],
    ['transcripts.list', { source_id: ID, id: ID }],
    ['transcripts.list', [{ source_id: ID }]],
    ['transcripts.get', {}],
    ['transcripts.get', undefined],
    ['transcripts.get', { id: ID, limit: 0 }],
    ['transcripts.get', { id: ID, limit: 1001 }],
    ['transcripts.get', { id: ID, limit: 1.5 }],
    ['transcripts.get', { id: ID, limit: '200' }],
    ['transcripts.get', { id: ID, limit: null }],
    ['transcripts.get', { id: ID, limit: Number.NaN }],
    ['transcripts.get', { id: ID, offset: -1 }],
    ['transcripts.get', { id: ID, offset: 2 ** 31 }],
    ['transcripts.get', { id: ID, offset: 0.5 }],
    ['transcripts.get', { id: ID, offset: '0' }],
    ['transcripts.get', { id: ID, offset: null }],
    ['transcripts.get', { id: ID, cursor: 'x' }],
    ['transcripts.get', { id: ID, extra: true }],
    ['transcripts.get', [{ id: ID }]],
    ['transcripts.correct', { id: ID }],
    ['transcripts.correct', { id: ID, edits: [] }],
    ['transcripts.correct', { id: ID, edits: 'w1' }],
    ['transcripts.correct', { id: ID, edits: null }],
    ['transcripts.correct', { id: ID, edits: {} }],
    ['transcripts.correct', { edits: [{ word_id: 'w1', text: 'a' }] }],
    ['transcripts.correct', { id: ID, edits: [{ word_id: 'w1', text: 'a' }], extra: 1 }],
    ['transcripts.correct', { id: ID, edits: [{ word_id: 'w1', segment_id: 's1', text: 'a' }] }],
    ['transcripts.correct', { id: ID, edits: [{ text: 'a' }] }],
    ['transcripts.correct', { id: ID, edits: [{ word_id: null, text: 'a' }] }],
    ['transcripts.correct', { id: ID, edits: [{ segment_id: null, text: 'a' }] }],
    ['transcripts.correct', { id: ID, edits: [{ word_id: 'w1', segment_id: null, text: 'a' }] }],
    ['transcripts.correct', { id: ID, edits: [{ word_id: 5, text: 'a' }] }],
    ['transcripts.correct', { id: ID, edits: [{ word_id: '', text: 'a' }] }],
    ['transcripts.correct', { id: ID, edits: [{ word_id: 'w1' }] }],
    ['transcripts.correct', { id: ID, edits: [{ word_id: 'w1', text: undefined }] }],
    ['transcripts.correct', { id: ID, edits: [{ word_id: 'w1', text: '' }] }],
    ['transcripts.correct', { id: ID, edits: [{ word_id: 'w1', text: ' \n\t ' }] }],
    ['transcripts.correct', { id: ID, edits: [{ word_id: 'w1', text: 5 }] }],
    ['transcripts.correct', { id: ID, edits: [{ word_id: 'w1', text: 'a', start_ms: 0 }] }],
    ['transcripts.correct', { id: ID, edits: [{ word_id: 'w1', text: 'a', speaker_local: 'S1' }] }],
    ['transcripts.correct', { id: ID, edits: [null] }],
    ['transcripts.correct', { id: ID, edits: ['w1'] }],
    ['transcripts.correct', { id: ID, edits: [{ word_id: 'w1', text: 'a' }, 'x'] }],
    ['credentials.check', undefined],
    ['credentials.check', null],
    ['credentials.check', []],
    ['credentials.check', 'check'],
    ['credentials.check', { openrouter_api_key: 'k'.repeat(30) }],
    ['credentials.check', { id: ID }],
  ] as const)('%s %j', (op, params) => {
    expect(() => buildPlan(op, params)).toThrow(ParamError);
  });

  it('rejects a bad export profile', () => {
    const call = (profile: unknown): unknown =>
      buildPlan('exports.create', {
        project_id: ID,
        clip_ids: [ID],
        folderToken: TOKEN,
        profile,
      });

    expect(() => call({ ...PROFILE, write_srt: 'no' })).toThrow(ParamError);
    expect(() => call({ ...PROFILE, audio_stream_index: '1' })).toThrow(ParamError);
    expect(() => call({ ...PROFILE, extra: 1 })).toThrow(ParamError);
    expect(() => call({ video_codec: 'h264' })).toThrow(ParamError);
    expect(() => call(null)).toThrow(ParamError);
  });

  it('rejects a malformed folder token', () => {
    for (const bad of [TOKEN.toUpperCase(), TOKEN.slice(1), `${TOKEN}0`, 'C:\\Videos', '', 5]) {
      expect(() =>
        buildPlan('exports.create', {
          project_id: ID,
          clip_ids: [ID],
          folderToken: bad,
          profile: PROFILE,
        }),
      ).toThrow(ParamError);
    }
  });

  it('rejects objects that are not plain', () => {
    class Params {
      readonly id = ID;
    }
    expect(() => buildPlan('sources.get', new Params())).toThrow(ParamError);
  });
});

describe('buildPlan: the renderer cannot pass a path, output_dir or evidence_type', () => {
  const forbidden = [
    { output_dir: 'C:\\Windows' },
    { path: 'C:\\video.mp4' },
    { outputDir: 'C:\\x' },
    { final_path: 'C:\\x' },
    { url: 'http://evil.example' },
    { method: 'DELETE' },
    { headers: { Authorization: 'x' } },
    { evidence_type: 'transcript' },
  ];

  it.each(forbidden)('exports.create rejects %j', (extra) => {
    const withToken = {
      project_id: ID,
      clip_ids: [ID],
      folderToken: TOKEN,
      profile: PROFILE,
      ...extra,
    };
    const withoutToken = { project_id: ID, clip_ids: [ID], profile: PROFILE, ...extra };

    expect(() => buildPlan('exports.create', withToken)).toThrow(ParamError);
    expect(() => buildPlan('exports.create', withoutToken)).toThrow(ParamError);
  });

  it('exports.create does not work with a path instead of a folder token', () => {
    expect(() =>
      buildPlan('exports.create', {
        project_id: ID,
        clip_ids: [ID],
        folderToken: 'C:\\Users\\me\\Videos',
        profile: PROFILE,
      }),
    ).toThrow(ParamError);
    expect(() =>
      buildPlan('exports.create', { project_id: ID, clip_ids: [ID], profile: PROFILE }),
    ).toThrow(ParamError);
  });

  it.each(forbidden)('every other op rejects %j', (extra) => {
    expect(() => buildPlan('projects.create', { name: 'a', ...extra })).toThrow(ParamError);
    expect(() => buildPlan('sources.get', { id: ID, ...extra })).toThrow(ParamError);
    expect(() => buildPlan('sources.list', { project_id: ID, ...extra })).toThrow(ParamError);
    expect(() => buildPlan('clips.list', extra)).toThrow(ParamError);
    expect(() =>
      buildPlan('clips.create', {
        source_id: ID,
        title: 't',
        start_ms: 0,
        end_ms: 1,
        categories: [],
        ...extra,
      }),
    ).toThrow(ParamError);
    expect(() => buildPlan('jobs.cancel', { id: ID, ...extra })).toThrow(ParamError);
    expect(() => buildPlan('exports.retry', { batch_id: ID, ...extra })).toThrow(ParamError);
    expect(() => buildPlan('previews.get', { id: ID, ...extra })).toThrow(ParamError);
    expect(() =>
      buildPlan('previews.create', { source_id: ID, audio_stream_index: 1, ...extra }),
    ).toThrow(ParamError);
    expect(() => buildPlan('jobs.resume', { id: ID, ...extra })).toThrow(ParamError);
    expect(() =>
      buildPlan('transcription.estimate', { source_id: ID, audio_stream_index: 1, ...extra }),
    ).toThrow(ParamError);
    expect(() =>
      buildPlan('transcription.create', { source_id: ID, audio_stream_index: 1, ...extra }),
    ).toThrow(ParamError);
    expect(() => buildPlan('transcription.get', { id: ID, ...extra })).toThrow(ParamError);
    expect(() => buildPlan('transcription.list', { source_id: ID, ...extra })).toThrow(ParamError);
    expect(() => buildPlan('transcripts.list', { source_id: ID, ...extra })).toThrow(ParamError);
    expect(() => buildPlan('transcripts.get', { id: ID, ...extra })).toThrow(ParamError);
    expect(() =>
      buildPlan('transcripts.correct', {
        id: ID,
        edits: [{ word_id: 'w1', text: 'a' }],
        ...extra,
      }),
    ).toThrow(ParamError);
    expect(() => buildPlan('credentials.check', extra)).toThrow(ParamError);
  });

  it('transcription ops send no model, language, budget or key: the backend takes them from the settings', () => {
    const create = buildPlan('transcription.create', {
      source_id: ID,
      audio_stream_index: 1,
      force: true,
    });
    const estimate = buildPlan('transcription.estimate', { source_id: ID, audio_stream_index: 1 });

    expect(Object.keys(create.body ?? {}).sort()).toEqual([
      'audio_stream_index',
      'force',
      'source_id',
    ]);
    expect(Object.keys(estimate.body ?? {}).sort()).toEqual(['audio_stream_index', 'source_id']);
    expect(buildPlan('jobs.resume', { id: ID, retry_unknown: true }).body).toEqual({
      retry_unknown: true,
    });
  });

  it('transcription.list encodes the source id in the query and sends no body', () => {
    const plan = buildPlan('transcription.list', { source_id: ID });

    expect(plan.path).toBe(`/v1/transcription-runs?source_id=${ID}`);
    expect(plan.body).toBeUndefined();
  });

  it('previews.create sends only the audio stream; the source id stays in the path', () => {
    const plan = buildPlan('previews.create', { source_id: ID, audio_stream_index: 3 });

    expect(plan.body).toEqual({ audio_stream_index: 3 });
    expect(JSON.stringify(plan.body)).not.toContain(ID);
  });

  it('rejects an own __proto__ key', () => {
    const params: unknown = JSON.parse(`{"id":"${ID}","__proto__":{"x":1}}`);
    const listParams: unknown = JSON.parse(`{"project_id":"${ID}","__proto__":{"x":1}}`);

    expect(() => buildPlan('sources.get', params)).toThrow(ParamError);
    expect(() => buildPlan('sources.list', listParams)).toThrow(ParamError);
  });
});

describe('buildPlan: transcript windows and corrections (ADR 030, 039)', () => {
  const edit = { word_id: 'w1', text: 'Привіт' };

  it('transcripts.get: offset 0, limit 1 and 1000 and the largest offset are accepted', () => {
    expect(buildPlan('transcripts.get', { id: ID, offset: 0, limit: 1 }).path).toBe(
      `/v1/transcripts/${ID}?offset=0&limit=1`,
    );
    expect(buildPlan('transcripts.get', { id: ID, limit: 1000 }).path).toBe(
      `/v1/transcripts/${ID}?limit=1000`,
    );
    expect(buildPlan('transcripts.get', { id: ID, offset: 2 ** 31 - 1 }).path).toBe(
      `/v1/transcripts/${ID}?offset=2147483647`,
    );
  });

  it('transcripts.get: without a window the path has no query, the backend keeps its defaults', () => {
    expect(buildPlan('transcripts.get', { id: ID }).path).toBe(`/v1/transcripts/${ID}`);
  });

  it('transcripts.correct: 1 and 500 edits are accepted, 0 and 501 are refused', () => {
    const max = Array.from({ length: 500 }, (_, index) => ({
      segment_id: `s${String(index)}`,
      text: 'a',
    }));
    const over = Array.from({ length: 501 }, (_, index) => ({
      word_id: `w${String(index)}`,
      text: 'a',
    }));

    expect(buildPlan('transcripts.correct', { id: ID, edits: [edit] }).body).toEqual({
      edits: [edit],
    });
    expect(buildPlan('transcripts.correct', { id: ID, edits: max }).body).toEqual({ edits: max });
    expect(() => buildPlan('transcripts.correct', { id: ID, edits: [] })).toThrow(ParamError);
    expect(() => buildPlan('transcripts.correct', { id: ID, edits: over })).toThrow(ParamError);
  });

  it('transcripts.correct: each edit names exactly one of word_id and segment_id, neither or both is refused', () => {
    expect(() => buildPlan('transcripts.correct', { id: ID, edits: [{ text: 'a' }] })).toThrow(
      ParamError,
    );
    expect(() =>
      buildPlan('transcripts.correct', {
        id: ID,
        edits: [{ word_id: 'w1', segment_id: 's1', text: 'a' }],
      }),
    ).toThrow(ParamError);
    expect(() =>
      buildPlan('transcripts.correct', {
        id: ID,
        edits: [{ word_id: null, segment_id: null, text: 'a' }],
      }),
    ).toThrow(ParamError);
  });

  it('transcripts.correct: an undefined id counts as absent, so the other id is the one sent', () => {
    const byWord = buildPlan('transcripts.correct', {
      id: ID,
      edits: [{ word_id: 'w1', segment_id: undefined, text: 'a' }],
    });
    const bySegment = buildPlan('transcripts.correct', {
      id: ID,
      edits: [{ word_id: undefined, segment_id: 's1', text: 'a' }],
    });

    expect(JSON.stringify(byWord.body)).toBe('{"edits":[{"word_id":"w1","text":"a"}]}');
    expect(JSON.stringify(bySegment.body)).toBe('{"edits":[{"segment_id":"s1","text":"a"}]}');
  });

  it('transcripts.correct: a hole, a non-object or any key besides the three is refused', () => {
    const holey: unknown[] = [edit];
    holey.length = 2;

    expect(() => buildPlan('transcripts.correct', { id: ID, edits: holey })).toThrow(ParamError);
    expect(() => buildPlan('transcripts.correct', { id: ID, edits: [edit, 'w2'] })).toThrow(
      ParamError,
    );
    for (const extra of [
      { start_ms: 0 },
      { end_ms: 1 },
      { ordinal: 2 },
      { speaker_local: 'S1' },
      { confidence: 1 },
      { chunk_id: 'c1' },
    ]) {
      expect(() => buildPlan('transcripts.correct', { id: ID, edits: [{ ...edit, ...extra }] })).toThrow(
        ParamError,
      );
    }
  });

  it('transcripts.correct: the text is sent as typed, and a blank text is refused', () => {
    expect(
      buildPlan('transcripts.correct', { id: ID, edits: [{ word_id: 'w1', text: '  Привіт  ' }] })
        .body,
    ).toEqual({ edits: [{ word_id: 'w1', text: '  Привіт  ' }] });
    for (const text of ['', ' ', '\n\t ']) {
      expect(() =>
        buildPlan('transcripts.correct', { id: ID, edits: [{ word_id: 'w1', text }] }),
      ).toThrow(ParamError);
    }
    expect(() =>
      buildPlan('transcripts.correct', { id: ID, edits: [{ word_id: 'w1', text: 5 }] }),
    ).toThrow(ParamError);
    // A segment text can be 2000 characters; the length itself is the backend's to check.
    expect(
      buildPlan('transcripts.correct', {
        id: ID,
        edits: [{ segment_id: 's1', text: 'a'.repeat(2000) }],
      }).body,
    ).toEqual({ edits: [{ segment_id: 's1', text: 'a'.repeat(2000) }] });
  });

  it('transcripts.correct: word and segment IDs are the document’s own, not uuids, up to 2000 characters', () => {
    for (const id of ['w1', 's1', 'w-0001', 'ж'.repeat(2000), '🎬'.repeat(2000)]) {
      expect(buildPlan('transcripts.correct', { id: ID, edits: [{ word_id: id, text: 'a' }] }).body)
        .toEqual({ edits: [{ word_id: id, text: 'a' }] });
    }
    for (const id of ['', 'a'.repeat(2001), '🎬'.repeat(2001), 5, null, ['w1'], { toString: () => 'w1' }]) {
      expect(() => buildPlan('transcripts.correct', { id: ID, edits: [{ word_id: id, text: 'a' }] })).toThrow(
        ParamError,
      );
      expect(() =>
        buildPlan('transcripts.correct', { id: ID, edits: [{ segment_id: id, text: 'a' }] }),
      ).toThrow(ParamError);
    }
  });
});

describe('buildPlan: credentials.check (ADR 039)', () => {
  it('posts with no params and no body, and the path holds no key', () => {
    const plan = buildPlan('credentials.check', {});

    expect(plan).toEqual({ method: 'POST', path: '/v1/runtime/credentials/check' });
    expect(plan.body).toBeUndefined();
  });

  it('refuses a key or any other param, so no key can ride along', () => {
    for (const params of [
      { openrouter_api_key: 'k'.repeat(30) },
      { key: 'k'.repeat(30) },
      { path: 'C:\\keys.txt' },
      { id: ID },
    ]) {
      expect(() => buildPlan('credentials.check', params)).toThrow(ParamError);
    }
  });
});

describe('buildPlan: jobs.resume budget_usd (ADR 038, доповнення 2)', () => {
  it.each(['0.01', '1', '2.50', '999.999999', '1000', '1000.000000'])(
    'accepts %s as the run ceiling and sends it as typed',
    (budget) => {
      expect(buildPlan('jobs.resume', { id: ID, budget_usd: budget }).body).toEqual({
        budget_usd: budget,
      });
    },
  );

  it.each(['0', '0.00', '0.009999', '1000.000001', '1000.0000001', '1001', '01', '1.', '.5', '+5', '-5', '1e2', ' 5', '5 ', '5,00', 'NaN', 'Infinity', ''])(
    'refuses %j, the settings rule of run_budget_usd',
    (budget) => {
      expect(() => buildPlan('jobs.resume', { id: ID, budget_usd: budget })).toThrow(ParamError);
    },
  );

  // One-element rows: vitest would spread an array given as a bare case into the arguments.
  it.each([[null], [5], [2.5], [true], [[]], [{}]])(
    'refuses %j: a number or null is not a budget, an absent one is a missing key',
    (budget) => {
      expect(() => buildPlan('jobs.resume', { id: ID, budget_usd: budget })).toThrow(ParamError);
    },
  );

  it('sends the decision and the budget together, and an undefined budget as absent', () => {
    expect(
      buildPlan('jobs.resume', { id: ID, retry_unknown: false, budget_usd: '2.5' }).body,
    ).toEqual({ retry_unknown: false, budget_usd: '2.5' });
    expect(buildPlan('jobs.resume', { id: ID, budget_usd: undefined }).body).toEqual({});
    expect(buildPlan('jobs.resume', { id: ID, budget_usd: '5' }).path).toBe(
      `/v1/jobs/${ID}/resume`,
    );
  });
});

describe('parseCall', () => {
  it('accepts op with and without params', () => {
    expect(parseCall({ op: 'projects.list', params: { limit: 1 } })).toEqual({
      op: 'projects.list',
      params: { limit: 1 },
    });
    expect(parseCall({ op: 'projects.list' })).toEqual({ op: 'projects.list', params: {} });
  });

  it.each([
    undefined,
    null,
    'projects.list',
    [],
    {},
    { params: {} },
    { op: 'projects.list', params: {}, path: '/v1/health' },
  ])('rejects %j', (payload) => {
    expect(() => parseCall(payload)).toThrow(ParamError);
  });
});

describe('parseOpenFolderParams', () => {
  it('takes only a uuid4 batch_id', () => {
    expect(parseOpenFolderParams({ batch_id: ID })).toEqual({ batch_id: ID });
    for (const bad of [
      { batch_id: 'x' },
      { batch_id: ID.toUpperCase() },
      { batch_id: ID, path: 'C:\\Exports' },
      { batch_id: ID, output_dir: 'C:\\Exports' },
      { id: ID },
      {},
      'C:\\Exports',
      null,
      undefined,
    ]) {
      expect(() => parseOpenFolderParams(bad)).toThrow(ParamError);
    }
  });
});

describe('parseImportParams', () => {
  it('takes only a uuid4 project_id', () => {
    expect(parseImportParams({ project_id: ID })).toEqual({ project_id: ID });
    expect(() => parseImportParams({ project_id: 'x' })).toThrow(ParamError);
    expect(() => parseImportParams({ project_id: ID, path: 'C:\\v.mp4' })).toThrow(ParamError);
    expect(() => parseImportParams({})).toThrow(ParamError);
    expect(() => parseImportParams(undefined)).toThrow(ParamError);
  });

  it('takes the project_id with a video token as its second form (ADR 024)', () => {
    expect(parseImportParams({ project_id: ID, file_token: TOKEN })).toEqual({
      project_id: ID,
      file_token: TOKEN,
    });
  });

  it('counts an undefined file_token as absent, so it is the first form', () => {
    const parsed = parseImportParams({ project_id: ID, file_token: undefined });

    expect(parsed).toEqual({ project_id: ID });
    expect(Object.hasOwn(parsed, 'file_token')).toBe(false);
  });

  it('rejects a bad token, a bad project_id and any key besides these two', () => {
    for (const bad of [
      { project_id: ID, file_token: 'x' },
      { project_id: ID, file_token: '' },
      { project_id: ID, file_token: null },
      { project_id: ID, file_token: 7 },
      { project_id: ID, file_token: TOKEN.toUpperCase() },
      { project_id: ID, file_token: TOKEN.slice(1) },
      { project_id: ID, file_token: `${TOKEN}0` },
      { project_id: ID, file_token: 'C:\\Videos\\a.mp4' },
      { project_id: 'x', file_token: TOKEN },
      { file_token: TOKEN },
      { project_id: ID, file_token: TOKEN, path: 'C:\\Videos\\a.mp4' },
      { project_id: ID, file_token: TOKEN, filePath: 'C:\\Videos\\a.mp4' },
      { project_id: ID, file_token: TOKEN, token: TOKEN },
      [{ project_id: ID, file_token: TOKEN }],
    ]) {
      expect(() => parseImportParams(bad)).toThrow(ParamError);
    }
  });
});

describe('parseDropParams', () => {
  it('takes exactly { path } and leaves the path itself to the gateway', () => {
    expect(parseDropParams({ path: 'C:\\Videos\\a.mp4' })).toEqual({ path: 'C:\\Videos\\a.mp4' });
    expect(parseDropParams({ path: 5 })).toEqual({ path: 5 });
    expect(parseDropParams({ path: null })).toEqual({ path: null });
    expect(parseDropParams({ path: '' })).toEqual({ path: '' });
  });

  it('rejects any other shape', () => {
    for (const bad of [
      {},
      { path: undefined },
      { path: 'C:\\a.mp4', extra: true },
      { path: 'C:\\a.mp4', project_id: ID },
      { file: 'C:\\a.mp4' },
      'C:\\a.mp4',
      ['C:\\a.mp4'],
      null,
      undefined,
    ]) {
      expect(() => parseDropParams(bad)).toThrow(ParamError);
    }
  });
});
