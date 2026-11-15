import { describe, expect, it } from 'vitest';

import type { AudioStreamDto, ExportBatchDto, ExportItemDto, JobDto } from '../shared/api';
import {
  audioChoices,
  batchOutcome,
  countItems,
  fileName,
  isBatchFinished,
  profileFor,
  runningProgress,
  summarizeSelection,
} from './export-logic';
import { CLIP_ID, makeClip, makeSource } from './test-bridge';

const OTHER_ID = '6d8f0a2c-4e5a-4b7c-9d9e-0f1a2b3c4d5e';

function track(index: number, overrides: Partial<AudioStreamDto> = {}): AudioStreamDto {
  return {
    index,
    codec: 'aac',
    channels: 2,
    language: null,
    title: null,
    default: false,
    start_offset_ms: 0,
    ...overrides,
  };
}

function item(state: string): ExportItemDto {
  return {
    id: `item-${state}`,
    clip_id: CLIP_ID,
    clip_revision: 1,
    state,
    final_path: null,
    duration_ms: null,
    error: null,
  };
}

function batch(status: string, ...states: string[]): ExportBatchDto {
  return {
    id: 'b',
    project_id: 'p',
    job_id: 'j',
    status,
    output_dir: 'C:\\Exports',
    items: states.map(item),
    manifest_path: null,
    catalog_path: null,
    created_at: '2026-11-14T10:00:00Z',
  };
}

describe('summarizeSelection', () => {
  const clips = new Map(
    [
      makeClip({ id: CLIP_ID, review_status: 'approved' }),
      makeClip({ id: OTHER_ID, title: 'Другий', review_status: 'suggested' }),
    ].map((clip) => [clip.id, clip]),
  );

  it('splits the ticked clips into approved and skipped, in registry order', () => {
    const selection = summarizeSelection(new Set([OTHER_ID, CLIP_ID]), clips);

    expect(selection.selected).toBe(2);
    expect(selection.approved.map((clip) => clip.id)).toEqual([CLIP_ID]);
    expect(selection.skipped).toEqual([
      { id: OTHER_ID, title: 'Другий', sourceId: makeClip().source_id },
    ]);
  });

  it('ignores clips that are not ticked', () => {
    expect(summarizeSelection(new Set(), clips)).toEqual({ selected: 0, approved: [], skipped: [] });
  });

  it('counts a ticked clip the registry does not know as skipped', () => {
    const selection = summarizeSelection(new Set(['unknown']), clips);

    expect(selection.selected).toBe(1);
    expect(selection.approved).toEqual([]);
    expect(selection.skipped).toEqual([{ id: 'unknown', title: null, sourceId: null }]);
  });
});

describe('audioChoices', () => {
  const values = (sources: Parameters<typeof audioChoices>[0]): string[] =>
    audioChoices(sources).options.map((option) => option.value);

  it('offers only «Без звуку» when there is nothing to export or no sound', () => {
    expect(audioChoices([])).toEqual({
      options: [{ value: 'none', label: 'Без звуку' }],
      defaultValue: 'none',
    });
    expect(values([makeSource({ audio_streams: [] })])).toEqual(['none']);
  });

  it('offers each track, all tracks and none for one source, preferring the default track', () => {
    const source = makeSource({ audio_streams: [track(2), track(1, { default: false }), track(3, { default: true })] });

    expect(values([source])).toEqual(['track:1', 'track:2', 'track:3', 'all', 'none']);
    expect(audioChoices([source]).defaultValue).toBe('track:3');
  });

  it('falls back to the lowest track when none is marked default', () => {
    const source = makeSource({ audio_streams: [track(4), track(2)] });

    expect(audioChoices([source]).defaultValue).toBe('track:2');
  });

  it('offers a track only when every source has it', () => {
    const a = makeSource({ audio_streams: [track(1), track(2)] });
    const b = makeSource({ id: OTHER_ID, audio_streams: [track(2), track(3)] });

    expect(values([a, b])).toEqual(['track:2', 'all', 'none']);
  });

  it('defaults to all tracks when the sources share none, and when one has no audio', () => {
    const a = makeSource({ audio_streams: [track(1)] });
    const b = makeSource({ id: OTHER_ID, audio_streams: [track(2)] });
    const silent = makeSource({ id: OTHER_ID, audio_streams: [] });

    expect(values([a, b])).toEqual(['all', 'none']);
    expect(audioChoices([a, b]).defaultValue).toBe('all');
    expect(values([a, silent])).toEqual(['all', 'none']);
    expect(audioChoices([a, silent]).defaultValue).toBe('all');
  });

  it('labels a track with its index, language and title', () => {
    const source = makeSource({ audio_streams: [track(1, { language: 'uk', title: 'Гра' }), track(2)] });

    expect(audioChoices([source]).options.map((option) => option.label)).toEqual([
      'Доріжка 1 · uk · Гра',
      'Доріжка 2',
      'Усі доріжки',
      'Без звуку',
    ]);
  });
});

describe('profileFor', () => {
  const base = { video_codec: 'h264', quality: 'high', write_srt: false };

  it('maps every choice to a profile the backend accepts', () => {
    expect(profileFor('track:3')).toEqual({ ...base, audio_mode: 'selected', audio_stream_index: 3 });
    expect(profileFor('all')).toEqual({ ...base, audio_mode: 'all', audio_stream_index: null });
    expect(profileFor('none')).toEqual({ ...base, audio_mode: 'none', audio_stream_index: null });
  });
});

describe('batch state', () => {
  it('a batch is open only while queued or running', () => {
    expect(['queued', 'running'].map(isBatchFinished)).toEqual([false, false]);
    expect(
      ['succeeded', 'partial_failed', 'failed', 'cancelled', 'something-new'].map(isBatchFinished),
    ).toEqual([true, true, true, true, true]);
  });

  it('counts items by state, treating unknown states as open', () => {
    expect(countItems(batch('running', 'verified', 'verified', 'failed', 'cancelled', 'queued', 'odd').items)).toEqual({
      total: 6,
      verified: 2,
      failed: 1,
      cancelled: 1,
      open: 2,
    });
  });

  it.each([
    ['queued', ['queued'], 'running'],
    ['running', ['verified', 'running'], 'running'],
    ['succeeded', ['verified', 'verified'], 'complete'],
    ['partial_failed', ['verified', 'failed'], 'complete'],
    ['failed', ['failed'], 'failed'],
    ['failed', ['failed', 'failed'], 'failed'],
    ['cancelled', ['verified', 'cancelled'], 'cancelled'],
    // A status the items do not back up is never reported as complete.
    ['succeeded', ['verified', 'queued'], 'failed'],
    ['succeeded', ['failed'], 'failed'],
    ['partial_failed', ['verified', 'cancelled'], 'failed'],
    ['succeeded', [], 'failed'],
  ] as const)('batchOutcome: %s with %j is %s', (status, states, expected) => {
    expect(batchOutcome(batch(status, ...states))).toBe(expected);
  });
});

describe('fileName', () => {
  it('takes the last component of a Windows or POSIX path', () => {
    expect(fileName('C:\\Exports\\Нарізка\\clip.mp4')).toBe('clip.mp4');
    expect(fileName('/home/me/clip.mp4')).toBe('clip.mp4');
    expect(fileName('C:\\Exports\\')).toBe('Exports');
    expect(fileName('clip.mp4')).toBe('clip.mp4');
  });
});

describe('runningProgress', () => {
  const job = (progress: Record<string, unknown>): JobDto => ({
    id: 'j',
    type: 'export',
    state: 'running',
    progress,
    error: null,
  });

  it('reads the stage and the percent of the item being rendered', () => {
    expect(runningProgress(job({ stage: 'render', item_percent: 42 }))).toEqual({ stage: 'render', percent: 42 });
    expect(runningProgress(job({ stage: 'verify', item_percent: 0 }))).toEqual({ stage: 'verify', percent: 0 });
  });

  it('says nothing for a missing job or values it does not know', () => {
    expect(runningProgress(null)).toEqual({ stage: null, percent: null });
    expect(runningProgress(job({ stage: 'queued', item_percent: 'half' }))).toEqual({ stage: null, percent: null });
    expect(runningProgress(job({ stage: 'render', item_percent: Number.NaN }))).toEqual({ stage: 'render', percent: null });
  });

  it('rounds and clamps the percent', () => {
    expect(runningProgress(job({ stage: 'render', item_percent: 41.6 })).percent).toBe(42);
    expect(runningProgress(job({ stage: 'render', item_percent: 250 })).percent).toBe(100);
    expect(runningProgress(job({ stage: 'render', item_percent: -5 })).percent).toBe(0);
  });
});
