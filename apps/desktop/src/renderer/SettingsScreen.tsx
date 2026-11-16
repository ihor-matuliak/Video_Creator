import { useEffect, useId, useState } from "react";
import type { SubmitEvent } from "react";

import type { SettingsDto } from "../shared/api";
import type { ApiErrorInfo, ApiKeyStatus } from "../shared/bridge";
import {
  callApi,
  loadApiKeyStatus,
  removeApiKey,
  saveApiKey,
} from "./bridge-calls";
import {
  KEY_STATE_VIEWS,
  STT_LANGUAGE_OPTIONS,
  STT_MODEL_OPTIONS,
  apiKeyProblem,
  draftOf,
  optionsWith,
  settingsProblem,
  updateOf,
} from "./settings-logic";
import type {
  SettingsDraft,
  SettingsField,
  SettingsProblem,
} from "./settings-logic";
import { keyLimitText } from "./transcription-logic";
import { StatusText } from "./ui";

type Load =
  | { readonly kind: "loading" }
  | { readonly kind: "error"; readonly error: ApiErrorInfo }
  | {
      readonly kind: "ready";
      readonly settings: SettingsDto;
      readonly key: ApiKeyStatus;
    };

/** Reads the settings and the state of the key together; either failing is one error. */
async function loadAll(): Promise<Load> {
  const [settings, key] = await Promise.all([
    callApi("settings.get", {}),
    loadApiKeyStatus(),
  ]);
  if (!settings.ok) {
    return { kind: "error", error: settings.error };
  }
  if (!key.ok) {
    return { kind: "error", error: key.error };
  }
  return { kind: "ready", settings: settings.data, key: key.data };
}

/**
 * «Налаштування» (docs/03 section 10). Only the AI block exists so far: the OpenRouter key, the
 * two models, the language and the budget of a run. The key goes to main and is never shown back;
 * the other four settings live in the backend.
 */
export function SettingsScreen() {
  const [load, setLoad] = useState<Load>({ kind: "loading" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    // An object, not a bare boolean: the cleanup flips it while the load below awaits.
    const run = { cancelled: false };
    void loadAll().then((next) => {
      if (!run.cancelled) {
        setLoad(next);
      }
    });
    return () => {
      run.cancelled = true;
    };
  }, [attempt]);

  return (
    <div className="screen" data-testid="settings-screen">
      <div className="screen-header">
        <h2>Налаштування</h2>
      </div>
      {load.kind === "loading" && (
        <p role="status" data-testid="settings-loading" className="note">
          Завантажуємо…
        </p>
      )}
      {load.kind === "error" && (
        <div role="alert" data-testid="settings-load-error" className="notice">
          <h3>Не вдалося прочитати налаштування</h3>
          <p>{load.error.message}</p>
          <button
            type="button"
            className="button"
            data-testid="settings-retry"
            onClick={() => {
              setLoad({ kind: "loading" });
              setAttempt((value) => value + 1);
            }}
          >
            Спробувати ще раз
          </button>
        </div>
      )}
      {load.kind === "ready" && (
        <AiBlock
          settings={load.settings}
          keyStatus={load.key}
          onKeyStatusLost={(error) => {
            setLoad({ kind: "error", error });
          }}
          onKeyStatus={(key, backendHasKey) => {
            setLoad((current) =>
              current.kind !== "ready"
                ? current
                : {
                    ...current,
                    key,
                    // A forget the backend confirmed: it holds no key now, whatever the last read said.
                    settings:
                      backendHasKey === undefined
                        ? current.settings
                        : { ...current.settings, has_api_key: backendHasKey },
                  },
            );
            // The backend's own view of the key (`has_api_key`) follows the change.
            void callApi("settings.get", {}).then((result) => {
              if (result.ok) {
                setLoad((current) =>
                  current.kind === "ready"
                    ? {
                        ...current,
                        settings: {
                          ...current.settings,
                          has_api_key: result.data.has_api_key,
                        },
                      }
                    : current,
                );
              }
            });
          }}
          onSaved={(settings) => {
            setLoad((current) =>
              current.kind === "ready" ? { ...current, settings } : current,
            );
          }}
        />
      )}
    </div>
  );
}

function AiBlock({
  settings,
  keyStatus,
  onKeyStatus,
  onKeyStatusLost,
  onSaved,
}: {
  readonly settings: SettingsDto;
  readonly keyStatus: ApiKeyStatus;
  readonly onKeyStatus: (status: ApiKeyStatus, backendHasKey?: boolean) => void;
  readonly onKeyStatusLost: (error: ApiErrorInfo) => void;
  readonly onSaved: (settings: SettingsDto) => void;
}) {
  const sync = keyStatus.state !== "none" && !settings.has_api_key;
  // Counts the saves and forgets of the key. A new count gives the key check a new `key`, so an answer
  // about the old key is never shown for the new one (ADR 039).
  const [keyVersion, setKeyVersion] = useState(0);
  return (
    <section
      className="panel"
      aria-labelledby="settings-ai-title"
      data-testid="settings-ai"
    >
      <h3 id="settings-ai-title">AI</h3>
      <ApiKeyForm
        status={keyStatus}
        backendHasKey={settings.has_api_key}
        onStatus={(key, backendHasKey) => {
          setKeyVersion((version) => version + 1);
          onKeyStatus(key, backendHasKey);
        }}
        onStatusLost={onKeyStatusLost}
      />
      {settings.has_api_key && <KeyCheck key={keyVersion} />}
      {sync && (
        <p role="status" data-testid="api-key-sync-note" className="note">
          <span aria-hidden="true">ⓘ </span>
          Ключ збережено, але локальний рушій його ще не отримав. Введіть ключ і
          збережіть його ще раз.
        </p>
      )}
      <ModelsForm settings={settings} onSaved={onSaved} />
    </section>
  );
}

/** What the key check answered: the key is accepted (with what is left of its limit), refused, or the check failed. */
type CheckOutcome =
  | { readonly kind: "valid"; readonly limitUsd: string | null }
  | { readonly kind: "invalid" }
  | { readonly kind: "failed"; readonly error: ApiErrorInfo };

/**
 * «Перевірити ключ» (ADR 039): asks OpenRouter whether the key the backend holds is accepted and what
 * is left of its limit. It is offered only while the backend holds a key. The answer stays on screen
 * until the key is saved or forgotten: the parent then gives it a new `key`, which starts it again.
 */
function KeyCheck() {
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<CheckOutcome | null>(null);

  const check = (): void => {
    if (busy) {
      return;
    }
    setBusy(true);
    setOutcome(null);
    void callApi("credentials.check", {}).then((result) => {
      setBusy(false);
      if (!result.ok) {
        setOutcome({ kind: "failed", error: result.error });
        return;
      }
      const answer = result.data;
      setOutcome(
        answer.state === "valid"
          ? { kind: "valid", limitUsd: answer.limit_remaining_usd }
          : { kind: "invalid" },
      );
    });
  };

  return (
    <div data-testid="api-key-check">
      <div className="actions">
        <button
          type="button"
          className="button"
          data-testid="check-api-key"
          disabled={busy}
          onClick={check}
        >
          Перевірити ключ
        </button>
      </div>
      {outcome?.kind === "valid" && (
        <>
          <p role="status" data-testid="api-key-check-result">
            <StatusText tone="success">Ключ дійсний</StatusText>
          </p>
          <p className="note" data-testid="api-key-limit">
            {keyLimitText(outcome.limitUsd)}
          </p>
        </>
      )}
      {outcome?.kind === "invalid" && (
        <p role="status" data-testid="api-key-check-result">
          <StatusText tone="danger">OpenRouter не прийняв ключ</StatusText>
        </p>
      )}
      {outcome?.kind === "failed" && (
        <p
          role="alert"
          data-testid="api-key-check-error"
          className="field-error"
        >
          <span aria-hidden="true">✕</span> {outcome.error.message}
        </p>
      )}
    </div>
  );
}

function ApiKeyForm({
  status,
  backendHasKey,
  onStatus,
  onStatusLost,
}: {
  readonly status: ApiKeyStatus;
  /** The backend says it holds a key (`has_api_key`), whatever main holds. */
  readonly backendHasKey: boolean;
  /** `backendHasKey` is passed when the backend is known to hold no key now (a confirmed forget). */
  readonly onStatus: (status: ApiKeyStatus, backendHasKey?: boolean) => void;
  /** Main could not say what it holds now: the screen cannot show a state it does not know. */
  readonly onStatusLost: (error: ApiErrorInfo) => void;
}) {
  const fieldId = useId();
  const hintId = useId();
  const errorId = useId();
  const noKeyId = useId();
  const backendKeyId = useId();
  // The key lives here only while it is typed: it is cleared as soon as main has it.
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiErrorInfo | null>(null);
  const view = KEY_STATE_VIEWS[status.state];
  // Main holds no key but the backend still does (a forget it did not confirm): the key could be
  // spent, so «Видалити ключ» stays available; main sends the DELETE again.
  const backendOnly = status.state === "none" && backendHasKey;
  const canForget =
    status.state !== "none" || status.message !== null || backendHasKey;

  const submit = (event: SubmitEvent<HTMLFormElement>): void => {
    event.preventDefault();
    if (busy || typed === "") {
      return;
    }
    setBusy(true);
    setError(null);
    void saveApiKey(typed).then((result) => {
      setBusy(false);
      if (result.ok) {
        setTyped("");
        onStatus(result.data);
      } else {
        setError(result.error);
      }
    });
  };

  const forget = (): void => {
    if (busy) {
      return;
    }
    setBusy(true);
    setError(null);
    void removeApiKey().then(async (result) => {
      if (result.ok) {
        setBusy(false);
        setTyped("");
        onStatus(result.data, false);
        return;
      }
      // A forget that failed may still have changed what main holds (the stored key goes before
      // the backend is asked), so the state shown is read again, next to the error.
      setError(result.error);
      const read = await loadApiKeyStatus();
      setBusy(false);
      if (read.ok) {
        onStatus(read.data);
      } else {
        onStatusLost(read.error);
      }
    });
  };

  return (
    <form
      className="settings-form"
      aria-label="Ключ OpenRouter"
      noValidate
      onSubmit={submit}
    >
      <h4 className="settings-subtitle">Ключ OpenRouter</h4>
      <p className="status-line">
        <StatusText tone={view.tone} testId="api-key-state">
          Стан ключа: {view.label}
        </StatusText>
      </p>
      {view.note !== null && <p className="note">{view.note}</p>}
      {status.message !== null && (
        <p role="status" data-testid="api-key-message" className="note">
          <span aria-hidden="true">⚠ </span>
          {status.message}
        </p>
      )}
      <div className="setting">
        <label htmlFor={fieldId}>Ключ API</label>
        <input
          id={fieldId}
          data-testid="api-key"
          className="field"
          type="password"
          value={typed}
          autoComplete="off"
          spellCheck={false}
          readOnly={busy}
          aria-invalid={error !== null}
          aria-describedby={error === null ? hintId : `${hintId} ${errorId}`}
          onChange={(event) => {
            setTyped(event.target.value);
          }}
        />
        <p id={hintId} className="note">
          Після збереження ключ не показується. Щоб замінити його, введіть
          новий.
        </p>
        {error !== null && (
          <p
            id={errorId}
            role="alert"
            data-testid="api-key-error"
            className="field-error"
          >
            <span aria-hidden="true">✕</span> {apiKeyProblem(error)}
          </p>
        )}
      </div>
      <div className="actions">
        <button
          type="submit"
          className="button primary"
          data-testid="save-api-key"
          disabled={busy || typed === ""}
        >
          {busy ? "Зберігаємо…" : "Зберегти ключ"}
        </button>
        <button
          type="button"
          className="button"
          data-testid="forget-api-key"
          disabled={busy || !canForget}
          aria-describedby={
            backendOnly ? backendKeyId : canForget ? undefined : noKeyId
          }
          onClick={forget}
        >
          Видалити ключ
        </button>
      </div>
      {backendOnly && (
        <p
          id={backendKeyId}
          role="status"
          data-testid="api-key-backend-note"
          className="note"
        >
          <span aria-hidden="true">⚠ </span>
          Застосунок уже не зберігає ключ, але локальний рушій усе ще тримає
          його і може ним скористатися. Натисніть «Видалити ключ», щоб прибрати
          й цю копію.
        </p>
      )}
      {!canForget && (
        <p id={noKeyId} className="note">
          Ключа немає, тож видаляти нічого.
        </p>
      )}
    </form>
  );
}

function ModelsForm({
  settings,
  onSaved,
}: {
  readonly settings: SettingsDto;
  readonly onSaved: (settings: SettingsDto) => void;
}) {
  const ids = {
    model: useId(),
    language: useId(),
    analysis: useId(),
    budget: useId(),
    error: useId(),
  };
  const [draft, setDraft] = useState<SettingsDraft>(() => draftOf(settings));
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [problem, setProblem] = useState<SettingsProblem | null>(null);

  const edit = (patch: Partial<SettingsDraft>): void => {
    setDraft((current) => ({ ...current, ...patch }));
    setSaved(false);
  };
  const invalid = (field: SettingsField): boolean => problem?.field === field;
  const describedBy = (
    field: SettingsField,
    hint?: string,
  ): string | undefined => {
    const parts = [hint, invalid(field) ? ids.error : undefined].filter(
      (part): part is string => part !== undefined,
    );
    return parts.length === 0 ? undefined : parts.join(" ");
  };

  const submit = (event: SubmitEvent<HTMLFormElement>): void => {
    event.preventDefault();
    if (saving) {
      return;
    }
    setSaving(true);
    setProblem(null);
    setSaved(false);
    void callApi("settings.update", updateOf(draft)).then((result) => {
      setSaving(false);
      if (result.ok) {
        setSaved(true);
        onSaved(result.data);
      } else {
        setProblem(settingsProblem(result.error));
      }
    });
  };

  return (
    <form
      className="settings-form"
      aria-label="Моделі й витрати"
      noValidate
      onSubmit={submit}
    >
      <h4 className="settings-subtitle">Розпізнавання мови</h4>
      <div className="setting">
        <label htmlFor={ids.model}>Модель розпізнавання</label>
        <select
          id={ids.model}
          data-testid="stt-model"
          className="field"
          value={draft.sttModel}
          aria-invalid={invalid("sttModel")}
          aria-describedby={describedBy("sttModel")}
          onChange={(event) => {
            edit({ sttModel: event.target.value });
          }}
        >
          {optionsWith(STT_MODEL_OPTIONS, draft.sttModel).map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </div>
      <div className="setting">
        <label htmlFor={ids.language}>Мова розпізнавання</label>
        <select
          id={ids.language}
          data-testid="stt-language"
          className="field"
          value={draft.sttLanguage}
          aria-invalid={invalid("sttLanguage")}
          aria-describedby={describedBy("sttLanguage")}
          onChange={(event) => {
            edit({ sttLanguage: event.target.value });
          }}
        >
          {optionsWith(STT_LANGUAGE_OPTIONS, draft.sttLanguage).map(
            (option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ),
          )}
        </select>
      </div>
      <p data-testid="price-note" className="note">
        Ціна: Невідомо
      </p>

      <h4 className="settings-subtitle">Аналіз тексту</h4>
      <div className="setting">
        <label htmlFor={ids.analysis}>Модель аналізу</label>
        <input
          id={ids.analysis}
          data-testid="analysis-model"
          className="field"
          type="text"
          value={draft.analysisModel}
          autoComplete="off"
          spellCheck={false}
          aria-invalid={invalid("analysisModel")}
          aria-describedby={describedBy(
            "analysisModel",
            `${ids.analysis}-hint`,
          )}
          onChange={(event) => {
            edit({ analysisModel: event.target.value });
          }}
        />
        <p id={`${ids.analysis}-hint`} className="note">
          У вигляді «постачальник/модель». Можна залишити порожнім.
        </p>
      </div>

      <h4 className="settings-subtitle">Витрати</h4>
      <div className="setting">
        <label htmlFor={ids.budget}>Ліміт витрат на один запуск, USD</label>
        <input
          id={ids.budget}
          data-testid="budget-limit"
          className="field"
          type="text"
          inputMode="decimal"
          value={draft.budget}
          autoComplete="off"
          aria-invalid={invalid("budget")}
          aria-describedby={describedBy("budget", `${ids.budget}-hint`)}
          onChange={(event) => {
            edit({ budget: event.target.value });
          }}
        />
        <p id={`${ids.budget}-hint`} className="note">
          Від 0.01 до 1000, наприклад 2.50. Без ліміту розпізнавання не
          запуститься.
        </p>
      </div>

      {problem !== null && (
        <p
          id={ids.error}
          role="alert"
          data-testid="settings-error"
          className="field-error"
        >
          <span aria-hidden="true">✕</span> {problem.message}
        </p>
      )}
      {saved && (
        <p role="status" data-testid="settings-saved">
          <StatusText tone="success">Збережено</StatusText>
        </p>
      )}
      <div className="actions">
        <button
          type="submit"
          className="button primary"
          data-testid="save-settings"
          disabled={saving}
        >
          {saving ? "Зберігаємо…" : "Зберегти налаштування"}
        </button>
      </div>
    </form>
  );
}
