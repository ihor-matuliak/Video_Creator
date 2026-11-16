import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type {
  ApiOp,
  JobDto,
  SourceDto,
  TranscriptionChunkDto,
  TranscriptionEstimateDto,
  TranscriptionRunDto,
  TranscriptionRunItemDto,
} from '../shared/api';
import { App } from './App';
import { OpenSettingsContext } from './settings-nav';
import { TranscriptionPanel } from './TranscriptionPanel';
import {
  SOURCE_ID,
  failure,
  installBridge,
  listOf,
  makeProject,
  makeSettings,
  makeSource,
  ok,
} from './test-bridge';
import type { ApiHandlers, InstalledBridge } from './test-bridge';
import { ViewActiveContext } from './view-active';

// ADR 038: the «Розпізнавання мовлення» block of the source inspector. The backend is a small fake
// that keeps the runs, so that a start, a cancel or a resume changes what the next read answers.

const RUN_ID = '6b8d0f2a-4c5e-4a7b-9c1d-2e3f4a5b6c7d';
const JOB_ID = '2e4a6c8b-1d3f-4a5b-8c7d-9e0f1a2b3c4e';
const CREATED_AT = '2026-11-16T10:00:00Z';
const MODEL = 'microsoft/mai-transcribe-2';

function estimateOf(overrides: Partial<TranscriptionEstimateDto> = {}): TranscriptionEstimateDto {
  return {
    source_id: SOURCE_ID,
    audio_stream_index: 1,
    model: MODEL,
    language: null,
    duration_ms: 12_000,
    chunk_count: 1,
    billed_seconds: 12,
    usd_per_second_upper: '0.0001',
    estimate_micro_usd: 1200,
    price_basis: 'live_probe_2026-11-15_x3',
    budget_usd: '2.50',
    within_budget: true,
    existing: null,
    ...overrides,
  };
}

function chunkOf(state: string, id: string): TranscriptionChunkDto {
  return { id, core_start_ms: 0, core_end_ms: 1000, state, attempts: 1, error_code: null };
}

/** Parts named `c0`, `c1`…, one per given state. */
function chunksOf(...states: string[]): TranscriptionChunkDto[] {
  return states.map((state, index) => chunkOf(state, `c${String(index)}`));
}

function runOf(status: string, chunks: TranscriptionChunkDto[], overrides: Partial<TranscriptionRunDto> = {}): TranscriptionRunDto {
  return {
    id: RUN_ID,
    source_id: SOURCE_ID,
    job_id: JOB_ID,
    model: MODEL,
    status,
    revision_id: null,
    error_code: null,
    committed_micro_usd: 0,
    chunks,
    ...overrides,
  };
}

function jobOf(state: string, error: JobDto['error'] = null): JobDto {
  return { id: JOB_ID, type: 'transcription', state, progress: {}, error };
}

function itemOf(run: TranscriptionRunDto): TranscriptionRunItemDto {
  return {
    id: run.id,
    source_id: run.source_id,
    job_id: run.job_id,
    model: run.model,
    status: run.status,
    revision_id: run.revision_id,
    error_code: run.error_code,
    committed_micro_usd: run.committed_micro_usd,
    created_at: CREATED_AT,
  };
}

interface FakeBackend {
  /** The runs of the source, the newest first. */
  runs: TranscriptionRunDto[];
  jobs: Record<string, JobDto>;
  estimate: TranscriptionEstimateDto;
  /** What the backend says about holding a key. */
  hasKey: boolean;
  /** The ceiling of the settings (`run_budget_usd`); null when none is set. */
  runBudgetUsd: string | null;
}

function fakeBackend(initial: Partial<FakeBackend> = {}): { state: FakeBackend; handlers: ApiHandlers } {
  const state: FakeBackend = {
    runs: [],
    jobs: {},
    estimate: estimateOf(),
    hasKey: true,
    runBudgetUsd: null,
    ...initial,
  };
  const handlers: ApiHandlers = {
    'settings.get': () => ok(makeSettings({ has_api_key: state.hasKey, run_budget_usd: state.runBudgetUsd })),
    'transcription.estimate': () => ok(state.estimate),
    'transcription.list': () => ok({ items: state.runs.map(itemOf) }),
    'transcription.get': (params) => {
      const run = state.runs.find((candidate) => candidate.id === params.id);
      return run === undefined ? failure('TRANSCRIPTION_RUN_NOT_FOUND', 'Запуск не знайдено') : ok(run);
    },
    'jobs.get': (params) => {
      const job = state.jobs[params.id];
      return job === undefined ? failure('JOB_NOT_FOUND', 'Завдання не знайдено') : ok(job);
    },
  };
  return { state, handlers };
}

interface Shown {
  readonly installed: InstalledBridge;
  readonly state: FakeBackend;
  /** Moves the whole view between active and hidden, as the shell does for «Налаштування». */
  readonly setActive: (active: boolean) => void;
  /** Shows another source in the same block, as the inspector does when the user picks one. */
  readonly setSource: (source: SourceDto) => void;
  /** Takes the block off the screen, as leaving the tab does. */
  readonly unmount: () => void;
}

interface ShowOptions {
  readonly source?: SourceDto;
  readonly backend?: Partial<FakeBackend>;
  readonly handlers?: ApiHandlers;
  readonly active?: boolean;
  readonly openSettings?: () => void;
}

function show(options: ShowOptions = {}): Shown {
  const { state, handlers } = fakeBackend(options.backend);
  const installed = installBridge({ handlers: { ...handlers, ...options.handlers } });
  let source = options.source ?? makeSource();
  let active = options.active ?? true;
  const tree = () => (
    <ViewActiveContext value={active}>
      <OpenSettingsContext value={options.openSettings ?? null}>
        <TranscriptionPanel source={source} availability={source.availability} />
      </OpenSettingsContext>
    </ViewActiveContext>
  );
  const rendered = render(tree());
  return {
    installed,
    state,
    setActive: (next) => {
      active = next;
      rendered.rerender(tree());
    },
    setSource: (next) => {
      source = next;
      rendered.rerender(tree());
    },
    unmount: rendered.unmount,
  };
}

function paramsOf(installed: InstalledBridge, op: ApiOp): unknown[] {
  return installed.call.mock.calls.filter(([called]) => called === op).map(([, params]) => params);
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

/** A read the test holds back: while `on` is set its answer waits until `release`. */
class Hold {
  on = false;
  private readonly waiting: (() => void)[] = [];

  answer<T>(value: T): T | Promise<T> {
    if (!this.on) {
      return value;
    }
    return new Promise<T>((resolve) => {
      this.waiting.push(() => {
        resolve(value);
      });
    });
  }

  release(): void {
    this.on = false;
    for (const resolveNow of this.waiting.splice(0)) {
      resolveNow();
    }
  }
}

function textOf(testId: string): string {
  return screen.getByTestId(testId).textContent;
}

describe('summary before a paid start', () => {
  it('shows the summary first and sends the create only when the user clicks', async () => {
    const created: unknown[] = [];
    const { installed, state } = show({
      backend: { estimate: estimateOf({ chunk_count: 3, estimate_micro_usd: 30_000, language: 'en' }) },
      handlers: {
        'transcription.create': (params) => {
          created.push(params);
          return ok({ id: RUN_ID, job_id: JOB_ID, status: 'queued' });
        },
      },
    });

    const summary = await screen.findByTestId('transcription-summary');

    expect(within(summary).getAllByRole('term').map((term) => term.textContent)).toEqual([
      'Тривалість аудіо',
      'Модель',
      'Мова',
      'Частини',
      'Орієнтовна вартість',
      'Ліміт витрат',
    ]);
    expect(within(summary).getAllByRole('definition').map((value) => value.textContent)).toEqual([
      '00:00:12.000',
      'MAI-Transcribe 2 (Microsoft)',
      'English',
      '3',
      'до $0,03',
      '$2,50',
    ]);
    expect(screen.getByText(/Оцінка за верхньою ціною/)).toBeVisible();
    const start = screen.getByTestId('transcription-start');
    expect(start).toHaveTextContent('Розпізнати мовлення');
    expect(start).toBeEnabled();
    // Nothing is created by looking.
    await sleep(50);
    expect(created).toEqual([]);
    expect(paramsOf(installed, 'transcription.create')).toEqual([]);

    state.runs = [runOf('queued', chunksOf('pending', 'pending', 'pending'))];
    state.jobs[JOB_ID] = jobOf('queued');
    fireEvent.click(start);

    expect(await screen.findByTestId('transcription-progress')).toHaveTextContent(
      'Розпізнаємо мовлення · 0 із 3 частин',
    );
    expect(created).toEqual([{ source_id: SOURCE_ID, audio_stream_index: 1 }]);
    expect(screen.queryByTestId('transcription-start')).not.toBeInTheDocument();
  });

  it('says «Автовизначення» when the language is not set, and «не задано» when there is no ceiling', async () => {
    show({ backend: { estimate: estimateOf({ budget_usd: null, within_budget: null }) } });

    await screen.findByTestId('transcription-summary');

    expect(textOf('transcription-language')).toBe('Автовизначення');
    expect(textOf('transcription-budget')).toBe('не задано');
    expect(textOf('transcription-cost')).toBe('до $0,01');
  });

  it('keeps the start off when no ceiling is set, says why next to it, and sends nothing', async () => {
    const created: unknown[] = [];
    const { installed } = show({
      backend: { estimate: estimateOf({ budget_usd: null, within_budget: null }) },
      handlers: {
        'transcription.create': (params) => {
          created.push(params);
          return ok({ id: RUN_ID, job_id: JOB_ID, status: 'queued' });
        },
      },
      openSettings: vi.fn(),
    });

    const start = await screen.findByTestId('transcription-start');

    expect(start).toBeDisabled();
    expect(screen.getByTestId('transcription-blocked')).toBeVisible();
    expect(screen.getByTestId('transcription-blocked')).toHaveTextContent(
      'Ліміт витрат не задано. Задайте його в Налаштуваннях',
    );
    // The reason is what the start is described by, not only what it looks like.
    expect(start).toHaveAccessibleDescription('Ліміт витрат не задано. Задайте його в Налаштуваннях');
    fireEvent.click(start);
    await sleep(50);
    expect(created).toEqual([]);
    expect(paramsOf(installed, 'transcription.create')).toEqual([]);
  });

  it('offers «Відкрити Налаштування» beside the missing ceiling, and calls the settings from there', async () => {
    const openSettings = vi.fn();
    show({ backend: { estimate: estimateOf({ budget_usd: null, within_budget: null }) }, openSettings });

    const start = await screen.findByTestId('transcription-start');
    const toSettings = screen.getByRole('button', { name: 'Відкрити Налаштування' });

    expect(start.parentElement).toContainElement(toSettings);
    expect(toSettings).toBeEnabled();
    fireEvent.click(toSettings);
    expect(openSettings).toHaveBeenCalledTimes(1);
  });

  it('keeps the start off while the estimate is over the ceiling, and says why next to it', async () => {
    const { installed } = show({
      backend: {
        estimate: estimateOf({ budget_usd: '0.01', within_budget: false, estimate_micro_usd: 30_000 }),
      },
      openSettings: vi.fn(),
    });

    const start = await screen.findByTestId('transcription-start');

    expect(start).toBeDisabled();
    const reason = screen.getByTestId('transcription-blocked');
    expect(reason).toHaveTextContent(
      'Оцінка вартості (до $0,03) перевищує ліміт витрат ($0,01). Збільште ліміт у Налаштуваннях',
    );
    // The icon is decorative (aria-hidden), so it is not part of the description.
    expect(start).toHaveAccessibleDescription(
      'Оцінка вартості (до $0,03) перевищує ліміт витрат ($0,01). Збільште ліміт у Налаштуваннях',
    );
    fireEvent.click(start);
    expect(paramsOf(installed, 'transcription.create')).toEqual([]);
    // The way out is one click away.
    expect(screen.getByTestId('transcription-open-settings')).toBeEnabled();
  });
});

describe('the audio track', () => {
  const TWO_TRACKS: SourceDto['audio_streams'] = [
    { index: 1, codec: 'aac', channels: 2, language: 'uk', title: 'Гра', default: false, start_offset_ms: 0 },
    { index: 2, codec: 'aac', channels: 2, language: 'en', title: 'Мікрофон', default: true, start_offset_ms: 0 },
  ];

  it('has no select for one track, and uses that track', async () => {
    const { installed } = show();

    await screen.findByTestId('transcription-summary');

    expect(screen.queryByTestId('transcription-track')).not.toBeInTheDocument();
    expect(paramsOf(installed, 'transcription.estimate')).toEqual([{ source_id: SOURCE_ID, audio_stream_index: 1 }]);
  });

  it('offers every track and starts with the default one', async () => {
    const { installed } = show({ source: makeSource({ audio_streams: TWO_TRACKS }) });

    const select = await screen.findByRole('combobox', { name: 'Доріжка для розпізнавання' });

    expect(select).toHaveValue('2');
    expect(within(select).getAllByRole('option').map((option) => option.textContent)).toEqual([
      'Доріжка 1 · uk · Гра · 2 кан. · AAC',
      'Доріжка 2 · en · Мікрофон · 2 кан. · AAC · за замовчуванням',
    ]);
    expect(paramsOf(installed, 'transcription.estimate')).toEqual([{ source_id: SOURCE_ID, audio_stream_index: 2 }]);
  });

  it('starts with the first track when none is the default', async () => {
    const tracks = TWO_TRACKS.map((track) => ({ ...track, default: false }));
    show({ source: makeSource({ audio_streams: tracks }) });

    expect(await screen.findByRole('combobox', { name: 'Доріжка для розпізнавання' })).toHaveValue('1');
  });

  it('estimates and then starts the track the user chose', async () => {
    const created: unknown[] = [];
    const { installed } = show({
      source: makeSource({ audio_streams: TWO_TRACKS }),
      handlers: {
        'transcription.create': (params) => {
          created.push(params);
          return failure('API_KEY_REQUIRED', 'Спершу задайте ключ API OpenRouter у Налаштуваннях');
        },
      },
    });
    const select = await screen.findByRole('combobox', { name: 'Доріжка для розпізнавання' });
    await screen.findByTestId('transcription-summary');

    fireEvent.change(select, { target: { value: '1' } });

    await waitFor(() => {
      expect(paramsOf(installed, 'transcription.estimate')).toContainEqual({
        source_id: SOURCE_ID,
        audio_stream_index: 1,
      });
    });
    fireEvent.click(await screen.findByTestId('transcription-start'));
    await screen.findByTestId('transcription-error');
    expect(created).toEqual([{ source_id: SOURCE_ID, audio_stream_index: 1 }]);
  });

  it('says there is nothing to recognise without audio, offers no start and asks nothing', async () => {
    const { installed } = show({ source: makeSource({ audio_streams: [] }) });

    expect(screen.getByTestId('transcription-no-audio')).toHaveTextContent(
      'Розпізнавання мовлення недоступне: у відео немає звукової доріжки.',
    );
    await sleep(50);

    expect(screen.queryByTestId('transcription-start')).not.toBeInTheDocument();
    expect(screen.queryByTestId('transcription-track')).not.toBeInTheDocument();
    expect(installed.call).not.toHaveBeenCalled();
  });
});

describe('the key', () => {
  it('offers «Відкрити Налаштування» instead of the start when the backend holds no key', async () => {
    const openSettings = vi.fn();
    const { installed } = show({ backend: { hasKey: false }, openSettings });

    const button = await screen.findByRole('button', { name: 'Відкрити Налаштування' });

    expect(screen.getByTestId('transcription-summary')).toBeVisible();
    expect(screen.queryByTestId('transcription-start')).not.toBeInTheDocument();
    expect(screen.getByTestId('transcription-no-key')).toHaveTextContent('Спершу задайте ключ API OpenRouter у Налаштуваннях');
    fireEvent.click(button);
    expect(openSettings).toHaveBeenCalledTimes(1);
    expect(paramsOf(installed, 'transcription.create')).toEqual([]);
  });

  it.each([
    ['API_KEY_REQUIRED', 'Спершу задайте ключ API OpenRouter у Налаштуваннях'],
    ['BUDGET_REQUIRED', 'Вкажіть ліміт витрат для цього запуску або задайте його в Налаштуваннях'],
  ])('shows %s as an alert with a way to the settings', async (code, message) => {
    const openSettings = vi.fn();
    show({ openSettings, handlers: { 'transcription.create': () => failure(code, message) } });

    fireEvent.click(await screen.findByTestId('transcription-start'));

    expect(await screen.findByRole('alert')).toHaveTextContent(message);
    fireEvent.click(screen.getByRole('button', { name: 'Відкрити Налаштування' }));
    expect(openSettings).toHaveBeenCalledTimes(1);
    // The refusal is read again first: the start comes back only when that read is done (ADR 038, доповнення 2).
    await waitFor(() => {
      expect(screen.getByTestId('transcription-start')).toBeEnabled();
    });
  });

  it('reaches the settings of the shell and reads everything again on the way back', async () => {
    const { state, handlers } = fakeBackend({ hasKey: false });
    const installed = installBridge({
      importVideo: () => ok(makeSource()),
      handlers: { 'projects.list': listOf(makeProject()), ...handlers },
    });
    render(<App />);
    fireEvent.click(await screen.findByTestId('project-item'));
    fireEvent.click(await screen.findByTestId('import-video'));

    fireEvent.click(await screen.findByTestId('transcription-open-settings'));

    expect(await screen.findByTestId('settings-screen')).toBeVisible();
    expect(screen.getByTestId('project-pane')).toHaveAttribute('hidden');
    // The key is saved there; the block has not heard of it yet.
    state.hasKey = true;
    const estimates = paramsOf(installed, 'transcription.estimate').length;

    fireEvent.click(
      within(screen.getByRole('navigation', { name: 'Основна навігація' })).getByRole('button', { name: 'Проєкти' }),
    );

    expect(await screen.findByTestId('transcription-start')).toBeEnabled();
    expect(screen.queryByTestId('transcription-open-settings')).not.toBeInTheDocument();
    expect(paramsOf(installed, 'transcription.estimate').length).toBeGreaterThan(estimates);
    expect(paramsOf(installed, 'transcription.create')).toEqual([]);
  });
});

describe('a transcript that already exists', () => {
  const EXISTING = { transcript_id: '9a1b3c5d-7e8f-4a0b-8c2d-4e6f8a0b2c4d', run_id: RUN_ID };

  it('says so, and the second run is a separate, paid and confirmed button that sends force', async () => {
    const created: unknown[] = [];
    show({
      backend: { estimate: estimateOf({ existing: EXISTING }), runs: [runOf('completed', chunksOf('succeeded'))] },
      handlers: {
        'transcription.create': (params) => {
          created.push(params);
          return ok({ id: RUN_ID, job_id: JOB_ID, status: 'queued' });
        },
      },
    });

    expect(await screen.findByTestId('transcription-existing')).toHaveTextContent('Транскрипт уже є');
    expect(screen.queryByTestId('transcription-start')).not.toBeInTheDocument();
    const again = screen.getByRole('button', { name: 'Розпізнати ще раз (платно)' });
    expect(created).toEqual([]);

    fireEvent.click(again);

    await waitFor(() => {
      expect(created).toEqual([{ source_id: SOURCE_ID, audio_stream_index: 1, force: true }]);
    });
  });

  it('shows the same message when the create is refused for it, and then offers the second run', async () => {
    const { state } = show({
      handlers: {
        'transcription.create': () =>
          failure('TRANSCRIPT_ALREADY_EXISTS', 'Для цього джерела вже є розпізнаний транскрипт'),
      },
    });
    const start = await screen.findByTestId('transcription-start');
    state.estimate = estimateOf({ existing: EXISTING });

    fireEvent.click(start);

    expect(await screen.findByRole('alert')).toHaveTextContent('Транскрипт уже є');
    expect(await screen.findByRole('button', { name: 'Розпізнати ще раз (платно)' })).toBeEnabled();
  });
});

describe('a run that is going', () => {
  it('counts the parts that are done out of the parts of the plan, and follows the run', async () => {
    const { state } = show({
      backend: {
        runs: [runOf('running', chunksOf('succeeded', 'succeeded', 'running', 'pending', 'superseded'))],
        jobs: { [JOB_ID]: jobOf('running') },
      },
    });

    expect(await screen.findByTestId('transcription-progress')).toHaveTextContent(
      'Розпізнаємо мовлення · 2 із 4 частин',
    );
    expect(screen.queryByTestId('transcription-summary')).not.toBeInTheDocument();
    expect(screen.queryByTestId('transcription-track')).not.toBeInTheDocument();

    state.runs = [runOf('running', chunksOf('succeeded', 'succeeded', 'succeeded', 'running', 'superseded'))];
    await waitFor(
      () => {
        expect(screen.getByTestId('transcription-progress')).toHaveTextContent('3 із 4 частин');
      },
      { timeout: 4000 },
    );
  }, 10_000);

  it('turns into the summary with the transcript when the run completes', async () => {
    const { state } = show({
      backend: { runs: [runOf('running', chunksOf('running'))], jobs: { [JOB_ID]: jobOf('running') } },
    });
    await screen.findByTestId('transcription-progress');

    state.runs = [runOf('completed', chunksOf('succeeded'), { revision_id: 'r1' })];
    state.jobs[JOB_ID] = jobOf('succeeded');
    state.estimate = estimateOf({ existing: { transcript_id: 'r1', run_id: RUN_ID } });

    expect(await screen.findByTestId('transcription-existing', {}, { timeout: 4000 })).toHaveTextContent(
      'Транскрипт уже є',
    );
    expect(screen.queryByTestId('transcription-progress')).not.toBeInTheDocument();
  }, 10_000);

  it('cancels through the job, says so, and offers to continue once the run has stopped', async () => {
    const { installed, state } = show({
      backend: { runs: [runOf('running', chunksOf('succeeded', 'running'))], jobs: { [JOB_ID]: jobOf('running') } },
      handlers: {
        'jobs.cancel': () => ok(jobOf('cancelling')),
      },
    });
    const cancel = await screen.findByRole('button', { name: 'Скасувати' });

    fireEvent.click(cancel);

    expect(paramsOf(installed, 'jobs.cancel')).toEqual([{ id: JOB_ID }]);
    expect(screen.getByTestId('transcription-progress')).toHaveTextContent('Скасовуємо розпізнавання…');
    expect(screen.getByTestId('transcription-cancel')).toBeDisabled();

    state.runs = [runOf('cancelled', chunksOf('succeeded', 'cancelled'))];
    state.jobs[JOB_ID] = jobOf('cancelled');

    expect(await screen.findByTestId('transcription-continue', {}, { timeout: 4000 })).toHaveTextContent('Продовжити');
    expect(screen.getByTestId('transcription-stopped')).toHaveTextContent('Розпізнавання скасовано');
    expect(screen.getByTestId('transcription-counts')).toHaveTextContent('Уже розпізнано 1 із 2 частин');
  }, 10_000);

  it('shows a refused cancel and leaves the run to the next read', async () => {
    show({
      backend: { runs: [runOf('running', chunksOf('running'))], jobs: { [JOB_ID]: jobOf('running') } },
      handlers: { 'jobs.cancel': () => failure('JOB_NOT_CANCELLABLE', 'Завдання вже завершено або перервано') },
    });

    fireEvent.click(await screen.findByTestId('transcription-cancel'));

    expect(await screen.findByRole('alert')).toHaveTextContent('Завдання вже завершено або перервано');
    expect(screen.getByTestId('transcription-cancel')).toBeEnabled();
    expect(screen.getByTestId('transcription-progress')).toHaveTextContent('Розпізнаємо мовлення');
  });

  it('knows that a stop was already asked for when it is opened again', async () => {
    show({
      backend: { runs: [runOf('running', chunksOf('running'))], jobs: { [JOB_ID]: jobOf('cancelling') } },
    });

    expect(await screen.findByTestId('transcription-progress')).toHaveTextContent('Скасовуємо розпізнавання…');
    expect(screen.getByTestId('transcription-cancel')).toBeDisabled();
  });
});

describe('polling and the view that is not active', () => {
  it('asks about the run once a second, stops while the view is hidden, and reads again when it returns', async () => {
    const { installed, setActive } = show({
      backend: { runs: [runOf('running', chunksOf('running'))], jobs: { [JOB_ID]: jobOf('running') } },
    });
    await screen.findByTestId('transcription-progress');
    const reads = (): number => paramsOf(installed, 'transcription.get').length;
    const afterLoad = reads();
    await waitFor(
      () => {
        expect(reads()).toBeGreaterThan(afterLoad);
      },
      { timeout: 3000 },
    );

    setActive(false);
    const frozen = reads();
    const lists = paramsOf(installed, 'transcription.list').length;
    await sleep(1600);

    expect(reads()).toBe(frozen);
    expect(paramsOf(installed, 'transcription.list')).toHaveLength(lists);

    setActive(true);

    await waitFor(() => {
      expect(paramsOf(installed, 'transcription.list').length).toBeGreaterThan(lists);
    });
    await waitFor(
      () => {
        expect(reads()).toBeGreaterThan(frozen + 1);
      },
      { timeout: 3000 },
    );
    expect(screen.getByTestId('transcription-progress')).toBeVisible();
  }, 15_000);

  it('does not ask anything while it is not active to begin with', async () => {
    const { installed, setActive } = show({ active: false });

    await sleep(100);
    expect(installed.call).not.toHaveBeenCalled();
    expect(screen.getByTestId('transcription-loading')).toBeVisible();

    setActive(true);
    expect(await screen.findByTestId('transcription-start')).toBeEnabled();
  });

  it('stops asking when the block goes away', async () => {
    const { installed, unmount } = show({
      backend: { runs: [runOf('running', chunksOf('running'))], jobs: { [JOB_ID]: jobOf('running') } },
    });
    await screen.findByTestId('transcription-progress');

    unmount();
    const reads = paramsOf(installed, 'transcription.get').length;
    await sleep(1600);

    expect(paramsOf(installed, 'transcription.get')).toHaveLength(reads);
  });
});

describe('restoring from the list of runs', () => {
  it('shows the newest run, reading only that one', async () => {
    const newest = runOf('partial', chunksOf('succeeded', 'failed', 'failed'), { id: 'newest' });
    const older = runOf('completed', chunksOf('succeeded'), { id: 'older', job_id: 'older-job' });
    const { installed } = show({ backend: { runs: [newest, older], jobs: { [JOB_ID]: jobOf('failed') } } });

    expect(await screen.findByTestId('transcription-stopped')).toHaveTextContent(
      'Мовлення розпізнано не повністю: 2 ділянки потребують повтору',
    );

    expect(paramsOf(installed, 'transcription.get')).toEqual([{ id: 'newest' }]);
    expect(screen.getByTestId('transcription-counts')).toHaveTextContent('Уже розпізнано 1 із 3 частин');
  });

  it('shows a newest run that is complete as the summary, without reading it', async () => {
    const { installed } = show({ backend: { runs: [runOf('completed', chunksOf('succeeded'))] } });

    expect(await screen.findByTestId('transcription-summary')).toBeVisible();

    expect(paramsOf(installed, 'transcription.get')).toEqual([]);
  });

  it('starts nothing by itself, even when the list is empty', async () => {
    const { installed } = show();

    await screen.findByTestId('transcription-summary');
    await sleep(50);

    expect(paramsOf(installed, 'transcription.create')).toEqual([]);
    expect(paramsOf(installed, 'jobs.resume')).toEqual([]);
  });

  it('shows the reason a failed job ended with', async () => {
    show({
      backend: {
        runs: [runOf('failed', chunksOf('failed', 'cancelled'))],
        jobs: { [JOB_ID]: jobOf('failed', { code: 'STT_AUTH', message: 'OpenRouter не прийняв ключ API' }) },
      },
    });

    expect(await screen.findByTestId('transcription-stopped')).toHaveTextContent('Розпізнавання не вдалося');
    expect(screen.getByTestId('transcription-reason')).toHaveTextContent('OpenRouter не прийняв ключ API');
  });

  it('says it could not read the state, with a retry that reads again', async () => {
    let failing = true;
    show({
      handlers: {
        'transcription.list': () =>
          failing ? failure('SIDECAR_UNAVAILABLE', 'Локальний рушій не відповідає', true) : ok({ items: [] }),
      },
    });

    const note = await screen.findByTestId('transcription-load-error');

    // A status, not an alert: other screens expect no alert when nothing was asked of the user.
    expect(note).toHaveAttribute('role', 'status');
    expect(note).toHaveTextContent('Не вдалося дізнатися стан розпізнавання. Локальний рушій не відповідає');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    failing = false;
    fireEvent.click(screen.getByRole('button', { name: 'Спробувати знову' }));

    expect(await screen.findByTestId('transcription-start')).toBeEnabled();
  });
});

describe('a run with failed parts', () => {
  it('offers «Повторити невдалі», which resumes the job without deciding anything about unknown parts', async () => {
    const resumed: unknown[] = [];
    const { state } = show({
      backend: {
        runs: [runOf('partial', chunksOf('succeeded', 'failed', 'failed'))],
        jobs: { [JOB_ID]: jobOf('failed') },
      },
      handlers: {
        'jobs.resume': (params) => {
          resumed.push(params);
          state.jobs[JOB_ID] = jobOf('queued');
          return ok(jobOf('queued'));
        },
      },
    });
    const retry = await screen.findByRole('button', { name: 'Повторити невдалі' });
    expect(screen.queryByTestId('transcription-continue')).not.toBeInTheDocument();
    expect(screen.queryByTestId('transcription-retry-unknown')).not.toBeInTheDocument();

    fireEvent.click(retry);

    // The resumed job is queued while its run still says «partial»: the block goes back to watching.
    expect(await screen.findByTestId('transcription-progress')).toHaveTextContent(
      'Розпізнаємо мовлення · 1 із 3 частин',
    );
    expect(resumed).toEqual([{ id: JOB_ID }]);
    expect(screen.queryByRole('button', { name: 'Повторити невдалі' })).not.toBeInTheDocument();
  });

  it('shows why a resume was refused, and reads again when the screen was out of date', async () => {
    const { installed } = show({
      backend: { runs: [runOf('partial', chunksOf('succeeded', 'failed'))], jobs: { [JOB_ID]: jobOf('failed') } },
      handlers: {
        'jobs.resume': () => failure('TRANSCRIPTION_RUN_ACTIVE', 'Розпізнавання цього запуску вже виконується'),
      },
    });
    const lists = (): number => paramsOf(installed, 'transcription.list').length;
    const retry = await screen.findByRole('button', { name: 'Повторити невдалі' });
    const before = lists();

    fireEvent.click(retry);

    expect(await screen.findByRole('alert')).toHaveTextContent('Розпізнавання цього запуску вже виконується');
    await waitFor(() => {
      expect(lists()).toBeGreaterThan(before);
    });
  });

  it('offers «Продовжити» for a run that was interrupted', async () => {
    const resumed: unknown[] = [];
    show({
      backend: { runs: [runOf('interrupted', chunksOf('succeeded', 'pending'))], jobs: { [JOB_ID]: jobOf('interrupted') } },
      handlers: {
        'jobs.resume': (params) => {
          resumed.push(params);
          return failure('API_KEY_REQUIRED', 'Спершу задайте ключ API OpenRouter у Налаштуваннях');
        },
      },
      openSettings: vi.fn(),
    });

    expect(await screen.findByTestId('transcription-stopped')).toHaveTextContent('Розпізнавання перервано');
    fireEvent.click(screen.getByRole('button', { name: 'Продовжити' }));

    // After a restart the key is gone until it is entered again: the way to the settings is here.
    expect(await screen.findByRole('alert')).toHaveTextContent('Спершу задайте ключ API OpenRouter у Налаштуваннях');
    expect(resumed).toEqual([{ id: JOB_ID }]);
    expect(screen.getByRole('button', { name: 'Відкрити Налаштування' })).toBeEnabled();
  });
});

describe('a part whose answer never came', () => {
  const UNKNOWN_RUN = (): TranscriptionRunDto =>
    runOf('interrupted', chunksOf('succeeded', 'succeeded', 'unknown_outcome', 'pending'));

  it('is restored from the list of runs and worded as docs/03 says, with no sum', async () => {
    show({ backend: { runs: [UNKNOWN_RUN()], jobs: { [JOB_ID]: jobOf('interrupted') } } });

    const block = await screen.findByTestId('transcription-unknown');

    expect(block).toHaveTextContent('Відповідь не отримано. Провайдер міг обробити запит');
    expect(block).toHaveTextContent('Це стосується 1 частини.');
    expect(block.textContent).not.toMatch(/\$/);
    expect(screen.getByTestId('transcription-stopped')).toHaveTextContent('Розпізнавання перервано');
    // The backend refuses a plain resume while a part is unanswered, so none is offered.
    expect(screen.queryByTestId('transcription-continue')).not.toBeInTheDocument();
    expect(screen.queryByTestId('transcription-retry-failed')).not.toBeInTheDocument();
  });

  it('is sent again only after the user confirms, and then with retry_unknown', async () => {
    const resumed: unknown[] = [];
    const { state } = show({
      backend: { runs: [UNKNOWN_RUN()], jobs: { [JOB_ID]: jobOf('interrupted') } },
      handlers: {
        'jobs.resume': (params) => {
          resumed.push(params);
          state.jobs[JOB_ID] = jobOf('queued');
          return ok(jobOf('queued'));
        },
      },
    });

    fireEvent.click(await screen.findByRole('button', { name: 'Надіслати ще раз' }));

    // Only the question so far.
    expect(resumed).toEqual([]);
    expect(screen.getByTestId('transcription-confirm-text')).toHaveTextContent(
      'Провайдер міг уже обробити цей запит і списати кошти. Якщо надіслати ще раз, за нього можуть заплатити вдруге.',
    );
    expect(screen.queryByRole('button', { name: 'Надіслати ще раз' })).not.toBeInTheDocument();

    // «Не надсилати» takes it back.
    const decline = screen.getByRole('button', { name: 'Не надсилати' });
    expect(decline).toHaveFocus();
    fireEvent.click(decline);
    expect(resumed).toEqual([]);
    expect(screen.queryByTestId('transcription-confirm-text')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Надіслати ще раз' }));
    fireEvent.click(screen.getByRole('button', { name: 'Так, надіслати ще раз' }));

    await waitFor(() => {
      expect(resumed).toEqual([{ id: JOB_ID, retry_unknown: true }]);
    });
    expect(await screen.findByTestId('transcription-progress')).toBeVisible();
  });

  it('is also what a partial run with failed and unanswered parts shows', async () => {
    show({
      backend: {
        runs: [runOf('partial', chunksOf('succeeded', 'failed', 'unknown_outcome'))],
        jobs: { [JOB_ID]: jobOf('failed') },
      },
    });

    await screen.findByTestId('transcription-unknown');

    expect(screen.queryByRole('button', { name: 'Повторити невдалі' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Надіслати ще раз' })).toBeEnabled();
  });
});

// ADR 038, доповнення 2: a stopped run has a way to a new start, a run that stopped on its ceiling
// goes on only with the ceiling of the settings, and a refused start or resume is read again first.

describe('a run that stopped on its ceiling', () => {
  const BUDGET_STOP = { code: 'BUDGET_EXCEEDED', message: 'Ліміт витрат запуску вичерпано: розпізнавання зупинено' };

  it('offers «Продовжити з лімітом» with the ceiling of the settings, and only that button sends it', async () => {
    const resumed: unknown[] = [];
    const { installed, state } = show({
      backend: {
        runs: [runOf('partial', chunksOf('succeeded', 'failed', 'failed'))],
        jobs: { [JOB_ID]: jobOf('failed', BUDGET_STOP) },
        runBudgetUsd: '3.00',
      },
      handlers: {
        'jobs.resume': (params) => {
          resumed.push(params);
          state.jobs[JOB_ID] = jobOf('queued');
          return ok(jobOf('queued'));
        },
      },
      openSettings: vi.fn(),
    });

    const toLimit = await screen.findByRole('button', { name: 'Продовжити з лімітом $3,00' });

    expect(toLimit).toBeEnabled();
    expect(screen.getByTestId('transcription-reason')).toHaveTextContent(BUDGET_STOP.message);
    // A plain resume would stop on the same ceiling again, so it is not offered.
    expect(screen.queryByRole('button', { name: 'Повторити невдалі' })).not.toBeInTheDocument();
    expect(paramsOf(installed, 'jobs.resume')).toEqual([]);

    fireEvent.click(toLimit);

    expect(await screen.findByTestId('transcription-progress')).toHaveTextContent(
      'Розпізнаємо мовлення · 1 із 3 частин',
    );
    expect(resumed).toStrictEqual([{ id: JOB_ID, budget_usd: '3.00' }]);
  });

  it('without a ceiling in the settings it offers «Відкрити Налаштування» and no resume', async () => {
    const openSettings = vi.fn();
    const { installed } = show({
      backend: {
        runs: [runOf('partial', chunksOf('succeeded', 'failed'))],
        jobs: { [JOB_ID]: jobOf('failed', BUDGET_STOP) },
        runBudgetUsd: null,
      },
      openSettings,
    });

    fireEvent.click(await screen.findByRole('button', { name: 'Відкрити Налаштування' }));

    expect(openSettings).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('button', { name: /Продовжити/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Повторити невдалі' })).not.toBeInTheDocument();
    expect(paramsOf(installed, 'jobs.resume')).toEqual([]);
  });
});

describe('starting over', () => {
  it.each(['partial', 'failed', 'interrupted', 'cancelled'])(
    'offers «Почати заново (платно)» for a run that is %s',
    async (status) => {
      show({ backend: { runs: [runOf(status, chunksOf('succeeded', 'failed'))], jobs: { [JOB_ID]: jobOf('failed') } } });

      expect(await screen.findByRole('button', { name: 'Почати заново (платно)' })).toBeEnabled();
    },
  );

  it('is not offered while the run is going', async () => {
    show({ backend: { runs: [runOf('running', chunksOf('running'))], jobs: { [JOB_ID]: jobOf('running') } } });

    await screen.findByTestId('transcription-progress');

    expect(screen.queryByRole('button', { name: 'Почати заново (платно)' })).not.toBeInTheDocument();
  });

  it('shows the start summary again with a fresh estimate and the track choice, and sends nothing by itself', async () => {
    const created: unknown[] = [];
    const tracks: SourceDto['audio_streams'] = [
      { index: 1, codec: 'aac', channels: 2, language: 'uk', title: 'Гра', default: false, start_offset_ms: 0 },
      { index: 2, codec: 'aac', channels: 2, language: 'en', title: 'Мікрофон', default: true, start_offset_ms: 0 },
    ];
    const { installed, state } = show({
      source: makeSource({ audio_streams: tracks }),
      backend: { runs: [runOf('partial', chunksOf('succeeded', 'failed', 'failed'))], jobs: { [JOB_ID]: jobOf('failed') } },
      handlers: {
        'transcription.create': (params) => {
          created.push(params);
          return ok({ id: RUN_ID, job_id: JOB_ID, status: 'queued' });
        },
      },
    });
    await screen.findByTestId('transcription-stopped');
    const estimates = paramsOf(installed, 'transcription.estimate').length;
    // What the backend would now estimate: the summary must show it, not the one read before.
    state.estimate = estimateOf({ chunk_count: 4 });

    fireEvent.click(screen.getByRole('button', { name: 'Почати заново (платно)' }));

    expect(await screen.findByTestId('transcription-summary')).toBeVisible();
    expect(screen.getByTestId('transcription-restart-note')).toHaveTextContent(
      'Новий запуск надішле все аудіо знову: уже розпізнані частини цього запуску не використовуються.',
    );
    expect(screen.queryByTestId('transcription-stopped')).not.toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Доріжка для розпізнавання' })).toHaveValue('2');
    await waitFor(() => {
      expect(textOf('transcription-parts')).toBe('4');
    });
    expect(paramsOf(installed, 'transcription.estimate').length).toBeGreaterThan(estimates);
    expect(created).toEqual([]);
    expect(paramsOf(installed, 'jobs.resume')).toEqual([]);

    const start = screen.getByTestId('transcription-start');
    await waitFor(() => {
      expect(start).toBeEnabled();
    });
    expect(start).toHaveTextContent('Розпізнати мовлення');
    fireEvent.click(start);
    await waitFor(() => {
      expect(created).toStrictEqual([{ source_id: SOURCE_ID, audio_stream_index: 2 }]);
    });
  });
});

describe('a refused start or resume is read again before the next paid action', () => {
  it('keeps the start off until the runs are read again, and keeps the refusal on screen meanwhile', async () => {
    const hold = new Hold();
    const created: unknown[] = [];
    const { installed } = show({
      handlers: {
        'transcription.create': (params) => {
          created.push(params);
          return failure('BRIDGE_ERROR', 'Локальний рушій не відповідає', true);
        },
        'transcription.list': () => hold.answer(ok({ items: [] })),
      },
    });
    const start = await screen.findByTestId('transcription-start');
    const reads = {
      settings: paramsOf(installed, 'settings.get').length,
      estimates: paramsOf(installed, 'transcription.estimate').length,
      lists: paramsOf(installed, 'transcription.list').length,
    };
    hold.on = true;

    fireEvent.click(start);

    expect(await screen.findByRole('alert')).toHaveTextContent('Локальний рушій не відповідає');
    await waitFor(() => {
      expect(paramsOf(installed, 'transcription.list').length).toBeGreaterThan(reads.lists);
    });
    expect(paramsOf(installed, 'settings.get').length).toBeGreaterThan(reads.settings);
    expect(paramsOf(installed, 'transcription.estimate').length).toBeGreaterThan(reads.estimates);
    // The read has not answered: the start is off, and a click on it sends nothing.
    expect(screen.getByTestId('transcription-start')).toBeDisabled();
    fireEvent.click(screen.getByTestId('transcription-start'));
    await sleep(50);
    expect(created).toHaveLength(1);

    hold.release();

    await waitFor(() => {
      expect(screen.getByTestId('transcription-start')).toBeEnabled();
    });
    expect(screen.getByRole('alert')).toHaveTextContent('Локальний рушій не відповідає');
    expect(created).toHaveLength(1);
  });

  it('reads the settings, the estimate and the runs again after a refused resume, too', async () => {
    const hold = new Hold();
    const stopped = runOf('partial', chunksOf('succeeded', 'failed'));
    const resumed: unknown[] = [];
    const { installed } = show({
      backend: { runs: [stopped], jobs: { [JOB_ID]: jobOf('failed') } },
      handlers: {
        'transcription.list': () => hold.answer(ok({ items: [itemOf(stopped)] })),
        'jobs.resume': (params) => {
          resumed.push(params);
          return failure('BRIDGE_ERROR', 'Локальний рушій не відповідає', true);
        },
      },
    });
    const retry = await screen.findByRole('button', { name: 'Повторити невдалі' });
    const reads = {
      settings: paramsOf(installed, 'settings.get').length,
      estimates: paramsOf(installed, 'transcription.estimate').length,
      lists: paramsOf(installed, 'transcription.list').length,
    };
    hold.on = true;

    fireEvent.click(retry);

    expect(await screen.findByRole('alert')).toHaveTextContent('Локальний рушій не відповідає');
    await waitFor(() => {
      expect(paramsOf(installed, 'transcription.list').length).toBeGreaterThan(reads.lists);
    });
    expect(paramsOf(installed, 'settings.get').length).toBeGreaterThan(reads.settings);
    expect(paramsOf(installed, 'transcription.estimate').length).toBeGreaterThan(reads.estimates);
    expect(screen.getByRole('button', { name: 'Повторити невдалі' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Почати заново (платно)' })).toBeDisabled();

    hold.release();

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Повторити невдалі' })).toBeEnabled();
    });
    expect(screen.getByRole('alert')).toHaveTextContent('Локальний рушій не відповідає');
    expect(resumed).toStrictEqual([{ id: JOB_ID }]);
  });
});

describe('the way to the settings beside the reason of a job', () => {
  it.each([
    ['STT_AUTH', 'OpenRouter не прийняв ключ API'],
    ['STT_FUNDS', 'На балансі OpenRouter недостатньо коштів'],
    ['BUDGET_EXCEEDED', 'Ліміт витрат запуску вичерпано: розпізнавання зупинено'],
  ])('is offered next to the reason %s, and calls the settings from there', async (code, message) => {
    const openSettings = vi.fn();
    show({
      backend: { runs: [runOf('failed', chunksOf('failed'))], jobs: { [JOB_ID]: jobOf('failed', { code, message }) } },
      openSettings,
    });

    const reason = await screen.findByTestId('transcription-reason');
    const toSettings = screen.getByRole('button', { name: 'Відкрити Налаштування' });

    expect(reason).toHaveTextContent(message);
    expect(reason.nextElementSibling).toBe(toSettings.parentElement);
    fireEvent.click(toSettings);
    expect(openSettings).toHaveBeenCalledTimes(1);
  });

  it('is shown once when a refused resume asks for the settings as well', async () => {
    show({
      backend: {
        runs: [runOf('interrupted', chunksOf('pending'))],
        jobs: { [JOB_ID]: jobOf('failed', { code: 'STT_AUTH', message: 'OpenRouter не прийняв ключ API' }) },
      },
      handlers: {
        'jobs.resume': () => failure('API_KEY_REQUIRED', 'Спершу задайте ключ API OpenRouter у Налаштуваннях'),
      },
      openSettings: vi.fn(),
    });

    fireEvent.click(await screen.findByRole('button', { name: 'Продовжити' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Спершу задайте ключ API OpenRouter у Налаштуваннях');
    expect(screen.getAllByRole('button', { name: 'Відкрити Налаштування' })).toHaveLength(1);
  });

  it('is not offered for a reason that the settings do not settle, and its reason is not repeated (L8)', async () => {
    show({
      backend: {
        runs: [runOf('partial', chunksOf('succeeded', 'unknown_outcome'))],
        jobs: { [JOB_ID]: jobOf('failed', { code: 'STT_UNKNOWN_OUTCOME', message: 'Відповідь не отримано' }) },
      },
      openSettings: vi.fn(),
    });

    expect(await screen.findByTestId('transcription-unknown')).toHaveTextContent('Відповідь не отримано');
    // The unknown-outcome block already says it: the job's own message would only repeat it.
    expect(screen.queryByTestId('transcription-reason')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Відкрити Налаштування' })).not.toBeInTheDocument();
  });
});

// --- Т9d2 review (L1–L4, L6): state and races of the block ----------------------------------------

const KEY_MESSAGE = 'Спершу задайте ключ API OpenRouter у Налаштуваннях';
const OTHER_SOURCE_ID = 'c3e5a7b9-1d2f-4a6c-8e0b-2d4f6a8c0e1b';
const STOPPED_ON_CEILING = { code: 'BUDGET_EXCEEDED', message: 'Ліміт витрат запуску вичерпано: розпізнавання зупинено' };

function unknownRun(): TranscriptionRunDto {
  return runOf('interrupted', chunksOf('succeeded', 'succeeded', 'unknown_outcome', 'pending'));
}

/** Lets time pass inside `act`, so that the polls of the block are not stray updates for React. */
async function pass(ms: number): Promise<void> {
  await act(async () => {
    await sleep(ms);
  });
}

/** Lets a held answer through inside `act`, and gives the answers that follow it time to settle. */
async function release(hold: Hold): Promise<void> {
  await act(async () => {
    hold.release();
    await sleep(0);
  });
}

/** React's own warnings (act, unmounted, keys) go to console.error: a test that expects none checks it. */
function watchConsoleErrors() {
  return vi.spyOn(console, 'error').mockImplementation(() => undefined);
}

describe('an action error on the way back to the block (L1)', () => {
  it('clears a refusal from before the block went behind the settings, once the read on the way back succeeds', async () => {
    const openSettings = vi.fn();
    const { setActive } = show({
      handlers: { 'transcription.create': () => failure('API_KEY_REQUIRED', KEY_MESSAGE) },
      openSettings,
    });

    fireEvent.click(await screen.findByTestId('transcription-start'));
    expect(await screen.findByRole('alert')).toHaveTextContent(KEY_MESSAGE);
    // The read after the refusal is done: the refusal is still on screen (ADR 038, доповнення 2).
    await waitFor(() => {
      expect(screen.getByTestId('transcription-start')).toBeEnabled();
    });
    expect(screen.getByRole('alert')).toHaveTextContent(KEY_MESSAGE);

    fireEvent.click(screen.getByRole('button', { name: 'Відкрити Налаштування' }));
    expect(openSettings).toHaveBeenCalledTimes(1);
    // The key is saved there; coming back reads the block again.
    setActive(false);
    setActive(true);

    await waitFor(() => {
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    });
    expect(screen.queryByRole('button', { name: 'Відкрити Налаштування' })).not.toBeInTheDocument();
    expect(screen.getByTestId('transcription-start')).toBeEnabled();
  });

  it('keeps the refusal when the read on the way back fails', async () => {
    let listFails = false;
    const { setActive } = show({
      handlers: {
        'transcription.create': () => failure('API_KEY_REQUIRED', KEY_MESSAGE),
        'transcription.list': () =>
          listFails ? failure('SIDECAR_UNAVAILABLE', 'Локальний рушій не відповідає', true) : ok({ items: [] }),
      },
    });

    fireEvent.click(await screen.findByTestId('transcription-start'));
    expect(await screen.findByRole('alert')).toHaveTextContent(KEY_MESSAGE);
    await waitFor(() => {
      expect(screen.getByTestId('transcription-start')).toBeEnabled();
    });

    listFails = true;
    setActive(false);
    setActive(true);

    expect(await screen.findByTestId('transcription-load-error')).toBeVisible();
    expect(screen.getByRole('alert')).toHaveTextContent(KEY_MESSAGE);
  });
});

describe('no paid action while the block reads (L2)', () => {
  it('keeps the start off during the read on the way back, and a click in that time sends nothing', async () => {
    const hold = new Hold();
    const created: unknown[] = [];
    const { installed, setActive } = show({
      handlers: {
        'transcription.create': (params) => {
          created.push(params);
          return ok({ id: RUN_ID, job_id: JOB_ID, status: 'queued' });
        },
        'transcription.list': () => hold.answer(ok({ items: [] })),
      },
    });
    expect(await screen.findByTestId('transcription-start')).toBeEnabled();
    const lists = paramsOf(installed, 'transcription.list').length;

    setActive(false);
    hold.on = true;
    setActive(true);
    await waitFor(() => {
      expect(paramsOf(installed, 'transcription.list').length).toBeGreaterThan(lists);
    });

    expect(screen.getByTestId('transcription-start')).toBeDisabled();
    fireEvent.click(screen.getByTestId('transcription-start'));
    await pass(50);
    expect(created).toEqual([]);

    await release(hold);
    await waitFor(() => {
      expect(screen.getByTestId('transcription-start')).toBeEnabled();
    });
    expect(created).toEqual([]);
  });

  it('keeps «Повторити невдалі» and «Почати заново» off during the read on the way back', async () => {
    const hold = new Hold();
    const stopped = runOf('partial', chunksOf('succeeded', 'failed'));
    const resumed: unknown[] = [];
    const { installed, setActive } = show({
      backend: { runs: [stopped], jobs: { [JOB_ID]: jobOf('failed') } },
      handlers: {
        'transcription.list': () => hold.answer(ok({ items: [itemOf(stopped)] })),
        'jobs.resume': (params) => {
          resumed.push(params);
          return ok(jobOf('queued'));
        },
      },
    });
    const retry = await screen.findByRole('button', { name: 'Повторити невдалі' });
    const startOver = screen.getByRole('button', { name: 'Почати заново (платно)' });
    const lists = paramsOf(installed, 'transcription.list').length;

    setActive(false);
    hold.on = true;
    setActive(true);
    await waitFor(() => {
      expect(paramsOf(installed, 'transcription.list').length).toBeGreaterThan(lists);
    });

    expect(retry).toBeDisabled();
    expect(startOver).toBeDisabled();
    fireEvent.click(retry);
    fireEvent.click(startOver);
    await pass(50);
    expect(resumed).toEqual([]);

    await release(hold);
    await waitFor(() => {
      expect(retry).toBeEnabled();
    });
    expect(startOver).toBeEnabled();
  });
});

describe('a run that says it is over while its job is not (L3)', () => {
  it.each(['queued', 'running', 'cancelling'])(
    'keeps watching while the job is %s, though the run is written as completed',
    async (jobState) => {
      const { installed, state } = show({
        backend: { runs: [runOf('running', chunksOf('running'))], jobs: { [JOB_ID]: jobOf('running') } },
      });
      await screen.findByTestId('transcription-progress');

      state.runs = [runOf('completed', chunksOf('succeeded'), { revision_id: 'r1' })];
      state.jobs[JOB_ID] = jobOf(jobState);

      const reads = paramsOf(installed, 'transcription.get').length;
      await waitFor(
        () => {
          expect(paramsOf(installed, 'transcription.get').length).toBeGreaterThan(reads + 2);
        },
        { timeout: 4000 },
      );
      expect(screen.getByTestId('transcription-progress')).toBeVisible();
      expect(screen.queryByTestId('transcription-summary')).not.toBeInTheDocument();
      expect(screen.queryByTestId('transcription-start')).not.toBeInTheDocument();
    },
    15_000,
  );

  it('reads settings, estimate and runs once when the job is over, and then the summary shows the transcript', async () => {
    const { installed, state } = show({
      backend: { runs: [runOf('running', chunksOf('running'))], jobs: { [JOB_ID]: jobOf('running') } },
    });
    await screen.findByTestId('transcription-progress');
    state.runs = [runOf('completed', chunksOf('succeeded'), { revision_id: 'r1' })];
    // The poll has seen the completed run, and the job is still open.
    const reads = paramsOf(installed, 'transcription.get').length;
    await waitFor(
      () => {
        expect(paramsOf(installed, 'transcription.get').length).toBeGreaterThan(reads + 1);
      },
      { timeout: 4000 },
    );
    expect(screen.getByTestId('transcription-progress')).toBeVisible();
    const before = {
      settings: paramsOf(installed, 'settings.get').length,
      estimates: paramsOf(installed, 'transcription.estimate').length,
      lists: paramsOf(installed, 'transcription.list').length,
    };

    state.jobs[JOB_ID] = jobOf('succeeded');
    state.estimate = estimateOf({ existing: { transcript_id: 'r1', run_id: RUN_ID } });

    expect(await screen.findByTestId('transcription-existing', {}, { timeout: 4000 })).toHaveTextContent(
      'Транскрипт уже є',
    );
    expect(screen.queryByTestId('transcription-progress')).not.toBeInTheDocument();
    expect(paramsOf(installed, 'settings.get')).toHaveLength(before.settings + 1);
    expect(paramsOf(installed, 'transcription.estimate')).toHaveLength(before.estimates + 1);
    expect(paramsOf(installed, 'transcription.list')).toHaveLength(before.lists + 1);
  }, 15_000);
});

describe('no poll after a terminal state (L4)', () => {
  it('schedules no poll once the run has stopped', async () => {
    const { installed, state } = show({
      backend: { runs: [runOf('running', chunksOf('running'))], jobs: { [JOB_ID]: jobOf('running') } },
    });
    await screen.findByTestId('transcription-progress');

    state.runs = [runOf('partial', chunksOf('succeeded', 'failed'))];
    state.jobs[JOB_ID] = jobOf('failed');

    expect(await screen.findByTestId('transcription-retry-failed', {}, { timeout: 4000 })).toBeEnabled();
    const reads = paramsOf(installed, 'transcription.get').length;
    await pass(1500);
    expect(paramsOf(installed, 'transcription.get')).toHaveLength(reads);
  }, 10_000);

  it('makes exactly one re-read for a terminal state, and does not poll while that read is slow', async () => {
    const hold = new Hold();
    const stopped = runOf('partial', chunksOf('succeeded', 'failed'));
    const { installed, state } = show({
      backend: { runs: [runOf('running', chunksOf('running'))], jobs: { [JOB_ID]: jobOf('running') } },
      handlers: { 'transcription.list': () => hold.answer(ok({ items: [itemOf(stopped)] })) },
    });
    await screen.findByTestId('transcription-progress');
    const before = {
      settings: paramsOf(installed, 'settings.get').length,
      estimates: paramsOf(installed, 'transcription.estimate').length,
      lists: paramsOf(installed, 'transcription.list').length,
    };

    hold.on = true;
    state.runs = [stopped];
    state.jobs[JOB_ID] = jobOf('failed');
    await waitFor(
      () => {
        expect(paramsOf(installed, 'settings.get').length).toBe(before.settings + 1);
      },
      { timeout: 4000 },
    );

    // The read is still out: the block asks about the run no more meanwhile.
    const reads = paramsOf(installed, 'transcription.get').length;
    await pass(1500);
    expect(paramsOf(installed, 'transcription.get')).toHaveLength(reads);

    await release(hold);
    expect(await screen.findByTestId('transcription-retry-failed')).toBeEnabled();
    expect(paramsOf(installed, 'settings.get')).toHaveLength(before.settings + 1);
    expect(paramsOf(installed, 'transcription.estimate')).toHaveLength(before.estimates + 1);
    expect(paramsOf(installed, 'transcription.list')).toHaveLength(before.lists + 1);

    const after = paramsOf(installed, 'transcription.get').length;
    await pass(1500);
    expect(paramsOf(installed, 'transcription.get')).toHaveLength(after);
  }, 15_000);
});

describe('a double click sends once (L6)', () => {
  it('sends one create for a double click on start', async () => {
    const hold = new Hold();
    hold.on = true;
    const created: unknown[] = [];
    const { state } = show({
      handlers: {
        'transcription.create': (params) => {
          created.push(params);
          return hold.answer(ok({ id: RUN_ID, job_id: JOB_ID, status: 'queued' }));
        },
      },
    });
    const start = await screen.findByTestId('transcription-start');

    fireEvent.click(start);
    fireEvent.click(start);
    await pass(50);
    expect(created).toEqual([{ source_id: SOURCE_ID, audio_stream_index: 1 }]);

    state.runs = [runOf('queued', chunksOf('pending'))];
    state.jobs[JOB_ID] = jobOf('queued');
    await release(hold);
    expect(await screen.findByTestId('transcription-progress')).toBeVisible();
    expect(created).toHaveLength(1);
  });

  it('sends one resume for a double click on «Повторити невдалі»', async () => {
    const hold = new Hold();
    hold.on = true;
    const resumed: unknown[] = [];
    const { state } = show({
      backend: { runs: [runOf('partial', chunksOf('succeeded', 'failed'))], jobs: { [JOB_ID]: jobOf('failed') } },
      handlers: {
        'jobs.resume': (params) => {
          resumed.push(params);
          return hold.answer(ok(jobOf('queued')));
        },
      },
    });
    const retry = await screen.findByRole('button', { name: 'Повторити невдалі' });

    fireEvent.click(retry);
    fireEvent.click(retry);
    await pass(50);
    expect(resumed).toEqual([{ id: JOB_ID }]);

    state.jobs[JOB_ID] = jobOf('queued');
    await release(hold);
    expect(await screen.findByTestId('transcription-progress')).toBeVisible();
    expect(resumed).toHaveLength(1);
  });

  it('sends one resume with the ceiling for a double click on «Продовжити з лімітом»', async () => {
    const hold = new Hold();
    hold.on = true;
    const resumed: unknown[] = [];
    const { state } = show({
      backend: {
        runs: [runOf('partial', chunksOf('succeeded', 'failed'))],
        jobs: { [JOB_ID]: jobOf('failed', STOPPED_ON_CEILING) },
        runBudgetUsd: '3.00',
      },
      handlers: {
        'jobs.resume': (params) => {
          resumed.push(params);
          return hold.answer(ok(jobOf('queued')));
        },
      },
    });
    const toLimit = await screen.findByRole('button', { name: 'Продовжити з лімітом $3,00' });

    fireEvent.click(toLimit);
    fireEvent.click(toLimit);
    await pass(50);
    expect(resumed).toStrictEqual([{ id: JOB_ID, budget_usd: '3.00' }]);

    state.jobs[JOB_ID] = jobOf('queued');
    await release(hold);
    expect(await screen.findByTestId('transcription-progress')).toBeVisible();
    expect(resumed).toHaveLength(1);
  });

  it('sends one resume with retry_unknown for a double click on «Так, надіслати ще раз»', async () => {
    const hold = new Hold();
    hold.on = true;
    const resumed: unknown[] = [];
    const { state } = show({
      backend: { runs: [unknownRun()], jobs: { [JOB_ID]: jobOf('interrupted') } },
      handlers: {
        'jobs.resume': (params) => {
          resumed.push(params);
          return hold.answer(ok(jobOf('queued')));
        },
      },
    });
    fireEvent.click(await screen.findByRole('button', { name: 'Надіслати ще раз' }));
    const confirm = screen.getByRole('button', { name: 'Так, надіслати ще раз' });

    fireEvent.click(confirm);
    fireEvent.click(confirm);
    await pass(50);
    expect(resumed).toStrictEqual([{ id: JOB_ID, retry_unknown: true }]);

    state.jobs[JOB_ID] = jobOf('queued');
    await release(hold);
    expect(await screen.findByTestId('transcription-progress')).toBeVisible();
    expect(resumed).toHaveLength(1);
  });

  it('never sends retry_unknown after «Не надсилати», not even when the block reads again', async () => {
    const { installed, setActive } = show({
      backend: { runs: [unknownRun()], jobs: { [JOB_ID]: jobOf('interrupted') } },
    });
    fireEvent.click(await screen.findByRole('button', { name: 'Надіслати ще раз' }));
    fireEvent.click(screen.getByRole('button', { name: 'Не надсилати' }));
    expect(screen.queryByTestId('transcription-confirm-text')).not.toBeInTheDocument();

    const lists = paramsOf(installed, 'transcription.list').length;
    setActive(false);
    setActive(true);
    await waitFor(() => {
      expect(paramsOf(installed, 'transcription.list').length).toBeGreaterThan(lists);
    });
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Надіслати ще раз' })).toBeEnabled();
    });
    await pass(1200);

    expect(paramsOf(installed, 'jobs.resume')).toEqual([]);
  });
});

describe('leaving the block while a read or a send is pending (L6)', () => {
  it('sends nothing more after unmount while a poll is pending, and reports no error to React', async () => {
    const errors = watchConsoleErrors();
    const hold = new Hold();
    const running = runOf('running', chunksOf('running'));
    const { installed, unmount } = show({
      backend: { runs: [running], jobs: { [JOB_ID]: jobOf('running') } },
      handlers: { 'transcription.get': () => hold.answer(ok(running)) },
    });
    await screen.findByTestId('transcription-progress');

    hold.on = true;
    const reads = paramsOf(installed, 'transcription.get').length;
    await waitFor(
      () => {
        expect(paramsOf(installed, 'transcription.get').length).toBeGreaterThan(reads);
      },
      { timeout: 3000 },
    );
    unmount();
    const calls = installed.call.mock.calls.length;

    await act(async () => {
      hold.release();
      await sleep(1200);
    });
    expect(installed.call).toHaveBeenCalledTimes(calls);
    expect(errors).not.toHaveBeenCalled();
  });

  it('sends nothing more after unmount while a create is pending', async () => {
    const errors = watchConsoleErrors();
    const hold = new Hold();
    hold.on = true;
    const { installed, unmount } = show({
      handlers: {
        'transcription.create': () => hold.answer(ok({ id: RUN_ID, job_id: JOB_ID, status: 'queued' })),
      },
    });
    fireEvent.click(await screen.findByTestId('transcription-start'));
    await waitFor(() => {
      expect(paramsOf(installed, 'transcription.create')).toHaveLength(1);
    });
    unmount();
    const calls = installed.call.mock.calls.length;

    await act(async () => {
      hold.release();
      await sleep(1200);
    });
    expect(installed.call).toHaveBeenCalledTimes(calls);
    expect(errors).not.toHaveBeenCalled();
  });

  it('asks nothing about the old run after the source changes, even when a poll of it is pending', async () => {
    const errors = watchConsoleErrors();
    const hold = new Hold();
    const running = runOf('running', chunksOf('running'));
    const { installed, setSource } = show({
      backend: { runs: [running], jobs: { [JOB_ID]: jobOf('running') } },
      handlers: {
        'transcription.get': () => hold.answer(ok(running)),
        'transcription.list': (params) => ok({ items: params.source_id === SOURCE_ID ? [itemOf(running)] : [] }),
      },
    });
    await screen.findByTestId('transcription-progress');

    hold.on = true;
    const reads = paramsOf(installed, 'transcription.get').length;
    await waitFor(
      () => {
        expect(paramsOf(installed, 'transcription.get').length).toBeGreaterThan(reads);
      },
      { timeout: 3000 },
    );
    setSource(makeSource({ id: OTHER_SOURCE_ID }));
    expect(await screen.findByTestId('transcription-summary')).toBeVisible();
    const calls = installed.call.mock.calls.length;

    await act(async () => {
      hold.release();
      await sleep(1200);
    });
    expect(installed.call).toHaveBeenCalledTimes(calls);
    expect(errors).not.toHaveBeenCalled();
  });

  it('does not block or re-read the other source when the create of the old one answers', async () => {
    const errors = watchConsoleErrors();
    const hold = new Hold();
    hold.on = true;
    const created: unknown[] = [];
    const { installed, setSource } = show({
      handlers: {
        'transcription.create': (params) => {
          created.push(params);
          return hold.answer(ok({ id: RUN_ID, job_id: JOB_ID, status: 'queued' }));
        },
      },
    });
    fireEvent.click(await screen.findByTestId('transcription-start'));
    await waitFor(() => {
      expect(created).toHaveLength(1);
    });

    setSource(makeSource({ id: OTHER_SOURCE_ID }));
    expect(await screen.findByTestId('transcription-start')).toBeEnabled();
    const calls = installed.call.mock.calls.length;

    await act(async () => {
      hold.release();
      await sleep(1200);
    });
    expect(installed.call).toHaveBeenCalledTimes(calls);
    expect(created).toEqual([{ source_id: SOURCE_ID, audio_stream_index: 1 }]);
    expect(errors).not.toHaveBeenCalled();
  });
});

// --- Т9d2 review, round 2: a budget stop with parts of unknown outcome (ADR 038, доповнення 3), a file
// that is missing or changed, and where the focus goes after a question or a cancel (L9) ------------------

const NEWER_RUN_ID = '0d1c2b3a-4f5e-4d6c-8b7a-9f0e1d2c3b4a';

describe('a budget stop with parts of unknown outcome (ADR 038, доповнення 3)', () => {
  it('confirms the resend with the ceiling of the settings, and the question names that ceiling', async () => {
    const resumed: unknown[] = [];
    const { state } = show({
      backend: {
        runs: [runOf('partial', chunksOf('succeeded', 'unknown_outcome', 'failed'))],
        jobs: { [JOB_ID]: jobOf('failed', STOPPED_ON_CEILING) },
        runBudgetUsd: '3.00',
      },
      handlers: {
        'jobs.resume': (params) => {
          resumed.push(params);
          state.jobs[JOB_ID] = jobOf('queued');
          return ok(jobOf('queued'));
        },
      },
    });

    fireEvent.click(await screen.findByRole('button', { name: 'Надіслати ще раз' }));

    expect(screen.getByTestId('transcription-confirm-text')).toHaveTextContent(
      'Продовження піде з лімітом витрат $3,00.',
    );
    expect(resumed).toEqual([]);
    expect(screen.queryByRole('button', { name: 'Повторити невдалі' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Продовжити з лімітом $3,00' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Так, надіслати ще раз' }));

    expect(await screen.findByTestId('transcription-progress')).toBeVisible();
    expect(resumed).toStrictEqual([{ id: JOB_ID, retry_unknown: true, budget_usd: '3.00' }]);
  });

  it('offers no resend without a ceiling in the settings: only «Відкрити Налаштування»', async () => {
    const openSettings = vi.fn();
    const { installed } = show({
      backend: {
        runs: [runOf('partial', chunksOf('succeeded', 'unknown_outcome', 'failed'))],
        jobs: { [JOB_ID]: jobOf('failed', STOPPED_ON_CEILING) },
        runBudgetUsd: null,
      },
      openSettings,
    });

    const unknown = await screen.findByTestId('transcription-unknown');

    expect(unknown).toHaveTextContent('Відповідь не отримано. Провайдер міг обробити запит');
    expect(within(unknown).queryAllByRole('button')).toEqual([]);
    expect(screen.queryByRole('button', { name: 'Надіслати ще раз' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Так, надіслати ще раз' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Повторити невдалі' })).not.toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: 'Відкрити Налаштування' })).toHaveLength(1);

    fireEvent.click(screen.getByRole('button', { name: 'Відкрити Налаштування' }));

    expect(openSettings).toHaveBeenCalledTimes(1);
    expect(paramsOf(installed, 'jobs.resume')).toEqual([]);
  });
});

const MISSING_TEXT = 'Файл не знайдено на цьому місці: розпізнавання неможливе.';
const CHANGED_TEXT = 'Файл змінено після додавання: розпізнавання неможливе.';
const UNAVAILABLE_FILES: readonly (readonly [string, string])[] = [
  ['missing', MISSING_TEXT],
  ['changed', CHANGED_TEXT],
];

/** The paid actions of a stopped run, with the names of their buttons. */
const STOPPED_CASES: readonly {
  readonly title: string;
  readonly run: TranscriptionRunDto;
  readonly job: JobDto;
  readonly budgetUsd: string | null;
  readonly names: readonly string[];
}[] = [
  {
    title: 'a partial run',
    run: runOf('partial', chunksOf('succeeded', 'failed')),
    job: jobOf('failed'),
    budgetUsd: null,
    names: ['Повторити невдалі', 'Почати заново (платно)'],
  },
  {
    title: 'an interrupted run',
    run: runOf('interrupted', chunksOf('succeeded', 'pending')),
    job: jobOf('interrupted'),
    budgetUsd: null,
    names: ['Продовжити', 'Почати заново (платно)'],
  },
  {
    title: 'a run that stopped on its ceiling',
    run: runOf('partial', chunksOf('succeeded', 'failed')),
    job: jobOf('failed', STOPPED_ON_CEILING),
    budgetUsd: '3.00',
    names: ['Продовжити з лімітом $3,00', 'Почати заново (платно)'],
  },
  {
    title: 'a run with parts of unknown outcome',
    run: unknownRun(),
    job: jobOf('interrupted'),
    budgetUsd: null,
    names: ['Надіслати ще раз', 'Почати заново (платно)'],
  },
];

describe('a file that is missing or changed (ADR 038, доповнення 3)', () => {
  for (const [availability, reason] of UNAVAILABLE_FILES) {
    describe(`a file that is ${availability}`, () => {
      it('keeps the start off, says why beside it, and sends nothing', async () => {
        const created: unknown[] = [];
        const { installed } = show({
          source: makeSource({ availability }),
          handlers: {
            'transcription.create': (params) => {
              created.push(params);
              return ok({ id: RUN_ID, job_id: JOB_ID, status: 'queued' });
            },
          },
        });

        const start = await screen.findByTestId('transcription-start');

        expect(screen.getByTestId('transcription-summary')).toBeVisible();
        expect(screen.getByTestId('transcription-unavailable')).toHaveTextContent(reason);
        expect(start).toBeDisabled();
        expect(start).toHaveAccessibleDescription(reason);
        fireEvent.click(start);
        await sleep(50);
        expect(created).toEqual([]);
        expect(paramsOf(installed, 'transcription.create')).toEqual([]);
      });

      it('turns the start off at once when the file stops being available while the block is open', async () => {
        const { installed, setSource } = show();
        expect(await screen.findByTestId('transcription-start')).toBeEnabled();

        setSource(makeSource({ availability }));

        expect(screen.getByTestId('transcription-start')).toBeDisabled();
        expect(screen.getByTestId('transcription-start')).toHaveAccessibleDescription(reason);
        fireEvent.click(screen.getByTestId('transcription-start'));
        await sleep(50);
        expect(paramsOf(installed, 'transcription.create')).toEqual([]);
      });

      it.each(STOPPED_CASES)(
        'turns off the paid actions of $title, and a click on them sends nothing',
        async (stopped) => {
          const resumed: unknown[] = [];
          const { installed } = show({
            source: makeSource({ availability }),
            backend: { runs: [stopped.run], jobs: { [JOB_ID]: stopped.job }, runBudgetUsd: stopped.budgetUsd },
            handlers: {
              'jobs.resume': (params) => {
                resumed.push(params);
                return ok(jobOf('queued'));
              },
            },
          });

          expect(await screen.findByTestId('transcription-unavailable')).toHaveTextContent(reason);
          for (const name of stopped.names) {
            const button = screen.getByRole('button', { name });
            expect(button).toBeDisabled();
            expect(button).toHaveAccessibleDescription(reason);
          }
          for (const name of stopped.names) {
            fireEvent.click(screen.getByRole('button', { name }));
          }
          await sleep(50);

          expect(resumed).toEqual([]);
          expect(paramsOf(installed, 'jobs.resume')).toEqual([]);
        },
      );
    });
  }

  it('turns the confirmed resend off at once when the file goes missing while the question is open', async () => {
    const resumed: unknown[] = [];
    const { setSource } = show({
      backend: { runs: [unknownRun()], jobs: { [JOB_ID]: jobOf('interrupted') } },
      handlers: {
        'jobs.resume': (params) => {
          resumed.push(params);
          return ok(jobOf('queued'));
        },
      },
    });
    fireEvent.click(await screen.findByRole('button', { name: 'Надіслати ще раз' }));

    setSource(makeSource({ availability: 'missing' }));

    const confirm = screen.getByRole('button', { name: 'Так, надіслати ще раз' });
    expect(confirm).toBeDisabled();
    expect(confirm).toHaveAccessibleDescription(MISSING_TEXT);
    expect(screen.getByRole('button', { name: 'Не надсилати' })).toBeEnabled();
    fireEvent.click(confirm);
    await sleep(50);
    expect(resumed).toEqual([]);
  });
});

describe('where the focus goes after a question or a cancel (L9)', () => {
  const headingOf = (): HTMLElement => screen.getByRole('heading', { name: 'Розпізнавання мовлення' });

  it('goes to the heading when the question closes with «Не надсилати»', async () => {
    show({ backend: { runs: [unknownRun()], jobs: { [JOB_ID]: jobOf('interrupted') } } });
    fireEvent.click(await screen.findByRole('button', { name: 'Надіслати ще раз' }));
    expect(screen.getByRole('button', { name: 'Не надсилати' })).toHaveFocus();

    fireEvent.click(screen.getByRole('button', { name: 'Не надсилати' }));

    expect(screen.queryByTestId('transcription-confirm-text')).not.toBeInTheDocument();
    // The heading takes focus (tabIndex -1), not the body.
    expect(headingOf()).toHaveAttribute('tabindex', '-1');
    expect(document.activeElement).toBe(headingOf());
  });

  it('goes to the heading when the question closes with «Так, надіслати ще раз», and keeps it there', async () => {
    const { state } = show({
      backend: { runs: [unknownRun()], jobs: { [JOB_ID]: jobOf('interrupted') } },
      handlers: {
        'jobs.resume': () => {
          state.jobs[JOB_ID] = jobOf('queued');
          return ok(jobOf('queued'));
        },
      },
    });
    fireEvent.click(await screen.findByRole('button', { name: 'Надіслати ще раз' }));

    fireEvent.click(screen.getByRole('button', { name: 'Так, надіслати ще раз' }));

    expect(document.activeElement).toBe(headingOf());
    expect(await screen.findByTestId('transcription-progress')).toBeVisible();
    expect(document.activeElement).toBe(headingOf());
  });

  it('goes to the heading when «Скасувати» turns disabled', async () => {
    const { installed } = show({
      backend: { runs: [runOf('running', chunksOf('succeeded', 'running'))], jobs: { [JOB_ID]: jobOf('running') } },
      handlers: { 'jobs.cancel': () => ok(jobOf('cancelling')) },
    });
    const cancel = await screen.findByRole('button', { name: 'Скасувати' });
    cancel.focus();
    expect(cancel).toHaveFocus();

    fireEvent.click(cancel);

    expect(screen.getByTestId('transcription-cancel')).toBeDisabled();
    expect(document.activeElement).toBe(headingOf());
    expect(paramsOf(installed, 'jobs.cancel')).toEqual([{ id: JOB_ID }]);
  });

  it('closes an open question when another run becomes the one shown', async () => {
    const { state, setActive } = show({
      backend: { runs: [unknownRun()], jobs: { [JOB_ID]: jobOf('interrupted') } },
    });
    fireEvent.click(await screen.findByRole('button', { name: 'Надіслати ще раз' }));
    expect(screen.getByTestId('transcription-confirm-text')).toBeVisible();

    state.runs = [runOf('partial', chunksOf('succeeded', 'unknown_outcome'), { id: NEWER_RUN_ID })];
    setActive(false);
    setActive(true);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Надіслати ще раз' })).toBeEnabled();
    });
    expect(screen.queryByTestId('transcription-confirm-text')).not.toBeInTheDocument();
  });
});
