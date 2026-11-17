import type {
  AnalysisEstimateDto,
  AnalysisProfile,
  AnalysisRunDto,
  JobDto,
  TranscriptDto,
} from '../shared/api';
import { formatTimecode } from '../shared/timecode';
import {
  UNKNOWN_OUTCOME_TEXT,
  describeModel,
  formatUpToUsd,
  formatUsd,
  isJobOpen,
  pluralUk,
} from './transcription-logic';

// The pure part of the «Що шукати» panel (ADR 041 §23, docs/03 §6–§8): the categories and the search
// profile the user edits, what the summary before a paid start says, and what a run on screen shows.
// No React, no bridge.

// --- categories and the profile -------------------------------------------------------------------

export type CategoryId = 'funny' | 'action' | 'fails' | 'unexpected' | 'game_details';

export interface Category {
  readonly id: CategoryId;
  readonly label: string;
  /** The short explanation under the chip (docs/03 §6). */
  readonly hint: string;
}

/** The five categories in canonical order: the order the backend keeps too (ADR 041 §23(2)). */
export const CATEGORIES: readonly Category[] = [
  { id: 'funny', label: 'Смішні моменти', hint: 'жарти, кумедні діалоги та реакції.' },
  { id: 'action', label: 'Екшн', hint: 'згадки про погоні, зіткнення, трюки та напружені події.' },
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
];

export const CATEGORY_IDS: readonly CategoryId[] = CATEGORIES.map((category) => category.id);

/** The label of a category; an id this version does not know is shown as it is (docs/03 §7). */
export function categoryLabel(id: string): string {
  return CATEGORIES.find((category) => category.id === id)?.label ?? id;
}

/** The search profile when the user changes nothing (ADR 041 §23(2)): durations 15–90 s, in ms. */
export const DEFAULT_PROFILE: AnalysisProfile = {
  categories: CATEGORY_IDS,
  custom_instruction: '',
  min_duration_ms: 15_000,
  max_duration_ms: 90_000,
  max_candidates: 20,
  allow_partial: false,
};

/** What the panel holds while the user edits it: the durations in whole seconds, as they are shown. */
export interface ProfileDraft {
  readonly categories: readonly CategoryId[];
  readonly customInstruction: string;
  readonly minSeconds: number;
  readonly maxSeconds: number;
  readonly maxCandidates: number;
  readonly allowPartial: boolean;
}

/** The draft the panel starts with: DEFAULT_PROFILE in seconds. */
export const DEFAULT_DRAFT: ProfileDraft = {
  categories: CATEGORY_IDS,
  customInstruction: '',
  minSeconds: 15,
  maxSeconds: 90,
  maxCandidates: 20,
  allowPartial: false,
};

const INSTRUCTION_MAX_LENGTH = 2000;
const SECONDS_MIN = 5;
const SECONDS_MAX = 600;
const CANDIDATES_MIN = 1;
const CANDIDATES_MAX = 100;

const CATEGORY_REQUIRED_TEXT = 'Оберіть хоча б одну категорію';
const INSTRUCTION_TOO_LONG_TEXT = `Побажання не можуть бути довшими за ${String(INSTRUCTION_MAX_LENGTH)} символів`;
const NOT_INTEGER_TEXT = 'Значення має бути цілим числом';
const SECONDS_RANGE_TEXT = `Тривалість має бути від ${String(SECONDS_MIN)} до ${String(SECONDS_MAX)} секунд`;
const SECONDS_ORDER_TEXT = 'Значення «від» не може бути більшим за «до»';
const CANDIDATES_RANGE_TEXT = `Кількість моментів має бути від ${String(CANDIDATES_MIN)} до ${String(CANDIDATES_MAX)}`;

/** The chosen categories with `id` toggled: added when absent, removed when present. Canonical order kept. */
export function toggleCategory(selected: readonly CategoryId[], id: CategoryId): CategoryId[] {
  const next = selected.includes(id)
    ? selected.filter((chosen) => chosen !== id)
    : [...selected, id];
  return CATEGORY_IDS.filter((category) => next.includes(category));
}

export type AllState = 'true' | 'mixed' | 'false';

/** The state of «Усі категорії» (aria-checked): every category chosen, none, or some of them. */
export function allState(selected: readonly CategoryId[]): AllState {
  const chosen = CATEGORY_IDS.filter((category) => selected.includes(category)).length;
  if (chosen === CATEGORY_IDS.length) {
    return 'true';
  }
  return chosen === 0 ? 'false' : 'mixed';
}

/** A click on «Усі категорії»: clears everything when all are chosen, otherwise chooses all. */
export function toggleAll(selected: readonly CategoryId[]): CategoryId[] {
  return allState(selected) === 'true' ? [] : [...CATEGORY_IDS];
}

/** Length in code points, the way the backend counts the instruction (not in UTF-16 units). */
function instructionLength(text: string): number {
  return Array.from(text).length;
}

/** The counter under «Додаткові побажання»: «n/2000». */
export function instructionCounter(text: string): string {
  return `${String(instructionLength(text))}/${String(INSTRUCTION_MAX_LENGTH)}`;
}

/** The problem with a whole-second value: not an integer, or outside 5–600; null when it is fine. */
function secondsError(seconds: number): string | null {
  if (!Number.isInteger(seconds)) {
    return NOT_INTEGER_TEXT;
  }
  return seconds < SECONDS_MIN || seconds > SECONDS_MAX ? SECONDS_RANGE_TEXT : null;
}

/** The message of each wrong field of a draft. A field that is fine has no key. */
export interface ProfileErrors {
  readonly categories?: string;
  readonly customInstruction?: string;
  readonly minSeconds?: string;
  readonly maxSeconds?: string;
  readonly maxCandidates?: string;
}

/**
 * The rules of the draft, the same ones the backend checks (ADR 041 §23(2)); the backend stays the
 * authority, its 422 is shown as a general error. An empty object when every field is fine.
 */
export function profileErrors(draft: ProfileDraft): ProfileErrors {
  const errors: Partial<Record<keyof ProfileErrors, string>> = {};
  if (draft.categories.length === 0) {
    errors.categories = CATEGORY_REQUIRED_TEXT;
  }
  if (instructionLength(draft.customInstruction) > INSTRUCTION_MAX_LENGTH) {
    errors.customInstruction = INSTRUCTION_TOO_LONG_TEXT;
  }
  const minError = secondsError(draft.minSeconds);
  const maxError = secondsError(draft.maxSeconds);
  if (minError !== null) {
    errors.minSeconds = minError;
  } else if (maxError === null && draft.minSeconds > draft.maxSeconds) {
    errors.minSeconds = SECONDS_ORDER_TEXT;
  }
  if (maxError !== null) {
    errors.maxSeconds = maxError;
  }
  if (!Number.isInteger(draft.maxCandidates)) {
    errors.maxCandidates = NOT_INTEGER_TEXT;
  } else if (draft.maxCandidates < CANDIDATES_MIN || draft.maxCandidates > CANDIDATES_MAX) {
    errors.maxCandidates = CANDIDATES_RANGE_TEXT;
  }
  return errors;
}

/**
 * The profile the backend takes: the durations converted from seconds to milliseconds, the categories
 * in canonical order, the instruction as typed. Null while any field is wrong.
 */
export function profileFromDraft(draft: ProfileDraft): AnalysisProfile | null {
  if (Object.keys(profileErrors(draft)).length > 0) {
    return null;
  }
  return {
    categories: CATEGORY_IDS.filter((id) => draft.categories.includes(id)),
    custom_instruction: draft.customInstruction,
    min_duration_ms: draft.minSeconds * 1000,
    max_duration_ms: draft.maxSeconds * 1000,
    max_candidates: draft.maxCandidates,
    allow_partial: draft.allowPartial,
  };
}

// --- the transcript the search reads --------------------------------------------------------------

const NO_TRANSCRIPT_TEXT = 'Спочатку розпізнайте мовлення або імпортуйте транскрипт';
const PARTIAL_TRANSCRIPT_TEXT =
  'Транскрипт неповний. Позначте «Шукати у готовій частині» або повторіть невдалі частини';

/**
 * Why the search cannot read the newest transcript (null: it can). A transcript with a part that is
 * not succeeded is partial: it needs «Шукати у готовій частині» (allow_partial).
 */
export function transcriptBlockReason(
  newest: TranscriptDto | null,
  allowPartial: boolean,
): string | null {
  if (newest === null) {
    return NO_TRANSCRIPT_TEXT;
  }
  const partial = newest.coverage.some((span) => span.state !== 'succeeded');
  return partial && !allowPartial ? PARTIAL_TRANSCRIPT_TEXT : null;
}

// --- the summary before a paid start --------------------------------------------------------------

export type SummaryId = 'duration' | 'categories' | 'model' | 'parts' | 'cost' | 'budget';

export interface SummaryRow {
  readonly id: SummaryId;
  readonly label: string;
  readonly value: string;
}

/** The lines the user reads before a paid search (ADR 041 §23(4)). */
export function summaryRows(
  estimate: AnalysisEstimateDto,
  categories: readonly string[],
  sourceDurationMs: number,
): readonly SummaryRow[] {
  return [
    { id: 'duration', label: 'Тривалість джерела', value: formatTimecode(sourceDurationMs) },
    {
      id: 'categories',
      label: 'Категорії',
      value: categories.map((id) => categoryLabel(id)).join(', '),
    },
    { id: 'model', label: 'Модель аналізу', value: describeModel(estimate.model) },
    { id: 'parts', label: 'Частини', value: String(estimate.window_count) },
    {
      id: 'cost',
      label: 'Орієнтовна вартість',
      value: formatUpToUsd(estimate.estimate_micro_usd),
    },
    {
      id: 'budget',
      label: 'Ліміт витрат',
      value: estimate.budget_usd === null ? 'не задано' : formatUsd(estimate.budget_usd),
    },
  ];
}

const NO_BUDGET_TEXT = 'Ліміт витрат не задано. Задайте його в Налаштуваннях';

/**
 * Why the paid start is unavailable: no ceiling is set, or the estimate is over it. Null when the
 * search may start. The texts are those of the speech-recognition summary (ADR 041 §23(4)).
 */
export function startBlockReason(estimate: AnalysisEstimateDto): string | null {
  if (estimate.budget_usd === null) {
    return NO_BUDGET_TEXT;
  }
  if (estimate.within_budget !== false) {
    return null;
  }
  return `Оцінка вартості (${formatUpToUsd(estimate.estimate_micro_usd)}) перевищує ліміт витрат (${formatUsd(estimate.budget_usd)}). Збільште ліміт у Налаштуваннях`;
}

// --- errors of an action and of a job ------------------------------------------------------------

/** The refusals of the start whose way out is on the «Налаштування» screen (ADR 041 §23(4)). */
const SETTINGS_CODES: ReadonlySet<string> = new Set([
  'API_KEY_REQUIRED',
  'BUDGET_REQUIRED',
  'ANALYSIS_MODEL_REQUIRED',
  'ANALYSIS_MODEL_NOT_SUPPORTED',
]);

export function needsSettings(code: string): boolean {
  return SETTINGS_CODES.has(code);
}

/** The reasons a job stopped for that are settled on the «Налаштування» screen (ADR 041 §23(5)). */
const JOB_SETTINGS_CODES: ReadonlySet<string> = new Set(['ANALYSIS_AUTH', 'ANALYSIS_FUNDS']);

export function jobNeedsSettings(job: JobDto | null): boolean {
  const code = job?.error?.code;
  return code !== undefined && JOB_SETTINGS_CODES.has(code);
}

// --- the run on screen -----------------------------------------------------------------------------

const NO_CANDIDATES_TEXT =
  'Моментів за цими налаштуваннями не знайдено. Змініть категорії або побажання';

export type AnalysisView =
  | { readonly kind: 'idle' }
  | { readonly kind: 'running'; readonly progress: string }
  | { readonly kind: 'completed'; readonly count: number; readonly text: string }
  | {
      readonly kind: 'stopped';
      /** The job's own reason, or «Пошук скасовано» / «Пошук перервано» when it has none. */
      readonly reason: string;
      /** Windows whose outcome is unknown: their requests may have been paid for. */
      readonly unknownCount: number;
      /** UNKNOWN_OUTCOME_TEXT while `unknownCount` is above zero; null otherwise. */
      readonly unknownText: string | null;
      /** The run stopped on its ceiling: it goes on only with a higher one (`jobs.resume` budget_usd). */
      readonly byBudget: boolean;
      /** The reason is settled on the «Налаштування» screen. */
      readonly needsSettings: boolean;
    };

function stoppedReason(run: AnalysisRunDto, job: JobDto | null): string {
  const error = job?.error ?? null;
  if (error !== null) {
    return error.message;
  }
  const cancelled = run.status === 'cancelled' || job?.state === 'cancelled';
  return cancelled ? 'Пошук скасовано' : 'Пошук перервано';
}

/**
 * What the panel shows for the newest run and its job (ADR 041 §23(5)). An open job is watched even
 * when the run still says it is not complete; a run without a job follows its own status.
 */
export function deriveView(run: AnalysisRunDto | null, job: JobDto | null): AnalysisView {
  if (run === null) {
    return { kind: 'idle' };
  }
  const open =
    job === null ? run.status === 'queued' || run.status === 'running' : isJobOpen(job.state);
  if (open) {
    const progress = `Шукаємо моменти · ${String(run.windows.succeeded)} із ${String(run.windows.total)} частин`;
    return { kind: 'running', progress };
  }
  if (run.status === 'completed') {
    const count = run.clip_ids.length;
    const text =
      count === 0
        ? NO_CANDIDATES_TEXT
        : `Знайдено ${String(count)} ${pluralUk(count, 'момент', 'моменти', 'моментів')}. Вони у вкладці «Моменти»`;
    return { kind: 'completed', count, text };
  }
  const unknownCount = run.windows.unknown_outcome;
  return {
    kind: 'stopped',
    reason: stoppedReason(run, job),
    unknownCount,
    unknownText: unknownCount > 0 ? UNKNOWN_OUTCOME_TEXT : null,
    byBudget: job?.error?.code === 'BUDGET_EXCEEDED',
    needsSettings: jobNeedsSettings(job),
  };
}

// --- ranking and the caption -----------------------------------------------------------------------

/**
 * The priority label of a clip by its rank (ADR 041 §23(7)): high from 80, medium from 60, low below.
 * Null for a clip with no rank (a manual one).
 */
export function priorityLabel(rank: number | null): string | null {
  if (rank === null) {
    return null;
  }
  if (rank >= 80) {
    return 'Пріоритет: високий';
  }
  return rank >= 60 ? 'Пріоритет: середній' : 'Пріоритет: низький';
}

/** The honest permanent caption of the panel: a property of the mode, not a warning per run (docs/03 §6). */
export const SEARCH_CAPTION =
  'Пошук використовує мовлення. Візуальні сцени без реплік можуть бути пропущені';
