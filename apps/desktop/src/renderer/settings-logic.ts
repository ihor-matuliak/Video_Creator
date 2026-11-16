import type { ApiParams, SettingsDto } from '../shared/api';
import { INVALID_API_KEY_MESSAGE } from '../shared/bridge';
import type { ApiErrorInfo, ApiKeyState } from '../shared/bridge';

export interface SelectOption {
  readonly value: string;
  readonly label: string;
}

/** The speech-recognition models the backend accepts (ADR 033); the backend still checks the value. */
export const STT_MODEL_OPTIONS: readonly SelectOption[] = [
  { value: 'microsoft/mai-transcribe-2', label: 'MAI-Transcribe 2 (Microsoft)' },
  { value: 'google/gemini-3.5-transcribe', label: 'Gemini 3.5 Transcribe (Google)' },
];

/** Languages offered for recognition. The empty value is «Автовизначення»: the setting is null. */
export const STT_LANGUAGE_OPTIONS: readonly SelectOption[] = [
  { value: '', label: 'Автовизначення' },
  { value: 'en', label: 'English' },
  { value: 'es', label: 'Español' },
  { value: 'uk', label: 'Українська' },
];

/**
 * The options of a select with the saved value among them. A value the list does not know (the
 * backend allows more than the list offers) is shown as it is, so the select never says
 * something other than what is saved.
 */
export function optionsWith(options: readonly SelectOption[], current: string): readonly SelectOption[] {
  return options.some((option) => option.value === current)
    ? options
    : [...options, { value: current, label: current }];
}

/** The four fields of the form as the user edits them: text only, empty meaning "not set". */
export interface SettingsDraft {
  readonly sttModel: string;
  readonly sttLanguage: string;
  readonly analysisModel: string;
  readonly budget: string;
}

export type SettingsField = keyof SettingsDraft;

export function draftOf(settings: SettingsDto): SettingsDraft {
  return {
    sttModel: settings.stt_model,
    sttLanguage: settings.stt_language ?? '',
    analysisModel: settings.analysis_model ?? '',
    budget: settings.run_budget_usd ?? '',
  };
}

/** What PATCH /v1/settings gets: all four keys, an empty field becoming null (the default). */
export function updateOf(draft: SettingsDraft): ApiParams['settings.update'] {
  const analysisModel = draft.analysisModel.trim();
  const budget = draft.budget.trim();
  return {
    stt_model: draft.sttModel,
    stt_language: draft.sttLanguage === '' ? null : draft.sttLanguage,
    analysis_model: analysisModel === '' ? null : analysisModel,
    run_budget_usd: budget === '' ? null : budget,
  };
}

export interface SettingsProblem {
  /** The field the problem is about; null for the form as a whole. */
  readonly field: SettingsField | null;
  readonly message: string;
}

const PROBLEMS: Readonly<Record<string, SettingsProblem>> = {
  INVALID_STT_MODEL: {
    field: 'sttModel',
    message: 'Оберіть одну з підтримуваних моделей розпізнавання',
  },
  INVALID_STT_LANGUAGE: {
    field: 'sttLanguage',
    message: 'Оберіть мову зі списку або автовизначення',
  },
  INVALID_ANALYSIS_MODEL: {
    field: 'analysisModel',
    message: 'Модель аналізу має мати вигляд «постачальник/модель» і містити не більше 100 символів',
  },
  INVALID_RUN_BUDGET: {
    field: 'budget',
    message:
      'Ліміт витрат має бути числом від 0.01 до 1000 USD, не більше ніж із 6 цифрами після крапки',
  },
  VALIDATION_ERROR: {
    field: null,
    message: 'Налаштування мають некоректний вигляд. Перевірте значення й спробуйте ще раз',
  },
};

/** The Ukrainian text of a 422 of the settings, by its code; any other error keeps its own text. */
export function settingsProblem(error: ApiErrorInfo): SettingsProblem {
  const known = Object.hasOwn(PROBLEMS, error.code) ? PROBLEMS[error.code] : undefined;
  return known ?? { field: null, message: error.message };
}

/** The text of a failed key save: the shape of the key is worded here, anything else is main's text. */
export function apiKeyProblem(error: ApiErrorInfo): string {
  return error.code === 'INVALID_API_KEY' ? INVALID_API_KEY_MESSAGE : error.message;
}

export interface KeyStateView {
  readonly label: string;
  readonly tone: 'success' | 'warning' | 'neutral';
  /** What the state means for the user; null when there is nothing to add. */
  readonly note: string | null;
}

export const KEY_STATE_VIEWS: Readonly<Record<ApiKeyState, KeyStateView>> = {
  protected: {
    label: 'захищено',
    tone: 'success',
    note: 'Ключ зашифровано засобами Windows і не показується.',
  },
  session: {
    label: 'лише на цей сеанс',
    tone: 'warning',
    note: "Система не може зашифрувати ключ: він лишається в пам'яті й зникне після закриття застосунку.",
  },
  none: { label: 'не задано', tone: 'neutral', note: null },
};
