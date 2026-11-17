import { useId, useState } from 'react';

import type { AnalysisEstimateDto, SourceDto } from '../shared/api';
import type { ApiErrorInfo } from '../shared/bridge';
import {
  CATEGORIES,
  SEARCH_CAPTION,
  allState,
  instructionCounter,
  needsSettings,
  profileErrors,
  profileFromDraft,
  startBlockReason,
  summaryRows,
  toggleAll,
  toggleCategory,
  transcriptBlockReason,
} from './analysis-logic';
import type { AnalysisView, Category, CategoryId } from './analysis-logic';
import { useOpenSettings } from './settings-nav';
import { partsText, pluralUk } from './transcription-logic';
import { StatusText } from './ui';
import type { Tone } from './ui';
import { useAnalysis } from './use-analysis';
import type { Analysis } from './use-analysis';

// The «Що шукати» panel of one source (docs/03 section 6, ADR 041 §23). The texts that are not in
// analysis-logic live here; the backend stays the authority on every value it refuses.

const PROFILE_FIX_TEXT = 'Виправте поля, позначені вище';
const STALE_TRANSCRIPT_TEXT = 'Транскрипт змінився. Перевірте оцінку ще раз';
const STALE_RUN_TEXT = 'Транскрипт змінився після цього пошуку';
const CANCELLING_TEXT = 'Скасовуємо пошук…';
const STOPPED_COLLAPSED_TEXT = 'Пошук зупинено. Розгорніть «Що шукати», щоб продовжити';
const RESEND_QUESTION =
  'Провайдер міг уже обробити ці частини і списати кошти. Якщо надіслати їх ще раз, за них можуть заплатити вдруге.';
const UPPER_BOUND_NOTE = 'Оцінка за верхньою межею: повтори можуть коштувати більше.';

type StoppedView = Extract<AnalysisView, { readonly kind: 'stopped' }>;

/** The ids a control is described by: only those of the notes shown; undefined when there are none. */
function describedBy(...ids: readonly (string | null | undefined)[]): string | undefined {
  const shown = ids.filter((id): id is string => typeof id === 'string');
  return shown.length === 0 ? undefined : shown.join(' ');
}

/** A whole number as its field shows it; NaN (an empty field) is shown empty. */
function numberText(value: number): string {
  return Number.isNaN(value) ? '' : String(value);
}

/** The text of a whole-number field as a number; an empty field is NaN, which the rules refuse. */
function parseNumber(text: string): number {
  return text.trim() === '' ? Number.NaN : Number(text);
}

/**
 * The «Що шукати» panel. Collapsed by default. The hook sits here, not in the body, so that the
 * profile and the search survive collapsing the panel. The parent keys it by the source.
 */
export function SearchPanel({ source }: { readonly source: SourceDto }) {
  const [expanded, setExpanded] = useState(false);
  const bodyId = useId();
  const analysis = useAnalysis(source.id);
  const status = collapsedStatus(analysis.state.view, analysis.state.job?.state === 'cancelling');

  return (
    <div className="search-panel" data-testid="search-panel">
      <button
        type="button"
        className="button"
        data-testid="search-settings"
        aria-expanded={expanded}
        aria-controls={expanded ? bodyId : undefined}
        onClick={() => {
          setExpanded((value) => !value);
        }}
      >
        Що шукати
      </button>
      {!expanded && status !== null && (
        <p role="status" data-testid="search-status-collapsed">
          <StatusText tone={status.tone}>{status.text}</StatusText>
        </p>
      )}
      {expanded && (
        <div id={bodyId} className="search-body">
          <SearchBody durationMs={source.duration_ms} analysis={analysis} />
        </div>
      )}
    </div>
  );
}

/**
 * The one line that keeps a search on screen visible while the panel is collapsed (docs/03 §7). Its texts
 * are those of the expanded blocks; null when there is no search on screen.
 */
function collapsedStatus(
  view: AnalysisView,
  cancelling: boolean,
): { readonly tone: Tone; readonly text: string } | null {
  if (view.kind === 'running') {
    return { tone: 'neutral', text: cancelling ? CANCELLING_TEXT : view.progress };
  }
  if (view.kind === 'completed') {
    return { tone: view.count > 0 ? 'success' : 'neutral', text: view.text };
  }
  if (view.kind === 'stopped') {
    return { tone: 'warning', text: STOPPED_COLLAPSED_TEXT };
  }
  return null;
}

function SearchBody({
  durationMs,
  analysis,
}: {
  readonly durationMs: number;
  readonly analysis: Analysis;
}) {
  const { state, actions } = analysis;
  const { draft, transcript, run, job, estimate, busy, error, view, setDraft } = state;
  const openSettings = useOpenSettings();
  const errors = profileErrors(draft);
  const transcriptReason = transcriptBlockReason(transcript, draft.allowPartial);
  const profileBroken = profileFromDraft(draft) === null;
  const findReason = transcriptReason ?? (profileBroken ? PROFILE_FIX_TEXT : null);
  // Only a partly recognised transcript shows the flag (docs/03 section 6, ADR 041 §23(3)).
  const partialShown = transcript?.coverage.some((span) => span.state !== 'succeeded') === true;
  const startReason = view.kind === 'idle' && estimate !== null ? startBlockReason(estimate) : null;
  // The actions that send a request to a job need the job of the search on screen.
  const canAct = (run?.job_id ?? null) !== null;
  const cancelling = job?.state === 'cancelling';
  const settingsWanted =
    (view.kind === 'stopped' && view.needsSettings) ||
    (error !== null && needsSettings(error.code)) ||
    startReason !== null;

  return (
    <>
      <p className="note" data-testid="search-caption">
        <span aria-hidden="true">ⓘ </span>
        {SEARCH_CAPTION}
      </p>
      <CategoryChips
        selected={draft.categories}
        error={errors.categories}
        onChange={(categories) => {
          setDraft({ ...draft, categories });
        }}
      />
      <InstructionField
        value={draft.customInstruction}
        error={errors.customInstruction}
        onChange={(customInstruction) => {
          setDraft({ ...draft, customInstruction });
        }}
      />
      <div className="search-durations">
        <NumberField
          label="Тривалість від, с"
          testId="search-min-seconds"
          value={draft.minSeconds}
          min={5}
          max={600}
          error={errors.minSeconds}
          onChange={(minSeconds) => {
            setDraft({ ...draft, minSeconds });
          }}
        />
        <NumberField
          label="Тривалість до, с"
          testId="search-max-seconds"
          value={draft.maxSeconds}
          min={5}
          max={600}
          error={errors.maxSeconds}
          onChange={(maxSeconds) => {
            setDraft({ ...draft, maxSeconds });
          }}
        />
        <NumberField
          label="До скількох моментів"
          testId="search-max-candidates"
          value={draft.maxCandidates}
          min={1}
          max={100}
          error={errors.maxCandidates}
          onChange={(maxCandidates) => {
            setDraft({ ...draft, maxCandidates });
          }}
        />
      </div>
      {partialShown && (
        <PartialFlag
          checked={draft.allowPartial}
          onChange={(allowPartial) => {
            setDraft({ ...draft, allowPartial });
          }}
        />
      )}

      {view.kind === 'idle' && (
        <StartBlock
          durationMs={durationMs}
          categories={draft.categories}
          estimate={estimate}
          busy={busy}
          findReason={findReason}
          startReason={startReason}
          onEstimate={actions.estimate}
          onCreate={actions.create}
        />
      )}
      {view.kind === 'running' && (
        <RunningBlock
          progress={view.progress}
          cancelling={cancelling}
          disabled={busy || !canAct}
          onCancel={actions.cancel}
        />
      )}
      {view.kind === 'completed' && (
        <CompletedBlock
          text={view.text}
          count={view.count}
          busy={busy}
          onRestart={actions.restart}
        />
      )}
      {view.kind === 'stopped' && (
        <StoppedBlock
          view={view}
          canAct={canAct}
          busy={busy}
          onResume={actions.resume}
          onResumeWithBudget={actions.resumeWithBudget}
          onRestart={actions.restart}
        />
      )}
      {run?.stale === true && view.kind !== 'idle' && (
        <p className="note" data-testid="search-stale">
          <span aria-hidden="true">⚠ </span>
          {STALE_RUN_TEXT}
        </p>
      )}

      <ActionError error={error} busy={busy} onReload={actions.reload} />
      {settingsWanted && openSettings !== null && (
        <div className="actions">
          <button
            type="button"
            className="button"
            data-testid="search-open-settings"
            onClick={() => {
              openSettings();
            }}
          >
            Відкрити Налаштування
          </button>
        </div>
      )}
    </>
  );
}

// --- the profile ------------------------------------------------------------------------------------

function CategoryChips({
  selected,
  error,
  onChange,
}: {
  readonly selected: readonly CategoryId[];
  readonly error: string | undefined;
  readonly onChange: (categories: CategoryId[]) => void;
}) {
  const titleId = useId();
  const errorId = useId();
  return (
    <div
      role="group"
      className="setting"
      aria-labelledby={titleId}
      aria-describedby={describedBy(error === undefined ? null : errorId)}
    >
      <p id={titleId} className="search-title">
        Категорії
      </p>
      <div className="search-chips">
        <button
          type="button"
          role="checkbox"
          aria-checked={allState(selected)}
          className="search-chip"
          data-testid="categories-all"
          onClick={() => {
            onChange(toggleAll(selected));
          }}
        >
          <span className="search-chip-label">Усі категорії</span>
        </button>
        {CATEGORIES.map((category) => (
          <CategoryChip
            key={category.id}
            category={category}
            checked={selected.includes(category.id)}
            onToggle={() => {
              onChange(toggleCategory(selected, category.id));
            }}
          />
        ))}
      </div>
      {error !== undefined && (
        <p id={errorId} className="field-error" data-testid="category-error">
          <span aria-hidden="true">✕</span> {error}
        </p>
      )}
    </div>
  );
}

function CategoryChip({
  category,
  checked,
  onToggle,
}: {
  readonly category: Category;
  readonly checked: boolean;
  readonly onToggle: () => void;
}) {
  const labelId = useId();
  const hintId = useId();
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={checked}
      aria-labelledby={labelId}
      aria-describedby={hintId}
      className="search-chip"
      data-testid={`category-${category.id}`}
      onClick={onToggle}
    >
      <span id={labelId} className="search-chip-label">
        {category.label}
      </span>
      <span id={hintId} className="search-hint">
        {category.hint}
      </span>
    </button>
  );
}

function InstructionField({
  value,
  error,
  onChange,
}: {
  readonly value: string;
  readonly error: string | undefined;
  readonly onChange: (text: string) => void;
}) {
  const inputId = useId();
  const helpId = useId();
  const counterId = useId();
  const errorId = useId();
  return (
    <div className="setting">
      <label htmlFor={inputId}>Додаткові побажання</label>
      <textarea
        id={inputId}
        className="field"
        rows={3}
        data-testid="custom-instruction"
        value={value}
        aria-invalid={error !== undefined}
        aria-describedby={describedBy(helpId, counterId, error === undefined ? null : errorId)}
        onChange={(event) => {
          onChange(event.target.value);
        }}
      />
      <p id={helpId} className="note">
        Наприклад: «Невдалі погоні зі смішною реакцією»
      </p>
      <p id={counterId} className="note" data-testid="custom-instruction-counter">
        {instructionCounter(value)}
      </p>
      {error !== undefined && (
        <p id={errorId} className="field-error">
          <span aria-hidden="true">✕</span> {error}
        </p>
      )}
    </div>
  );
}

function NumberField({
  label,
  testId,
  value,
  min,
  max,
  error,
  onChange,
}: {
  readonly label: string;
  readonly testId: string;
  readonly value: number;
  readonly min: number;
  readonly max: number;
  readonly error: string | undefined;
  readonly onChange: (value: number) => void;
}) {
  const inputId = useId();
  const errorId = useId();
  return (
    <div className="setting">
      <label htmlFor={inputId}>{label}</label>
      <input
        id={inputId}
        type="number"
        inputMode="numeric"
        step={1}
        min={min}
        max={max}
        className="field"
        data-testid={testId}
        value={numberText(value)}
        aria-invalid={error !== undefined}
        aria-describedby={describedBy(error === undefined ? null : errorId)}
        onChange={(event) => {
          onChange(parseNumber(event.target.value));
        }}
      />
      {error !== undefined && (
        <p id={errorId} className="field-error">
          <span aria-hidden="true">✕</span> {error}
        </p>
      )}
    </div>
  );
}

function PartialFlag({
  checked,
  onChange,
}: {
  readonly checked: boolean;
  readonly onChange: (checked: boolean) => void;
}) {
  return (
    <label className="search-check">
      <input
        type="checkbox"
        data-testid="search-allow-partial"
        checked={checked}
        onChange={(event) => {
          onChange(event.target.checked);
        }}
      />
      Шукати у готовій частині
    </label>
  );
}

// --- the start --------------------------------------------------------------------------------------

function StartBlock({
  durationMs,
  categories,
  estimate,
  busy,
  findReason,
  startReason,
  onEstimate,
  onCreate,
}: {
  readonly durationMs: number;
  readonly categories: readonly string[];
  readonly estimate: AnalysisEstimateDto | null;
  readonly busy: boolean;
  readonly findReason: string | null;
  readonly startReason: string | null;
  readonly onEstimate: () => void;
  readonly onCreate: () => void;
}) {
  const findId = useId();
  const startId = useId();
  // The reason of the search is read only once the read is done: while it is on the way, the
  // transcript may still be there.
  const shownFindReason = busy ? null : findReason;

  return (
    <div className="search-start">
      <div className="actions">
        <button
          type="button"
          className="button primary"
          data-testid="find-highlights"
          disabled={busy || findReason !== null}
          aria-describedby={describedBy(shownFindReason === null ? null : findId)}
          onClick={onEstimate}
        >
          Знайти моменти
        </button>
      </div>
      {shownFindReason !== null && (
        <p id={findId} className="note" data-testid="find-reason">
          <span aria-hidden="true">⚠ </span>
          {shownFindReason}
        </p>
      )}
      {estimate !== null && (
        <>
          <dl className="facts" data-testid="search-summary">
            {summaryRows(estimate, categories, durationMs).map((row) => (
              <div key={row.id}>
                <dt>{row.label}</dt>
                <dd data-testid={`search-${row.id}`}>{row.value}</dd>
              </div>
            ))}
          </dl>
          <p className="note">
            <span aria-hidden="true">ⓘ </span>
            {UPPER_BOUND_NOTE}
          </p>
          {startReason !== null && (
            <p id={startId} className="note" data-testid="search-blocked">
              <span aria-hidden="true">⚠ </span>
              {startReason}
            </p>
          )}
          <div className="actions">
            <button
              type="button"
              className="button primary"
              data-testid="search-start"
              disabled={busy || startReason !== null}
              aria-describedby={describedBy(startReason === null ? null : startId)}
              onClick={onCreate}
            >
              Запустити пошук (платно)
            </button>
          </div>
        </>
      )}
    </div>
  );
}

// --- the search on screen ---------------------------------------------------------------------------

function RunningBlock({
  progress,
  cancelling,
  disabled,
  onCancel,
}: {
  readonly progress: string;
  readonly cancelling: boolean;
  readonly disabled: boolean;
  readonly onCancel: () => void;
}) {
  return (
    <>
      <p role="status" data-testid="search-progress">
        <StatusText tone="neutral">{cancelling ? CANCELLING_TEXT : progress}</StatusText>
      </p>
      <div className="actions">
        <button
          type="button"
          className="button"
          data-testid="cancel-job"
          disabled={disabled || cancelling}
          onClick={onCancel}
        >
          Скасувати
        </button>
      </div>
    </>
  );
}

function CompletedBlock({
  text,
  count,
  busy,
  onRestart,
}: {
  readonly text: string;
  readonly count: number;
  readonly busy: boolean;
  readonly onRestart: () => void;
}) {
  return (
    <>
      <p>
        <StatusText tone={count > 0 ? 'success' : 'neutral'} testId={count > 0 ? 'candidates-ready' : 'candidates-none'}>
          {text}
        </StatusText>
      </p>
      <div className="actions">
        <button
          type="button"
          className="button"
          data-testid="search-restart"
          disabled={busy}
          onClick={() => {
            onRestart();
          }}
        >
          Новий пошук
        </button>
      </div>
    </>
  );
}

function StoppedBlock({
  view,
  canAct,
  busy,
  onResume,
  onResumeWithBudget,
  onRestart,
}: {
  readonly view: StoppedView;
  readonly canAct: boolean;
  readonly busy: boolean;
  readonly onResume: (retryUnknown: boolean) => void;
  readonly onResumeWithBudget: (budgetUsd: string, retryUnknown?: boolean) => void;
  readonly onRestart: () => void;
}) {
  const budgetId = useId();
  const budgetHelpId = useId();
  const [budget, setBudget] = useState('');
  const budgetValue = budget.trim();
  const off = busy || !canAct;
  // A run that stopped on its ceiling goes on only with a new one, which the user types here.
  const needsBudget = view.byBudget && budgetValue === '';
  const unknownLabel = `Надіслати ${String(view.unknownCount)} ${pluralUk(view.unknownCount, 'частину', 'частини', 'частин')} ще раз (платно)`;

  return (
    <>
      <p>
        <StatusText tone="warning" testId="search-stopped">
          {view.reason}
        </StatusText>
      </p>
      {view.unknownText !== null && (
        <div className="notice notice-warning" data-testid="search-unknown">
          <p>
            <StatusText tone="warning">{view.unknownText}</StatusText>
          </p>
          <p className="note">
            Це стосується {partsText(view.unknownCount)}. Автоматичного повтору немає: рішення за вами.
          </p>
          <p className="note">{RESEND_QUESTION}</p>
        </div>
      )}
      {view.byBudget && (
        <div className="setting">
          <label htmlFor={budgetId}>Новий ліміт витрат на пошук, USD</label>
          <input
            id={budgetId}
            type="text"
            inputMode="decimal"
            autoComplete="off"
            className="field"
            data-testid="search-budget"
            value={budget}
            aria-describedby={budgetHelpId}
            onChange={(event) => {
              setBudget(event.target.value);
            }}
          />
          <p id={budgetHelpId} className="note">
            Від 0.01 до 1000, наприклад 3.00. Пошук продовжиться лише з цим лімітом.
          </p>
        </div>
      )}
      <div className="actions">
        {view.unknownCount > 0 ? (
          // The parts of unknown outcome are sent again only by this confirmed button (ADR 041 §23(5)).
          <button
            type="button"
            className="button primary"
            data-testid="resume-job"
            disabled={off || needsBudget}
            onClick={() => {
              if (view.byBudget) {
                onResumeWithBudget(budgetValue, true);
              } else {
                onResume(true);
              }
            }}
          >
            {unknownLabel}
          </button>
        ) : view.byBudget ? (
          <button
            type="button"
            className="button primary"
            data-testid="resume-job"
            disabled={off || needsBudget}
            onClick={() => {
              onResumeWithBudget(budgetValue);
            }}
          >
            Продовжити з новим лімітом (платно)
          </button>
        ) : (
          <button
            type="button"
            className="button primary"
            data-testid="resume-job"
            disabled={off}
            onClick={() => {
              onResume(false);
            }}
          >
            Продовжити пошук (платно)
          </button>
        )}
        <button
          type="button"
          className="button"
          data-testid="search-restart"
          disabled={busy}
          onClick={() => {
            onRestart();
          }}
        >
          Новий пошук
        </button>
      </div>
    </>
  );
}

function ActionError({
  error,
  busy,
  onReload,
}: {
  readonly error: ApiErrorInfo | null;
  readonly busy: boolean;
  readonly onReload: () => void;
}) {
  if (error === null) {
    return null;
  }
  const message = error.code === 'TRANSCRIPT_REVISION_STALE' ? STALE_TRANSCRIPT_TEXT : error.message;
  return (
    <>
      <p role="alert" data-testid="search-error" className="field-error">
        <span aria-hidden="true">✕</span> {message}
      </p>
      {error.retryable && (
        <div className="actions">
          <button
            type="button"
            className="button"
            data-testid="search-reload"
            disabled={busy}
            onClick={() => {
              onReload();
            }}
          >
            Спробувати знову
          </button>
        </div>
      )}
    </>
  );
}
