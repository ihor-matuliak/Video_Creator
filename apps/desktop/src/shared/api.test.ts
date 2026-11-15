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
