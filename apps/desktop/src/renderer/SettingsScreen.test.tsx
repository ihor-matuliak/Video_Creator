import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { ApiParams, CredentialCheckDto, SettingsDto } from '../shared/api';
import { INVALID_API_KEY_MESSAGE, apiFailure } from '../shared/bridge';
import type { ApiKeyStatus, ApiResult } from '../shared/bridge';
import { App } from './App';
import { SettingsScreen } from './SettingsScreen';
import {
  NO_KEY,
  PROJECT_ID,
  deferred,
  failure,
  flushAnswers,
  installBridge,
  listOf,
  makeProject,
  makeSettings,
  ok,
} from './test-bridge';
import type { ApiHandlers, BridgeOptions, InstalledBridge } from './test-bridge';

// docs/03 section 10 and ADR 033/034: the AI block of «Налаштування».

const KEY = 'sk-or-v1-0123456789abcdefghij';

interface Opened {
  readonly installed: InstalledBridge;
  readonly updates: ApiParams['settings.update'][];
  readonly current: () => SettingsDto;
}

/** Opens the screen over a backend that keeps the settings it is given, like the real one. */
async function openSettings(
  options: {
    readonly settings?: Partial<SettingsDto>;
    /** What the backend says about holding a key, read at every answer (a key it still holds, or not). */
    readonly hasApiKey?: () => boolean;
    readonly bridge?: Partial<BridgeOptions>;
    readonly update?: (params: ApiParams['settings.update']) => ReturnType<typeof failure> | null;
    /** The answer of the key check (`credentials.check`); without it the op has no handler. */
    readonly check?: NonNullable<ApiHandlers['credentials.check']>;
  } = {},
): Promise<Opened> {
  let settings = makeSettings(options.settings);
  const updates: ApiParams['settings.update'][] = [];
  const answered = (): SettingsDto =>
    options.hasApiKey === undefined ? settings : { ...settings, has_api_key: options.hasApiKey() };
  const installed = installBridge({
    ...options.bridge,
    handlers: {
      'settings.get': () => ok(answered()),
      ...(options.check === undefined ? {} : { 'credentials.check': options.check }),
      'settings.update': (params) => {
        updates.push(params);
        const refused = options.update?.(params) ?? null;
        if (refused !== null) {
          return refused;
        }
        settings = {
          ...settings,
          ...(params.stt_model === undefined ? {} : { stt_model: params.stt_model }),
          ...(params.stt_language === undefined ? {} : { stt_language: params.stt_language }),
          ...(params.analysis_model === undefined ? {} : { analysis_model: params.analysis_model }),
          ...(params.run_budget_usd === undefined ? {} : { run_budget_usd: params.run_budget_usd }),
        };
        return ok(answered());
      },
    },
  });
  render(<SettingsScreen />);
  await screen.findByTestId('settings-ai');
  return { installed, updates, current: () => settings };
}

function input(testId: string): HTMLInputElement {
  return screen.getByTestId<HTMLInputElement>(testId);
}

function select(testId: string): HTMLSelectElement {
  return screen.getByTestId<HTMLSelectElement>(testId);
}

function type(testId: string, value: string): void {
  fireEvent.change(input(testId), { target: { value } });
}

describe('settings: the AI block', () => {
  it('shows the masked key field, the state of the key, the models, the language and the budget', async () => {
    await openSettings();

    const ai = screen.getByTestId('settings-ai');
    expect(within(ai).getByTestId('api-key')).toHaveAttribute('type', 'password');
    expect(within(ai).getByTestId('api-key')).toHaveValue('');
    expect(within(ai).getByTestId('save-api-key')).toBeDisabled();
    expect(within(ai).getByRole('button', { name: 'Видалити ключ' })).toBeDisabled();
    expect(within(ai).getByTestId('api-key-state')).toHaveTextContent('Стан ключа: не задано');
    expect(within(ai).getByTestId('stt-model')).toHaveValue('microsoft/mai-transcribe-2');
    expect(within(ai).getByTestId('analysis-model')).toHaveValue('');
    expect(within(ai).getByTestId('budget-limit')).toHaveValue('');
    expect(within(ai).getByTestId('price-note')).toHaveTextContent('Ціна: Невідомо');
    expect(screen.getByRole('heading', { level: 2, name: 'Налаштування' })).toBeVisible();
  });

  it('offers the two models of recognition and the four languages, Автовизначення being null', async () => {
    await openSettings();

    expect([...select('stt-model').options].map((option) => option.value)).toEqual([
      'microsoft/mai-transcribe-2',
      'google/gemini-3.5-transcribe',
    ]);
    expect([...select('stt-language').options].map((option) => [option.value, option.textContent])).toEqual(
      [
        ['', 'Автовизначення'],
        ['en', 'English'],
        ['es', 'Español'],
        ['uk', 'Українська'],
      ],
    );
    expect(select('stt-language')).toHaveValue('');
  });

  it('shows the saved values, and a language the list does not know as it is', async () => {
    await openSettings({
      settings: {
        stt_model: 'google/gemini-3.5-transcribe',
        stt_language: 'de',
        analysis_model: 'vendor/model',
        run_budget_usd: '2.50',
      },
    });

    expect(select('stt-model')).toHaveValue('google/gemini-3.5-transcribe');
    expect(select('stt-language')).toHaveValue('de');
    expect(input('analysis-model')).toHaveValue('vendor/model');
    expect(input('budget-limit')).toHaveValue('2.50');
  });

  it('has a label for every field', async () => {
    await openSettings();

    for (const name of [
      'Ключ API',
      'Модель розпізнавання',
      'Мова розпізнавання',
      'Модель аналізу',
      'Ліміт витрат на один запуск, USD',
    ]) {
      expect(screen.getByLabelText(name)).toBeInTheDocument();
    }
  });
});

describe('settings: the key', () => {
  it('hands the key to main, clears the field and shows the state; the key is nowhere in the page', async () => {
    const { installed } = await openSettings();

    type('api-key', KEY);
    expect(screen.getByTestId('save-api-key')).toBeEnabled();
    fireEvent.click(screen.getByTestId('save-api-key'));

    await waitFor(() => {
      expect(screen.getByTestId('api-key-state')).toHaveTextContent('Стан ключа: захищено');
    });
    expect(installed.setApiKey.mock.calls).toEqual([[KEY]]);
    expect(input('api-key')).toHaveValue('');
    expect(document.body.innerHTML).not.toContain(KEY);
    expect(screen.getByTestId('forget-api-key')).toBeEnabled();
    // The key never goes through api:call.
    expect(JSON.stringify(installed.call.mock.calls)).not.toContain(KEY);
  });

  it('Enter in the field saves the key', async () => {
    const { installed } = await openSettings();

    type('api-key', KEY);
    fireEvent.submit(screen.getByRole('form', { name: 'Ключ OpenRouter' }));

    await waitFor(() => {
      expect(installed.setApiKey).toHaveBeenCalledTimes(1);
    });
  });

  it('says that a session key is only for this session', async () => {
    await openSettings({
      bridge: { setApiKey: () => ok<ApiKeyStatus>({ state: 'session', message: null }) },
    });

    type('api-key', KEY);
    fireEvent.click(screen.getByTestId('save-api-key'));

    expect(await screen.findByText('Стан ключа: лише на цей сеанс')).toBeVisible();
    expect(screen.getByText(/зникне після закриття застосунку/)).toBeVisible();
  });

  it('reads the state main reports at the start, and a fixed message about an unreadable file', async () => {
    await openSettings({
      bridge: {
        apiKey: { state: 'none', message: 'Збережений ключ не вдалося прочитати. Введіть ключ ще раз' },
      },
    });

    expect(screen.getByTestId('api-key-state')).toHaveTextContent('Стан ключа: не задано');
    expect(screen.getByTestId('api-key-message')).toHaveTextContent('Збережений ключ не вдалося прочитати');
    // The unreadable file can still be removed.
    expect(screen.getByTestId('forget-api-key')).toBeEnabled();
  });

  it('shows the Ukrainian text for a key of the wrong shape, keeps what was typed and never echoes it', async () => {
    await openSettings({
      bridge: { setApiKey: () => apiFailure('INVALID_API_KEY', 'Invalid key text of the backend') },
    });

    type('api-key', KEY);
    fireEvent.click(screen.getByTestId('save-api-key'));

    const error = await screen.findByTestId('api-key-error');
    expect(error).toHaveTextContent(INVALID_API_KEY_MESSAGE);
    expect(error).not.toHaveTextContent('Invalid key text');
    expect(input('api-key')).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByTestId('api-key-state')).toHaveTextContent('не задано');
  });

  it('shows the text of any other failure of the save', async () => {
    await openSettings({
      bridge: { setApiKey: () => apiFailure('API_KEY_NOT_SAVED', 'Не вдалося зберегти ключ') },
    });

    type('api-key', KEY);
    fireEvent.click(screen.getByTestId('save-api-key'));

    expect(await screen.findByTestId('api-key-error')).toHaveTextContent('Не вдалося зберегти ключ');
  });

  it('forgets the key: the state returns to «не задано» and the delete button is explained', async () => {
    // The backend lets go of its key when the forget is confirmed, like the real one.
    let held = true;
    const { installed } = await openSettings({
      hasApiKey: () => held,
      bridge: {
        apiKey: { state: 'protected', message: null },
        forgetApiKey: () => {
          held = false;
          return ok(NO_KEY);
        },
      },
    });
    expect(screen.getByTestId('api-key-state')).toHaveTextContent('захищено');

    fireEvent.click(screen.getByRole('button', { name: 'Видалити ключ' }));

    await waitFor(() => {
      expect(screen.getByTestId('api-key-state')).toHaveTextContent('не задано');
    });
    expect(installed.forgetApiKey).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('forget-api-key')).toBeDisabled();
    expect(screen.getByTestId('forget-api-key')).toHaveAccessibleDescription('Ключа немає, тож видаляти нічого.');
    expect(screen.queryByTestId('api-key-backend-note')).not.toBeInTheDocument();
  });

  it('a forget the backend confirmed leaves no warning, even before the backend answers again', async () => {
    let reads = 0;
    installBridge({
      apiKey: { state: 'protected', message: null },
      handlers: {
        'settings.get': () => {
          reads += 1;
          // The first read says the backend holds the key; the one after the forget never comes.
          return reads === 1 ? ok(makeSettings({ has_api_key: true })) : new Promise(() => undefined);
        },
      },
    });
    render(<SettingsScreen />);
    await screen.findByTestId('settings-ai');

    fireEvent.click(screen.getByTestId('forget-api-key'));

    await waitFor(() => {
      expect(screen.getByTestId('api-key-state')).toHaveTextContent('не задано');
    });
    expect(screen.getByTestId('forget-api-key')).toBeDisabled();
    expect(screen.queryByTestId('api-key-backend-note')).not.toBeInTheDocument();
    expect(reads).toBe(2);
  });

  it('offers «Видалити ключ» with a warning when the backend holds a key that main does not', async () => {
    await openSettings({ hasApiKey: () => true });

    expect(screen.getByTestId('api-key-state')).toHaveTextContent('не задано');
    expect(screen.getByTestId('forget-api-key')).toBeEnabled();
    expect(screen.getByTestId('api-key-backend-note')).toHaveTextContent('локальний рушій усе ще тримає');
    expect(screen.getByTestId('forget-api-key')).toHaveAccessibleDescription(/усе ще тримає/);
    expect(screen.queryByText('Ключа немає, тож видаляти нічого.')).not.toBeInTheDocument();
  });

  it('shows the error of a forget that failed and keeps the state', async () => {
    await openSettings({
      bridge: {
        apiKey: { state: 'protected', message: null },
        forgetApiKey: () => apiFailure('API_KEY_NOT_REMOVED', 'Не вдалося видалити збережений ключ'),
      },
    });

    fireEvent.click(screen.getByTestId('forget-api-key'));

    expect(await screen.findByTestId('api-key-error')).toHaveTextContent('Не вдалося видалити');
    expect(screen.getByTestId('api-key-state')).toHaveTextContent('захищено');
  });

  it('after a forget the backend did not confirm, shows what main holds now with the error, never «захищено»', async () => {
    // Like main: the stored key is gone before the backend is asked, and the backend's answer fails.
    let main: ApiKeyStatus = { state: 'protected', message: null };
    await openSettings({
      hasApiKey: () => true,
      bridge: {
        getApiKeyStatus: () => main,
        forgetApiKey: () => {
          main = NO_KEY;
          return apiFailure('BACKEND_UNAVAILABLE', 'Локальний рушій недоступний', false);
        },
      },
    });
    expect(screen.getByTestId('api-key-state')).toHaveTextContent('захищено');

    fireEvent.click(screen.getByTestId('forget-api-key'));

    expect(await screen.findByTestId('api-key-error')).toHaveTextContent('Локальний рушій недоступний');
    await waitFor(() => {
      expect(screen.getByTestId('api-key-state')).toHaveTextContent('не задано');
    });
    expect(screen.getByTestId('settings-ai')).not.toHaveTextContent('захищено');
    // The backend still holds the key, so the way to remove it stays open, with the reason.
    expect(screen.getByTestId('forget-api-key')).toBeEnabled();
    expect(screen.getByTestId('api-key-backend-note')).toBeVisible();
  });

  it('retries a forget the backend refused: the button stays, the warning shows, and the second forget goes through', async () => {
    let main: ApiKeyStatus = { state: 'protected', message: null };
    let held = true;
    let refuse = true;
    const { installed } = await openSettings({
      hasApiKey: () => held,
      bridge: {
        getApiKeyStatus: () => main,
        forgetApiKey: () => {
          // Main's own copy is gone either way; only a confirmed DELETE lets the backend go.
          main = NO_KEY;
          if (refuse) {
            return apiFailure('BACKEND_UNAVAILABLE', 'Локальний рушій недоступний', false);
          }
          held = false;
          return ok(NO_KEY);
        },
      },
    });
    fireEvent.click(screen.getByTestId('forget-api-key'));
    expect(await screen.findByTestId('api-key-error')).toBeVisible();
    await waitFor(() => {
      expect(screen.getByTestId('forget-api-key')).toBeEnabled();
    });
    expect(screen.getByTestId('api-key-backend-note')).toBeVisible();

    refuse = false;
    fireEvent.click(screen.getByTestId('forget-api-key'));

    await waitFor(() => {
      expect(screen.getByTestId('forget-api-key')).toBeDisabled();
    });
    expect(installed.forgetApiKey).toHaveBeenCalledTimes(2);
    expect(screen.queryByTestId('api-key-error')).not.toBeInTheDocument();
    expect(screen.queryByTestId('api-key-backend-note')).not.toBeInTheDocument();
    expect(screen.getByTestId('api-key-state')).toHaveTextContent('не задано');
    expect(screen.getByTestId('forget-api-key')).toHaveAccessibleDescription('Ключа немає, тож видаляти нічого.');
  });

  it('shows a load error, not a state it does not know, when main cannot say what it holds after a failed forget', async () => {
    let reads = 0;
    await openSettings({
      settings: { has_api_key: true },
      bridge: {
        getApiKeyStatus: () => {
          reads += 1;
          return reads === 1
            ? { state: 'protected', message: null }
            : Promise.reject(new Error('IPC refused'));
        },
        forgetApiKey: () => apiFailure('BACKEND_UNAVAILABLE', 'Локальний рушій недоступний', false),
      },
    });

    fireEvent.click(screen.getByTestId('forget-api-key'));

    expect(await screen.findByTestId('settings-load-error')).toBeVisible();
    expect(screen.queryByTestId('api-key-state')).not.toBeInTheDocument();
    expect(screen.queryByText(/захищено/)).not.toBeInTheDocument();
  });

  it('asks the backend again after a change, and warns when it did not get the key', async () => {
    const { installed } = await openSettings();

    // The fake backend still says has_api_key: false after the key was saved.
    type('api-key', KEY);
    fireEvent.click(screen.getByTestId('save-api-key'));

    expect(await screen.findByTestId('api-key-sync-note')).toHaveTextContent('ще не отримав');
    expect(installed.call.mock.calls.filter(([op]) => op === 'settings.get')).toHaveLength(2);
  });

  it('shows no sync warning when the backend has the key', async () => {
    await openSettings({ bridge: { apiKey: { state: 'protected', message: null } }, settings: { has_api_key: true } });

    expect(screen.queryByTestId('api-key-sync-note')).not.toBeInTheDocument();
  });
});

describe('settings: models, language and budget', () => {
  it('saves the four settings, an empty field as null, and shows that it is saved', async () => {
    const { updates, current } = await openSettings();

    fireEvent.change(select('stt-model'), { target: { value: 'google/gemini-3.5-transcribe' } });
    fireEvent.change(select('stt-language'), { target: { value: 'es' } });
    type('analysis-model', ' vendor/model ');
    type('budget-limit', ' 2.50 ');
    fireEvent.click(screen.getByTestId('save-settings'));

    expect(await screen.findByTestId('settings-saved')).toHaveTextContent('Збережено');
    expect(updates).toEqual([
      {
        stt_model: 'google/gemini-3.5-transcribe',
        stt_language: 'es',
        analysis_model: 'vendor/model',
        run_budget_usd: '2.50',
      },
    ]);
    expect(current().run_budget_usd).toBe('2.50');
    // The field still shows what the user wrote.
    expect(input('budget-limit')).toHaveValue(' 2.50 ');
  });

  it('sends null for Автовизначення and for emptied fields', async () => {
    const { updates } = await openSettings({
      settings: { stt_language: 'uk', analysis_model: 'vendor/model', run_budget_usd: '1' },
    });

    fireEvent.change(select('stt-language'), { target: { value: '' } });
    type('analysis-model', '');
    type('budget-limit', '');
    fireEvent.click(screen.getByTestId('save-settings'));

    await screen.findByTestId('settings-saved');
    expect(updates).toEqual([
      { stt_model: 'microsoft/mai-transcribe-2', stt_language: null, analysis_model: null, run_budget_usd: null },
    ]);
  });

  it('shows the Ukrainian text of an invalid budget by its code, and marks the field', async () => {
    await openSettings({
      update: () => failure('INVALID_RUN_BUDGET', 'Backend text in another language'),
    });

    type('budget-limit', 'abc');
    fireEvent.click(screen.getByTestId('save-settings'));

    const error = await screen.findByTestId('settings-error');
    expect(error).toHaveTextContent('Ліміт витрат має бути числом від 0.01 до 1000 USD');
    expect(error).not.toHaveTextContent('Backend text');
    expect(input('budget-limit')).toHaveAttribute('aria-invalid', 'true');
    expect(input('budget-limit')).toHaveAccessibleDescription(/Від 0.01 до 1000/);
    expect(screen.queryByTestId('settings-saved')).not.toBeInTheDocument();
    // What was typed stays, so it can be corrected.
    expect(input('budget-limit')).toHaveValue('abc');
  });

  it.each([
    ['INVALID_STT_MODEL', 'stt-model', 'Оберіть одну з підтримуваних моделей розпізнавання'],
    ['INVALID_STT_LANGUAGE', 'stt-language', 'Оберіть мову зі списку або автовизначення'],
    ['INVALID_ANALYSIS_MODEL', 'analysis-model', 'Модель аналізу має мати вигляд «постачальник/модель»'],
  ])('shows the text of %s and marks %s', async (code, field, text) => {
    await openSettings({ update: () => failure(code, 'x') });

    fireEvent.click(screen.getByTestId('save-settings'));

    expect(await screen.findByTestId('settings-error')).toHaveTextContent(text);
    expect(screen.getByTestId(field)).toHaveAttribute('aria-invalid', 'true');
  });

  it('shows a general text for VALIDATION_ERROR and marks no field', async () => {
    await openSettings({ update: () => failure('VALIDATION_ERROR', 'x') });

    fireEvent.click(screen.getByTestId('save-settings'));

    expect(await screen.findByTestId('settings-error')).toHaveTextContent('Налаштування мають некоректний вигляд');
    for (const testId of ['stt-model', 'stt-language', 'analysis-model', 'budget-limit']) {
      expect(screen.getByTestId(testId)).toHaveAttribute('aria-invalid', 'false');
    }
  });

  it('shows the text of the backend for a code it does not know', async () => {
    await openSettings({ update: () => failure('BACKEND_UNAVAILABLE', 'Локальний рушій недоступний', true) });

    fireEvent.click(screen.getByTestId('save-settings'));

    expect(await screen.findByTestId('settings-error')).toHaveTextContent('Локальний рушій недоступний');
  });

  it('clears the error on the next save and the «Збережено» mark on the next edit', async () => {
    let refuse = true;
    await openSettings({
      update: () => (refuse ? failure('INVALID_RUN_BUDGET', 'x') : null),
    });

    type('budget-limit', 'abc');
    fireEvent.click(screen.getByTestId('save-settings'));
    await screen.findByTestId('settings-error');

    refuse = false;
    type('budget-limit', '3');
    fireEvent.click(screen.getByTestId('save-settings'));
    await screen.findByTestId('settings-saved');
    expect(screen.queryByTestId('settings-error')).not.toBeInTheDocument();

    type('budget-limit', '4');
    expect(screen.queryByTestId('settings-saved')).not.toBeInTheDocument();
  });
});

describe('settings: loading and errors', () => {
  it('shows a loading note and then the block', async () => {
    installBridge({ handlers: { 'settings.get': () => ok(makeSettings()) } });
    render(<SettingsScreen />);

    expect(screen.getByTestId('settings-loading')).toBeVisible();
    expect(await screen.findByTestId('settings-ai')).toBeVisible();
    expect(screen.queryByTestId('settings-loading')).not.toBeInTheDocument();
  });

  it('shows an error with «Спробувати ще раз» when the settings cannot be read, and recovers', async () => {
    let attempts = 0;
    installBridge({
      handlers: {
        'settings.get': () => {
          attempts += 1;
          return attempts === 1 ? failure('BACKEND_UNAVAILABLE', 'Локальний рушій недоступний', true) : ok(makeSettings());
        },
      },
    });
    render(<SettingsScreen />);

    expect(await screen.findByTestId('settings-load-error')).toHaveTextContent('Локальний рушій недоступний');
    expect(screen.queryByTestId('settings-ai')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('settings-retry'));

    expect(await screen.findByTestId('settings-ai')).toBeVisible();
  });

  it('shows the same error when main cannot report the state of the key', async () => {
    installBridge({
      handlers: { 'settings.get': () => ok(makeSettings()) },
      getApiKeyStatus: () => Promise.reject(new Error('IPC refused')),
    });
    render(<SettingsScreen />);

    expect(await screen.findByTestId('settings-load-error')).toBeVisible();
  });
});

describe('settings: checking the key (ADR 039)', () => {
  const CHECK = 'Перевірити ключ';
  const HELD: Partial<SettingsDto> = { has_api_key: true };

  it('is not offered while the backend holds no key', async () => {
    await openSettings();

    expect(screen.queryByRole('button', { name: CHECK })).toBeNull();
  });

  it('is offered while the backend holds a key', async () => {
    await openSettings({ settings: HELD });

    expect(screen.getByRole('button', { name: CHECK })).toBeEnabled();
  });

  it('says that the key is valid and what is left of its limit, in dollars', async () => {
    const { installed } = await openSettings({
      settings: HELD,
      check: () => ok<CredentialCheckDto>({ state: 'valid', is_free_tier: false, limit_remaining_usd: '4.25' }),
    });

    fireEvent.click(screen.getByRole('button', { name: CHECK }));

    const result = await screen.findByTestId('api-key-check-result');
    expect(result).toHaveAttribute('role', 'status');
    expect(result).toHaveTextContent('Ключ дійсний');
    expect(screen.getByTestId('api-key-limit')).toHaveTextContent('Залишок ліміту: $4,25');
    expect(installed.call).toHaveBeenCalledWith('credentials.check', {});
  });

  it('says that the key is valid without a limit when the account has none', async () => {
    await openSettings({
      settings: HELD,
      check: () => ok<CredentialCheckDto>({ state: 'valid', is_free_tier: true, limit_remaining_usd: null }),
    });

    fireEvent.click(screen.getByRole('button', { name: CHECK }));

    expect(await screen.findByTestId('api-key-check-result')).toHaveTextContent('Ключ дійсний');
    expect(screen.getByTestId('api-key-limit')).toHaveTextContent('Ліміт витрат для ключа не задано');
  });

  it.each([
    ['0', 'Ліміт витрат ключа вичерпано'],
    ['0.000000', 'Ліміт витрат ключа вичерпано'],
    ['-0.5', 'Ліміт витрат ключа вичерпано'],
    ['0.01', 'Залишок ліміту: $0,01'],
  ])('says what is left of the limit when the remaining sum is %s', async (remaining, text) => {
    await openSettings({
      settings: HELD,
      check: () => ok<CredentialCheckDto>({ state: 'valid', is_free_tier: false, limit_remaining_usd: remaining }),
    });

    fireEvent.click(screen.getByRole('button', { name: CHECK }));

    expect(await screen.findByTestId('api-key-check-result')).toHaveTextContent('Ключ дійсний');
    expect(screen.getByTestId('api-key-limit')).toHaveTextContent(text);
  });

  it('says that OpenRouter did not accept the key', async () => {
    await openSettings({ settings: HELD, check: () => ok<CredentialCheckDto>({ state: 'invalid' }) });

    fireEvent.click(screen.getByRole('button', { name: CHECK }));

    const result = await screen.findByTestId('api-key-check-result');
    expect(result).toHaveAttribute('role', 'status');
    expect(result).toHaveTextContent('OpenRouter не прийняв ключ');
    expect(screen.queryByTestId('api-key-limit')).toBeNull();
  });

  it.each([
    ['KEY_CHECK_UPSTREAM', 'OpenRouter не відповів. Спробуйте ще раз'],
    ['API_KEY_REQUIRED', 'Спершу задайте ключ API OpenRouter'],
  ])('shows the backend message of a failed check (%s) with role alert', async (code, message) => {
    await openSettings({ settings: HELD, check: () => apiFailure(code, message, true) });

    fireEvent.click(screen.getByRole('button', { name: CHECK }));

    const error = await screen.findByTestId('api-key-check-error');
    expect(error).toHaveAttribute('role', 'alert');
    expect(error).toHaveTextContent(message);
    expect(screen.queryByTestId('api-key-check-result')).toBeNull();
  });

  it('is off while the check is on its way', async () => {
    const reply = deferred<ApiResult<CredentialCheckDto>>();
    await openSettings({ settings: HELD, check: () => reply.promise });

    fireEvent.click(screen.getByRole('button', { name: CHECK }));
    expect(screen.getByRole('button', { name: CHECK })).toBeDisabled();

    await act(async () => {
      reply.resolve(ok<CredentialCheckDto>({ state: 'invalid' }));
      await flushAnswers();
    });
    expect(screen.getByRole('button', { name: CHECK })).toBeEnabled();
    expect(screen.getByTestId('api-key-check-result')).toHaveTextContent('OpenRouter не прийняв ключ');
  });

  it('forgets the answer when a key is saved, and the check of the new key starts without one', async () => {
    const { installed } = await openSettings({
      settings: HELD,
      check: () => ok<CredentialCheckDto>({ state: 'valid', is_free_tier: false, limit_remaining_usd: '4.25' }),
    });

    fireEvent.click(screen.getByRole('button', { name: CHECK }));
    expect(await screen.findByTestId('api-key-check-result')).toHaveTextContent('Ключ дійсний');

    type('api-key', KEY);
    fireEvent.click(screen.getByTestId('save-api-key'));

    await waitFor(() => {
      expect(screen.queryByTestId('api-key-check-result')).toBeNull();
    });
    expect(screen.queryByTestId('api-key-limit')).toBeNull();
    expect(screen.getByRole('button', { name: CHECK })).toBeEnabled();
    expect(installed.setApiKey).toHaveBeenCalledTimes(1);
  });

  it('forgets the answer when the key is forgotten, and a key saved again starts without one', async () => {
    let held = true;
    await openSettings({
      settings: HELD,
      hasApiKey: () => held,
      check: () => ok<CredentialCheckDto>({ state: 'invalid' }),
      bridge: {
        forgetApiKey: () => {
          held = false;
          return ok(NO_KEY);
        },
      },
    });

    fireEvent.click(screen.getByRole('button', { name: CHECK }));
    expect(await screen.findByTestId('api-key-check-result')).toHaveTextContent('OpenRouter не прийняв ключ');

    fireEvent.click(screen.getByTestId('forget-api-key'));
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: CHECK })).toBeNull();
    });
    expect(screen.queryByTestId('api-key-check-result')).toBeNull();

    held = true;
    type('api-key', KEY);
    fireEvent.click(screen.getByTestId('save-api-key'));
    expect(await screen.findByRole('button', { name: CHECK })).toBeEnabled();
    expect(screen.queryByTestId('api-key-check-result')).toBeNull();
  });

  it('drops an answer about the old key that comes after the key was saved', async () => {
    const reply = deferred<ApiResult<CredentialCheckDto>>();
    const { installed } = await openSettings({ settings: HELD, check: () => reply.promise });

    fireEvent.click(screen.getByRole('button', { name: CHECK }));
    type('api-key', KEY);
    fireEvent.click(screen.getByTestId('save-api-key'));
    await waitFor(() => {
      expect(installed.setApiKey).toHaveBeenCalledTimes(1);
    });
    await waitFor(() => {
      expect(screen.getByRole('button', { name: CHECK })).toBeEnabled();
    });

    await act(async () => {
      reply.resolve(ok<CredentialCheckDto>({ state: 'invalid' }));
      await flushAnswers();
    });
    expect(screen.queryByTestId('api-key-check-result')).toBeNull();
    expect(screen.getByRole('button', { name: CHECK })).toBeEnabled();
  });
});

describe('settings in the shell', () => {
  async function openApp(): Promise<InstalledBridge> {
    const installed = installBridge({
      handlers: {
        'projects.list': listOf(makeProject()),
        'settings.get': () => ok(makeSettings()),
      },
    });
    render(<App />);
    await screen.findByTestId('app-ready');
    return installed;
  }

  it('opens from the sidebar, marks it as the current page and goes back to the projects', async () => {
    await openApp();
    const nav = screen.getByRole('navigation', { name: 'Основна навігація' });

    fireEvent.click(within(nav).getByRole('button', { name: 'Налаштування' }));

    expect(await screen.findByTestId('settings-ai')).toBeVisible();
    expect(within(nav).getByRole('button', { name: 'Налаштування' })).toHaveAttribute('aria-current', 'page');
    expect(within(nav).getByRole('button', { name: 'Проєкти' })).not.toHaveAttribute('aria-current');

    fireEvent.click(within(nav).getByRole('button', { name: 'Проєкти' }));

    expect(await screen.findAllByTestId('project-item')).toHaveLength(1);
    expect(screen.queryByTestId('settings-ai')).not.toBeInTheDocument();
    expect(within(nav).getByRole('button', { name: 'Проєкти' })).toHaveAttribute('aria-current', 'page');
  });

  it('opening the settings does not forget the open project', async () => {
    const installed = installBridge({
      viewState: {
        project_id: PROJECT_ID,
        tab: 'sources',
        layout: { list_px: 320, details_px: 320 },
      },
      handlers: {
        'projects.list': listOf(makeProject()),
        'settings.get': () => ok(makeSettings()),
      },
    });
    render(<App />);
    await screen.findByRole('heading', { level: 2, name: makeProject().name });
    const nav = screen.getByRole('navigation', { name: 'Основна навігація' });
    installed.setViewState.mockClear();

    fireEvent.click(within(nav).getByRole('button', { name: 'Налаштування' }));
    await screen.findByTestId('settings-ai');
    fireEvent.click(within(nav).getByRole('button', { name: 'Проєкти' }));

    // Back on the project, and nothing was written to the remembered state meanwhile.
    expect(await screen.findByRole('heading', { level: 2, name: makeProject().name })).toBeVisible();
    expect(installed.setViewState).not.toHaveBeenCalled();
  });
});
