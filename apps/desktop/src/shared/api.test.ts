import { describe, expect, it } from 'vitest';

import {
  API_DATA_PARSERS,
  API_OPS,
  isApiOp,
  isFolderToken,
  isUuid4,
  isVideoToken,
  parseBackendExportBatch,
  parseClip,
  parseExportBatch,
  parseJob,
  parsePage,
  parseProject,
  parseProjectListItem,
  parseSource,
  parseSourceFields,
} from './api';

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
  audio_streams: [
    { index: 1, codec: 'aac', channels: 2, language: 'uk', title: null, default: true, start_offset_ms: 0 },
  ],
  availability: 'available',
  hash_status: 'pending',
  hash_job_id: null,
  created_at: '2026-11-14T10:00:00Z',
  playback: { direct: true, reason: null, direct_audio_stream_index: 1 },
  video: { width: 320, height: 180, frame_rate: '30/1', vfr: false },
};
const PREVIEW = {
  id: ID,
  source_id: ID,
  audio_stream_index: 1,
  status: 'failed',
  job_id: ID,
  error: { code: 'PREVIEW_FAILED', message: 'Не вдалося' },
};
const CLIP = {
  id: ID,
  source_id: ID,
  analysis_run_id: null,
  revision: 1,
  title: 'Мій фрагмент',
  start_ms: 0,
  end_ms: 1000,
  recommended_start_ms: null,
  recommended_end_ms: null,
  categories: ['funny'],
  evidence_type: 'manual',
  evidence_ids: [],
  reason: null,
  scores: { funny: 7 },
  rank: null,
  visual_review_required: false,
  review_status: 'suggested',
  export_state: 'none',
  created_at: '2026-11-14T10:00:00Z',
  updated_at: '2026-11-14T10:00:00Z',
  analysis_stale: false,
};

const ESTIMATE = {
  source_id: ID,
  audio_stream_index: 1,
  model: 'microsoft/mai-transcribe-2',
  language: 'uk',
  duration_ms: 125_000,
  chunk_count: 1,
  billed_seconds: 125,
  usd_per_second_upper: '0.0001',
  estimate_micro_usd: 12_500,
  price_basis: 'upper_bound_per_second',
  budget_usd: '2.50',
  within_budget: true,
  existing: { transcript_id: ID, run_id: ID },
};
const RUN_FIELDS = {
  id: ID,
  source_id: ID,
  job_id: ID,
  model: 'microsoft/mai-transcribe-2',
  status: 'partial',
  revision_id: null,
  error_code: 'TRANSCRIPTION_PARTIAL',
  committed_micro_usd: 12_500,
};
const RUN = {
  ...RUN_FIELDS,
  chunks: [
    {
      id: ID,
      core_start_ms: 0,
      core_end_ms: 60_000,
      state: 'succeeded',
      attempts: 1,
      error_code: null,
    },
    {
      id: '0a9d7e6c-1b2f-4c3d-8e4f-5a6b7c8d9e0f',
      core_start_ms: 60_000,
      core_end_ms: 125_000,
      state: 'unknown_outcome',
      attempts: 1,
      error_code: 'STT_UNKNOWN_OUTCOME',
    },
  ],
};

describe('ids and tokens', () => {
  it('isUuid4 accepts only lowercase uuid4', () => {
    expect(isUuid4(ID)).toBe(true);
    for (const bad of [ID.toUpperCase(), `${ID} `, ID.slice(1), '', 7, null, undefined, '../x']) {
      expect(isUuid4(bad)).toBe(false);
    }
  });

  it('isFolderToken accepts only 64 lowercase hex characters', () => {
    expect(isFolderToken('0f'.repeat(32))).toBe(true);
    for (const bad of ['0F'.repeat(32), '0f'.repeat(31), '0f'.repeat(33), 'g'.repeat(64), 5]) {
      expect(isFolderToken(bad)).toBe(false);
    }
  });

  it('isVideoToken has the shape of a folder token', () => {
    expect(isVideoToken('0f'.repeat(32))).toBe(true);
    for (const bad of ['0F'.repeat(32), '0f'.repeat(31), '0f'.repeat(33), 'g'.repeat(64), 5, null, undefined]) {
      expect(isVideoToken(bad)).toBe(false);
    }
  });

  it('isApiOp accepts exactly the allowlist', () => {
    for (const op of API_OPS) {
      expect(isApiOp(op)).toBe(true);
    }
    for (const bad of ['constructor', '__proto__', 'projects.delete', '', 1, null]) {
      expect(isApiOp(bad)).toBe(false);
    }
  });

  it('has a data parser for every op and nothing else', () => {
    expect(Object.keys(API_DATA_PARSERS).sort()).toEqual([...API_OPS].sort());
  });
});

describe('response parsers', () => {
  it('parseProject drops unknown keys such as the PATCH `archived` flag', () => {
    expect(parseProject({ ...PROJECT, archived: false, extra: 1 })).toEqual(PROJECT);
  });

  it('parseSource keeps display data and drops path, probe and origin', () => {
    const parsed = parseSource({
      ...SOURCE,
      path: 'C:\\Videos\\color_gop.mp4',
      probe: { format: {} },
      canonical_origin: {},
      sha256: 'abc',
    });

    expect(parsed).toEqual(SOURCE);
  });

  it('parseSource requires playback and keeps a proxy reason', () => {
    const withoutPlayback = { ...SOURCE, playback: undefined };
    const proxy = { direct: false, reason: 'CONTAINER_UNSUPPORTED', direct_audio_stream_index: null };

    expect(() => parseSource(withoutPlayback)).toThrow(/поле playback$/);
    expect(parseSource({ ...SOURCE, playback: proxy }).playback).toEqual(proxy);
    expect(parseSource({ ...SOURCE, playback: { ...proxy, extra: 1 } }).playback).toEqual(proxy);
  });

  it('parseSource requires video and keeps a field that is null', () => {
    const unknown = { width: null, height: null, frame_rate: null, vfr: null };

    expect(() => parseSource({ ...SOURCE, video: undefined })).toThrow(/поле video$/);
    expect(() => parseSource({ ...SOURCE, video: { width: 320, height: 180 } })).toThrow(
      /поле video$/,
    );
    expect(parseSource({ ...SOURCE, video: unknown }).video).toEqual(unknown);
    expect(
      parseSource({ ...SOURCE, video: { ...SOURCE.video, vfr: true, extra: 1 } }).video,
    ).toEqual({ ...SOURCE.video, vfr: true });
  });

  it('parseSourceFields does not look at playback or video', () => {
    const fields = parseSourceFields({ ...SOURCE, playback: undefined, video: undefined });

    expect(fields).not.toHaveProperty('playback');
    expect(fields).not.toHaveProperty('video');
    expect(parseSourceFields(SOURCE)).toEqual(fields);
  });

  it('parsePreviewCreated / parsePreview keep their fields and drop the rest', () => {
    const created = { id: ID, job_id: ID, status: 'queued' };

    expect(API_DATA_PARSERS['previews.create']({ ...created, path: 'C:\\x' })).toEqual(created);
    expect(API_DATA_PARSERS['previews.get']({ ...PREVIEW, path: 'C:\\x', file: 'p.mp4' })).toEqual(
      PREVIEW,
    );
    expect(API_DATA_PARSERS['previews.get']({ ...PREVIEW, audio_stream_index: null, error: null })).toMatchObject({
      audio_stream_index: null,
      error: null,
    });
  });

  it('system.checks keeps the three checks and drops every other key, a path included', () => {
    const checks = {
      ffmpeg: { ok: true, version: '8.0.1' },
      ffprobe: { ok: false, version: null },
      data_dir: { ok: true, free_bytes: 1_000_000 },
    };
    const parse = API_DATA_PARSERS['system.checks'];

    expect(
      parse({
        ffmpeg: { ...checks.ffmpeg, path: 'C:\\ffmpeg.exe' },
        ffprobe: checks.ffprobe,
        data_dir: { ...checks.data_dir, path: 'C:\\Data' },
        extra: 1,
      }),
    ).toEqual(checks);
    expect(parse({ ...checks, data_dir: { ok: false, free_bytes: null } })).toMatchObject({
      data_dir: { ok: false, free_bytes: null },
    });
  });

  it('settings.get and settings.update keep the five fields and drop every other key', () => {
    const settings = {
      stt_model: 'microsoft/mai-transcribe-2',
      stt_language: 'uk',
      analysis_model: null,
      run_budget_usd: '2.50',
      has_api_key: true,
    };

    for (const op of ['settings.get', 'settings.update'] as const) {
      expect(
        API_DATA_PARSERS[op]({ ...settings, openrouter_api_key: 'k'.repeat(30), extra: 1 }),
      ).toEqual(settings);
    }
  });

  it('settings refuses a missing field and a wrong type', () => {
    const settings = {
      stt_model: 'microsoft/mai-transcribe-2',
      stt_language: null,
      analysis_model: null,
      run_budget_usd: null,
      has_api_key: false,
    };
    const parse = API_DATA_PARSERS['settings.get'];

    for (const bad of [
      null,
      [],
      {},
      { ...settings, stt_model: null },
      { ...settings, stt_language: 5 },
      { ...settings, run_budget_usd: 2.5 },
      { ...settings, has_api_key: 'yes' },
      { ...settings, has_api_key: undefined },
    ]) {
      expect(() => parse(bad)).toThrow(TypeError);
    }
  });

  it('system.checks refuses a missing check, a wrong type and a negative or fractional free space', () => {
    const checks = {
      ffmpeg: { ok: true, version: '8.0.1' },
      ffprobe: { ok: true, version: '8.0.1' },
      data_dir: { ok: true, free_bytes: 5 },
    };
    const parse = API_DATA_PARSERS['system.checks'];

    for (const bad of [
      null,
      [],
      {},
      { ffmpeg: checks.ffmpeg, ffprobe: checks.ffprobe },
      { ...checks, ffmpeg: { version: '8.0.1' } },
      { ...checks, ffmpeg: { ok: 1, version: '8.0.1' } },
      { ...checks, ffprobe: { ok: true } },
      { ...checks, ffprobe: { ok: true, version: 8 } },
      { ...checks, data_dir: { ok: true } },
      { ...checks, data_dir: { ok: true, free_bytes: -1 } },
      { ...checks, data_dir: { ok: true, free_bytes: 0.5 } },
      { ...checks, data_dir: { ok: true, free_bytes: '5' } },
    ]) {
      expect(() => parse(bad)).toThrow(TypeError);
    }
  });

  it('transcription.estimate keeps its thirteen fields and drops every other key', () => {
    const parse = API_DATA_PARSERS['transcription.estimate'];

    expect(parse({ ...ESTIMATE, path: 'C:\\x', openrouter_api_key: 'k'.repeat(30) })).toEqual(
      ESTIMATE,
    );
    expect(parse({ ...ESTIMATE, existing: { ...ESTIMATE.existing, extra: 1 } })).toEqual(ESTIMATE);
  });

  it('transcription.estimate keeps the nulls of a run without a language, a budget or a repeat', () => {
    const open = {
      ...ESTIMATE,
      language: null,
      budget_usd: null,
      within_budget: null,
      existing: null,
    };

    expect(API_DATA_PARSERS['transcription.estimate'](open)).toEqual(open);
    expect(
      API_DATA_PARSERS['transcription.estimate']({ ...ESTIMATE, within_budget: false }),
    ).toMatchObject({ within_budget: false });
  });

  it('transcription.estimate refuses a missing field and a wrong type', () => {
    const parse = API_DATA_PARSERS['transcription.estimate'];

    for (const bad of [
      null,
      [],
      {},
      { ...ESTIMATE, source_id: undefined },
      { ...ESTIMATE, audio_stream_index: '1' },
      { ...ESTIMATE, audio_stream_index: 1.5 },
      { ...ESTIMATE, model: null },
      { ...ESTIMATE, language: 5 },
      { ...ESTIMATE, duration_ms: '12000' },
      { ...ESTIMATE, duration_ms: -1 },
      { ...ESTIMATE, chunk_count: 1.5 },
      { ...ESTIMATE, billed_seconds: null },
      { ...ESTIMATE, usd_per_second_upper: 0.0001 },
      { ...ESTIMATE, estimate_micro_usd: '1200' },
      { ...ESTIMATE, estimate_micro_usd: -5 },
      { ...ESTIMATE, price_basis: undefined },
      { ...ESTIMATE, budget_usd: 2.5 },
      { ...ESTIMATE, within_budget: 'yes' },
      { ...ESTIMATE, within_budget: undefined },
      { ...ESTIMATE, existing: undefined },
      { ...ESTIMATE, existing: {} },
      { ...ESTIMATE, existing: { transcript_id: ID } },
      { ...ESTIMATE, existing: { transcript_id: ID, run_id: 7 } },
      { ...ESTIMATE, existing: [] },
    ]) {
      expect(() => parse(bad)).toThrow(TypeError);
    }
    expect(() => parse({ ...ESTIMATE, existing: { transcript_id: ID } })).toThrow(/поле existing$/);
  });

  it('transcription.create keeps the id of the run and of its job', () => {
    const created = { id: ID, job_id: ID, status: 'queued' };
    const parse = API_DATA_PARSERS['transcription.create'];

    expect(parse({ ...created, source_id: ID, extra: 1 })).toEqual(created);
    for (const bad of [
      null,
      {},
      { id: ID },
      { ...created, job_id: null },
      { ...created, job_id: 5 },
      { ...created, status: undefined },
      { ...created, id: 1 },
    ]) {
      expect(() => parse(bad)).toThrow(TypeError);
    }
  });

  it('transcription.get keeps the run with its parts and drops every other key', () => {
    const parse = API_DATA_PARSERS['transcription.get'];

    expect(
      parse({
        ...RUN,
        created_at: '2026-11-16T10:00:00Z',
        chunks: RUN.chunks.map((chunk) => ({ ...chunk, response_path: 'C:\\x' })),
      }),
    ).toEqual(RUN);
    expect(parse({ ...RUN, job_id: null, revision_id: null, error_code: null })).toMatchObject({
      job_id: null,
      revision_id: null,
      error_code: null,
    });
    expect(parse({ ...RUN, chunks: [] }).chunks).toEqual([]);
  });

  it('transcription.get refuses a missing field and a wrong type, in the run or in a part', () => {
    const parse = API_DATA_PARSERS['transcription.get'];
    const chunk = RUN.chunks[0];

    for (const bad of [
      null,
      [],
      {},
      { ...RUN, id: 5 },
      { ...RUN, source_id: undefined },
      { ...RUN, job_id: undefined },
      { ...RUN, job_id: 7 },
      { ...RUN, model: null },
      { ...RUN, status: 1 },
      { ...RUN, revision_id: 7 },
      { ...RUN, error_code: 7 },
      { ...RUN, committed_micro_usd: '0' },
      { ...RUN, committed_micro_usd: 1.5 },
      { ...RUN, committed_micro_usd: -1 },
      { ...RUN, chunks: undefined },
      { ...RUN, chunks: null },
      { ...RUN, chunks: 'a' },
      { ...RUN, chunks: [null] },
      { ...RUN, chunks: [{ ...chunk, id: 1 }] },
      { ...RUN, chunks: [{ ...chunk, core_start_ms: '0' }] },
      { ...RUN, chunks: [{ ...chunk, core_end_ms: 1.5 }] },
      { ...RUN, chunks: [{ ...chunk, state: null }] },
      { ...RUN, chunks: [{ ...chunk, attempts: '1' }] },
      { ...RUN, chunks: [{ ...chunk, attempts: -1 }] },
      { ...RUN, chunks: [{ ...chunk, error_code: 5 }] },
      { ...RUN, chunks: [{ ...chunk, error_code: undefined }] },
    ]) {
      expect(() => parse(bad)).toThrow(TypeError);
    }
    expect(() => parse({ ...RUN, committed_micro_usd: '0' })).toThrow(/поле committed_micro_usd$/);
    expect(() => parse({ ...RUN, chunks: [{ ...chunk, attempts: '1' }] })).toThrow(/поле chunks$/);
  });

  it('transcription.list keeps the runs without their parts, newest first as the backend sent them', () => {
    const older = {
      ...RUN_FIELDS,
      id: '0a9d7e6c-1b2f-4c3d-8e4f-5a6b7c8d9e0f',
      created_at: '2026-11-15T10:00:00Z',
    };
    const newer = { ...RUN_FIELDS, created_at: '2026-11-16T10:00:00Z' };
    const parse = API_DATA_PARSERS['transcription.list'];

    expect(parse({ items: [newer, older], extra: 1 })).toEqual({ items: [newer, older] });
    expect(parse({ items: [{ ...newer, chunks: RUN.chunks }] })).toEqual({ items: [newer] });
    expect(parse({ items: [] })).toEqual({ items: [] });
  });

  it('transcription.list refuses an answer that is not {items} of runs, with created_at', () => {
    const item = { ...RUN_FIELDS, created_at: '2026-11-16T10:00:00Z' };
    const parse = API_DATA_PARSERS['transcription.list'];

    for (const bad of [
      null,
      [],
      {},
      { items: null },
      { items: {} },
      { items: [null] },
      { items: [RUN_FIELDS] },
      { items: [{ ...item, created_at: 5 }] },
      { items: [{ ...item, model: undefined }] },
      { items: [{ ...item, committed_micro_usd: '0' }] },
      { items: [{ ...item, revision_id: 7 }] },
    ]) {
      expect(() => parse(bad)).toThrow(TypeError);
    }
  });

  it('jobs.resume answers with the job, like jobs.cancel', () => {
    const job = { id: ID, type: 'transcription', state: 'queued', progress: {}, error: null };
    const parse = API_DATA_PARSERS['jobs.resume'];

    expect(parse({ ...job, lease_owner: 'x' })).toEqual(job);
    expect(parse({ ...job, state: 'failed', error: { code: 'STT_FUNDS', message: 'Баланс' } })).toMatchObject({
      error: { code: 'STT_FUNDS', message: 'Баланс' },
    });
    for (const bad of [null, {}, { ...job, progress: 5 }, { ...job, error: { code: 'X' } }, { ...job, state: 1 }]) {
      expect(() => parse(bad)).toThrow(TypeError);
    }
  });

  it('parseProjectListItem keeps the figures of the list and drops unknown keys', () => {
    const item = { ...PROJECT, source_count: 2, clip_count: 0, last_activity_at: '2026-11-14T11:30:00Z' };

    expect(parseProjectListItem({ ...item, archived: false, extra: 1 })).toEqual(item);
    expect(API_DATA_PARSERS['projects.list']({ items: [item], next_cursor: null })).toEqual({
      items: [item],
      next_cursor: null,
    });
  });

  it('projects.list requires the figures, as whole non-negative numbers', () => {
    const item = { ...PROJECT, source_count: 2, clip_count: 0, last_activity_at: '2026-11-14T11:30:00Z' };
    const parse = API_DATA_PARSERS['projects.list'];

    for (const broken of [
      PROJECT,
      { ...item, source_count: undefined },
      { ...item, clip_count: -1 },
      { ...item, source_count: 1.5 },
      { ...item, clip_count: '3' },
      { ...item, last_activity_at: null },
    ]) {
      expect(() => parse({ items: [broken], next_cursor: null })).toThrow(TypeError);
    }
    expect(() => parseProjectListItem({ ...item, clip_count: '3' })).toThrow(/поле clip_count$/);
  });

  it('parseClip keeps scores and nullable fields', () => {
    expect(parseClip(CLIP)).toEqual(CLIP);
    expect(parseClip({ ...CLIP, scores: null }).scores).toBeNull();
  });

  it('parseClip requires analysis_stale as a boolean and keeps its value (ADR 041 §11)', () => {
    const withoutStale = Object.fromEntries(
      Object.entries(CLIP).filter(([key]) => key !== 'analysis_stale'),
    );

    expect(parseClip({ ...CLIP, analysis_stale: true }).analysis_stale).toBe(true);
    expect(parseClip(CLIP).analysis_stale).toBe(false);
    expect(() => parseClip(withoutStale)).toThrow(/поле analysis_stale$/);
    for (const stale of ['false', 0, null, undefined]) {
      expect(() => parseClip({ ...CLIP, analysis_stale: stale })).toThrow(/поле analysis_stale$/);
    }
  });

  it('parsePage parses every item', () => {
    expect(parsePage(parseProject)({ items: [PROJECT], next_cursor: 'c' })).toEqual({
      items: [PROJECT],
      next_cursor: 'c',
    });
    expect(() => parsePage(parseProject)({ items: [{ id: 1 }], next_cursor: null })).toThrow(
      TypeError,
    );
  });

  const EXPORT_BATCH = {
    id: ID,
    project_id: ID,
    job_id: null,
    status: 'partial_failed',
    output_dir: 'C:\\Exports',
    items: [
      {
        id: ID,
        clip_id: ID,
        clip_revision: 2,
        state: 'failed',
        final_path: null,
        duration_ms: null,
        error: { code: 'OUTPUT_INVALID', message: 'Перевірка не пройшла' },
      },
    ],
    manifest_path: null,
    catalog_path: null,
    created_at: '2026-11-14T10:00:00Z',
  };

  it('parseExportBatch keeps item errors and durations and drops unknown keys', () => {
    expect(
      parseExportBatch({
        ...EXPORT_BATCH,
        profile: {},
        items: EXPORT_BATCH.items.map((item) => ({ ...item, observed: {} })),
      }),
    ).toEqual(EXPORT_BATCH);
    expect(
      parseExportBatch({
        ...EXPORT_BATCH,
        items: [{ ...EXPORT_BATCH.items[0], duration_ms: 3760 }],
      }).items[0]?.duration_ms,
    ).toBe(3760);
  });

  it('parseBackendExportBatch turns observed.observed_duration_ms into duration_ms and drops the dump', () => {
    const backend = (observed: unknown): unknown => ({
      ...EXPORT_BATCH,
      profile: { quality: 'high' },
      items: [{ ...EXPORT_BATCH.items[0], duration_ms: undefined, observed }],
    });

    expect(
      parseBackendExportBatch(
        backend({ observed_duration_ms: 3760, ffmpeg_version: '8.0', seek_mode: 'preseek' }),
      ),
    ).toEqual({ ...EXPORT_BATCH, items: [{ ...EXPORT_BATCH.items[0], duration_ms: 3760 }] });
    for (const observed of [null, {}, { observed_duration_ms: '3760' }, { observed_duration_ms: -1 }]) {
      expect(parseBackendExportBatch(backend(observed)).items[0]?.duration_ms).toBeNull();
    }
    expect(() => parseBackendExportBatch(backend(undefined))).toThrow(/поле items$/);
    expect(() => parseBackendExportBatch(backend('x'))).toThrow(TypeError);
  });

  it('exports.retry answers with the id of the new batch', () => {
    const created = { id: ID, job_id: ID, status: 'queued' };

    expect(API_DATA_PARSERS['exports.retry']({ ...created, output_dir: 'C:\\x' })).toEqual(created);
    expect(() => API_DATA_PARSERS['exports.retry']({ id: ID })).toThrow(TypeError);
  });

  it('parseJob keeps progress and the error', () => {
    const job = { id: ID, type: 'export', state: 'failed', progress: { done: 1 }, error: { code: 'X', message: 'Y' } };

    expect(parseJob({ ...job, lease_owner: 'x' })).toEqual(job);
  });

  it.each([
    [parseProject, { ...PROJECT, name: 5 }],
    [parseProject, { ...PROJECT, archived_at: undefined }],
    [parseProject, null],
    [parseProject, []],
    [parseSource, { ...SOURCE, size: 1.5 }],
    [parseSource, { ...SOURCE, audio_streams: [{ index: 1 }] }],
    [parseSource, { ...SOURCE, playback: null }],
    [parseSource, { ...SOURCE, playback: { direct: 'yes', reason: null, direct_audio_stream_index: 1 } }],
    [parseSource, { ...SOURCE, playback: { direct: true, reason: null } }],
    [parseSource, { ...SOURCE, video: null }],
    [parseSource, { ...SOURCE, video: { ...SOURCE.video, width: '320' } }],
    [parseSource, { ...SOURCE, video: { ...SOURCE.video, frame_rate: 30 } }],
    [parseSource, { ...SOURCE, video: { ...SOURCE.video, vfr: 'no' } }],
    [API_DATA_PARSERS['previews.create'], { id: ID, job_id: 5, status: 'queued' }],
    [API_DATA_PARSERS['previews.get'], { ...PREVIEW, audio_stream_index: '1' }],
    [API_DATA_PARSERS['previews.get'], { ...PREVIEW, error: { code: 'X' } }],
    [parseClip, { ...CLIP, scores: { funny: 'high' } }],
    [parseClip, { ...CLIP, categories: 'funny' }],
    [parseClip, { ...CLIP, revision: Number.NaN }],
    [parseClip, { ...CLIP, analysis_stale: undefined }],
    [parseClip, { ...CLIP, analysis_stale: 'false' }],
    [parseClip, { ...CLIP, analysis_stale: null }],
    [parseJob, { id: ID, type: 'x', state: 'y', progress: 5, error: null }],
  ] as const)('rejects a malformed value %#', (parse, value) => {
    expect(() => {
      parse(value);
    }).toThrow(TypeError);
  });

  it('names the failing field but never echoes its value', () => {
    expect(() => parseProject({ ...PROJECT, name: { secret: 'tok-123' } })).toThrow(
      /поле name$/,
    );
  });
});

describe('analysis parsers (ADR 041 §10, §21)', () => {
  const ESTIMATE_A = {
    model: 'test/selector',
    unit_level: 'word',
    unit_count: 240,
    window_count: 2,
    estimate_micro_usd: 3_600,
    prompt_usd_per_mtok: '1.00',
    completion_usd_per_mtok: '2.00',
    max_tokens: 4096,
    price_basis: 'test',
    budget_usd: '2.50',
    within_budget: true,
  };
  const CREATED_A = { id: ID, job_id: ID, status: 'queued' };
  const WINDOWS = { total: 2, succeeded: 1, failed: 0, unknown_outcome: 0, pending: 1 };
  const RUN_A = {
    id: ID,
    source_id: ID,
    transcript_id: ID,
    transcript_revision: 2,
    model: 'test/selector',
    provider: 'test',
    prompt_version: 'sha256:0123456789abcdef',
    schema_version: 1,
    unit_level: 'word',
    status: 'running',
    error_code: null,
    created_at: '2026-11-17T10:00:00Z',
    updated_at: '2026-11-17T10:05:00Z',
    job_id: ID,
    committed_micro_usd: 1_200,
    windows: WINDOWS,
    clip_ids: [ID],
    stale: false,
    diagnostics_summary: { dropped: { LOW_QUALITY: 2 }, suppressed: { already_reviewed: 1 } },
  };
  const ITEM_A = {
    id: ID,
    source_id: ID,
    transcript_id: ID,
    transcript_revision: 2,
    model: 'test/selector',
    status: 'completed',
    error_code: 'ANALYSIS_UNKNOWN_OUTCOME',
    job_id: ID,
    committed_micro_usd: 1_200,
    created_at: '2026-11-17T10:00:00Z',
  };

  it('analysis.estimate keeps its eleven fields and drops every other key', () => {
    const parse = API_DATA_PARSERS['analysis.estimate'];

    expect(parse({ ...ESTIMATE_A, profile: {}, openrouter_api_key: 'k'.repeat(30) })).toEqual(
      ESTIMATE_A,
    );
    expect(parse({ ...ESTIMATE_A, budget_usd: null, within_budget: null })).toEqual({
      ...ESTIMATE_A,
      budget_usd: null,
      within_budget: null,
    });
  });

  it('analysis.estimate refuses a missing field and a wrong type', () => {
    const parse = API_DATA_PARSERS['analysis.estimate'];

    for (const bad of [
      null,
      [],
      {},
      { ...ESTIMATE_A, model: undefined },
      { ...ESTIMATE_A, model: null },
      { ...ESTIMATE_A, unit_level: 5 },
      { ...ESTIMATE_A, unit_count: '240' },
      { ...ESTIMATE_A, unit_count: -1 },
      { ...ESTIMATE_A, window_count: 1.5 },
      { ...ESTIMATE_A, estimate_micro_usd: '3600' },
      { ...ESTIMATE_A, estimate_micro_usd: undefined },
      { ...ESTIMATE_A, prompt_usd_per_mtok: 1 },
      { ...ESTIMATE_A, completion_usd_per_mtok: null },
      { ...ESTIMATE_A, max_tokens: -4096 },
      { ...ESTIMATE_A, price_basis: undefined },
      { ...ESTIMATE_A, budget_usd: 2.5 },
      { ...ESTIMATE_A, budget_usd: undefined },
      { ...ESTIMATE_A, within_budget: 'yes' },
      { ...ESTIMATE_A, within_budget: undefined },
    ]) {
      expect(() => parse(bad)).toThrow(TypeError);
    }
    expect(() => parse({ ...ESTIMATE_A, budget_usd: 2.5 })).toThrow(/поле budget_usd$/);
  });

  it('analysis.create keeps the id of the run and of its job, and drops every other key', () => {
    const parse = API_DATA_PARSERS['analysis.create'];

    expect(parse({ ...CREATED_A, source_id: ID, extra: 1 })).toEqual(CREATED_A);
    for (const bad of [
      null,
      {},
      { id: ID },
      { ...CREATED_A, job_id: null },
      { ...CREATED_A, job_id: 5 },
      { ...CREATED_A, status: undefined },
      { ...CREATED_A, id: 1 },
    ]) {
      expect(() => parse(bad)).toThrow(TypeError);
    }
  });

  it('analysis.get keeps the run with its windows, clips and diagnostics, and drops every other key', () => {
    const parse = API_DATA_PARSERS['analysis.get'];

    expect(
      parse({
        ...RUN_A,
        openrouter_api_key: 'k'.repeat(30),
        windows: { ...WINDOWS, extra: 1 },
      }),
    ).toEqual(RUN_A);
    expect(parse({ ...RUN_A, error_code: null, job_id: null })).toMatchObject({
      error_code: null,
      job_id: null,
    });
    expect(parse({ ...RUN_A, stale: true, diagnostics_summary: {}, clip_ids: [] })).toMatchObject({
      stale: true,
      diagnostics_summary: {},
      clip_ids: [],
    });
  });

  it('analysis.get refuses a missing field and a wrong type, in the run, the windows or the diagnostics', () => {
    const parse = API_DATA_PARSERS['analysis.get'];

    for (const bad of [
      null,
      [],
      {},
      { ...RUN_A, transcript_revision: '2' },
      { ...RUN_A, transcript_revision: -1 },
      { ...RUN_A, schema_version: 1.5 },
      { ...RUN_A, provider: undefined },
      { ...RUN_A, status: null },
      { ...RUN_A, error_code: 5 },
      { ...RUN_A, error_code: undefined },
      { ...RUN_A, job_id: 7 },
      { ...RUN_A, committed_micro_usd: '1200' },
      { ...RUN_A, committed_micro_usd: -1 },
      { ...RUN_A, windows: undefined },
      { ...RUN_A, windows: null },
      { ...RUN_A, windows: { ...WINDOWS, pending: undefined } },
      { ...RUN_A, windows: { ...WINDOWS, total: '2' } },
      { ...RUN_A, windows: { ...WINDOWS, failed: -1 } },
      { ...RUN_A, clip_ids: 'x' },
      { ...RUN_A, clip_ids: [5] },
      { ...RUN_A, clip_ids: undefined },
      { ...RUN_A, stale: 'no' },
      { ...RUN_A, stale: undefined },
      { ...RUN_A, diagnostics_summary: undefined },
      { ...RUN_A, diagnostics_summary: null },
      { ...RUN_A, diagnostics_summary: [] },
      { ...RUN_A, diagnostics_summary: { dropped: 2 } },
      { ...RUN_A, diagnostics_summary: { dropped: null } },
      { ...RUN_A, diagnostics_summary: { dropped: { LOW_QUALITY: '2' } } },
      { ...RUN_A, diagnostics_summary: { dropped: { LOW_QUALITY: -1 } } },
      { ...RUN_A, diagnostics_summary: { dropped: { LOW_QUALITY: 1.5 } } },
      { ...RUN_A, diagnostics_summary: { dropped: { LOW_QUALITY: [1] } } },
      { ...RUN_A, created_at: 5 },
    ]) {
      expect(() => parse(bad)).toThrow(TypeError);
    }
    expect(() => parse({ ...RUN_A, windows: { ...WINDOWS, pending: undefined } })).toThrow(
      /поле windows$/,
    );
    expect(() =>
      parse({ ...RUN_A, diagnostics_summary: { dropped: { LOW_QUALITY: '2' } } }),
    ).toThrow(/поле diagnostics_summary$/);
  });

  it('analysis.list keeps the runs without their windows, clips and diagnostics, and drops every other key', () => {
    const parse = API_DATA_PARSERS['analysis.list'];

    expect(parse({ items: [{ ...ITEM_A, windows: WINDOWS, clip_ids: [] }], extra: 1 })).toEqual({
      items: [ITEM_A],
    });
    expect(parse({ items: [{ ...ITEM_A, error_code: null, job_id: null }] })).toEqual({
      items: [{ ...ITEM_A, error_code: null, job_id: null }],
    });
    expect(parse({ items: [] })).toEqual({ items: [] });
  });

  it('analysis.list refuses an answer that is not {items} of runs', () => {
    const parse = API_DATA_PARSERS['analysis.list'];

    for (const bad of [
      null,
      [],
      {},
      { items: null },
      { items: {} },
      { items: [null] },
      { items: [{ ...ITEM_A, created_at: 5 }] },
      { items: [{ ...ITEM_A, transcript_id: undefined }] },
      { items: [{ ...ITEM_A, transcript_revision: '2' }] },
      { items: [{ ...ITEM_A, committed_micro_usd: '1' }] },
      { items: [{ ...ITEM_A, status: 1 }] },
      { items: [{ ...ITEM_A, job_id: 7 }] },
      { items: [{ ...ITEM_A, error_code: undefined }] },
    ]) {
      expect(() => parse(bad)).toThrow(TypeError);
    }
  });
});

describe('transcript and credential parsers (ADR 030, 039)', () => {
  const REVISION = {
    id: ID,
    source_id: ID,
    revision: 2,
    origin: 'correction',
    run_id: null,
    parent_id: ID,
    timing_level: 'word',
    language: 'uk',
    duration_ms: 125_000,
    word_count: 2,
    segment_count: 1,
    coverage: [{ start_ms: 0, end_ms: 125_000, state: 'succeeded' }],
    created_at: '2026-11-16T10:00:00Z',
  };
  const WORD = {
    id: 'w1',
    ordinal: 0,
    start_ms: 0,
    end_ms: 400,
    text: 'Привіт',
    chunk_id: ID,
    speaker_local: null,
    confidence: 0.91,
  };
  const SEGMENT = {
    id: 's1',
    ordinal: 0,
    start_ms: 0,
    end_ms: 900,
    text: 'Привіт світ',
    word_ids: ['w1', 'w2'],
    timing_level: 'word',
  };
  const WINDOW = {
    ...REVISION,
    offset: 0,
    limit: 200,
    next_offset: null,
    segments: [SEGMENT],
    words: [WORD],
  };
  /** The backend's own dump of the revision: main must not keep it. */
  const METADATA = { metadata: { provider: 'openrouter', options_hash: 'secret-looking' } };

  it('transcripts.list keeps the revisions and the cursor, and drops metadata and unknown keys', () => {
    const parse = API_DATA_PARSERS['transcripts.list'];

    expect(
      parse({ items: [{ ...REVISION, ...METADATA, extra: 1 }], next_cursor: 'c2', path: 'C:\\x' }),
    ).toEqual({ items: [REVISION], next_cursor: 'c2' });
    expect(parse({ items: [], next_cursor: null })).toEqual({ items: [], next_cursor: null });
  });

  it('transcripts.correct answers the new revision without metadata', () => {
    const parse = API_DATA_PARSERS['transcripts.correct'];
    const parsed = parse({ ...REVISION, ...METADATA });

    expect(parsed).toEqual(REVISION);
    expect(Object.keys(parsed)).not.toContain('metadata');
    expect(parse({ ...REVISION, run_id: ID, parent_id: null })).toMatchObject({
      run_id: ID,
      parent_id: null,
    });
  });

  it('transcripts.get keeps the window: the revision, offset, limit, next_offset, segments and words', () => {
    const parse = API_DATA_PARSERS['transcripts.get'];

    expect(parse({ ...WINDOW, ...METADATA, words: [{ ...WORD, extra: 1 }] })).toEqual(WINDOW);
    expect(parse({ ...WINDOW, next_offset: 200 }).next_offset).toBe(200);
    expect(parse({ ...WINDOW, words: [{ ...WORD, confidence: null }] }).words[0]?.confidence).toBeNull();
    expect(parse({ ...WINDOW, segments: [], words: [] })).toMatchObject({ segments: [], words: [] });
  });

  it('transcripts refuses a missing field and a wrong type in the revision', () => {
    const parse = API_DATA_PARSERS['transcripts.correct'];

    for (const bad of [
      null,
      [],
      {},
      { ...REVISION, id: 5 },
      { ...REVISION, source_id: undefined },
      { ...REVISION, revision: '2' },
      { ...REVISION, revision: -1 },
      { ...REVISION, revision: 1.5 },
      { ...REVISION, origin: null },
      { ...REVISION, run_id: 7 },
      { ...REVISION, parent_id: undefined },
      { ...REVISION, language: null },
      { ...REVISION, duration_ms: '1' },
      { ...REVISION, word_count: undefined },
      { ...REVISION, segment_count: -1 },
      { ...REVISION, coverage: undefined },
      { ...REVISION, coverage: {} },
      { ...REVISION, coverage: [null] },
      { ...REVISION, coverage: [{ start_ms: 0, end_ms: 1 }] },
      { ...REVISION, coverage: [{ start_ms: '0', end_ms: 1, state: 'succeeded' }] },
      { ...REVISION, created_at: 5 },
    ]) {
      expect(() => parse(bad)).toThrow(TypeError);
    }
    expect(() => parse({ ...REVISION, word_count: undefined })).toThrow(/поле word_count$/);
  });

  it('transcripts.get refuses a missing field and a wrong type in the window, a segment or a word', () => {
    const parse = API_DATA_PARSERS['transcripts.get'];

    for (const bad of [
      { ...WINDOW, offset: undefined },
      { ...WINDOW, limit: '200' },
      { ...WINDOW, next_offset: 'x' },
      { ...WINDOW, segments: null },
      { ...WINDOW, segments: [{ ...SEGMENT, word_ids: 'w1' }] },
      { ...WINDOW, segments: [{ ...SEGMENT, word_ids: [1] }] },
      { ...WINDOW, segments: [{ ...SEGMENT, timing_level: undefined }] },
      { ...WINDOW, words: undefined },
      { ...WINDOW, words: [null] },
      { ...WINDOW, words: [{ ...WORD, confidence: '0.9' }] },
      { ...WINDOW, words: [{ ...WORD, confidence: Number.POSITIVE_INFINITY }] },
      { ...WINDOW, words: [{ ...WORD, confidence: undefined }] },
      { ...WINDOW, words: [{ ...WORD, chunk_id: undefined }] },
      { ...WINDOW, words: [{ ...WORD, speaker_local: 5 }] },
    ]) {
      expect(() => parse(bad)).toThrow(TypeError);
    }
    expect(() => parse({ ...WINDOW, offset: undefined })).toThrow(/поле offset$/);
  });

  it('transcripts.list refuses a page that is not {items, next_cursor} of revisions', () => {
    const parse = API_DATA_PARSERS['transcripts.list'];

    for (const bad of [
      null,
      [],
      {},
      { items: null, next_cursor: null },
      { items: [REVISION] },
      { items: [REVISION], next_cursor: 5 },
      { items: [null], next_cursor: null },
    ]) {
      expect(() => parse(bad)).toThrow(TypeError);
    }
  });

  it('credentials.check: valid keeps the free-tier flag and the remaining limit, and drops every other key', () => {
    const parse = API_DATA_PARSERS['credentials.check'];

    expect(
      parse({ state: 'valid', is_free_tier: false, limit_remaining_usd: '4.25', label: 'x', usage: 1 }),
    ).toEqual({ state: 'valid', is_free_tier: false, limit_remaining_usd: '4.25' });
    expect(parse({ state: 'valid', is_free_tier: true, limit_remaining_usd: null })).toEqual({
      state: 'valid',
      is_free_tier: true,
      limit_remaining_usd: null,
    });
    expect(parse({ state: 'valid', is_free_tier: false, limit_remaining_usd: '-0.5' })).toEqual({
      state: 'valid',
      is_free_tier: false,
      limit_remaining_usd: '-0.5',
    });
  });

  it('credentials.check: invalid keeps only its state', () => {
    expect(API_DATA_PARSERS['credentials.check']({ state: 'invalid', is_free_tier: true, limit_remaining_usd: '1', label: 'x' })).toEqual({
      state: 'invalid',
    });
  });

  it('credentials.check refuses an unknown state, a missing or wrong field, and a non-object', () => {
    const parse = API_DATA_PARSERS['credentials.check'];

    for (const bad of [
      null,
      [],
      {},
      { state: 'ok' },
      { state: null },
      { state: 'VALID', is_free_tier: false, limit_remaining_usd: null },
      { state: 'valid' },
      { state: 'valid', is_free_tier: 'no', limit_remaining_usd: null },
      { state: 'valid', is_free_tier: false },
      { state: 'valid', is_free_tier: false, limit_remaining_usd: 4.25 },
      { state: 'valid', is_free_tier: undefined, limit_remaining_usd: null },
    ]) {
      expect(() => parse(bad)).toThrow(TypeError);
    }
    expect(() => parse({ state: 'valid', is_free_tier: 'no', limit_remaining_usd: null })).toThrow(
      /поле is_free_tier$/,
    );
  });

  it('credentials.check accepts limit_remaining_usd as null or a plain decimal string, a minus sign included', () => {
    const parse = API_DATA_PARSERS['credentials.check'];

    for (const limit of ['4.25', '0', '5', '-5', '-0.5', '-0.01', null]) {
      expect(parse({ state: 'valid', is_free_tier: false, limit_remaining_usd: limit })).toEqual({
        state: 'valid',
        is_free_tier: false,
        limit_remaining_usd: limit,
      });
    }
  });

  it('credentials.check refuses limit_remaining_usd that is not null or a plain decimal string', () => {
    const parse = API_DATA_PARSERS['credentials.check'];

    for (const limit of ['1e-3', '1.5E2', ' 2.5', '2.', '.5', '', 'abc', '+1']) {
      expect(() => parse({ state: 'valid', is_free_tier: false, limit_remaining_usd: limit })).toThrow(
        TypeError,
      );
    }
    expect(() => parse({ state: 'valid', is_free_tier: false, limit_remaining_usd: '1e-3' })).toThrow(
      /поле limit_remaining_usd$/,
    );
  });
});
