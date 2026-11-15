import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  ApiParams,
  AudioStreamDto,
  ClipDto,
  ExportBatchDto,
  ExportItemDto,
  JobDto,
  SourceDto,
} from '../shared/api';
import { DEFAULT_LAYOUT } from '../shared/bridge';
import type { ApiResult, FolderOpened } from '../shared/bridge';
import { App } from './App';
import {
  CLIP_ID,
  FOLDER_TOKEN,
  PROJECT_ID,
  SOURCE_ID,
  failure,
  installBridge,
  listOf,
  makeClip,
  makeProject,
  makeSource,
  ok,
} from './test-bridge';
import type { ApiHandlers, BridgeOptions, InstalledBridge } from './test-bridge';

const SECOND_CLIP_ID = '6d8f0a2c-4e5a-4b7c-9d9e-0f1a2b3c4d5e';
const THIRD_CLIP_ID = '7e9a1b3d-5f6b-4c8d-8e0f-1a2b3c4d5e6f';
const SECOND_SOURCE_ID = '9b2e5a7d-3c4f-4d6e-9f80-1b0c2d3e4f5a';
const BATCH_ID = '4a6c8e0b-2d3f-4b5c-9d7e-8f0a1b2c3d4e';
const JOB_ID = '2e4a6c8b-1d3f-4a5b-8c7d-9e0f1a2b3c4e';
const NEW_BATCH_ID = '5b7d9f1c-3e4a-4c6d-8e8f-9a0b1c2d3e4f';
const NEW_JOB_ID = '1c3e5a7b-9d0f-4e2a-8b4c-6d8e0f1a2b3c';
const SECOND_TOKEN = 'cd'.repeat(32);
const EXPORT_DIR = 'C:\\Exports\\Нарізка';

const PROFILE = {
  video_codec: 'h264',
  quality: 'high',
  audio_mode: 'selected',
  audio_stream_index: 1,
  write_srt: false,
};

const GAME_TRACK: AudioStreamDto = {
  index: 1,
  codec: 'aac',
  channels: 2,
  language: 'uk',
  title: 'Гра',
  default: true,
  start_offset_ms: 0,
};
const MIC_TRACK: AudioStreamDto = { ...GAME_TRACK, index: 2, language: null, title: 'Мікрофон', default: false };
const TWO_TRACKS: readonly AudioStreamDto[] = [GAME_TRACK, MIC_TRACK];

beforeEach(() => {
  // jsdom does not play media: the player may ask for it.
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined);
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => undefined);
});

// --- fixtures ----------------------------------------------------------------------------------

function approved(overrides: Partial<ClipDto> = {}): ClipDto {
  return makeClip({ review_status: 'approved', revision: 2, ...overrides });
}

function item(overrides: Partial<ExportItemDto> = {}): ExportItemDto {
  return {
    id: '8f0a1b2c-3d4e-4f5a-8b6c-7d8e9f0a1b2c',
    clip_id: CLIP_ID,
    clip_revision: 2,
    state: 'queued',
    final_path: null,
    duration_ms: null,
    error: null,
    ...overrides,
  };
}

const VERIFIED_PATH = `${EXPORT_DIR}\\color_gop_${CLIP_ID}_r2_00h00m04s120-00h00m07s880.mp4`;

function verified(overrides: Partial<ExportItemDto> = {}): ExportItemDto {
  return item({ state: 'verified', final_path: VERIFIED_PATH, duration_ms: 3760, ...overrides });
}

function batchOf(
  items: readonly ExportItemDto[],
  status: string,
  overrides: Partial<ExportBatchDto> = {},
): ExportBatchDto {
  return {
    id: BATCH_ID,
    project_id: PROJECT_ID,
    job_id: JOB_ID,
    status,
    output_dir: EXPORT_DIR,
    items,
    manifest_path: null,
    catalog_path: null,
    created_at: '2026-11-14T10:10:00Z',
    ...overrides,
  };
}

function jobOf(state: string, progress: Record<string, unknown> = {}, id = JOB_ID): JobDto {
  return { id, type: 'export', state, progress, error: null };
}

const CREATED = ok({ id: BATCH_ID, job_id: JOB_ID, status: 'queued' });

// --- driving the app ---------------------------------------------------------------------------

interface Opened {
  readonly installed: InstalledBridge;
  readonly clips: ClipDto[];
}

/** A fake backend for the clips of the sources: list and review act on one array. */
function clipHandlers(clips: ClipDto[]): ApiHandlers {
  return {
    'clips.list': (params) =>
      ok({ items: clips.filter((clip) => clip.source_id === params.source_id), next_cursor: null }),
    'clips.review': (params) => {
      const index = clips.findIndex((clip) => clip.id === params.id);
      const current = clips[index];
      if (current === undefined) {
        return failure('CLIP_NOT_FOUND', 'Кліп не знайдено');
      }
      const next = { ...current, revision: current.revision + 1, review_status: params.status };
      clips[index] = next;
      return ok(next);
    },
  };
}

/** The project with the sources imported, then the Моменти tab. */
async function openMoments(
  clips: readonly ClipDto[],
  options: {
    readonly sources?: readonly SourceDto[];
    readonly handlers?: ApiHandlers;
    readonly bridge?: Pick<BridgeOptions, 'chooseExportFolder' | 'openExportFolder'>;
  } = {},
): Promise<Opened> {
  const list = [...clips];
  const queue = [...(options.sources ?? [makeSource()])];
  const installed = installBridge({
    importVideo: () => ok(queue.shift() ?? makeSource()),
    chooseExportFolder: () => ({ token: FOLDER_TOKEN, displayName: 'Нарізка' }),
    ...options.bridge,
    handlers: {
      'projects.list': listOf(makeProject()),
      ...clipHandlers(list),
      ...options.handlers,
    },
  });
  render(<App />);
  fireEvent.click(await screen.findByTestId('project-item'));
  for (let index = 0; index < (options.sources ?? [makeSource()]).length; index += 1) {
    fireEvent.click(await screen.findByTestId('import-video'));
    await waitFor(() => {
      expect(installed.importVideo).toHaveBeenCalledTimes(index + 1);
    });
    await screen.findByTestId('source-ready');
  }
  if ((options.sources ?? []).length > 1) {
    await screen.findByRole('list', { name: 'Джерела проєкту' });
  }
  fireEvent.click(screen.getByRole('tab', { name: 'Моменти' }));
  await screen.findByTestId('player');
  return { installed, clips: list };
}

function card(clipId: string): HTMLElement {
  const found = screen
    .getAllByTestId('clip-card')
    .find((element) => element.getAttribute('data-clip-id') === clipId);
  if (found === undefined) {
    throw new Error(`no card for ${clipId}`);
  }
  return found;
}

async function select(...clipIds: string[]): Promise<void> {
  for (const clipId of clipIds) {
    await waitFor(() => {
      expect(card(clipId)).toBeInTheDocument();
    });
    fireEvent.click(within(card(clipId)).getByTestId('select-clip'));
  }
}

/** The nth row of the batch view. */
function rowAt(index: number): HTMLElement {
  const row = screen.getAllByTestId('export-item')[index];
  if (row === undefined) {
    throw new Error(`no export row ${String(index)}`);
  }
  return row;
}

function paramsOf(installed: InstalledBridge, op: string): unknown[] {
  return installed.call.mock.calls.filter(([called]) => called === op).map(([, params]) => params);
}

/** Selects the clip, opens the summary through the folder picker and returns the bridge. */
async function openSummary(clipIds: readonly string[] = [CLIP_ID]): Promise<void> {
  await select(...clipIds);
  fireEvent.click(screen.getByTestId('export-selected'));
  await screen.findByTestId('export-summary');
}

const creates = (installed: InstalledBridge): unknown[] => paramsOf(installed, 'exports.create');

// --- the bottom bar ----------------------------------------------------------------------------

describe('Моменти: the export bar', () => {
  const THREE = [
    approved(),
    makeClip({ id: SECOND_CLIP_ID, title: 'Ще не перевірений', review_status: 'suggested' }),
    makeClip({ id: THIRD_CLIP_ID, title: 'Відхилений кадр', review_status: 'rejected' }),
  ];

  it('says «Вибрано N · підтверджено M» and offers to export M', async () => {
    await openMoments(THREE);
    expect(screen.getByTestId('export-counts')).toHaveTextContent('Вибрано 0 · підтверджено 0');

    await select(CLIP_ID, SECOND_CLIP_ID, THIRD_CLIP_ID);

    expect(screen.getByTestId('export-counts')).toHaveTextContent('Вибрано 3 · підтверджено 1');
    expect(screen.getByTestId('export-selected')).toHaveTextContent('Експортувати 1');
    expect(screen.getByTestId('export-selected')).toBeEnabled();
  });

  it('shows the selected clips that are not approved as skipped, with a way to review them', async () => {
    await openMoments(THREE);
    await select(CLIP_ID, SECOND_CLIP_ID, THIRD_CLIP_ID);

    const skipped = screen.getByTestId('export-skipped');
    expect(skipped).toHaveTextContent('Пропущено 2');
    expect(
      within(skipped)
        .getAllByTestId('export-skipped-item')
        .map((entry) => entry.textContent),
    ).toEqual(['Ще не перевірений', 'Відхилений кадр']);
    expect(screen.getByTestId('review-skipped')).toBeVisible();
  });

  it('disables «Експортувати» at M=0 and says why, for no selection and for suggested clips only', async () => {
    await openMoments(THREE);

    expect(screen.getByTestId('export-selected')).toBeDisabled();
    expect(screen.getByTestId('export-selected')).toHaveTextContent('Експортувати 0');
    expect(screen.getByTestId('export-hint')).toHaveTextContent('Виберіть фрагменти для експорту');
    expect(screen.queryByTestId('export-skipped')).not.toBeInTheDocument();

    await select(SECOND_CLIP_ID);

    expect(screen.getByTestId('export-counts')).toHaveTextContent('Вибрано 1 · підтверджено 0');
    expect(screen.getByTestId('export-selected')).toBeDisabled();
    expect(screen.getByTestId('export-selected')).toHaveAccessibleDescription(
      /Жоден із вибраних фрагментів ще не підтверджено/,
    );
    expect(screen.getByTestId('export-skipped')).toHaveTextContent('Пропущено 1');
  });

  it('counts a clip as confirmed as soon as it is approved, without changing the selection', async () => {
    await openMoments([makeClip()]);
    await select(CLIP_ID);
    expect(screen.getByTestId('export-counts')).toHaveTextContent('Вибрано 1 · підтверджено 0');

    fireEvent.click(within(card(CLIP_ID)).getByTestId('approve-clip'));

    await waitFor(() => {
      expect(screen.getByTestId('export-counts')).toHaveTextContent('Вибрано 1 · підтверджено 1');
    });
    expect(screen.getByTestId('export-selected')).toBeEnabled();
    expect(screen.queryByTestId('export-skipped')).not.toBeInTheDocument();
  });

  it('«Перейти до перевірки» puts the keyboard focus on the review action of the first skipped clip', async () => {
    await openMoments(THREE);
    await select(CLIP_ID, SECOND_CLIP_ID);

    fireEvent.click(screen.getByTestId('review-skipped'));

    await waitFor(() => {
      expect(within(card(SECOND_CLIP_ID)).getByTestId('approve-clip')).toHaveFocus();
    });
  });

  it('keeps counting the clips ticked in another source and exports them together', async () => {
    const second = makeSource({ id: SECOND_SOURCE_ID, display_name: 'two_tracks.mkv' });
    const clips = [approved(), approved({ id: SECOND_CLIP_ID, source_id: SECOND_SOURCE_ID, title: 'З другого' })];
    const { installed } = await openMoments(clips, { sources: [makeSource(), second] });
    // The source added last is the current one.
    await select(SECOND_CLIP_ID);
    fireEvent.change(screen.getByTestId('clips-source'), { target: { value: SOURCE_ID } });
    await select(CLIP_ID);

    expect(screen.getByTestId('export-counts')).toHaveTextContent('Вибрано 2 · підтверджено 2');
    fireEvent.click(screen.getByTestId('export-selected'));
    fireEvent.click(await screen.findByTestId('confirm-export'));

    await waitFor(() => {
      expect(creates(installed)).toHaveLength(1);
    });
    expect(creates(installed)[0]).toMatchObject({ clip_ids: [CLIP_ID, SECOND_CLIP_ID] });
  });
});

// --- folder, summary, create -------------------------------------------------------------------

describe('Моменти: folder choice and the summary', () => {
  it('starts nothing when the user cancels the folder picker', async () => {
    const { installed } = await openMoments([approved()], {
      bridge: { chooseExportFolder: () => ({ canceled: true }) },
    });
    await select(CLIP_ID);

    fireEvent.click(screen.getByTestId('export-selected'));

    await waitFor(() => {
      expect(installed.chooseExportFolder).toHaveBeenCalledTimes(1);
    });
    await waitFor(() => {
      expect(screen.getByTestId('export-selected')).toBeEnabled();
    });
    expect(screen.queryByTestId('export-summary')).not.toBeInTheDocument();
    expect(screen.queryByTestId('confirm-export')).not.toBeInTheDocument();
    expect(creates(installed)).toHaveLength(0);
    expect(screen.getByRole('tab', { name: 'Експорт' })).toBeDisabled();
  });

  it('reports a folder picker that fails and starts nothing', async () => {
    const { installed } = await openMoments([approved()]);
    installed.chooseExportFolder.mockRejectedValueOnce(new Error('ipc'));
    await select(CLIP_ID);

    fireEvent.click(screen.getByTestId('export-selected'));

    expect(await screen.findByTestId('export-error')).toHaveTextContent(
      "Не вдалося зв'язатися з локальним рушієм",
    );
    expect(creates(installed)).toHaveLength(0);
  });

  it('summarises folder, count, format, audio, SRT and quality before anything is created', async () => {
    const { installed } = await openMoments([approved()]);

    await openSummary();

    const summary = screen.getByTestId('export-summary');
    expect(summary).toHaveAccessibleName('Перевірте параметри експорту');
    expect(within(summary).getByTestId('export-folder')).toHaveTextContent('Нарізка');
    expect(within(summary).getByTestId('export-clip-count')).toHaveTextContent('1');
    expect(within(summary).getByTestId('export-format')).toHaveTextContent('MP4 · H.264 · AAC');
    expect(within(summary).getByTestId('export-audio')).toHaveValue('track:1');
    // SRT is not accepted by the backend yet: shown, disabled and explained.
    expect(within(summary).getByTestId('export-srt')).toBeDisabled();
    expect(within(summary).getByTestId('export-srt')).not.toBeChecked();
    expect(within(summary).getByTestId('export-srt')).toHaveAccessibleDescription(/транскрипцією/);
    // Quality is one value, high, tucked into «Якість».
    expect(within(summary).getByTestId('export-quality-details')).toHaveTextContent('Якість: Висока');
    expect(within(summary).getByTestId('export-quality')).toHaveValue('high');
    expect(
      within(within(summary).getByTestId('export-quality')).getAllByRole('option'),
    ).toHaveLength(1);
    expect(creates(installed)).toHaveLength(0);
  });

  it('creates the batch with exactly the chosen params: project, approved clips, one-time token, profile', async () => {
    const { installed } = await openMoments([approved()], {
      handlers: { 'exports.create': () => CREATED, 'exports.get': () => ok(batchOf([item()], 'queued')), 'jobs.get': () => ok(jobOf('queued')) },
    });
    await openSummary();

    fireEvent.click(screen.getByTestId('confirm-export'));

    await waitFor(() => {
      expect(creates(installed)).toHaveLength(1);
    });
    expect(installed.call).toHaveBeenCalledWith('exports.create', {
      project_id: PROJECT_ID,
      clip_ids: [CLIP_ID],
      folderToken: FOLDER_TOKEN,
      profile: PROFILE,
    } satisfies ApiParams['exports.create']);
    // No path of any kind goes through the renderer.
    expect(JSON.stringify(creates(installed))).not.toMatch(/output_dir|C:\\|Нарізка/);
  });

  it('remembers «Моменти», not the batch-only «Експорт» tab, for the next start', async () => {
    const { installed } = await openMoments([approved()], {
      handlers: { 'exports.create': () => CREATED, 'exports.get': () => ok(batchOf([item()], 'queued')), 'jobs.get': () => ok(jobOf('queued')) },
    });
    await openSummary();

    fireEvent.click(screen.getByTestId('confirm-export'));

    expect(await screen.findByRole('tab', { name: 'Експорт' })).toHaveAttribute('aria-selected', 'true');
    expect(installed.setViewState).toHaveBeenLastCalledWith({
      project_id: PROJECT_ID,
      tab: 'clips',
      layout: DEFAULT_LAYOUT,
    });
    expect(JSON.stringify(installed.setViewState.mock.calls)).not.toContain('export');
  });

  it('exports only the approved ticked clips; the skipped ones never reach the backend', async () => {
    const { installed } = await openMoments(
      [approved(), makeClip({ id: SECOND_CLIP_ID, title: 'Ще не перевірений', review_status: 'suggested' })],
      { handlers: { 'exports.create': () => CREATED, 'exports.get': () => ok(batchOf([item()], 'queued')), 'jobs.get': () => ok(jobOf('queued')) } },
    );
    await openSummary([CLIP_ID, SECOND_CLIP_ID]);
    expect(screen.getByTestId('export-clip-count')).toHaveTextContent('1');

    fireEvent.click(screen.getByTestId('confirm-export'));

    await waitFor(() => {
      expect(creates(installed)).toHaveLength(1);
    });
    expect(creates(installed)[0]).toMatchObject({ clip_ids: [CLIP_ID] });
  });

  it('offers the tracks every source has, all tracks and no sound, and sends the choice', async () => {
    const { installed } = await openMoments([approved()], {
      sources: [makeSource({ audio_streams: [...TWO_TRACKS] })],
      handlers: { 'exports.create': () => CREATED, 'exports.get': () => ok(batchOf([item()], 'queued')), 'jobs.get': () => ok(jobOf('queued')) },
    });
    await openSummary();
    const audio = screen.getByTestId('export-audio');

    expect(within(audio).getAllByRole('option').map((option) => option.textContent)).toEqual([
      'Доріжка 1 · uk · Гра',
      'Доріжка 2 · Мікрофон',
      'Усі доріжки',
      'Без звуку',
    ]);
    expect(audio).toHaveValue('track:1');

    fireEvent.change(audio, { target: { value: 'track:2' } });
    expect(screen.getByTestId('export-format')).toHaveTextContent('MP4 · H.264 · AAC');
    fireEvent.change(audio, { target: { value: 'all' } });
    fireEvent.change(audio, { target: { value: 'none' } });
    expect(screen.getByTestId('export-format')).toHaveTextContent('без звуку');
    fireEvent.click(screen.getByTestId('confirm-export'));

    await waitFor(() => {
      expect(creates(installed)).toHaveLength(1);
    });
    expect(creates(installed)[0]).toMatchObject({
      profile: { ...PROFILE, audio_mode: 'none', audio_stream_index: null },
    });
  });

  it.each([
    ['track:2', { audio_mode: 'selected', audio_stream_index: 2 }],
    ['all', { audio_mode: 'all', audio_stream_index: null }],
  ])('sends %s as the audio profile', async (value, expected) => {
    const { installed } = await openMoments([approved()], {
      sources: [makeSource({ audio_streams: [...TWO_TRACKS] })],
      handlers: { 'exports.create': () => CREATED, 'exports.get': () => ok(batchOf([item()], 'queued')), 'jobs.get': () => ok(jobOf('queued')) },
    });
    await openSummary();

    fireEvent.change(screen.getByTestId('export-audio'), { target: { value } });
    fireEvent.click(screen.getByTestId('confirm-export'));

    await waitFor(() => {
      expect(creates(installed)).toHaveLength(1);
    });
    expect(creates(installed)[0]).toMatchObject({ profile: { ...PROFILE, ...expected } });
  });

  it('offers a single track only when every exported source has it', async () => {
    const mic: AudioStreamDto = { ...MIC_TRACK, index: 3 };
    const sources = [
      makeSource({ audio_streams: [...TWO_TRACKS] }),
      makeSource({ id: SECOND_SOURCE_ID, display_name: 'two_tracks.mkv', audio_streams: [GAME_TRACK, mic] }),
    ];
    await openMoments(
      [approved(), approved({ id: SECOND_CLIP_ID, source_id: SECOND_SOURCE_ID, title: 'З другого' })],
      { sources },
    );
    await select(SECOND_CLIP_ID);
    fireEvent.change(screen.getByTestId('clips-source'), { target: { value: SOURCE_ID } });
    await select(CLIP_ID);
    fireEvent.click(screen.getByTestId('export-selected'));
    await screen.findByTestId('export-summary');

    expect(
      within(screen.getByTestId('export-audio'))
        .getAllByRole('option')
        .map((option) => option.textContent),
    ).toEqual(['Доріжка 1 · uk · Гра', 'Усі доріжки', 'Без звуку']);
  });

  it('offers only «Без звуку» for a video without audio', async () => {
    const { installed } = await openMoments([approved()], {
      sources: [makeSource({ audio_streams: [] })],
      handlers: { 'exports.create': () => CREATED, 'exports.get': () => ok(batchOf([item()], 'queued')), 'jobs.get': () => ok(jobOf('queued')) },
    });
    await openSummary();

    const audio = screen.getByTestId('export-audio');
    expect(within(audio).getAllByRole('option').map((option) => option.textContent)).toEqual(['Без звуку']);
    expect(screen.getByTestId('export-format')).toHaveTextContent('без звуку');
    fireEvent.click(screen.getByTestId('confirm-export'));

    await waitFor(() => {
      expect(creates(installed)).toHaveLength(1);
    });
    expect(creates(installed)[0]).toMatchObject({
      profile: { ...PROFILE, audio_mode: 'none', audio_stream_index: null },
    });
  });

  it('closes the summary with «Скасувати» and with Escape, creating nothing', async () => {
    const { installed } = await openMoments([approved()]);
    await openSummary();

    fireEvent.click(screen.getByTestId('export-summary-cancel'));
    expect(screen.queryByTestId('export-summary')).not.toBeInTheDocument();
    expect(screen.getByTestId('export-selected')).toBeEnabled();

    fireEvent.click(screen.getByTestId('export-selected'));
    fireEvent.keyDown(await screen.findByTestId('export-audio'), { key: 'Escape' });
    expect(screen.queryByTestId('export-summary')).not.toBeInTheDocument();
    expect(creates(installed)).toHaveLength(0);
  });

  it('shows the backend refusal, then needs a new folder because the token is single-use', async () => {
    const tokens = [FOLDER_TOKEN, SECOND_TOKEN];
    let attempts = 0;
    const { installed } = await openMoments([approved()], {
      bridge: { chooseExportFolder: () => ({ token: tokens.shift() ?? SECOND_TOKEN, displayName: 'Нарізка' }) },
      handlers: {
        'exports.create': () => {
          attempts += 1;
          return attempts === 1 ? failure('CLIP_NOT_APPROVED', 'Експортувати можна лише підтверджені кліпи') : CREATED;
        },
        'exports.get': () => ok(batchOf([item()], 'queued')),
        'jobs.get': () => ok(jobOf('queued')),
      },
    });
    await openSummary();

    fireEvent.click(screen.getByTestId('confirm-export'));

    expect(await screen.findByTestId('export-summary-error')).toHaveTextContent(
      'Експортувати можна лише підтверджені кліпи',
    );
    expect(screen.queryByTestId('confirm-export')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('export-rechoose'));
    await screen.findByTestId('confirm-export');
    expect(screen.queryByTestId('export-summary-error')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('confirm-export'));

    await waitFor(() => {
      expect(creates(installed)).toHaveLength(2);
    });
    expect(creates(installed).map((params) => (params as { folderToken: string }).folderToken)).toEqual([
      FOLDER_TOKEN,
      SECOND_TOKEN,
    ]);
  });

  it('blocks a second confirm while the batch is being created', async () => {
    const { installed } = await openMoments([approved()], {
      handlers: { 'exports.create': () => new Promise<never>(() => undefined) },
    });
    await openSummary();

    fireEvent.click(screen.getByTestId('confirm-export'));
    fireEvent.click(screen.getByTestId('confirm-export'));

    expect(screen.getByTestId('confirm-export')).toBeDisabled();
    expect(creates(installed)).toHaveLength(1);
  });
});

// --- the batch view ----------------------------------------------------------------------------

/** Handlers for a batch that runs on the first read and has finished on the second. */
function runningThen(
  finished: ExportBatchDto,
  running: ExportBatchDto = batchOf([item({ state: 'running' })], 'running'),
): { readonly handlers: ApiHandlers; readonly reads: () => number } {
  let reads = 0;
  return {
    reads: () => reads,
    handlers: {
      'exports.create': () => CREATED,
      'exports.get': () => {
        reads += 1;
        return ok(reads < 2 ? running : finished);
      },
      'jobs.get': () =>
        ok(
          reads < 2
            ? jobOf('running', { stage: 'render', total: 1, completed: 0, failed: 0, item_percent: 42 })
            : jobOf('succeeded', { stage: 'done', total: 1, completed: 1, failed: 0 }),
        ),
    },
  };
}

async function exportFirstClip(): Promise<void> {
  await openSummary();
  fireEvent.click(screen.getByTestId('confirm-export'));
  await screen.findByTestId('export-view');
}

describe('Експорт: the batch view', () => {
  it('stays disabled until a batch exists, then opens with it', async () => {
    const { handlers } = runningThen(batchOf([verified()], 'succeeded'));
    await openMoments([approved()], { handlers });
    expect(screen.getByRole('tab', { name: 'Експорт' })).toBeDisabled();

    await exportFirstClip();

    const tab = screen.getByRole('tab', { name: 'Експорт' });
    expect(tab).toBeEnabled();
    expect(tab).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tabpanel', { name: 'Експорт' })).toBeVisible();
    expect(screen.queryByText('Розділ «Експорт» ще недоступний.')).not.toBeInTheDocument();
  });

  it('polls once a second and shows export-complete only when the batch is finished and verified', async () => {
    const { handlers, reads } = runningThen(batchOf([verified()], 'succeeded'));
    const opened = await openMoments([approved()], { handlers });

    await exportFirstClip();

    // First read: still running.
    const row = await screen.findByTestId('export-item');
    expect(row).toHaveAttribute('data-clip-id', CLIP_ID);
    expect(within(row).getByTestId('export-item-state')).toHaveTextContent('Експортується');
    expect(within(row).getByTestId('export-item-progress')).toHaveTextContent('Створюємо кліп · 42%');
    expect(within(row).getByText('Мій фрагмент')).toBeVisible();
    expect(screen.getByTestId('export-progress')).toHaveTextContent('Готово 0 із 1');
    expect(screen.getByTestId('cancel-job')).toBeEnabled();
    expect(screen.queryByTestId('export-complete')).not.toBeInTheDocument();
    expect(screen.queryByTestId('open-folder')).not.toBeInTheDocument();

    // Second read, a second later: verified and finished.
    expect(await screen.findByTestId('export-complete', {}, { timeout: 4000 })).toHaveTextContent(
      'усі файли перевірено',
    );
    expect(reads()).toBe(2);
    expect(opened.installed.call).toHaveBeenCalledWith('exports.get', { id: BATCH_ID });
    expect(opened.installed.call).toHaveBeenCalledWith('jobs.get', { id: JOB_ID });
    const done = screen.getByTestId('export-item');
    expect(within(done).getByTestId('export-item-state')).toHaveTextContent('Перевірено');
    expect(within(done).getByTestId('export-item-file')).toHaveTextContent(
      `color_gop_${CLIP_ID}_r2_00h00m04s120-00h00m07s880.mp4`,
    );
    expect(within(done).getByTestId('export-item-file')).not.toHaveTextContent('C:\\');
    expect(within(done).getByTestId('export-item-duration')).toHaveTextContent('00:00:03.760');
    expect(screen.getByTestId('export-progress')).toHaveTextContent('Готово 1 із 1');
    expect(screen.queryByTestId('cancel-job')).not.toBeInTheDocument();
    expect(screen.queryByTestId('retry-failed')).not.toBeInTheDocument();
    expect(screen.getByTestId('open-folder')).toBeEnabled();
  }, 15_000);

  it('stops asking once the batch is finished', async () => {
    const { handlers, reads } = runningThen(batchOf([verified()], 'succeeded'));
    const opened = await openMoments([approved()], { handlers });
    await exportFirstClip();
    await screen.findByTestId('export-complete', {}, { timeout: 4000 });
    const settled = paramsOf(opened.installed, 'exports.get').length;

    await new Promise((resolve) => setTimeout(resolve, 1500));

    expect(paramsOf(opened.installed, 'exports.get')).toHaveLength(settled);
    expect(reads()).toBe(2);
  }, 15_000);

  it('does not show export-complete for a batch that is finished but nothing was verified', async () => {
    const failed = item({
      state: 'failed',
      error: { code: 'FFMPEG_FAILED', message: 'FFmpeg не зміг створити кліп' },
    });
    const { handlers } = runningThen(batchOf([failed], 'failed'), batchOf([failed], 'failed'));
    await openMoments([approved()], { handlers });

    await exportFirstClip();

    expect(await screen.findByTestId('export-failed')).toHaveTextContent('Експорт не вдався');
    expect(screen.queryByTestId('export-complete')).not.toBeInTheDocument();
    expect(screen.getByTestId('export-item-error')).toHaveTextContent('FFmpeg не зміг створити кліп');
    expect(screen.getByTestId('export-progress')).toHaveTextContent('Готово 0 із 1');
    expect(screen.getByTestId('retry-failed')).toBeEnabled();
    expect(screen.queryByTestId('open-folder')).not.toBeInTheDocument();
  });

  it('does not show export-complete while the backend still calls the batch running, even if a file is verified', async () => {
    const batch = batchOf([verified(), item({ id: 'a1', clip_id: SECOND_CLIP_ID, state: 'running' })], 'running');
    const opened = await openMoments(
      [approved(), approved({ id: SECOND_CLIP_ID, title: 'Другий' })],
      {
        handlers: {
          'exports.create': () => CREATED,
          'exports.get': () => ok(batch),
          'jobs.get': () => ok(jobOf('running', { stage: 'verify', item_percent: 10 })),
        },
      },
    );
    await openSummary([CLIP_ID, SECOND_CLIP_ID]);
    fireEvent.click(screen.getByTestId('confirm-export'));

    await screen.findByTestId('export-view');

    expect(screen.getByTestId('export-progress')).toHaveTextContent('Готово 1 із 2');
    expect(screen.queryByTestId('export-complete')).not.toBeInTheDocument();
    expect(within(rowAt(0)).getByTestId('export-item-state')).toHaveTextContent('Перевірено');
    expect(within(rowAt(1)).getByTestId('export-item-progress')).toHaveTextContent(
      'Перевіряємо файл · 10%',
    );
    expect(opened.installed.call).toHaveBeenCalledWith('exports.get', { id: BATCH_ID });
  });

  it('shows «Готово 1 із 2» with the Ukrainian error and «Повторити невдалі» for a partial failure', async () => {
    const failed = item({
      id: 'b2',
      clip_id: SECOND_CLIP_ID,
      state: 'failed',
      error: { code: 'OUTPUT_INVALID', message: 'Створений кліп не пройшов перевірку' },
    });
    const opened = await openMoments(
      [approved(), approved({ id: SECOND_CLIP_ID, title: 'Другий' })],
      {
        handlers: {
          'exports.create': () => CREATED,
          'exports.get': () => ok(batchOf([verified(), failed], 'partial_failed')),
          'jobs.get': () => ok(jobOf('succeeded')),
        },
      },
    );
    await openSummary([CLIP_ID, SECOND_CLIP_ID]);
    fireEvent.click(screen.getByTestId('confirm-export'));

    await screen.findByTestId('export-complete');

    expect(screen.getByTestId('export-progress')).toHaveTextContent('Готово 1 із 2');
    expect(screen.getByTestId('export-complete')).toHaveTextContent('частково');
    expect(within(rowAt(1)).getByTestId('export-item-state')).toHaveTextContent('Не вдалося');
    expect(within(rowAt(1)).getByTestId('export-item-error')).toHaveTextContent(
      'Створений кліп не пройшов перевірку',
    );
    expect(screen.getByTestId('retry-failed')).toBeEnabled();
    expect(screen.getByTestId('open-folder')).toBeVisible();
    expect(paramsOf(opened.installed, 'exports.retry')).toHaveLength(0);
  });

  it('«Повторити невдалі» calls exports.retry with the batch id alone, then follows the new batch', async () => {
    const failed = item({
      id: 'b2',
      clip_id: SECOND_CLIP_ID,
      state: 'failed',
      error: { code: 'OUTPUT_INVALID', message: 'Створений кліп не пройшов перевірку' },
    });
    const opened = await openMoments(
      [approved(), approved({ id: SECOND_CLIP_ID, title: 'Другий' })],
      {
        handlers: {
          'exports.create': () => CREATED,
          'exports.get': (params) =>
            params.id === BATCH_ID
              ? ok(batchOf([verified(), failed], 'partial_failed'))
              : ok(batchOf([item({ clip_id: SECOND_CLIP_ID, state: 'running' })], 'running', { id: NEW_BATCH_ID, job_id: NEW_JOB_ID })),
          'jobs.get': (params) => ok(jobOf(params.id === JOB_ID ? 'succeeded' : 'running', {}, params.id)),
          'exports.retry': () => ok({ id: NEW_BATCH_ID, job_id: NEW_JOB_ID, status: 'queued' }),
        },
      },
    );
    await openSummary([CLIP_ID, SECOND_CLIP_ID]);
    fireEvent.click(screen.getByTestId('confirm-export'));
    fireEvent.click(await screen.findByTestId('retry-failed'));

    await waitFor(() => {
      expect(screen.getByTestId('export-view')).toHaveAttribute('data-batch-status', 'running');
    });
    expect(paramsOf(opened.installed, 'exports.retry')).toEqual([{ batch_id: BATCH_ID }]);
    expect(opened.installed.call).toHaveBeenCalledWith('exports.get', { id: NEW_BATCH_ID });
    expect(opened.installed.call).toHaveBeenCalledWith('jobs.get', { id: NEW_JOB_ID });
    expect(screen.getAllByTestId('export-item')).toHaveLength(1);
    expect(screen.getByTestId('export-progress')).toHaveTextContent('Готово 0 із 1');
    // The retry is not a new folder choice and no renderer path is sent.
    expect(creates(opened.installed)).toHaveLength(1);
    expect(JSON.stringify(paramsOf(opened.installed, 'exports.retry'))).not.toMatch(/output_dir|profile|clip_ids/);
  });

  it('shows why a retry was refused and keeps the batch', async () => {
    const failed = item({ state: 'failed', error: { code: 'FFMPEG_FAILED', message: 'FFmpeg не зміг створити кліп' } });
    await openMoments([approved()], {
      handlers: {
        'exports.create': () => CREATED,
        'exports.get': () => ok(batchOf([failed], 'failed')),
        'jobs.get': () => ok(jobOf('failed')),
        'exports.retry': () => failure('CLIP_NOT_APPROVED', 'Експортувати можна лише підтверджені кліпи'),
      },
    });
    await openSummary();
    fireEvent.click(screen.getByTestId('confirm-export'));

    fireEvent.click(await screen.findByTestId('retry-failed'));

    expect(await screen.findByTestId('export-action-error')).toHaveTextContent(
      'Експортувати можна лише підтверджені кліпи',
    );
    expect(screen.getByTestId('retry-failed')).toBeEnabled();
    expect(screen.getByTestId('export-view')).toHaveAttribute('data-batch-status', 'failed');
  });

  it('cancels through the job, shows the cancelling state, then the cancelled batch', async () => {
    let cancelled = false;
    const kept = verified();
    const opened = await openMoments(
      [approved(), approved({ id: SECOND_CLIP_ID, title: 'Другий' })],
      {
        handlers: {
          'exports.create': () => CREATED,
          'exports.get': () =>
            ok(
              cancelled
                ? batchOf([kept, item({ id: 'c3', clip_id: SECOND_CLIP_ID, state: 'cancelled' })], 'cancelled')
                : batchOf([kept, item({ id: 'c3', clip_id: SECOND_CLIP_ID, state: 'running' })], 'running'),
            ),
          'jobs.get': () => ok(jobOf(cancelled ? 'cancelled' : 'running')),
          'jobs.cancel': () => {
            cancelled = true;
            return ok(jobOf('cancelling'));
          },
        },
      },
    );
    await openSummary([CLIP_ID, SECOND_CLIP_ID]);
    fireEvent.click(screen.getByTestId('confirm-export'));
    await screen.findByTestId('export-view');

    fireEvent.click(screen.getByTestId('cancel-job'));

    expect(opened.installed.call).toHaveBeenCalledWith('jobs.cancel', { id: JOB_ID });
    expect(screen.getByTestId('export-running')).toHaveTextContent('Скасовуємо експорт');
    expect(screen.getByTestId('cancel-job')).toBeDisabled();
    expect(await screen.findByTestId('export-cancelled', {}, { timeout: 4000 })).toHaveTextContent(
      'Готові файли збережено: 1 із 2',
    );
    expect(screen.queryByTestId('export-complete')).not.toBeInTheDocument();
    expect(screen.queryByTestId('cancel-job')).not.toBeInTheDocument();
    expect(screen.getByTestId('retry-failed')).toBeEnabled();
  }, 15_000);

  it('shows a refused cancel and lets the user try again', async () => {
    await openMoments([approved()], {
      handlers: {
        'exports.create': () => CREATED,
        'exports.get': () => ok(batchOf([item({ state: 'running' })], 'running')),
        'jobs.get': () => ok(jobOf('running')),
        'jobs.cancel': () => failure('JOB_NOT_CANCELLABLE', 'Завдання вже завершено'),
      },
    });
    await openSummary();
    fireEvent.click(screen.getByTestId('confirm-export'));
    await screen.findByTestId('export-view');

    fireEvent.click(screen.getByTestId('cancel-job'));

    expect(await screen.findByTestId('export-action-error')).toHaveTextContent('Завдання вже завершено');
    expect(screen.getByTestId('cancel-job')).toBeEnabled();
  });

  it('opens the folder of the batch by its id, and shows why it could not', async () => {
    let answer: ApiResult<FolderOpened> = ok({ opened: true });
    const { handlers } = runningThen(batchOf([verified()], 'succeeded'));
    const opened = await openMoments([approved()], {
      handlers,
      bridge: { openExportFolder: () => answer },
    });
    await exportFirstClip();
    await screen.findByTestId('export-complete', {}, { timeout: 4000 });

    fireEvent.click(screen.getByTestId('open-folder'));

    await waitFor(() => {
      expect(opened.installed.openExportFolder).toHaveBeenCalledTimes(1);
    });
    expect(opened.installed.openExportFolder).toHaveBeenCalledWith({ batch_id: BATCH_ID });
    expect(screen.queryByTestId('export-action-error')).not.toBeInTheDocument();

    answer = failure('EXPORT_FOLDER_MISSING', 'Теки експорту більше немає на цьому місці');
    await waitFor(() => {
      expect(screen.getByTestId('open-folder')).toBeEnabled();
    });
    fireEvent.click(screen.getByTestId('open-folder'));

    expect(await screen.findByTestId('export-action-error')).toHaveTextContent(
      'Теки експорту більше немає на цьому місці',
    );
  }, 15_000);

  it('stops polling when the tab is left', async () => {
    const opened = await openMoments([approved()], {
      handlers: {
        'exports.create': () => CREATED,
        'exports.get': () => ok(batchOf([item({ state: 'running' })], 'running')),
        'jobs.get': () => ok(jobOf('running')),
      },
    });
    await exportFirstClip();
    await waitFor(() => {
      expect(paramsOf(opened.installed, 'exports.get').length).toBeGreaterThan(1);
    }, { timeout: 3000 });

    fireEvent.click(screen.getByRole('tab', { name: 'Моменти' }));
    await screen.findByTestId('player');
    const readsOnLeaving = paramsOf(opened.installed, 'exports.get').length;
    await new Promise((resolve) => setTimeout(resolve, 1500));

    expect(paramsOf(opened.installed, 'exports.get')).toHaveLength(readsOnLeaving);
    // The batch is still there when the user comes back.
    fireEvent.click(screen.getByRole('tab', { name: 'Експорт' }));
    expect(await screen.findByTestId('export-item')).toBeVisible();
  }, 15_000);

  it('reports a batch that cannot be read and reads it again on request', async () => {
    let reads = 0;
    const opened = await openMoments([approved()], {
      handlers: {
        'exports.create': () => CREATED,
        'exports.get': () => {
          reads += 1;
          return reads === 1
            ? failure('EXPORT_BATCH_NOT_FOUND', 'Пакет експорту не знайдено')
            : ok(batchOf([verified()], 'succeeded'));
        },
        'jobs.get': () => ok(jobOf('succeeded')),
      },
    });
    await openSummary();
    fireEvent.click(screen.getByTestId('confirm-export'));

    expect(await screen.findByTestId('export-load-error')).toHaveTextContent('Пакет експорту не знайдено');
    fireEvent.click(screen.getByTestId('export-reload'));

    expect(await screen.findByTestId('export-complete')).toBeVisible();
    expect(paramsOf(opened.installed, 'exports.get')).toHaveLength(2);
  });
});
