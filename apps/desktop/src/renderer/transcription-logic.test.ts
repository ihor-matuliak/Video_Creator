import { describe, expect, it } from 'vitest';

import type {
  AudioStreamDto,
  JobDto,
  TranscriptionChunkDto,
  TranscriptionEstimateDto,
  TranscriptionRunDto,
} from '../shared/api';
import {
  UNKNOWN_OUTCOME_TEXT,
  actionMessage,
  countChunks,
  defaultAudioStream,
  deriveView,
  describeLanguage,
  describeModel,
  formatUpToUsd,
  formatUsd,
  isJobOpen,
  keyLimitText,
  jobNeedsSettings,
  jobReason,
  needsSettings,
  openStage,
  partialText,
  partsText,
  pluralUk,
  progressText,
  resendQuestion,
  resolveAudioStream,
  retryCount,
  runStage,
  startBlockReason,
  stoppedByBudget,
  stoppedText,
  summaryRows,
  unavailableText,
} from './transcription-logic';
import type { Snapshot } from './transcription-logic';

function stream(index: number, isDefault: boolean): AudioStreamDto {
  return { index, codec: 'aac', channels: 2, language: null, title: null, default: isDefault, start_offset_ms: 0 };
}

function chunk(state: string, id = state): TranscriptionChunkDto {
  return { id, core_start_ms: 0, core_end_ms: 1000, state, attempts: 1, error_code: null };
}

function estimate(overrides: Partial<TranscriptionEstimateDto> = {}): TranscriptionEstimateDto {
  return {
    source_id: 's',
    audio_stream_index: 1,
    model: 'microsoft/mai-transcribe-2',
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

function run(status: string, chunks: readonly TranscriptionChunkDto[] = []): TranscriptionRunDto {
  return {
    id: 'run-1',
    source_id: 's',
    job_id: 'job-1',
    model: 'microsoft/mai-transcribe-2',
    status,
    revision_id: null,
    error_code: null,
    committed_micro_usd: 0,
    chunks,
  };
}

function job(state: string, error: JobDto['error'] = null): JobDto {
  return { id: 'job-1', type: 'transcription', state, progress: {}, error };
}

describe('the audio track of a run', () => {
  it('is the default track, else the first, else none', () => {
    expect(defaultAudioStream([stream(1, false), stream(2, true)])).toBe(2);
    expect(defaultAudioStream([stream(4, false), stream(7, false)])).toBe(4);
    expect(defaultAudioStream([])).toBeNull();
  });

  it('keeps the track the user picked while the source still has it', () => {
    const streams = [stream(1, true), stream(2, false)];
    expect(resolveAudioStream(streams, 2, 1)).toBe(2);
    expect(resolveAudioStream(streams, 9, 1)).toBe(1);
    expect(resolveAudioStream(streams, null, 1)).toBe(1);
  });
});

describe('counting the parts of a run', () => {
  it('counts the plan, leaving out the parts that were split in two', () => {
    const counts = countChunks([
      chunk('succeeded', 'a'),
      chunk('succeeded', 'b'),
      chunk('running', 'c'),
      chunk('pending', 'd'),
      chunk('superseded', 'e'),
      chunk('failed', 'f'),
      chunk('unknown_outcome', 'g'),
    ]);

    expect(counts).toEqual({ total: 6, succeeded: 2, failed: 1, unknown: 1 });
    expect(progressText(counts)).toBe('Розпізнаємо мовлення · 2 із 6 частин');
  });

  it('counts what a plain retry would send: not done and not of unknown outcome', () => {
    expect(retryCount({ total: 6, succeeded: 2, failed: 1, unknown: 1 })).toBe(3);
  });

  it('declines the nouns by number, the Ukrainian way', () => {
    expect(['1', '2', '4', '5', '11', '12', '21', '22', '25'].map((n) => pluralUk(Number(n), 'а', 'б', 'в'))).toEqual(
      ['а', 'б', 'б', 'в', 'в', 'в', 'а', 'б', 'в'],
    );
    expect(partsText(1)).toBe('1 частини');
    expect(partsText(3)).toBe('3 частин');
    expect(partsText(7)).toBe('7 частин');
    expect(partsText(21)).toBe('21 частини');
  });

  it('words a partial run as docs/03 does', () => {
    expect(partialText(1)).toBe('Мовлення розпізнано не повністю: 1 ділянка потребує повтору');
    expect(partialText(2)).toBe('Мовлення розпізнано не повністю: 2 ділянки потребують повтору');
    expect(partialText(5)).toBe('Мовлення розпізнано не повністю: 5 ділянок потребують повтору');
    expect(partialText(0)).toBe('Мовлення розпізнано не повністю');
  });
});

describe('the stage of a run', () => {
  it('follows the status, and knows nothing about a completed or an unknown one', () => {
    expect(['queued', 'running', 'partial', 'failed', 'interrupted', 'cancelled', 'completed', 'new'].map(openStage)).toEqual(
      ['active', 'active', 'retry', 'retry', 'resume', 'resume', null, null],
    );
  });

  it('is active again when a resumed job is open while its run still has the old status', () => {
    expect(runStage(run('partial'), job('queued'))).toBe('active');
    expect(runStage(run('interrupted'), job('running'))).toBe('active');
    expect(runStage(run('partial'), job('failed'))).toBe('retry');
    expect(runStage(run('partial'), null)).toBe('retry');
    // A run written as completed while its job still works is watched until the job is over (L3).
    expect(runStage(run('completed'), job('queued'))).toBe('active');
    expect(runStage(run('completed'), job('cancelling'))).toBe('active');
    expect(runStage(run('completed'), job('succeeded'))).toBeNull();
    expect(runStage(run('completed'), null)).toBeNull();
    expect(['queued', 'running', 'cancelling', 'failed', 'succeeded'].map(isJobOpen)).toEqual([
      true,
      true,
      true,
      false,
      false,
    ]);
  });

  it('words a stopped run, and adds the reason of the job unless it only says the run is partial', () => {
    expect(stoppedText('partial', 2)).toBe('Мовлення розпізнано не повністю: 2 ділянки потребують повтору');
    expect(stoppedText('failed', 2)).toBe('Розпізнавання не вдалося');
    expect(stoppedText('cancelled', 0)).toBe('Розпізнавання скасовано');
    expect(stoppedText('interrupted', 0)).toBe('Розпізнавання перервано');
    expect(jobReason(job('failed', { code: 'STT_AUTH', message: 'Ключ не прийнято' }))).toBe('Ключ не прийнято');
    expect(jobReason(job('failed', { code: 'TRANSCRIPTION_PARTIAL', message: 'Розпізнано лише частину' }))).toBeNull();
    expect(jobReason(job('failed'))).toBeNull();
    expect(jobReason(null)).toBeNull();
  });

  it('does not repeat the reason of an unknown outcome, the unknown block says it (L8)', () => {
    expect(jobReason(job('failed', { code: 'STT_UNKNOWN_OUTCOME', message: 'Відповідь не отримано' }))).toBeNull();
  });

  it('says why a missing or changed file cannot be recognised, and nothing for an available one', () => {
    expect(unavailableText('missing')).toBe('Файл не знайдено на цьому місці: розпізнавання неможливе.');
    expect(unavailableText('changed')).toBe('Файл змінено після додавання: розпізнавання неможливе.');
    expect(unavailableText('available')).toBeNull();
    expect(unavailableText('pending')).toBeNull();
  });

  it('names the ceiling of a confirmed resend of a budget stop, and names none without one', () => {
    expect(resendQuestion(null)).toBe(
      'Провайдер міг уже обробити цей запит і списати кошти. Якщо надіслати ще раз, за нього можуть заплатити вдруге.',
    );
    expect(resendQuestion('3.00')).toBe(
      'Провайдер міг уже обробити цей запит і списати кошти. Якщо надіслати ще раз, за нього можуть заплатити вдруге. Продовження піде з лімітом витрат $3,00.',
    );
  });
});

describe('what is shown', () => {
  const ready = (overrides: Partial<Extract<Snapshot, { readonly ok: true }>> = {}): Snapshot => ({
    ok: true,
    estimate: { ok: true, data: estimate() },
    hasKey: true,
    budgetUsd: null,
    run: null,
    ...overrides,
  });

  it('loads until the first answer', () => {
    expect(deriveView(null, null)).toEqual({ kind: 'loading' });
  });

  it('shows the summary and the start when there is no run to deal with', () => {
    expect(deriveView(ready(), null)).toEqual({ kind: 'start', estimate: estimate(), hasKey: true, restarted: false });
  });

  it('shows the summary of a new run, and says so, when the user chose to start over', () => {
    const stopped = run('partial', [chunk('succeeded', 'a'), chunk('failed', 'b')]);
    const snapshot = ready({ run: { run: stopped, job: job('failed') } });

    expect(deriveView(snapshot, null, stopped.id)).toEqual({
      kind: 'start',
      estimate: estimate(),
      hasKey: true,
      restarted: true,
    });
    // Another run is not the one that was started over: it stays on screen.
    expect(deriveView(snapshot, null, 'another')).toMatchObject({ kind: 'run', stage: 'retry' });
  });

  it('carries the ceiling of the settings for a run that stopped', () => {
    const stopped = run('partial', [chunk('failed', 'a')]);

    expect(deriveView(ready({ budgetUsd: '3.00', run: { run: stopped, job: job('failed') } }), null)).toMatchObject({
      kind: 'run',
      budgetUsd: '3.00',
    });
  });

  it('shows the run that is not complete, with its counts', () => {
    const stopped = run('partial', [chunk('succeeded', 'a'), chunk('failed', 'b')]);

    const view = deriveView(ready({ run: { run: stopped, job: job('failed') } }), null);

    expect(view).toMatchObject({
      kind: 'run',
      stage: 'retry',
      counts: { total: 2, succeeded: 1, failed: 1, unknown: 0 },
      cancelling: false,
    });
  });

  it('knows that a stop was asked for, here or before the screen was opened', () => {
    const going = run('running', [chunk('running')]);

    expect(deriveView(ready({ run: { run: going, job: job('running') } }), 'run-1')).toMatchObject({ cancelling: true });
    expect(deriveView(ready({ run: { run: going, job: job('cancelling') } }), null)).toMatchObject({ cancelling: true });
    expect(deriveView(ready({ run: { run: going, job: job('running') } }), 'another')).toMatchObject({ cancelling: false });
  });

  it('goes back to the summary when the run turned out complete', () => {
    expect(deriveView(ready({ run: { run: run('completed'), job: job('succeeded') } }), null).kind).toBe('start');
  });

  it('keeps the run on screen while its job is open, though the run says it completed (L3)', () => {
    expect(deriveView(ready({ run: { run: run('completed'), job: job('running') } }), null)).toMatchObject({
      kind: 'run',
      stage: 'active',
      cancelling: false,
    });
  });

  it('does not need the estimate to show a run, but needs it to show the start', () => {
    const lost = { ok: false, error: { code: 'X', message: 'Немає оцінки', retryable: false } } as const;

    expect(deriveView(ready({ estimate: lost, run: { run: run('running'), job: null } }), null).kind).toBe('run');
    expect(deriveView(ready({ estimate: lost }), null)).toEqual({ kind: 'failed', error: lost.error });
  });

  it('reports a read that failed as a whole', () => {
    const error = { code: 'BRIDGE_ERROR', message: 'Немає зв’язку', retryable: true };
    expect(deriveView({ ok: false, error }, null)).toEqual({ kind: 'failed', error });
  });
});

describe('the summary before a paid start', () => {
  it('rounds the estimate up to the next cent', () => {
    expect([0, 1, 1200, 10_000, 10_001, 30_000, 123_456, 2_500_000].map(formatUpToUsd)).toEqual([
      'до $0,00',
      'до $0,01',
      'до $0,01',
      'до $0,01',
      'до $0,02',
      'до $0,03',
      'до $0,13',
      'до $2,50',
    ]);
  });

  it('writes a budget with a decimal comma and at least two decimals', () => {
    expect(['2.50', '2.5', '10', '0.015', '1000'].map(formatUsd)).toEqual(['$2,50', '$2,50', '$10,00', '$0,015', '$1000,00']);
    expect(formatUsd('abc')).toBe('$abc');
  });

  it('says what is left of a key limit: none set, used up at zero or less, or the sum left', () => {
    expect(keyLimitText(null)).toBe('Ліміт витрат для ключа не задано');
    expect(['0', '0.00', '0.000000', '-0.5'].map(keyLimitText)).toEqual(
      Array(4).fill('Ліміт витрат ключа вичерпано'),
    );
    expect(keyLimitText('0.01')).toBe('Залишок ліміту: $0,01');
    expect(keyLimitText('4.25')).toBe('Залишок ліміту: $4,25');
  });

  it('names the model and the language, and «Автовизначення» for none', () => {
    expect(describeModel('microsoft/mai-transcribe-2')).toBe('MAI-Transcribe 2 (Microsoft)');
    expect(describeModel('vendor/new-model')).toBe('vendor/new-model');
    expect(describeLanguage(null)).toBe('Автовизначення');
    expect(describeLanguage('es')).toBe('Español');
    expect(describeLanguage('de')).toBe('de');
  });

  it('is six lines: duration, model, language, parts, cost, ceiling', () => {
    expect(summaryRows(estimate({ chunk_count: 3, language: 'en', estimate_micro_usd: 30_000 }))).toEqual([
      { id: 'duration', label: 'Тривалість аудіо', value: '00:00:12.000' },
      { id: 'model', label: 'Модель', value: 'MAI-Transcribe 2 (Microsoft)' },
      { id: 'language', label: 'Мова', value: 'English' },
      { id: 'parts', label: 'Частини', value: '3' },
      { id: 'cost', label: 'Орієнтовна вартість', value: 'до $0,03' },
      { id: 'budget', label: 'Ліміт витрат', value: '$2,50' },
    ]);
    expect(summaryRows(estimate({ budget_usd: null, within_budget: null })).at(-1)).toEqual({
      id: 'budget',
      label: 'Ліміт витрат',
      value: 'не задано',
    });
  });

  it('blocks the start without a ceiling, and while the estimate is over it, and says why', () => {
    const noCeiling = 'Ліміт витрат не задано. Задайте його в Налаштуваннях';
    expect(startBlockReason(estimate())).toBeNull();
    expect(startBlockReason(estimate({ budget_usd: null, within_budget: null }))).toBe(noCeiling);
    expect(startBlockReason(estimate({ budget_usd: null, within_budget: false }))).toBe(noCeiling);
    expect(startBlockReason(estimate({ budget_usd: '0.01', within_budget: false, estimate_micro_usd: 30_000 }))).toBe(
      'Оцінка вартості (до $0,03) перевищує ліміт витрат ($0,01). Збільште ліміт у Налаштуваннях',
    );
  });
});

describe('the refusals of an action', () => {
  it('sends the key and the ceiling to the settings', () => {
    expect(['API_KEY_REQUIRED', 'BUDGET_REQUIRED', 'STT_AUTH', 'X'].map(needsSettings)).toEqual([true, true, false, false]);
  });

  it('finds the job reasons whose way out is on the settings screen, and the budget stop', () => {
    const reasonOf = (code: string | null): JobDto | null =>
      code === null ? null : job('failed', { code, message: 'x' });

    expect(['STT_AUTH', 'STT_FUNDS', 'BUDGET_EXCEEDED', 'STT_UNKNOWN_OUTCOME', null].map((code) => jobNeedsSettings(reasonOf(code)))).toEqual([
      true,
      true,
      true,
      false,
      false,
    ]);
    expect(jobNeedsSettings(job('failed'))).toBe(false);
    expect(stoppedByBudget(reasonOf('BUDGET_EXCEEDED'))).toBe(true);
    expect(stoppedByBudget(reasonOf('STT_FUNDS'))).toBe(false);
    expect(stoppedByBudget(null)).toBe(false);
  });

  it('shows the backend text, except for the transcript that already exists', () => {
    expect(actionMessage({ code: 'API_KEY_REQUIRED', message: 'Спершу задайте ключ', retryable: false })).toBe('Спершу задайте ключ');
    expect(actionMessage({ code: 'TRANSCRIPT_ALREADY_EXISTS', message: 'x', retryable: false })).toContain('Транскрипт уже є');
  });

  it('has the wording of docs/03 for a request whose answer never came', () => {
    expect(UNKNOWN_OUTCOME_TEXT).toBe('Відповідь не отримано. Провайдер міг обробити запит');
  });
});
