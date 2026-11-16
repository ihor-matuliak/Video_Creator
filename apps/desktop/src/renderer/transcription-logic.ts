import type {
  AudioStreamDto,
  JobDto,
  TranscriptionChunkDto,
  TranscriptionEstimateDto,
  TranscriptionRunDto,
} from '../shared/api';
import type { ApiErrorInfo, ApiResult } from '../shared/bridge';
import { formatTimecode } from '../shared/timecode';
import { STT_LANGUAGE_OPTIONS, STT_MODEL_OPTIONS } from './settings-logic';

// The pure part of the «Розпізнавання мовлення» block (ADR 038): which track is used, how a run is
// counted and worded, and what the summary before a paid start says. No React, no bridge.

/** The audio track recognition starts with: the default one, else the first; null without audio. */
export function defaultAudioStream(streams: readonly AudioStreamDto[]): number | null {
  const chosen = streams.find((stream) => stream.default) ?? streams[0];
  return chosen === undefined ? null : chosen.index;
}

/** The track the user picked, if it is still one of the source's; otherwise `fallback`. */
export function resolveAudioStream(
  streams: readonly AudioStreamDto[],
  picked: number | null,
  fallback: number,
): number {
  return picked !== null && streams.some((stream) => stream.index === picked) ? picked : fallback;
}

/** Ukrainian plural form of a count: 1 ділянка, 2 ділянки, 5 ділянок, 11 ділянок, 21 ділянка. */
export function pluralUk(count: number, one: string, few: string, many: string): string {
  const lastTwo = count % 100;
  const last = count % 10;
  if (lastTwo >= 11 && lastTwo <= 14) {
    return many;
  }
  if (last === 1) {
    return one;
  }
  return last >= 2 && last <= 4 ? few : many;
}

// --- parts of a run -------------------------------------------------------------------------------

const CHUNK_SUCCEEDED = 'succeeded';
const CHUNK_FAILED = 'failed';
const CHUNK_UNKNOWN_OUTCOME = 'unknown_outcome';
/** A part that was split in two: its halves are listed too, so it is not a part of the plan. */
const CHUNK_SUPERSEDED = 'superseded';

export interface ChunkCounts {
  /** The parts of the plan now: every part but the superseded ones. */
  readonly total: number;
  readonly succeeded: number;
  readonly failed: number;
  /** The request may have gone out and been paid for; the answer never came. */
  readonly unknown: number;
}

export function countChunks(chunks: readonly TranscriptionChunkDto[]): ChunkCounts {
  const plan = chunks.filter((chunk) => chunk.state !== CHUNK_SUPERSEDED);
  const inState = (state: string): number => plan.filter((chunk) => chunk.state === state).length;
  return {
    total: plan.length,
    succeeded: inState(CHUNK_SUCCEEDED),
    failed: inState(CHUNK_FAILED),
    unknown: inState(CHUNK_UNKNOWN_OUTCOME),
  };
}

/** «Розпізнаємо мовлення · 8 із 24 частин» (docs/03 section 7). */
export function progressText(counts: ChunkCounts): string {
  return `Розпізнаємо мовлення · ${String(counts.succeeded)} із ${String(counts.total)} частин`;
}

/** The parts a plain «Повторити невдалі» would send again: not done, and not of unknown outcome. */
export function retryCount(counts: ChunkCounts): number {
  return counts.total - counts.succeeded - counts.unknown;
}

/** «1 частини», «3 частин»: the genitive, for «Це стосується …». */
export function partsText(count: number): string {
  return `${String(count)} ${pluralUk(count, 'частини', 'частин', 'частин')}`;
}

/** docs/03 section 7: «Мовлення розпізнано не повністю: 2 ділянки потребують повтору». */
export function partialText(count: number): string {
  if (count <= 0) {
    return 'Мовлення розпізнано не повністю';
  }
  const sections = `${String(count)} ${pluralUk(count, 'ділянка', 'ділянки', 'ділянок')}`;
  return `Мовлення розпізнано не повністю: ${sections} ${pluralUk(count, 'потребує', 'потребують', 'потребують')} повтору`;
}

export const UNKNOWN_OUTCOME_TEXT = 'Відповідь не отримано. Провайдер міг обробити запит';

const RESEND_QUESTION =
  'Провайдер міг уже обробити цей запит і списати кошти. Якщо надіслати ще раз, за нього можуть заплатити вдруге.';

/**
 * The question before a confirmed resend. A stop on the ceiling names the ceiling the resend goes on
 * with (ADR 038, доповнення 3); with no ceiling (null) it names none.
 */
export function resendQuestion(limitUsd: string | null): string {
  if (limitUsd === null) {
    return RESEND_QUESTION;
  }
  return `${RESEND_QUESTION} Продовження піде з лімітом витрат ${formatUsd(limitUsd)}.`;
}

// --- the run on screen ----------------------------------------------------------------------------

/**
 * What a run that is not complete needs: `active` (queued or running) is watched, `retry` (partial
 * or failed) offers «Повторити невдалі», `resume` (interrupted or cancelled) offers «Продовжити».
 * A completed run, and a status this version does not know, are none of these (null).
 */
export type OpenStage = 'active' | 'retry' | 'resume';

export function openStage(status: string): OpenStage | null {
  switch (status) {
    case 'queued':
    case 'running':
      return 'active';
    case 'partial':
    case 'failed':
      return 'retry';
    case 'interrupted':
    case 'cancelled':
      return 'resume';
    default:
      return null;
  }
}

/** A job somebody still works on (or is asked to stop): `resume` queues it before the run says so. */
export function isJobOpen(state: string): boolean {
  return state === 'queued' || state === 'running' || state === 'cancelling';
}

/**
 * The stage of a run with its job. An open job makes the run `active` whatever its status says: a
 * resumed job is `queued` while its run still carries the status it stopped with, and a run written
 * as completed may still have its job working (L3). Only a run whose job is over keeps its status.
 */
export function runStage(run: TranscriptionRunDto, job: JobDto | null): OpenStage | null {
  if (job !== null && isJobOpen(job.state)) {
    return 'active';
  }
  return openStage(run.status);
}

/** The newest run that is not complete, as last read, with its job. */
export interface RunSnapshot {
  readonly run: TranscriptionRunDto;
  readonly job: JobDto | null;
}

/** What one load of the block found. The estimate may fail alone: a run on screen does not need it. */
export type Snapshot =
  | { readonly ok: false; readonly error: ApiErrorInfo }
  | {
      readonly ok: true;
      readonly estimate: ApiResult<TranscriptionEstimateDto>;
      /** The backend's own view of the key; null when it could not be read. */
      readonly hasKey: boolean | null;
      /** The ceiling of the settings as read (`run_budget_usd`); null: none is set, or it could not be read. */
      readonly budgetUsd: string | null;
      readonly run: RunSnapshot | null;
    };

export type TranscriptionView =
  | { readonly kind: 'loading' }
  | { readonly kind: 'failed'; readonly error: ApiErrorInfo }
  | {
      readonly kind: 'start';
      readonly estimate: TranscriptionEstimateDto;
      readonly hasKey: boolean | null;
      /** The summary of a new run after «Почати заново (платно)»: it says that all audio is sent again. */
      readonly restarted: boolean;
    }
  | {
      readonly kind: 'run';
      readonly stage: OpenStage;
      readonly run: TranscriptionRunDto;
      readonly job: JobDto | null;
      readonly counts: ChunkCounts;
      /** The user asked to stop; the run is still winding down. */
      readonly cancelling: boolean;
      /** The ceiling in the settings, which «Продовжити з лімітом» takes; null when none is set. */
      readonly budgetUsd: string | null;
    };

/**
 * The newest run that is not complete is what the block is about; otherwise the summary and start.
 * A run the user chose to start over (`restartedFor`) is no longer shown as a run: the summary is.
 */
export function deriveView(
  snapshot: Snapshot | null,
  cancelRequestedFor: string | null,
  restartedFor: string | null = null,
): TranscriptionView {
  if (snapshot === null) {
    return { kind: 'loading' };
  }
  if (!snapshot.ok) {
    return { kind: 'failed', error: snapshot.error };
  }
  const restarted = snapshot.run !== null && snapshot.run.run.id === restartedFor;
  if (snapshot.run !== null && !restarted) {
    const { run, job } = snapshot.run;
    const stage = runStage(run, job);
    if (stage !== null) {
      return {
        kind: 'run',
        stage,
        run,
        job,
        counts: countChunks(run.chunks),
        cancelling: stage === 'active' && (cancelRequestedFor === run.id || job?.state === 'cancelling'),
        budgetUsd: snapshot.budgetUsd,
      };
    }
  }
  if (!snapshot.estimate.ok) {
    return { kind: 'failed', error: snapshot.estimate.error };
  }
  return { kind: 'start', estimate: snapshot.estimate.data, hasKey: snapshot.hasKey, restarted };
}

/** The line for a run that stopped, by its status. */
export function stoppedText(status: string, retry: number): string {
  switch (status) {
    case 'partial':
      return partialText(retry);
    case 'failed':
      return 'Розпізнавання не вдалося';
    case 'cancelled':
      return 'Розпізнавання скасовано';
    default:
      return 'Розпізнавання перервано';
  }
}

/** Fixed by the backend for a run that is simply not whole; the line above already says it. */
const PARTIAL_CODE = 'TRANSCRIPTION_PARTIAL';
/** The parts of unknown outcome have their own block, which says the same (L8). */
const UNKNOWN_OUTCOME_CODE = 'STT_UNKNOWN_OUTCOME';

/** The Ukrainian reason the job ended with, when there is one worth adding to the line. */
export function jobReason(job: JobDto | null): string | null {
  const error = job === null ? null : job.error;
  if (error === null || error.code === PARTIAL_CODE || error.code === UNKNOWN_OUTCOME_CODE) {
    return null;
  }
  return error.message;
}

/**
 * Why recognition cannot run on the file as it is now: it is missing or changed since it was added
 * (ADR 038, доповнення 3). Null when the file is available, or its state is not one of these.
 */
export function unavailableText(availability: string): string | null {
  switch (availability) {
    case 'missing':
      return 'Файл не знайдено на цьому місці: розпізнавання неможливе.';
    case 'changed':
      return 'Файл змінено після додавання: розпізнавання неможливе.';
    default:
      return null;
  }
}

// --- the summary before a paid start --------------------------------------------------------------

export function describeModel(model: string): string {
  return STT_MODEL_OPTIONS.find((option) => option.value === model)?.label ?? model;
}

/** «Автовизначення» when the provider detects the language (the setting is null). */
export function describeLanguage(language: string | null): string {
  const key = language ?? '';
  return STT_LANGUAGE_OPTIONS.find((option) => option.value === key)?.label ?? key;
}

/** `$2,50` from the decimal string of the settings: a decimal comma, at least two decimals. */
export function formatUsd(decimal: string): string {
  const match = /^(\d+)(?:\.(\d+))?$/.exec(decimal);
  if (match === null) {
    return `$${decimal}`;
  }
  return `$${match[1] ?? '0'},${(match[2] ?? '').padEnd(2, '0')}`;
}

/**
 * What is left of a key's limit (ADR 039), in Ukrainian: no limit set, a limit used up (zero or less), or
 * the sum left in dollars.
 */
export function keyLimitText(remainingUsd: string | null): string {
  if (remainingUsd === null) {
    return 'Ліміт витрат для ключа не задано';
  }
  return Number(remainingUsd) <= 0 ? 'Ліміт витрат ключа вичерпано' : `Залишок ліміту: ${formatUsd(remainingUsd)}`;
}

/** «до $0,03»: an estimate in millionths of a dollar, rounded up to the next cent. */
export function formatUpToUsd(microUsd: number): string {
  const cents = Math.ceil(microUsd / 10_000);
  const whole = Math.floor(cents / 100);
  return `до $${String(whole)},${String(cents % 100).padStart(2, '0')}`;
}

export type SummaryId = 'duration' | 'model' | 'language' | 'parts' | 'cost' | 'budget';

export interface SummaryRow {
  readonly id: SummaryId;
  readonly label: string;
  readonly value: string;
}

/** The lines the user reads before paying: duration, model, language, parts, «до $X», the ceiling. */
export function summaryRows(estimate: TranscriptionEstimateDto): readonly SummaryRow[] {
  return [
    { id: 'duration', label: 'Тривалість аудіо', value: formatTimecode(estimate.duration_ms) },
    { id: 'model', label: 'Модель', value: describeModel(estimate.model) },
    { id: 'language', label: 'Мова', value: describeLanguage(estimate.language) },
    { id: 'parts', label: 'Частини', value: String(estimate.chunk_count) },
    { id: 'cost', label: 'Орієнтовна вартість', value: formatUpToUsd(estimate.estimate_micro_usd) },
    {
      id: 'budget',
      label: 'Ліміт витрат',
      value: estimate.budget_usd === null ? 'не задано' : formatUsd(estimate.budget_usd),
    },
  ];
}

const NO_BUDGET_TEXT = 'Ліміт витрат не задано. Задайте його в Налаштуваннях';

/**
 * Why start is unavailable: no ceiling is set (checked before the backend would refuse with
 * BUDGET_REQUIRED), or the estimate is over it. Null when it may start.
 */
export function startBlockReason(estimate: TranscriptionEstimateDto): string | null {
  if (estimate.budget_usd === null) {
    return NO_BUDGET_TEXT;
  }
  if (estimate.within_budget !== false) {
    return null;
  }
  return `Оцінка вартості (${formatUpToUsd(estimate.estimate_micro_usd)}) перевищує ліміт витрат (${formatUsd(estimate.budget_usd)}). Збільште ліміт у Налаштуваннях`;
}

// --- errors of an action --------------------------------------------------------------------------

/** The refusals whose way out is on the «Налаштування» screen: the key, the ceiling. */
const SETTINGS_CODES: ReadonlySet<string> = new Set(['API_KEY_REQUIRED', 'BUDGET_REQUIRED']);

export function needsSettings(code: string): boolean {
  return SETTINGS_CODES.has(code);
}

/**
 * The reasons a job stopped for that are settled on the «Налаштування» screen: the key, the funds,
 * the ceiling (ADR 038, доповнення 2).
 */
const JOB_SETTINGS_CODES: ReadonlySet<string> = new Set(['STT_AUTH', 'STT_FUNDS', 'BUDGET_EXCEEDED']);

export function jobNeedsSettings(job: JobDto | null): boolean {
  const code = job?.error?.code;
  return code !== undefined && JOB_SETTINGS_CODES.has(code);
}

/** A job that stopped on the run's ceiling: it goes on only with a higher one (ADR 038, доповнення 2). */
export function stoppedByBudget(job: JobDto | null): boolean {
  return job?.error?.code === 'BUDGET_EXCEEDED';
}

const ALREADY_EXISTS_TEXT =
  'Транскрипт уже є. Щоб розпізнати ще раз за окрему плату, скористайтеся кнопкою «Розпізнати ще раз (платно)»';

/** The message under the buttons; the backend's own text, except for the one the summary already shows. */
export function actionMessage(error: ApiErrorInfo): string {
  return error.code === 'TRANSCRIPT_ALREADY_EXISTS' ? ALREADY_EXISTS_TEXT : error.message;
}
