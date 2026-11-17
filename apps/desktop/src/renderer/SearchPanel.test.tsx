import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type {
  AnalysisEstimateDto,
  AnalysisRunDto,
  AnalysisRunItemDto,
  ApiOp,
  JobDto,
  TranscriptDto,
} from '../shared/api';
import { CATEGORY_IDS } from './analysis-logic';
import { OpenSettingsContext } from './settings-nav';
import { SearchPanel } from './SearchPanel';
import { SOURCE_ID, failure, installBridge, makeSource, ok } from './test-bridge';
import type { ApiHandlers, InstalledBridge } from './test-bridge';

// ADR 041 §23: the «Що шукати» panel of a source. The backend is a small fake that keeps the
// transcript, the searches and their jobs, so that a start, a cancel or a resume changes what the
// next read answers.

const TRANSCRIPT_ID = '9a1b3c5d-7e8f-4a0b-8c2d-4e6f8a0b2c4d';
const RUN_ID = '6b8d0f2a-4c5e-4a7b-9c1d-2e3f4a5b6c7d';
const JOB_ID = '2e4a6c8b-1d3f-4a5b-8c7d-9e0f1a2b3c4e';
const CREATED_AT = '2026-11-17T10:00:00Z';
const INSTRUCTION = 'Невдалі погоні зі смішною реакцією';
const KEY_MESSAGE = 'Спершу задайте ключ API OpenRouter у Налаштуваннях';
const NO_TRANSCRIPT = 'Спочатку розпізнайте мовлення або імпортуйте транскрипт';
const PARTIAL = 'Транскрипт неповний. Позначте «Шукати у готовій частині» або повторіть невдалі частини';

function transcriptOf(overrides: Partial<TranscriptDto> = {}): TranscriptDto {
  return {
    id: TRANSCRIPT_ID,
    source_id: SOURCE_ID,
    revision: 1,
    origin: 'stt',
    run_id: null,
    parent_id: null,
    timing_level: 'segment',
    language: 'en',
    duration_ms: 12_000,
    word_count: 0,
    segment_count: 2,
    coverage: [{ start_ms: 0, end_ms: 12_000, state: 'succeeded' }],
    created_at: CREATED_AT,
    ...overrides,
  };
}

function estimateOf(overrides: Partial<AnalysisEstimateDto> = {}): AnalysisEstimateDto {
  return {
    model: 'test/selector',
    unit_level: 'segment',
    unit_count: 2,
    window_count: 3,
    estimate_micro_usd: 30_000,
    prompt_usd_per_mtok: '1',
    completion_usd_per_mtok: '2',
    max_tokens: 4096,
    price_basis: 'test',
    budget_usd: '2.50',
    within_budget: true,
    ...overrides,
  };
}

function runOf(
  status: string,
  windows: Partial<AnalysisRunDto['windows']> = {},
  overrides: Partial<AnalysisRunDto> = {},
): AnalysisRunDto {
  return {
    id: RUN_ID,
    source_id: SOURCE_ID,
    transcript_id: TRANSCRIPT_ID,
    transcript_revision: 1,
    model: 'test/selector',
    provider: 'test',
    prompt_version: 'sha256:0000000000000000',
    schema_version: 1,
    unit_level: 'segment',
    status,
    error_code: null,
    created_at: CREATED_AT,
    updated_at: CREATED_AT,
    job_id: JOB_ID,
    committed_micro_usd: 0,
    windows: { total: 3, succeeded: 1, failed: 0, unknown_outcome: 0, pending: 2, ...windows },
    clip_ids: [],
    stale: false,
    diagnostics_summary: {},
    ...overrides,
  };
}

function jobOf(state: string, error: JobDto['error'] = null): JobDto {
  return { id: JOB_ID, type: 'analysis', state, progress: {}, error };
}

function itemOf(run: AnalysisRunDto): AnalysisRunItemDto {
  return {
    id: run.id,
    source_id: run.source_id,
    transcript_id: run.transcript_id,
    transcript_revision: run.transcript_revision,
    model: run.model,
    status: run.status,
    error_code: run.error_code,
    job_id: run.job_id,
    committed_micro_usd: run.committed_micro_usd,
    created_at: run.created_at,
  };
}

/** What the fake main process holds for the source. The tests change it between the calls. */
interface Server {
  /** The revisions of the source, oldest first. */
  transcripts: TranscriptDto[];
  /** The searches of the source, the newest first. */
  runs: AnalysisRunDto[];
  jobs: Record<string, JobDto>;
}

function serverOf(overrides: Partial<Server> = {}): Server {
  return { transcripts: [transcriptOf()], runs: [], jobs: {}, ...overrides };
}

interface ShowOptions {
  readonly server?: Server;
  readonly handlers?: ApiHandlers;
  readonly openSettings?: () => void;
}

function show(options: ShowOptions = {}): { readonly installed: InstalledBridge; readonly server: Server } {
  const server = options.server ?? serverOf();
  const installed = installBridge({
    handlers: {
      'transcripts.list': () => ok({ items: server.transcripts, next_cursor: null }),
      'analysis.list': () => ok({ items: server.runs.map(itemOf) }),
      'analysis.get': (params) => {
        const run = server.runs.find((candidate) => candidate.id === params.id);
        return run === undefined ? failure('ANALYSIS_RUN_NOT_FOUND', 'Пошук не знайдено') : ok(run);
      },
      'jobs.get': (params) => {
        const job = server.jobs[params.id];
        return job === undefined ? failure('JOB_NOT_FOUND', 'Завдання не знайдено') : ok(job);
      },
      'analysis.estimate': () => ok(estimateOf()),
      'analysis.create': () => ok({ id: RUN_ID, job_id: JOB_ID, status: 'queued' }),
      'jobs.cancel': () => ok(jobOf('cancelling')),
      'jobs.resume': () => ok(jobOf('queued')),
      ...options.handlers,
    },
  });
  render(
    <OpenSettingsContext value={options.openSettings ?? null}>
      <SearchPanel source={makeSource()} />
    </OpenSettingsContext>,
  );
  return { installed, server };
}

function paramsOf(installed: InstalledBridge, op: ApiOp): unknown[] {
  return installed.call.mock.calls.filter(([called]) => called === op).map(([, params]) => params);
}

function pause(ms: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

function expand(): void {
  fireEvent.click(screen.getByTestId('search-settings'));
}

/** Waits until the reads on the way in are done and the start is on screen. */
async function untilReady(): Promise<void> {
  await waitFor(() => {
    expect(screen.getByTestId('find-highlights')).toBeEnabled();
  });
}

describe('the panel is collapsed by default', () => {
  it('shows only its button until it is pressed, and then the categories', async () => {
    show();

    const toggle = screen.getByTestId('search-settings');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByTestId('category-funny')).not.toBeInTheDocument();

    expand();

    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByTestId('category-funny')).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByTestId('categories-all')).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByTestId('search-caption')).toHaveTextContent(
      'Пошук використовує мовлення. Візуальні сцени без реплік можуть бути пропущені',
    );
    await untilReady();
  });
});

describe('the profile fields have their labels', () => {
  it('the number of moments is labelled «До скількох моментів»', async () => {
    show();
    expand();

    expect(screen.getByRole('spinbutton', { name: 'До скількох моментів' })).toBe(
      screen.getByTestId('search-max-candidates'),
    );
    await untilReady();
  });
});

describe('E2E-04: empty categories disable the search', () => {
  it('«Усі категорії» clears every category, which turns the search off with a reason', async () => {
    show({ server: serverOf({ transcripts: [transcriptOf()] }) });
    expand();
    await untilReady();
    expect(screen.queryByRole('checkbox', { name: 'Шукати у готовій частині' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('categories-all'));

    for (const id of CATEGORY_IDS) {
      expect(screen.getByTestId(`category-${id}`)).toHaveAttribute('aria-checked', 'false');
    }
    expect(screen.getByTestId('categories-all')).toHaveAttribute('aria-checked', 'false');
    expect(screen.getByTestId('find-highlights')).toBeDisabled();
    expect(screen.getByTestId('category-error')).toHaveTextContent('Оберіть хоча б одну категорію');
    expect(screen.getByTestId('find-reason')).toHaveTextContent('Виправте поля, позначені вище');
  });

  it('one category chosen again makes «Усі категорії» mixed and the search possible again', async () => {
    show();
    expand();
    await untilReady();
    fireEvent.click(screen.getByTestId('categories-all'));

    fireEvent.click(screen.getByRole('checkbox', { name: 'Екшн' }));

    expect(screen.getByTestId('category-action')).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByTestId('categories-all')).toHaveAttribute('aria-checked', 'mixed');
    expect(screen.queryByTestId('category-error')).not.toBeInTheDocument();
    await untilReady();
  });
});

describe('without a transcript', () => {
  it('the categories can still be chosen, and the search says why it is off', async () => {
    show({ server: serverOf({ transcripts: [] }) });
    expand();

    await waitFor(() => {
      expect(screen.getByTestId('find-reason')).toHaveTextContent(NO_TRANSCRIPT);
    });
    const find = screen.getByTestId('find-highlights');
    expect(find).toBeDisabled();
    expect(find).toHaveAccessibleDescription(NO_TRANSCRIPT);

    fireEvent.click(screen.getByRole('checkbox', { name: 'Смішні моменти' }));

    expect(screen.getByTestId('category-funny')).toHaveAttribute('aria-checked', 'false');
    expect(screen.getByTestId('categories-all')).toHaveAttribute('aria-checked', 'mixed');
  });
});

describe('a partly recognised transcript', () => {
  const PARTLY = transcriptOf({
    coverage: [
      { start_ms: 0, end_ms: 6000, state: 'succeeded' },
      { start_ms: 6000, end_ms: 12_000, state: 'failed' },
    ],
  });

  it('needs «Шукати у готовій частині», and the estimate then sends allow_partial', async () => {
    const { installed } = show({ server: serverOf({ transcripts: [PARTLY] }) });
    expand();

    await waitFor(() => {
      expect(screen.getByTestId('find-reason')).toHaveTextContent(PARTIAL);
    });
    expect(screen.getByTestId('find-highlights')).toBeDisabled();

    fireEvent.click(screen.getByRole('checkbox', { name: 'Шукати у готовій частині' }));

    await waitFor(() => {
      expect(screen.getByTestId('find-highlights')).toBeEnabled();
    });
    expect(screen.queryByTestId('find-reason')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('find-highlights'));
    await screen.findByTestId('search-summary');
    expect(paramsOf(installed, 'analysis.estimate')).toMatchObject([
      { transcript_id: TRANSCRIPT_ID, profile: { allow_partial: true } },
    ]);
  });
});

describe('estimate, then the paid start', () => {
  it('estimates first, shows the summary, and the paid start sends the same profile', async () => {
    const { installed } = show();
    expand();
    await untilReady();
    fireEvent.change(screen.getByTestId('custom-instruction'), { target: { value: INSTRUCTION } });
    expect(screen.getByTestId('custom-instruction-counter')).toHaveTextContent(
      `${String(INSTRUCTION.length)}/2000`,
    );

    fireEvent.click(screen.getByTestId('find-highlights'));

    const summary = await screen.findByTestId('search-summary');
    expect(within(summary).getAllByRole('term').map((term) => term.textContent)).toEqual([
      'Тривалість джерела',
      'Категорії',
      'Модель аналізу',
      'Частини',
      'Орієнтовна вартість',
      'Ліміт витрат',
    ]);
    expect(within(summary).getAllByRole('definition').map((value) => value.textContent)).toEqual([
      '00:00:12.000',
      'Смішні моменти, Екшн, Провали, Несподівані ситуації, Цікаві деталі гри',
      'test/selector',
      '3',
      'до $0,03',
      '$2,50',
    ]);
    const estimated = paramsOf(installed, 'analysis.estimate');
    expect(estimated).toStrictEqual([
      {
        source_id: SOURCE_ID,
        transcript_id: TRANSCRIPT_ID,
        profile: {
          categories: [...CATEGORY_IDS],
          custom_instruction: INSTRUCTION,
          min_duration_ms: 15_000,
          max_duration_ms: 90_000,
          max_candidates: 20,
          allow_partial: false,
        },
      },
    ]);
    // Looking at the summary sends nothing paid.
    await pause(50);
    expect(paramsOf(installed, 'analysis.create')).toStrictEqual([]);

    fireEvent.click(screen.getByRole('button', { name: 'Запустити пошук (платно)' }));

    await waitFor(() => {
      expect(paramsOf(installed, 'analysis.create')).toStrictEqual(estimated);
    });
  });

  it('a change of the profile takes the summary away, because it is for the profile it was made for', async () => {
    show();
    expand();
    await untilReady();
    fireEvent.click(screen.getByTestId('find-highlights'));
    await screen.findByTestId('search-summary');

    fireEvent.click(screen.getByTestId('category-funny'));

    expect(screen.queryByTestId('search-summary')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Запустити пошук (платно)' })).not.toBeInTheDocument();
  });

  it('without a ceiling the start is off, says why, and offers the settings', async () => {
    const openSettings = vi.fn();
    const { installed } = show({
      handlers: { 'analysis.estimate': () => ok(estimateOf({ budget_usd: null, within_budget: null })) },
      openSettings,
    });
    expand();
    await untilReady();
    fireEvent.click(screen.getByTestId('find-highlights'));

    const start = await screen.findByRole('button', { name: 'Запустити пошук (платно)' });
    const reason = 'Ліміт витрат не задано. Задайте його в Налаштуваннях';
    expect(start).toBeDisabled();
    expect(screen.getByTestId('search-blocked')).toHaveTextContent(reason);
    expect(start).toHaveAccessibleDescription(reason);
    fireEvent.click(start);
    await pause(50);
    expect(paramsOf(installed, 'analysis.create')).toStrictEqual([]);

    fireEvent.click(screen.getByRole('button', { name: 'Відкрити Налаштування' }));
    expect(openSettings).toHaveBeenCalledTimes(1);
  });

  it('a refused start for the missing key shows the refusal and «Відкрити Налаштування»', async () => {
    const openSettings = vi.fn();
    show({
      handlers: { 'analysis.create': () => failure('API_KEY_REQUIRED', KEY_MESSAGE) },
      openSettings,
    });
    expand();
    await untilReady();
    fireEvent.click(screen.getByTestId('find-highlights'));
    fireEvent.click(await screen.findByRole('button', { name: 'Запустити пошук (платно)' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(KEY_MESSAGE);
    fireEvent.click(screen.getByRole('button', { name: 'Відкрити Налаштування' }));
    expect(openSettings).toHaveBeenCalledTimes(1);
  });
});

describe('a search that is going', () => {
  it('counts the parts done out of the parts of the search, and «Скасувати» asks the job to stop', async () => {
    const server = serverOf({
      runs: [runOf('running', { total: 4, succeeded: 2 })],
      jobs: { [JOB_ID]: jobOf('running') },
    });
    const { installed } = show({
      server,
      handlers: {
        'jobs.cancel': () => {
          server.jobs[JOB_ID] = jobOf('cancelling');
          return ok(jobOf('cancelling'));
        },
      },
    });
    expand();

    expect(await screen.findByTestId('search-progress')).toHaveTextContent(
      'Шукаємо моменти · 2 із 4 частин',
    );
    expect(screen.queryByTestId('find-highlights')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Скасувати' }));

    expect(paramsOf(installed, 'jobs.cancel')).toStrictEqual([{ id: JOB_ID }]);
    await waitFor(() => {
      expect(screen.getByTestId('search-progress')).toHaveTextContent('Скасовуємо пошук…');
    });
    expect(screen.getByTestId('cancel-job')).toBeDisabled();
  });
});

describe('a finished search', () => {
  it.each([
    [3, 'candidates-ready', 'Знайдено 3 моменти. Вони у вкладці «Моменти»'],
    [0, 'candidates-none', 'Моментів за цими налаштуваннями не знайдено. Змініть категорії або побажання'],
  ])(
    'with %i moments shows %s, and «Новий пошук» brings the start back and sends nothing',
    async (count, testId, text) => {
      const clipIds = Array.from({ length: count }, (_, index) => `clip-${String(index)}`);
      const { installed } = show({
        server: serverOf({
          runs: [runOf('completed', { succeeded: 3 }, { clip_ids: clipIds })],
          jobs: { [JOB_ID]: jobOf('succeeded') },
        }),
      });
      expand();

      expect(await screen.findByTestId(testId)).toHaveTextContent(text);
      expect(screen.queryByTestId('find-highlights')).not.toBeInTheDocument();

      fireEvent.click(screen.getByRole('button', { name: 'Новий пошук' }));

      expect(await screen.findByTestId('find-highlights')).toBeInTheDocument();
      expect(screen.queryByTestId(testId)).not.toBeInTheDocument();
      await pause(50);
      expect(paramsOf(installed, 'analysis.create')).toStrictEqual([]);
      expect(paramsOf(installed, 'jobs.resume')).toStrictEqual([]);
    },
  );

  it('with no moments the ready marker is not shown', async () => {
    show({
      server: serverOf({
        runs: [runOf('completed', { succeeded: 3 }, { clip_ids: [] })],
        jobs: { [JOB_ID]: jobOf('succeeded') },
      }),
    });
    expand();

    expect(await screen.findByTestId('candidates-none')).toBeInTheDocument();
    expect(screen.queryByTestId('candidates-ready')).not.toBeInTheDocument();
  });
});

describe('a search whose transcript changed after it', () => {
  it('shows the note that the transcript changed after this search', async () => {
    show({
      server: serverOf({
        runs: [runOf('completed', { succeeded: 3 }, { clip_ids: ['clip-0'], stale: true })],
        jobs: { [JOB_ID]: jobOf('succeeded') },
      }),
    });
    expand();

    expect(await screen.findByTestId('candidates-ready')).toBeInTheDocument();
    expect(screen.getByTestId('search-stale')).toHaveTextContent('Транскрипт змінився після цього пошуку');
  });

  it('shows no note when the transcript did not change', async () => {
    show({
      server: serverOf({
        runs: [runOf('completed', { succeeded: 3 }, { clip_ids: ['clip-0'], stale: false })],
        jobs: { [JOB_ID]: jobOf('succeeded') },
      }),
    });
    expand();

    expect(await screen.findByTestId('candidates-ready')).toBeInTheDocument();
    expect(screen.queryByTestId('search-stale')).not.toBeInTheDocument();
  });
});

describe('a stopped search', () => {
  it('says why it stopped, and «Продовжити пошук (платно)» sends the resume', async () => {
    const server = serverOf({
      runs: [runOf('interrupted', { succeeded: 1, failed: 2 })],
      jobs: {
        [JOB_ID]: jobOf('failed', { code: 'ANALYSIS_UPSTREAM', message: 'Сервіс аналізу тимчасово недоступний' }),
      },
    });
    const { installed } = show({
      server,
      handlers: {
        'jobs.resume': () => {
          server.jobs[JOB_ID] = jobOf('queued');
          return ok(jobOf('queued'));
        },
      },
    });
    expand();

    expect(await screen.findByTestId('search-stopped')).toHaveTextContent(
      'Сервіс аналізу тимчасово недоступний',
    );
    expect(screen.getByTestId('resume-job')).toHaveTextContent('Продовжити пошук (платно)');

    fireEvent.click(screen.getByTestId('resume-job'));

    expect(await screen.findByTestId('search-progress')).toBeVisible();
    expect(paramsOf(installed, 'jobs.resume')).toStrictEqual([{ id: JOB_ID }]);
  });

  it('a part whose answer never came: no plain resume, and only the confirmed resend sends retry_unknown', async () => {
    const server = serverOf({
      runs: [runOf('interrupted', { succeeded: 1, unknown_outcome: 1, pending: 1 })],
      jobs: { [JOB_ID]: jobOf('interrupted') },
    });
    const { installed } = show({
      server,
      handlers: {
        'jobs.resume': () => {
          server.jobs[JOB_ID] = jobOf('queued');
          return ok(jobOf('queued'));
        },
      },
    });
    expand();

    const unknown = await screen.findByTestId('search-unknown');
    expect(unknown).toHaveTextContent('Відповідь не отримано. Провайдер міг обробити запит');
    expect(unknown).toHaveTextContent('Це стосується 1 частини.');
    expect(screen.getByTestId('resume-job')).toHaveTextContent('Надіслати 1 частину ще раз (платно)');
    expect(screen.queryByRole('button', { name: 'Продовжити пошук (платно)' })).not.toBeInTheDocument();
    await pause(50);
    expect(paramsOf(installed, 'jobs.resume')).toStrictEqual([]);

    fireEvent.click(screen.getByTestId('resume-job'));

    await waitFor(() => {
      expect(paramsOf(installed, 'jobs.resume')).toStrictEqual([{ id: JOB_ID, retry_unknown: true }]);
    });
  });

  it('stopped on its ceiling: it goes on only with a new ceiling, and that is what it sends', async () => {
    const server = serverOf({
      runs: [runOf('failed', { succeeded: 1, failed: 2 })],
      jobs: {
        [JOB_ID]: jobOf('failed', {
          code: 'BUDGET_EXCEEDED',
          message: 'Ліміт витрат пошуку вичерпано: пошук зупинено',
        }),
      },
    });
    const { installed } = show({
      server,
      handlers: {
        'jobs.resume': () => {
          server.jobs[JOB_ID] = jobOf('queued');
          return ok(jobOf('queued'));
        },
      },
    });
    expand();

    const budget = await screen.findByTestId('search-budget');
    expect(screen.getByTestId('search-stopped')).toHaveTextContent(
      'Ліміт витрат пошуку вичерпано: пошук зупинено',
    );
    expect(screen.queryByRole('button', { name: 'Продовжити пошук (платно)' })).not.toBeInTheDocument();
    expect(screen.getByTestId('resume-job')).toBeDisabled();

    fireEvent.change(budget, { target: { value: '3.00' } });
    expect(screen.getByTestId('resume-job')).toHaveTextContent('Продовжити з новим лімітом (платно)');
    expect(screen.getByTestId('resume-job')).toBeEnabled();
    fireEvent.click(screen.getByTestId('resume-job'));

    await waitFor(() => {
      expect(paramsOf(installed, 'jobs.resume')).toStrictEqual([{ id: JOB_ID, budget_usd: '3.00' }]);
    });
  });
});

describe('a search on screen while the panel is collapsed', () => {
  it('a search that is going shows its count, and expanding takes that line away', async () => {
    show({
      server: serverOf({
        runs: [runOf('running', { total: 4, succeeded: 2 })],
        jobs: { [JOB_ID]: jobOf('running') },
      }),
    });

    const status = await screen.findByTestId('search-status-collapsed');
    expect(status).toHaveAttribute('role', 'status');
    expect(status).toHaveTextContent('Шукаємо моменти · 2 із 4 частин');
    expect(screen.queryByTestId('search-progress')).not.toBeInTheDocument();

    expand();

    expect(screen.queryByTestId('search-status-collapsed')).not.toBeInTheDocument();
    expect(await screen.findByTestId('search-progress')).toHaveTextContent(
      'Шукаємо моменти · 2 із 4 частин',
    );
  });

  it('a search that is being cancelled says so, as the expanded panel does', async () => {
    show({
      server: serverOf({
        runs: [runOf('running', { total: 4, succeeded: 2 })],
        jobs: { [JOB_ID]: jobOf('cancelling') },
      }),
    });

    expect(await screen.findByTestId('search-status-collapsed')).toHaveTextContent('Скасовуємо пошук…');
  });

  it('a finished search shows the count of candidates-ready', async () => {
    show({
      server: serverOf({
        runs: [runOf('completed', { succeeded: 3 }, { clip_ids: ['clip-0', 'clip-1', 'clip-2'] })],
        jobs: { [JOB_ID]: jobOf('succeeded') },
      }),
    });

    expect(await screen.findByTestId('search-status-collapsed')).toHaveTextContent(
      'Знайдено 3 моменти. Вони у вкладці «Моменти»',
    );
    expect(screen.queryByTestId('candidates-ready')).not.toBeInTheDocument();
  });

  it('a stopped search says so and points to the panel to go on', async () => {
    show({
      server: serverOf({
        runs: [runOf('interrupted', { succeeded: 1, failed: 2 })],
        jobs: {
          [JOB_ID]: jobOf('failed', { code: 'ANALYSIS_UPSTREAM', message: 'Сервіс аналізу тимчасово недоступний' }),
        },
      }),
    });

    expect(await screen.findByTestId('search-status-collapsed')).toHaveTextContent(
      'Пошук зупинено. Розгорніть «Що шукати», щоб продовжити',
    );
  });

  it('an idle source shows nothing while the panel is collapsed', async () => {
    const { installed } = show();

    await waitFor(() => {
      expect(paramsOf(installed, 'analysis.list')).not.toHaveLength(0);
    });
    await pause(50);
    expect(screen.queryByTestId('search-status-collapsed')).not.toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });
});

describe('the reads that the panel depends on', () => {
  it('a search that went to the settings for its key offers «Відкрити Налаштування» with the resume', async () => {
    const openSettings = vi.fn();
    show({
      server: serverOf({
        runs: [runOf('interrupted', { succeeded: 1, pending: 2 })],
        jobs: { [JOB_ID]: jobOf('failed', { code: 'ANALYSIS_AUTH', message: 'OpenRouter не прийняв ключ' }) },
      }),
      openSettings,
    });
    expand();

    expect(await screen.findByTestId('search-stopped')).toHaveTextContent('OpenRouter не прийняв ключ');
    expect(screen.getByTestId('resume-job')).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Відкрити Налаштування' }));
    expect(openSettings).toHaveBeenCalledTimes(1);
  });
});
