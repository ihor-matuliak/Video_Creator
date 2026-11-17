import { describe, expect, it } from 'vitest';

import type {
  AnalysisEstimateDto,
  AnalysisRunDto,
  JobDto,
  TranscriptDto,
} from '../shared/api';
import {
  CATEGORIES,
  DEFAULT_DRAFT,
  DEFAULT_PROFILE,
  SEARCH_CAPTION,
  allState,
  categoryLabel,
  deriveView,
  instructionCounter,
  jobNeedsSettings,
  needsSettings,
  priorityLabel,
  profileErrors,
  profileFromDraft,
  startBlockReason,
  summaryRows,
  toggleAll,
  toggleCategory,
  transcriptBlockReason,
} from './analysis-logic';
import type { CategoryId, ProfileDraft } from './analysis-logic';
import { UNKNOWN_OUTCOME_TEXT } from './transcription-logic';

function draft(overrides: Partial<ProfileDraft> = {}): ProfileDraft {
  return { ...DEFAULT_DRAFT, ...overrides };
}

function estimate(overrides: Partial<AnalysisEstimateDto> = {}): AnalysisEstimateDto {
  return {
    model: 'test/selector',
    unit_level: 'segment',
    unit_count: 40,
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

function transcript(states: readonly string[]): TranscriptDto {
  return {
    id: 'tr-1',
    source_id: 'src-1',
    revision: 1,
    origin: 'stt',
    run_id: 'run-1',
    parent_id: null,
    timing_level: 'segment',
    language: 'en',
    duration_ms: 60_000,
    word_count: 0,
    segment_count: 0,
    coverage: states.map((state, index) => ({
      start_ms: index * 1000,
      end_ms: (index + 1) * 1000,
      state,
    })),
    created_at: '2026-11-17T10:00:00.000Z',
  };
}

function windows(overrides: Partial<AnalysisRunDto['windows']> = {}): AnalysisRunDto['windows'] {
  return { total: 5, succeeded: 2, failed: 0, unknown_outcome: 0, pending: 3, ...overrides };
}

function run(status: string, overrides: Partial<AnalysisRunDto> = {}): AnalysisRunDto {
  return {
    id: 'run-1',
    source_id: 'src-1',
    transcript_id: 'tr-1',
    transcript_revision: 1,
    model: 'test/selector',
    provider: 'test',
    prompt_version: 'sha256:0123456789abcdef',
    schema_version: 1,
    unit_level: 'segment',
    status,
    error_code: null,
    created_at: '2026-11-17T10:00:00.000Z',
    updated_at: '2026-11-17T10:00:00.000Z',
    job_id: 'job-1',
    committed_micro_usd: 0,
    windows: windows(),
    clip_ids: [],
    stale: false,
    diagnostics_summary: {},
    ...overrides,
  };
}

function job(state: string, error: JobDto['error'] = null): JobDto {
  return { id: 'job-1', type: 'analysis', state, progress: {}, error };
}

function clipIds(count: number): string[] {
  return Array.from({ length: count }, (_, index) => `clip-${String(index)}`);
}

const ALL: readonly CategoryId[] = ['funny', 'action', 'fails', 'unexpected', 'game_details'];

describe('the five categories and their labels', () => {
  it('are in canonical order, with the labels and hints of docs/03 section 6', () => {
    expect(CATEGORIES).toEqual([
      { id: 'funny', label: 'Смішні моменти', hint: 'жарти, кумедні діалоги та реакції.' },
      {
        id: 'action',
        label: 'Екшн',
        hint: 'згадки про погоні, зіткнення, трюки та напружені події.',
      },
      { id: 'fails', label: 'Провали', hint: 'невдалі спроби, помилки та зірвані плани.' },
      {
        id: 'unexpected',
        label: 'Несподівані ситуації',
        hint: 'раптові повороти і неочікувані результати.',
      },
      {
        id: 'game_details',
        label: 'Цікаві деталі гри',
        hint: 'механіки, знахідки та корисні спостереження.',
      },
    ]);
  });

  it('shows an id this version does not know as it is', () => {
    expect(categoryLabel('game_details')).toBe('Цікаві деталі гри');
    expect(categoryLabel('action')).toBe('Екшн');
    expect(categoryLabel('new_kind')).toBe('new_kind');
  });
});

describe('the default search profile', () => {
  it('is all five categories, no instruction, 15 to 90 seconds, up to 20 clips, no partial transcript', () => {
    expect(DEFAULT_PROFILE).toEqual({
      categories: ['funny', 'action', 'fails', 'unexpected', 'game_details'],
      custom_instruction: '',
      min_duration_ms: 15_000,
      max_duration_ms: 90_000,
      max_candidates: 20,
      allow_partial: false,
    });
  });

  it('is what the default draft becomes in milliseconds', () => {
    expect(profileFromDraft(DEFAULT_DRAFT)).toEqual(DEFAULT_PROFILE);
  });
});

describe('the category chips and «Усі категорії»', () => {
  it('toggles one category, keeping the canonical order', () => {
    expect(toggleCategory([], 'funny')).toEqual(['funny']);
    expect(toggleCategory(['fails'], 'funny')).toEqual(['funny', 'fails']);
    expect(toggleCategory(['funny', 'fails'], 'funny')).toEqual(['fails']);
    expect(toggleCategory(['funny', 'game_details'], 'action')).toEqual([
      'funny',
      'action',
      'game_details',
    ]);
  });

  it('is true when all are chosen, false when none, mixed otherwise', () => {
    expect(allState(ALL)).toBe('true');
    expect(allState([])).toBe('false');
    expect(allState(['funny'])).toBe('mixed');
    expect(allState(['funny', 'action', 'fails', 'unexpected'])).toBe('mixed');
  });

  it('clears all when all are chosen, and chooses all otherwise (mixed or none)', () => {
    expect(toggleAll(ALL)).toEqual([]);
    expect(toggleAll(['funny', 'action', 'fails', 'unexpected'])).toEqual(ALL);
    expect(toggleAll(['funny'])).toEqual(ALL);
    expect(toggleAll([])).toEqual(ALL);
  });
});

describe('the instruction and the profile fields', () => {
  it('counts the instruction in code points, as «n/2000»', () => {
    expect(instructionCounter('')).toBe('0/2000');
    expect(instructionCounter('абв')).toBe('3/2000');
    // One emoji is one code point, though it takes two UTF-16 units.
    expect(instructionCounter('a😀b')).toBe('3/2000');
  });

  it('accepts exactly 2000 code points and reports more as a field error', () => {
    expect(profileErrors(draft({ customInstruction: 'я'.repeat(2000) }))).toEqual({});
    expect(profileErrors(draft({ customInstruction: '😀'.repeat(2000) }))).toEqual({});
    expect(profileErrors(draft({ customInstruction: 'я'.repeat(2001) }))).toEqual({
      customInstruction: 'Побажання не можуть бути довшими за 2000 символів',
    });
  });

  it('keeps the instruction as typed, and passes it on untrimmed', () => {
    const typed = '  Невдалі погоні зі смішною реакцією ';
    expect(profileFromDraft(draft({ customInstruction: typed }))?.custom_instruction).toBe(typed);
  });

  it('requires at least one category, with the text of docs/03', () => {
    expect(profileErrors(draft({ categories: [] }))).toEqual({
      categories: 'Оберіть хоча б одну категорію',
    });
    expect(profileFromDraft(draft({ categories: [] }))).toBeNull();
  });

  it('takes the durations in whole seconds from 5 to 600', () => {
    expect(profileErrors(draft({ minSeconds: 5, maxSeconds: 600 }))).toEqual({});
    expect(profileErrors(draft({ minSeconds: 4 }))).toEqual({
      minSeconds: 'Тривалість має бути від 5 до 600 секунд',
    });
    expect(profileErrors(draft({ maxSeconds: 601 }))).toEqual({
      maxSeconds: 'Тривалість має бути від 5 до 600 секунд',
    });
    expect(profileErrors(draft({ minSeconds: 5.5 }))).toEqual({
      minSeconds: 'Значення має бути цілим числом',
    });
    expect(profileErrors(draft({ maxSeconds: Number.NaN }))).toEqual({
      maxSeconds: 'Значення має бути цілим числом',
    });
  });

  it('requires «від» not to be more than «до», and reports a range error before it', () => {
    expect(profileErrors(draft({ minSeconds: 20, maxSeconds: 10 }))).toEqual({
      minSeconds: 'Значення «від» не може бути більшим за «до»',
    });
    expect(profileErrors(draft({ minSeconds: 30, maxSeconds: 30 }))).toEqual({});
    expect(profileErrors(draft({ minSeconds: 700, maxSeconds: 10 }))).toEqual({
      minSeconds: 'Тривалість має бути від 5 до 600 секунд',
    });
  });

  it('converts seconds to milliseconds for the profile, and sorts the categories', () => {
    expect(profileFromDraft(draft({ minSeconds: 5, maxSeconds: 600 }))).toMatchObject({
      min_duration_ms: 5000,
      max_duration_ms: 600_000,
    });
    expect(profileFromDraft(draft({ categories: ['fails', 'funny'] }))?.categories).toEqual([
      'funny',
      'fails',
    ]);
    expect(profileFromDraft(draft({ allowPartial: true }))?.allow_partial).toBe(true);
    expect(profileFromDraft(draft({ minSeconds: 4 }))).toBeNull();
  });

  it('takes «До N моментів» from 1 to 100', () => {
    expect(profileErrors(draft({ maxCandidates: 1 }))).toEqual({});
    expect(profileErrors(draft({ maxCandidates: 100 }))).toEqual({});
    expect(profileErrors(draft({ maxCandidates: 0 }))).toEqual({
      maxCandidates: 'Кількість моментів має бути від 1 до 100',
    });
    expect(profileErrors(draft({ maxCandidates: 101 }))).toEqual({
      maxCandidates: 'Кількість моментів має бути від 1 до 100',
    });
    expect(profileErrors(draft({ maxCandidates: 2.5 }))).toEqual({
      maxCandidates: 'Значення має бути цілим числом',
    });
  });
});

describe('the transcript the search reads', () => {
  const partialText =
    'Транскрипт неповний. Позначте «Шукати у готовій частині» або повторіть невдалі частини';

  it('needs a transcript, with the text of docs/03', () => {
    expect(transcriptBlockReason(null, false)).toBe(
      'Спочатку розпізнайте мовлення або імпортуйте транскрипт',
    );
    expect(transcriptBlockReason(null, true)).toBe(
      'Спочатку розпізнайте мовлення або імпортуйте транскрипт',
    );
  });

  it('lets a complete transcript through, and an empty one too', () => {
    expect(transcriptBlockReason(transcript(['succeeded', 'succeeded']), false)).toBeNull();
    expect(transcriptBlockReason(transcript([]), false)).toBeNull();
  });

  it('blocks a partial transcript unless the user allows partial coverage', () => {
    for (const state of ['failed', 'pending', 'unknown_outcome']) {
      expect(transcriptBlockReason(transcript(['succeeded', state]), false)).toBe(partialText);
      expect(transcriptBlockReason(transcript(['succeeded', state]), true)).toBeNull();
    }
  });
});

describe('the summary before a paid search', () => {
  it('has six lines: source duration, categories, model, parts, cost, ceiling', () => {
    expect(
      summaryRows(
        estimate({ window_count: 3, estimate_micro_usd: 30_000 }),
        ['funny', 'game_details'],
        125_000,
      ),
    ).toEqual([
      { id: 'duration', label: 'Тривалість джерела', value: '00:02:05.000' },
      { id: 'categories', label: 'Категорії', value: 'Смішні моменти, Цікаві деталі гри' },
      { id: 'model', label: 'Модель аналізу', value: 'test/selector' },
      { id: 'parts', label: 'Частини', value: '3' },
      { id: 'cost', label: 'Орієнтовна вартість', value: 'до $0,03' },
      { id: 'budget', label: 'Ліміт витрат', value: '$2,50' },
    ]);
  });

  it('says «не задано» for a ceiling that is not set', () => {
    const rows = summaryRows(estimate({ budget_usd: null, within_budget: null }), ALL, 1000);
    expect(rows.at(-1)).toEqual({ id: 'budget', label: 'Ліміт витрат', value: 'не задано' });
  });

  it('blocks the start without a ceiling, and while the estimate is over it, with the texts of recognition', () => {
    const noCeiling = 'Ліміт витрат не задано. Задайте його в Налаштуваннях';
    expect(startBlockReason(estimate())).toBeNull();
    expect(startBlockReason(estimate({ within_budget: null }))).toBeNull();
    expect(startBlockReason(estimate({ budget_usd: null, within_budget: null }))).toBe(noCeiling);
    expect(startBlockReason(estimate({ budget_usd: null, within_budget: false }))).toBe(noCeiling);
    expect(
      startBlockReason(estimate({ budget_usd: '0.01', within_budget: false, estimate_micro_usd: 30_000 })),
    ).toBe(
      'Оцінка вартості (до $0,03) перевищує ліміт витрат ($0,01). Збільште ліміт у Налаштуваннях',
    );
  });
});

describe('the refusals and stops that go to the settings screen', () => {
  it('sends the start refusals of the key, the ceiling and the model to the settings', () => {
    expect(
      [
        'API_KEY_REQUIRED',
        'BUDGET_REQUIRED',
        'ANALYSIS_MODEL_REQUIRED',
        'ANALYSIS_MODEL_NOT_SUPPORTED',
        'ANALYSIS_AUTH',
        'X',
      ].map(needsSettings),
    ).toEqual([true, true, true, true, false, false]);
  });

  it('sends the job stops ANALYSIS_AUTH and ANALYSIS_FUNDS to the settings, and not the budget stop', () => {
    const withCode = (code: string): JobDto => job('failed', { code, message: 'x' });
    expect(
      ['ANALYSIS_AUTH', 'ANALYSIS_FUNDS', 'BUDGET_EXCEEDED', 'ANALYSIS_UPSTREAM'].map((code) =>
        jobNeedsSettings(withCode(code)),
      ),
    ).toEqual([true, true, false, false]);
    expect(jobNeedsSettings(job('failed'))).toBe(false);
    expect(jobNeedsSettings(null)).toBe(false);
  });
});

describe('what a run on screen shows', () => {
  it('is idle when there is no run', () => {
    expect(deriveView(null, null)).toEqual({ kind: 'idle' });
  });

  it('is running while the job is open, with the progress of its windows', () => {
    const progress = 'Шукаємо моменти · 2 із 5 частин';
    expect(deriveView(run('running'), job('running'))).toEqual({ kind: 'running', progress });
    expect(deriveView(run('queued'), job('queued'))).toEqual({ kind: 'running', progress });
    expect(deriveView(run('running'), job('cancelling'))).toEqual({ kind: 'running', progress });
  });

  it('follows the run status when there is no job', () => {
    expect(deriveView(run('running'), null)).toMatchObject({ kind: 'running' });
    expect(deriveView(run('failed'), null)).toMatchObject({ kind: 'stopped' });
  });

  it('watches a run written as completed while its job is still open', () => {
    expect(deriveView(run('completed', { clip_ids: clipIds(3) }), job('running'))).toMatchObject({
      kind: 'running',
    });
  });

  it('says how many moments were found, with the Ukrainian plural', () => {
    const completed = (count: number): AnalysisRunDto =>
      run('completed', { clip_ids: clipIds(count) });
    expect(deriveView(completed(3), job('succeeded'))).toEqual({
      kind: 'completed',
      count: 3,
      text: 'Знайдено 3 моменти. Вони у вкладці «Моменти»',
    });
    expect(deriveView(completed(1), job('succeeded'))).toMatchObject({
      text: 'Знайдено 1 момент. Вони у вкладці «Моменти»',
    });
    expect(deriveView(completed(5), job('succeeded'))).toMatchObject({
      text: 'Знайдено 5 моментів. Вони у вкладці «Моменти»',
    });
    expect(deriveView(completed(21), job('succeeded'))).toMatchObject({
      text: 'Знайдено 21 момент. Вони у вкладці «Моменти»',
    });
  });

  it('says that nothing was found when the run has no clips', () => {
    expect(deriveView(run('completed'), job('succeeded'))).toEqual({
      kind: 'completed',
      count: 0,
      text: 'Моментів за цими налаштуваннями не знайдено. Змініть категорії або побажання',
    });
  });

  it('shows the reason of a stopped job as its own message', () => {
    const message = 'OpenRouter не прийняв ключ API. Перевірте ключ у Налаштуваннях і відновіть аналіз';
    expect(
      deriveView(run('failed'), job('failed', { code: 'ANALYSIS_AUTH', message })),
    ).toEqual({
      kind: 'stopped',
      reason: message,
      unknownCount: 0,
      unknownText: null,
      byBudget: false,
      needsSettings: true,
    });
  });

  it('marks a stop on the ceiling, which goes on only with a higher one', () => {
    const stopped = deriveView(
      run('failed'),
      job('failed', { code: 'BUDGET_EXCEEDED', message: 'Ліміт витрат запуску вичерпано: аналіз зупинено' }),
    );
    expect(stopped).toMatchObject({ kind: 'stopped', byBudget: true, needsSettings: false });
  });

  it('marks a stop on the funds as one to settle on the settings screen', () => {
    expect(
      deriveView(run('failed'), job('failed', { code: 'ANALYSIS_FUNDS', message: 'x' })),
    ).toMatchObject({ kind: 'stopped', needsSettings: true, byBudget: false });
    expect(
      deriveView(run('failed'), job('failed', { code: 'ANALYSIS_UPSTREAM', message: 'x' })),
    ).toMatchObject({ kind: 'stopped', needsSettings: false, byBudget: false });
  });

  it('says the search was cancelled or interrupted when the job gives no reason', () => {
    expect(deriveView(run('cancelled'), job('cancelled'))).toMatchObject({
      kind: 'stopped',
      reason: 'Пошук скасовано',
    });
    expect(deriveView(run('interrupted'), job('interrupted'))).toMatchObject({
      kind: 'stopped',
      reason: 'Пошук перервано',
    });
  });

  it('counts the windows of unknown outcome and shows the text for them', () => {
    expect(
      deriveView(
        run('failed', { windows: windows({ unknown_outcome: 2, succeeded: 1 }) }),
        job('failed', { code: 'ANALYSIS_UNKNOWN_OUTCOME', message: 'x' }),
      ),
    ).toMatchObject({ kind: 'stopped', unknownCount: 2, unknownText: UNKNOWN_OUTCOME_TEXT });
  });
});

describe('the priority label and the caption', () => {
  it('labels a clip high from rank 80, medium from 60, low below, and none without a rank', () => {
    expect([100, 80, 79, 60, 59, 0, null].map(priorityLabel)).toEqual([
      'Пріоритет: високий',
      'Пріоритет: високий',
      'Пріоритет: середній',
      'Пріоритет: середній',
      'Пріоритет: низький',
      'Пріоритет: низький',
      null,
    ]);
  });

  it('has the caption of docs/03 section 6', () => {
    expect(SEARCH_CAPTION).toBe(
      'Пошук використовує мовлення. Візуальні сцени без реплік можуть бути пропущені',
    );
  });
});
