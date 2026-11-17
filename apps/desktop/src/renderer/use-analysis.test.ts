import { act, renderHook, waitFor } from '@testing-library/react';
import { createElement } from 'react';
import type { ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';

import type {
  AnalysisEstimateDto,
  AnalysisRunDto,
  AnalysisRunItemDto,
  ApiOp,
  JobDto,
  TranscriptDto,
} from '../shared/api';
import type { ApiResult } from '../shared/bridge';
import { DEFAULT_DRAFT } from './analysis-logic';
import type { ProfileDraft } from './analysis-logic';
import type { ApiHandlers, InstalledBridge } from './test-bridge';
import { SOURCE_ID, deferred, failure, flushAnswers, installBridge, ok } from './test-bridge';
import { notifyTranscriptChanged } from './transcript-events';
import { isJobOpen } from './transcription-logic';
import { ANALYSIS_POLL_MS, useAnalysis } from './use-analysis';
import { ViewActiveContext } from './view-active';

const TRANSCRIPT_ID = 'tr-1';
const RUN_ID = 'run-1';
const JOB_ID = 'job-1';

const UNKNOWN_ERROR = { code: 'ANALYSIS_UNKNOWN_OUTCOME', message: 'Відповідь не отримано' };
const BUDGET_ERROR = { code: 'BUDGET_EXCEEDED', message: 'Ліміт витрат вичерпано' };
/** What the renderer gets when the link to main is refused (`callApi`), so a read fails. */
const REFUSED = { code: 'BRIDGE_ERROR', message: "Не вдалося зв'язатися з локальним рушієм. Спробуйте ще раз" };

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
    created_at: '2026-11-17T10:00:00Z',
    ...overrides,
  };
}

function jobOf(state: string, overrides: Partial<JobDto> = {}): JobDto {
  return { id: JOB_ID, type: 'analysis', state, progress: {}, error: null, ...overrides };
}

function runOf(overrides: Partial<AnalysisRunDto> = {}): AnalysisRunDto {
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
    status: 'running',
    error_code: null,
    created_at: '2026-11-17T10:05:00Z',
    updated_at: '2026-11-17T10:05:00Z',
    job_id: JOB_ID,
    committed_micro_usd: 0,
    windows: { total: 3, succeeded: 1, failed: 0, unknown_outcome: 0, pending: 2 },
    clip_ids: [],
    stale: false,
    diagnostics_summary: {},
    ...overrides,
  };
}

/** A stopped search: its job is over with the given error, and its run says so. */
function stoppedRun(windows: Partial<AnalysisRunDto['windows']> = {}): AnalysisRunDto {
  return runOf({
    status: 'interrupted',
    windows: { total: 3, succeeded: 1, failed: 0, unknown_outcome: 0, pending: 2, ...windows },
  });
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

function estimateOf(overrides: Partial<AnalysisEstimateDto> = {}): AnalysisEstimateDto {
  return {
    model: 'test/selector',
    unit_level: 'segment',
    unit_count: 2,
    window_count: 1,
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

/** What the fake main process holds for the source. The tests change it between the calls. */
interface Server {
  transcript: TranscriptDto | null;
  run: AnalysisRunDto | null;
  job: JobDto | null;
}

/**
 * Puts a fake bridge in place, answering from `server`, and renders the hook for the source. The view
 * is on screen until a test takes it off with `setActive` (ADR 041 §23(9)).
 */
function setup(server: Server, extra: ApiHandlers = {}) {
  const installed = installBridge({
    handlers: {
      'transcripts.list': () =>
        ok({ items: server.transcript === null ? [] : [server.transcript], next_cursor: null }),
      'analysis.list': () => ok({ items: server.run === null ? [] : [itemOf(server.run)] }),
      'analysis.get': () =>
        server.run === null ? failure('ANALYSIS_RUN_NOT_FOUND', 'Пошук не знайдено') : ok(server.run),
      'jobs.get': () =>
        server.job === null ? failure('JOB_NOT_FOUND', 'Завдання не знайдено') : ok(server.job),
      'analysis.estimate': () => ok(estimateOf()),
      'analysis.create': () => ok({ id: RUN_ID, job_id: JOB_ID, status: 'queued' }),
      'jobs.resume': () => ok(jobOf('queued')),
      'jobs.cancel': () => ok(jobOf('cancelling')),
      ...extra,
    },
  });
  const screen = { active: true };
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(ViewActiveContext, { value: screen.active }, children);
  const hook = renderHook(() => useAnalysis(SOURCE_ID), { wrapper });
  /** Takes the view off the screen or puts it back, as the settings screen does. */
  const setActive = (active: boolean): void => {
    screen.active = active;
    act(() => {
      hook.rerender();
    });
  };
  return { installed, hook, setActive };
}

/** The params of every call of one op, in the order they were sent. */
function callsOf(installed: InstalledBridge, op: ApiOp): unknown[] {
  return installed.call.mock.calls.filter(([called]) => called === op).map(([, params]) => params);
}

/** Waits until the first read is applied. */
async function loaded(hook: ReturnType<typeof setup>['hook']): Promise<void> {
  await waitFor(() => {
    expect(hook.result.current.state.busy).toBe(false);
  });
}

/**
 * Lets fake time pass inside `act`. The fake clock yields to pending promises around every timer, so
 * a poll and the read it starts are done when this returns.
 */
async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

function withDraft(overrides: Partial<ProfileDraft>): ProfileDraft {
  return { ...DEFAULT_DRAFT, ...overrides };
}

describe('useAnalysis without a transcript', () => {
  it('sends no estimate while the source has no transcript', async () => {
    const server: Server = { transcript: null, run: null, job: null };
    const { installed, hook } = setup(server);
    await loaded(hook);

    expect(hook.result.current.state.transcript).toBeNull();
    expect(hook.result.current.state.view).toEqual({ kind: 'idle' });

    act(() => {
      hook.result.current.actions.estimate();
    });

    expect(callsOf(installed, 'analysis.estimate')).toHaveLength(0);
    expect(hook.result.current.state.estimate).toBeNull();
  });

  it('sends no estimate for a partial transcript until partial results are allowed', async () => {
    const server: Server = {
      transcript: transcriptOf({ coverage: [{ start_ms: 0, end_ms: 12_000, state: 'failed' }] }),
      run: null,
      job: null,
    };
    const { installed, hook } = setup(server);
    await loaded(hook);

    act(() => {
      hook.result.current.actions.estimate();
    });
    expect(callsOf(installed, 'analysis.estimate')).toHaveLength(0);

    act(() => {
      hook.result.current.state.setDraft(withDraft({ allowPartial: true }));
    });
    act(() => {
      hook.result.current.actions.estimate();
    });
    await waitFor(() => {
      expect(hook.result.current.state.estimate).not.toBeNull();
    });
    expect(callsOf(installed, 'analysis.estimate')).toHaveLength(1);
  });
});

describe('estimate and create', () => {
  it('creates the search with the same profile and transcript as its estimate', async () => {
    const server: Server = { transcript: transcriptOf(), run: null, job: null };
    const { installed, hook } = setup(server);
    await loaded(hook);

    act(() => {
      hook.result.current.state.setDraft(
        withDraft({
          categories: ['fails', 'funny'],
          customInstruction: 'Тільки жарти',
          minSeconds: 20,
          maxSeconds: 60,
          maxCandidates: 7,
        }),
      );
    });
    act(() => {
      hook.result.current.actions.estimate();
    });
    await waitFor(() => {
      expect(hook.result.current.state.estimate).not.toBeNull();
    });
    act(() => {
      hook.result.current.actions.create();
    });
    await waitFor(() => {
      expect(callsOf(installed, 'analysis.create')).toHaveLength(1);
    });

    const estimateParams = callsOf(installed, 'analysis.estimate')[0];
    expect(estimateParams).toEqual({
      source_id: SOURCE_ID,
      transcript_id: TRANSCRIPT_ID,
      profile: {
        // The categories in their canonical order, whatever order the draft has.
        categories: ['funny', 'fails'],
        custom_instruction: 'Тільки жарти',
        min_duration_ms: 20_000,
        max_duration_ms: 60_000,
        max_candidates: 7,
        allow_partial: false,
      },
    });
    expect(callsOf(installed, 'analysis.create')[0]).toEqual(estimateParams);
  });

  it('clears the estimate on any change of the draft, so that create sends nothing', async () => {
    const server: Server = { transcript: transcriptOf(), run: null, job: null };
    const { installed, hook } = setup(server);
    await loaded(hook);

    act(() => {
      hook.result.current.actions.estimate();
    });
    await waitFor(() => {
      expect(hook.result.current.state.estimate).not.toBeNull();
    });

    act(() => {
      hook.result.current.state.setDraft(withDraft({ minSeconds: 20 }));
    });
    expect(hook.result.current.state.estimate).toBeNull();

    act(() => {
      hook.result.current.actions.create();
    });
    expect(callsOf(installed, 'analysis.create')).toHaveLength(0);
  });

  it('does not show an estimate that was answered after the draft changed', async () => {
    let answer: (value: AnalysisEstimateDto) => void = () => undefined;
    const held = new Promise<AnalysisEstimateDto>((resolve) => {
      answer = resolve;
    });
    const server: Server = { transcript: transcriptOf(), run: null, job: null };
    const { hook } = setup(server, {
      'analysis.estimate': () => held.then((dto) => ok(dto)),
    });
    await loaded(hook);

    act(() => {
      hook.result.current.actions.estimate();
    });
    expect(hook.result.current.state.busy).toBe(true);
    act(() => {
      hook.result.current.state.setDraft(withDraft({ maxCandidates: 3 }));
    });
    await act(async () => {
      answer(estimateOf());
      await flushAnswers();
    });
    // The answer is in: the request is no longer on its way, yet the estimate is not shown.
    await loaded(hook);

    expect(hook.result.current.state.estimate).toBeNull();
  });
});

describe('resume and cancel of a stopped search', () => {
  it('sends retry_unknown only when resume is called with true', async () => {
    const server: Server = {
      transcript: transcriptOf(),
      run: stoppedRun({ unknown_outcome: 1, pending: 1 }),
      job: jobOf('failed', { error: UNKNOWN_ERROR }),
    };
    const { installed, hook } = setup(server);
    await loaded(hook);
    expect(hook.result.current.state.view.kind).toBe('stopped');

    act(() => {
      hook.result.current.actions.resume(false);
    });
    await waitFor(() => {
      expect(callsOf(installed, 'jobs.resume')).toHaveLength(1);
    });
    await loaded(hook);
    expect(callsOf(installed, 'jobs.resume')[0]).toEqual({ id: JOB_ID });
    expect(callsOf(installed, 'jobs.resume')[0]).not.toHaveProperty('retry_unknown');

    act(() => {
      hook.result.current.actions.resume(true);
    });
    await waitFor(() => {
      expect(callsOf(installed, 'jobs.resume')).toHaveLength(2);
    });
    expect(callsOf(installed, 'jobs.resume')[1]).toEqual({ id: JOB_ID, retry_unknown: true });
  });

  it('sends budget_usd, and only that, for a resume with a new ceiling', async () => {
    const server: Server = {
      transcript: transcriptOf(),
      run: stoppedRun(),
      job: jobOf('failed', { error: BUDGET_ERROR }),
    };
    const { installed, hook } = setup(server);
    await loaded(hook);
    expect(hook.result.current.state.view).toMatchObject({ kind: 'stopped', byBudget: true });

    act(() => {
      hook.result.current.actions.resumeWithBudget('3.50');
    });
    await waitFor(() => {
      expect(callsOf(installed, 'jobs.resume')).toHaveLength(1);
    });
    const params = callsOf(installed, 'jobs.resume')[0];
    expect(params).toEqual({ id: JOB_ID, budget_usd: '3.50' });
    expect(params).not.toHaveProperty('retry_unknown');
  });

  it('sends budget_usd and retry_unknown with a resume that has a new ceiling and a confirmed resend', async () => {
    const server: Server = {
      transcript: transcriptOf(),
      run: stoppedRun({ unknown_outcome: 1, pending: 1 }),
      job: jobOf('failed', { error: BUDGET_ERROR }),
    };
    const { installed, hook } = setup(server);
    await loaded(hook);
    expect(hook.result.current.state.view).toMatchObject({
      kind: 'stopped',
      byBudget: true,
      unknownCount: 1,
    });

    act(() => {
      hook.result.current.actions.resumeWithBudget('3.50', true);
    });
    await waitFor(() => {
      expect(callsOf(installed, 'jobs.resume')).toHaveLength(1);
    });
    await loaded(hook);

    expect(callsOf(installed, 'jobs.resume')[0]).toEqual({
      id: JOB_ID,
      budget_usd: '3.50',
      retry_unknown: true,
    });
  });

  it('cancels a running search by its job', async () => {
    const server: Server = {
      transcript: transcriptOf(),
      run: runOf(),
      job: jobOf('running'),
    };
    const { installed, hook } = setup(server);
    await loaded(hook);
    expect(hook.result.current.state.view.kind).toBe('running');

    act(() => {
      hook.result.current.actions.cancel();
    });
    await waitFor(() => {
      expect(callsOf(installed, 'jobs.cancel')).toEqual([{ id: JOB_ID }]);
    });
  });
});

describe('polling a running search', () => {
  it('asks about the search once a second, and stops when its job is over', async () => {
    vi.useFakeTimers();
    try {
      const server: Server = { transcript: transcriptOf(), run: runOf(), job: jobOf('running') };
      const { installed, hook } = setup(server);
      await advance(0);
      expect(hook.result.current.state.view.kind).toBe('running');

      const jobReads = (): number => callsOf(installed, 'jobs.get').length;
      const before = jobReads();
      await advance(ANALYSIS_POLL_MS);
      expect(jobReads()).toBe(before + 1);

      server.run = runOf({
        status: 'completed',
        windows: { total: 3, succeeded: 3, failed: 0, unknown_outcome: 0, pending: 0 },
        clip_ids: ['clip-1'],
      });
      server.job = jobOf('succeeded');
      await advance(ANALYSIS_POLL_MS);
      expect(hook.result.current.state.view.kind).toBe('completed');

      const afterClose = jobReads();
      const runReads = callsOf(installed, 'analysis.get').length;
      await advance(ANALYSIS_POLL_MS * 5);
      expect(jobReads()).toBe(afterClose);
      expect(callsOf(installed, 'analysis.get')).toHaveLength(runReads);
    } finally {
      vi.useRealTimers();
    }
  });

  it('shows a search that completed as completed when its job closes, before the read after it is answered', async () => {
    vi.useFakeTimers();
    try {
      const completed = runOf({
        status: 'completed',
        windows: { total: 3, succeeded: 3, failed: 0, unknown_outcome: 0, pending: 0 },
        clip_ids: ['clip-1'],
      });
      const gate = deferred<null>();
      let holdTranscripts = false;
      let closeOnNextJobRead = false;
      const server: Server = { transcript: transcriptOf(), run: runOf(), job: jobOf('running') };
      const { hook } = setup(server, {
        'transcripts.list': async () => {
          if (holdTranscripts) {
            await gate.promise;
          }
          return ok({ items: server.transcript === null ? [] : [server.transcript], next_cursor: null });
        },
        // The backend writes the run as completed, then closes the job. The closing poll reads the run
        // before that and the job after it, so only the read of the run that follows can show completion.
        'jobs.get': () => {
          if (closeOnNextJobRead) {
            closeOnNextJobRead = false;
            server.run = completed;
            server.job = jobOf('succeeded');
          }
          return server.job === null ? failure('JOB_NOT_FOUND', 'Завдання не знайдено') : ok(server.job);
        },
      });
      await advance(0);
      expect(hook.result.current.state.view.kind).toBe('running');

      closeOnNextJobRead = true;
      holdTranscripts = true;
      await advance(ANALYSIS_POLL_MS);

      // The read that follows the closing poll is still on its way.
      expect(hook.result.current.state.busy).toBe(true);
      expect(hook.result.current.state.view.kind).toBe('completed');

      gate.resolve(null);
      await advance(0);
      expect(hook.result.current.state.busy).toBe(false);
      expect(hook.result.current.state.view.kind).toBe('completed');
    } finally {
      vi.useRealTimers();
    }
  });

  it('shows a panel error after five failed polls in a row, and asks nothing more', async () => {
    vi.useFakeTimers();
    try {
      const linkDown = { run: false, job: false };
      const run = runOf();
      const job = jobOf('running');
      const server: Server = { transcript: transcriptOf(), run, job };
      const { installed, hook } = setup(server, {
        'analysis.get': () => (linkDown.run ? failure(REFUSED.code, REFUSED.message, true) : ok(run)),
        'jobs.get': () => (linkDown.job ? failure(REFUSED.code, REFUSED.message, true) : ok(job)),
      });
      await advance(0);
      expect(hook.result.current.state.view.kind).toBe('running');

      // Each poll reads the run and then the job; the failed read alternates between the two.
      const downs = ['run', 'job', 'run', 'job', 'run'] as const;
      for (const [index, down] of downs.entries()) {
        linkDown[down] = true;
        await advance(ANALYSIS_POLL_MS);
        linkDown[down] = false;
        if (index < downs.length - 1) {
          expect(hook.result.current.state.error).toBeNull();
        }
      }
      expect(hook.result.current.state.error?.code).toBe(REFUSED.code);
      expect(hook.result.current.state.view.kind).toBe('running');

      const callsAfterError = installed.call.mock.calls.length;
      await advance(ANALYSIS_POLL_MS * 5);
      expect(installed.call.mock.calls.length).toBe(callsAfterError);
    } finally {
      vi.useRealTimers();
    }
  });

  it('counts a failed run read after the job closed as a miss: five in a row show the panel error, and nothing more is asked', async () => {
    vi.useFakeTimers();
    try {
      const run = runOf();
      const server: Server = { transcript: transcriptOf(), run, job: jobOf('running') };
      // A poll that finds the job closed reads the run once more, right after the job. The job read sets
      // `closingRead`, and the read of the run that follows takes it: that read is the one refused here.
      let closingRead = false;
      const { installed, hook } = setup(server, {
        'jobs.get': () => {
          if (server.job === null) {
            return failure('JOB_NOT_FOUND', 'Завдання не знайдено');
          }
          closingRead = !isJobOpen(server.job.state);
          return ok(server.job);
        },
        'analysis.get': () => {
          const refused = closingRead;
          closingRead = false;
          return refused ? failure(REFUSED.code, REFUSED.message, true) : ok(run);
        },
      });
      const jobReads = (): number => callsOf(installed, 'jobs.get').length;
      await advance(0);
      expect(hook.result.current.state.view.kind).toBe('running');

      server.job = jobOf('succeeded');
      for (let poll = 1; poll <= 5; poll += 1) {
        const before = jobReads();
        await advance(ANALYSIS_POLL_MS);
        // Every poll reads the job, so each one ran, and the miss schedules the next one.
        expect(jobReads()).toBe(before + 1);
        if (poll < 5) {
          expect(hook.result.current.state.error).toBeNull();
        }
      }
      expect(hook.result.current.state.error?.code).toBe(REFUSED.code);
      // The closed job was never applied: the panel still shows the search as running.
      expect(hook.result.current.state.job?.state).toBe('running');
      expect(hook.result.current.state.view.kind).toBe('running');

      const callsAfterError = installed.call.mock.calls.length;
      await advance(ANALYSIS_POLL_MS * 5);
      expect(installed.call.mock.calls.length).toBe(callsAfterError);
    } finally {
      vi.useRealTimers();
    }
  });

  it('sends nothing and changes nothing when the hook unmounts while the closing run read is pending', async () => {
    vi.useFakeTimers();
    const consoleError = vi.spyOn(console, 'error');
    try {
      const run = runOf();
      const server: Server = { transcript: transcriptOf(), run, job: jobOf('running') };
      // A poll that finds the job closed reads the run once more, right after the job. The job read sets
      // `closingRead`, and the read of the run that follows takes it: that read is held until the test answers.
      let closingRead = false;
      const held = deferred<ApiResult<AnalysisRunDto>>();
      const { installed, hook } = setup(server, {
        'jobs.get': () => {
          if (server.job === null) {
            return failure('JOB_NOT_FOUND', 'Завдання не знайдено');
          }
          closingRead = !isJobOpen(server.job.state);
          return ok(server.job);
        },
        'analysis.get': () => {
          const closing = closingRead;
          closingRead = false;
          return closing ? held.promise : ok(run);
        },
      });
      await advance(0);
      expect(hook.result.current.state.view.kind).toBe('running');

      server.job = jobOf('succeeded');
      const readsBeforePoll = callsOf(installed, 'analysis.get').length;
      await advance(ANALYSIS_POLL_MS);
      // The closing poll has read the run and the job, and waits for the read of the run that follows.
      expect(callsOf(installed, 'analysis.get')).toHaveLength(readsBeforePoll + 2);

      hook.unmount();
      const callsAtUnmount = installed.call.mock.calls.length;

      // The late answer is a refusal. If the poll were not stopped, the refusal would be a miss and schedule
      // another poll. React drops the state updates of an unmounted hook, so only that poll would show.
      act(() => {
        held.resolve(failure(REFUSED.code, REFUSED.message, true));
      });
      await advance(ANALYSIS_POLL_MS * 5);

      expect(installed.call.mock.calls.length).toBe(callsAtUnmount);
      expect(consoleError).not.toHaveBeenCalled();
    } finally {
      consoleError.mockRestore();
      vi.useRealTimers();
    }
  });
});

describe('an out-of-date panel is read again', () => {
  it('re-reads the transcript and clears the estimate on TRANSCRIPT_REVISION_STALE', async () => {
    const server: Server = { transcript: transcriptOf({ id: TRANSCRIPT_ID }), run: null, job: null };
    const { installed, hook } = setup(server, {
      'analysis.create': () => failure('TRANSCRIPT_REVISION_STALE', 'Транскрипт змінився'),
    });
    await loaded(hook);

    act(() => {
      hook.result.current.actions.estimate();
    });
    await waitFor(() => {
      expect(hook.result.current.state.estimate).not.toBeNull();
    });
    const transcriptReads = callsOf(installed, 'transcripts.list').length;

    server.transcript = transcriptOf({ id: 'tr-2', revision: 2 });
    act(() => {
      hook.result.current.actions.create();
    });
    await waitFor(() => {
      expect(hook.result.current.state.error?.code).toBe('TRANSCRIPT_REVISION_STALE');
    });
    await waitFor(() => {
      expect(hook.result.current.state.transcript?.id).toBe('tr-2');
    });

    expect(callsOf(installed, 'transcripts.list').length).toBeGreaterThan(transcriptReads);
    expect(hook.result.current.state.estimate).toBeNull();
  });

  it('re-reads the search on ANALYSIS_RUN_ACTIVE', async () => {
    const server: Server = { transcript: transcriptOf(), run: null, job: null };
    const { installed, hook } = setup(server, {
      'analysis.create': () => failure('ANALYSIS_RUN_ACTIVE', 'Пошук уже виконується'),
    });
    await loaded(hook);

    act(() => {
      hook.result.current.actions.estimate();
    });
    await waitFor(() => {
      expect(hook.result.current.state.estimate).not.toBeNull();
    });
    const listReads = callsOf(installed, 'analysis.list').length;

    // Another search was started elsewhere: the list shows it when it is read again.
    server.run = runOf({ id: 'run-2', status: 'queued', job_id: 'job-2' });
    server.job = jobOf('queued', { id: 'job-2' });
    act(() => {
      hook.result.current.actions.create();
    });
    await waitFor(() => {
      expect(hook.result.current.state.error?.code).toBe('ANALYSIS_RUN_ACTIVE');
    });
    await waitFor(() => {
      expect(hook.result.current.state.view.kind).toBe('running');
    });

    expect(callsOf(installed, 'analysis.list').length).toBeGreaterThan(listReads);
    expect(hook.result.current.state.run?.id).toBe('run-2');
  });
});

describe('an estimate when the view comes back (ADR 041 §23(9))', () => {
  it('drops a shown estimate when the view comes back, and create sends nothing for it', async () => {
    const server: Server = { transcript: transcriptOf(), run: null, job: null };
    const { installed, hook, setActive } = setup(server);
    await loaded(hook);

    act(() => {
      hook.result.current.actions.estimate();
    });
    await waitFor(() => {
      expect(hook.result.current.state.estimate).not.toBeNull();
    });

    setActive(false);
    setActive(true);
    await loaded(hook);

    expect(hook.result.current.state.estimate).toBeNull();
    act(() => {
      hook.result.current.actions.create();
    });
    expect(callsOf(installed, 'analysis.create')).toHaveLength(0);
  });
});

describe('restart', () => {
  it('shows the start for a new search and sends nothing, and reload does not bring the old one back', async () => {
    const server: Server = {
      transcript: transcriptOf(),
      run: stoppedRun(),
      job: jobOf('failed', { error: BUDGET_ERROR }),
    };
    const { installed, hook } = setup(server);
    await loaded(hook);
    expect(hook.result.current.state.view.kind).toBe('stopped');

    act(() => {
      hook.result.current.actions.restart();
    });
    expect(hook.result.current.state.view).toEqual({ kind: 'idle' });

    act(() => {
      hook.result.current.actions.reload();
    });
    await loaded(hook);

    expect(hook.result.current.state.view).toEqual({ kind: 'idle' });
    expect(callsOf(installed, 'analysis.create')).toHaveLength(0);
    expect(callsOf(installed, 'jobs.resume')).toHaveLength(0);
    expect(callsOf(installed, 'analysis.estimate')).toHaveLength(0);
  });
});

describe('a new transcript revision (ADR 041 §23(10))', () => {
  const OTHER_SOURCE_ID = 'c3e5a7b9-1d2f-4a6c-8e0b-2d4f6a8c0e1b';

  it('re-reads the newest transcript and clears a shown estimate when the source is notified', async () => {
    const server: Server = { transcript: transcriptOf(), run: null, job: null };
    const { installed, hook } = setup(server);
    await loaded(hook);

    act(() => {
      hook.result.current.actions.estimate();
    });
    await waitFor(() => {
      expect(hook.result.current.state.estimate).not.toBeNull();
    });
    const transcriptReads = callsOf(installed, 'transcripts.list').length;

    server.transcript = transcriptOf({ id: 'tr-2', revision: 2 });
    act(() => {
      notifyTranscriptChanged(SOURCE_ID);
    });

    // The estimate is gone at once, before the read is answered.
    expect(hook.result.current.state.estimate).toBeNull();
    await waitFor(() => {
      expect(hook.result.current.state.transcript?.id).toBe('tr-2');
    });
    expect(callsOf(installed, 'transcripts.list').length).toBeGreaterThan(transcriptReads);
    expect(hook.result.current.state.estimate).toBeNull();
  });

  it('reads nothing when another source is notified', async () => {
    const server: Server = { transcript: transcriptOf(), run: null, job: null };
    const { installed, hook } = setup(server);
    await loaded(hook);
    const transcriptReads = callsOf(installed, 'transcripts.list').length;

    act(() => {
      notifyTranscriptChanged(OTHER_SOURCE_ID);
    });
    await act(async () => {
      await flushAnswers();
    });

    expect(callsOf(installed, 'transcripts.list')).toHaveLength(transcriptReads);
  });

  it('stops listening once the panel is gone', async () => {
    const server: Server = { transcript: transcriptOf(), run: null, job: null };
    const { installed, hook } = setup(server);
    await loaded(hook);
    hook.unmount();
    const transcriptReads = callsOf(installed, 'transcripts.list').length;

    notifyTranscriptChanged(SOURCE_ID);
    await act(async () => {
      await flushAnswers();
    });

    expect(callsOf(installed, 'transcripts.list')).toHaveLength(transcriptReads);
  });
});
