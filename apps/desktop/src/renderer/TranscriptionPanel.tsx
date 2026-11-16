import { useId, useRef, useState } from 'react';

import type { SourceDto } from '../shared/api';
import type { ApiErrorInfo } from '../shared/bridge';
import { useOpenSettings } from './settings-nav';
import { describeTrack } from './source-format';
import {
  UNKNOWN_OUTCOME_TEXT,
  actionMessage,
  defaultAudioStream,
  formatUsd,
  jobNeedsSettings,
  jobReason,
  needsSettings,
  partsText,
  progressText,
  resendQuestion,
  resolveAudioStream,
  retryCount,
  startBlockReason,
  stoppedByBudget,
  stoppedText,
  summaryRows,
  unavailableText,
} from './transcription-logic';
import type { TranscriptionView } from './transcription-logic';
import { StatusText } from './ui';
import type { Tone } from './ui';
import { useTranscription } from './use-transcription';

type RunView = Extract<TranscriptionView, { readonly kind: 'run' }>;
type StartView = Extract<TranscriptionView, { readonly kind: 'start' }>;

/**
 * The state of the file: why it cannot be recognised (null when it can), and the id of that reason.
 * The paid buttons point at the reason by `aria-describedby` (ADR 038, доповнення 3).
 */
interface FileState {
  readonly text: string | null;
  readonly id: string;
}

/** The ids a control is described by: only those of the reasons shown; undefined when there are none. */
function describedBy(...ids: readonly (string | null)[]): string | undefined {
  const shown = ids.filter((id): id is string => id !== null);
  return shown.length === 0 ? undefined : shown.join(' ');
}

/** The «Розпізнавання мовлення» block of the source inspector (docs/03 sections 5 and 7, ADR 038). */
export function TranscriptionPanel({
  source,
  availability,
}: {
  readonly source: SourceDto;
  /** The state of the file («available», «missing», «changed»): a paid action waits for an available file. */
  readonly availability: string;
}) {
  const headingId = useId();
  const headingRef = useRef<HTMLHeadingElement>(null);
  const fallback = defaultAudioStream(source.audio_streams);
  // Where the focus goes when the control it was on closes or is disabled; otherwise it falls to the body.
  const focusHeading = (): void => {
    headingRef.current?.focus();
  };

  return (
    <section className="transcription" data-testid="transcription" aria-labelledby={headingId}>
      <h4 id={headingId} ref={headingRef} tabIndex={-1} className="settings-subtitle">
        Розпізнавання мовлення
      </h4>
      {fallback === null ? (
        <p className="note" data-testid="transcription-no-audio">
          <span aria-hidden="true">ⓘ </span>
          Розпізнавання мовлення недоступне: у відео немає звукової доріжки.
        </p>
      ) : (
        <TranscriptionBody
          source={source}
          availability={availability}
          fallback={fallback}
          onFocusHeading={focusHeading}
        />
      )}
    </section>
  );
}

function TranscriptionBody({
  source,
  availability,
  fallback,
  onFocusHeading,
}: {
  readonly source: SourceDto;
  readonly availability: string;
  readonly fallback: number;
  readonly onFocusHeading: () => void;
}) {
  const streams = source.audio_streams;
  const trackId = useId();
  const fileId = useId();
  const [picked, setPicked] = useState<number | null>(null);
  const audioIndex = resolveAudioStream(streams, picked, fallback);
  const fileText = unavailableText(availability);
  const file: FileState = { text: fileText, id: fileId };
  const transcription = useTranscription(source.id, audioIndex, fileText === null);
  const { view, busy, error } = transcription;
  const openSettings = useOpenSettings();

  return (
    <>
      {streams.length > 1 && view.kind !== 'run' && (
        <div className="setting">
          <label htmlFor={trackId}>Доріжка для розпізнавання</label>
          <select
            id={trackId}
            data-testid="transcription-track"
            className="field"
            value={String(audioIndex)}
            disabled={busy}
            onChange={(event) => {
              setPicked(Number(event.target.value));
            }}
          >
            {streams.map((stream) => (
              <option key={stream.index} value={String(stream.index)}>
                {describeTrack(stream)}
              </option>
            ))}
          </select>
        </div>
      )}

      {view.kind === 'loading' && (
        <p role="status" data-testid="transcription-loading" className="note">
          <StatusText tone="neutral">Дізнаємося стан розпізнавання…</StatusText>
        </p>
      )}

      {view.kind === 'failed' && (
        <>
          <p role="status" data-testid="transcription-load-error">
            <StatusText tone="warning">
              Не вдалося дізнатися стан розпізнавання. {view.error.message}
            </StatusText>
          </p>
          {/* A refused start or resume stays on screen while the state is read again. */}
          <ActionError error={error} />
          <div className="actions">
            <button
              type="button"
              className="button"
              data-testid="transcription-reload"
              onClick={transcription.reload}
            >
              Спробувати знову
            </button>
          </div>
        </>
      )}

      {view.kind === 'start' && (
        <StartBlock
          view={view}
          busy={busy}
          error={error}
          file={file}
          openSettings={openSettings}
          onStart={transcription.start}
        />
      )}

      {view.kind === 'run' && (
        <RunBlock
          key={view.run.id}
          view={view}
          busy={busy}
          error={error}
          file={file}
          openSettings={openSettings}
          onFocusHeading={onFocusHeading}
          onCancel={transcription.cancel}
          onResume={transcription.resume}
          onResumeWithBudget={transcription.resumeWithBudget}
          onRestart={transcription.restart}
        />
      )}
    </>
  );
}

function ActionError({ error }: { readonly error: ApiErrorInfo | null }) {
  if (error === null) {
    return null;
  }
  return (
    <p role="alert" data-testid="transcription-error" className="field-error">
      <span aria-hidden="true">✕</span> {actionMessage(error)}
    </p>
  );
}

function SettingsButton({
  primary,
  onOpen,
}: {
  readonly primary: boolean;
  readonly onOpen: () => void;
}) {
  return (
    <button
      type="button"
      className={primary ? 'button primary' : 'button'}
      data-testid="transcription-open-settings"
      onClick={onOpen}
    >
      Відкрити Налаштування
    </button>
  );
}

/** The summary of what a run would cost, shown before anything is sent, and the way to start it. */
function StartBlock({
  view,
  busy,
  error,
  file,
  openSettings,
  onStart,
}: {
  readonly view: StartView;
  readonly busy: boolean;
  readonly error: ApiErrorInfo | null;
  readonly file: FileState;
  readonly openSettings: (() => void) | null;
  readonly onStart: (force: boolean) => void;
}) {
  const reasonId = useId();
  const { estimate, hasKey } = view;
  const blocked = startBlockReason(estimate);
  const noKey = hasKey === false;
  const existing = estimate.existing !== null;
  const wantsSettings = noKey || blocked !== null || (error !== null && needsSettings(error.code));
  const fileDescribed = file.text === null ? null : file.id;

  return (
    <>
      {existing && (
        <p>
          <StatusText tone="success" testId="transcription-existing">
            Транскрипт уже є
          </StatusText>
        </p>
      )}
      <dl className="facts" data-testid="transcription-summary">
        {summaryRows(estimate).map((row) => (
          <div key={row.id}>
            <dt>{row.label}</dt>
            <dd data-testid={`transcription-${row.id}`}>{row.value}</dd>
          </div>
        ))}
      </dl>
      <p className="note">
        <span aria-hidden="true">ⓘ </span>
        Оцінка за верхньою ціною: одна спроба на кожну частину. Повтори й поділ частин можуть коштувати більше.
      </p>
      {view.restarted && (
        <p className="note" data-testid="transcription-restart-note">
          <span aria-hidden="true">ⓘ </span>
          Новий запуск надішле все аудіо знову: уже розпізнані частини цього запуску не використовуються.
        </p>
      )}
      {file.text !== null && (
        <p id={file.id} className="note" data-testid="transcription-unavailable">
          <span aria-hidden="true">⚠ </span>
          {file.text}
        </p>
      )}
      {noKey && (
        <p className="note" data-testid="transcription-no-key">
          <span aria-hidden="true">⚠ </span>
          Спершу задайте ключ API OpenRouter у Налаштуваннях.
        </p>
      )}
      {blocked !== null && (
        <p id={reasonId} className="note" data-testid="transcription-blocked">
          <span aria-hidden="true">⚠ </span>
          {blocked}
        </p>
      )}
      <ActionError error={error} />
      <div className="actions">
        {!noKey && (
          <button
            type="button"
            className={existing ? 'button' : 'button primary'}
            data-testid={existing ? 'transcription-restart' : 'transcription-start'}
            disabled={busy || blocked !== null || file.text !== null}
            aria-describedby={describedBy(fileDescribed, blocked === null ? null : reasonId)}
            onClick={() => {
              onStart(existing);
            }}
          >
            {existing ? 'Розпізнати ще раз (платно)' : 'Розпізнати мовлення'}
          </button>
        )}
        {wantsSettings && openSettings !== null && <SettingsButton primary={noKey} onOpen={openSettings} />}
      </div>
    </>
  );
}

const STOPPED_TONES: Readonly<Record<string, Tone>> = {
  partial: 'warning',
  failed: 'danger',
};

/**
 * A run that is going, or one that stopped and waits for the user's decision. Keyed by the run, so the
 * confirmation of a resend never outlives the run it was asked for.
 */
function RunBlock({
  view,
  busy,
  error,
  file,
  openSettings,
  onFocusHeading,
  onCancel,
  onResume,
  onResumeWithBudget,
  onRestart,
}: {
  readonly view: RunView;
  readonly busy: boolean;
  readonly error: ApiErrorInfo | null;
  readonly file: FileState;
  readonly openSettings: (() => void) | null;
  readonly onFocusHeading: () => void;
  readonly onCancel: () => void;
  readonly onResume: (retryUnknown: boolean) => void;
  readonly onResumeWithBudget: (budgetUsd: string, retryUnknown: boolean) => void;
  readonly onRestart: () => void;
}) {
  const { stage, run, job, counts, cancelling, budgetUsd } = view;
  const [confirming, setConfirming] = useState(false);
  const canAct = run.job_id !== null;
  const hasUnknown = counts.unknown > 0;
  const budgetStop = stoppedByBudget(job);
  // A job that stopped on its ceiling goes on only with a ceiling: without one in the settings a resend
  // is not offered, and the settings are the only way out (ADR 038, доповнення 3).
  const resendOffered = !budgetStop || budgetUsd !== null;
  const fileDescribed = file.text === null ? null : file.id;
  const reason = jobReason(job);
  const settingsButton = openSettings === null ? null : <SettingsButton primary={false} onOpen={openSettings} />;
  // The job's reason carries the way to the settings when the job stopped for them; the refusal of an
  // action then does not add a second «Відкрити Налаштування».
  const jobSettings = jobNeedsSettings(job) ? settingsButton : null;
  const errorSettings =
    error !== null && needsSettings(error.code) && jobSettings === null ? settingsButton : null;

  if (stage === 'active') {
    return (
      <>
        <p role="status" data-testid="transcription-progress">
          <StatusText tone="neutral">{cancelling ? 'Скасовуємо розпізнавання…' : progressText(counts)}</StatusText>
        </p>
        <ActionError error={error} />
        <div className="actions">
          <button
            type="button"
            className="button"
            data-testid="transcription-cancel"
            disabled={cancelling || !canAct}
            onClick={() => {
              // The button turns disabled with this click: the focus moves to the heading before that.
              onFocusHeading();
              onCancel();
            }}
          >
            Скасувати
          </button>
        </div>
      </>
    );
  }

  return (
    <>
      <p>
        <StatusText tone={STOPPED_TONES[run.status] ?? 'neutral'} testId="transcription-stopped">
          {stoppedText(run.status, retryCount(counts))}
        </StatusText>
      </p>
      {reason !== null && (
        <p className="note" data-testid="transcription-reason">
          {reason}
        </p>
      )}
      {jobSettings !== null && <div className="actions">{jobSettings}</div>}
      <p className="note" data-testid="transcription-counts">
        Уже розпізнано {String(counts.succeeded)} із {String(counts.total)} частин. Розпізнане збережено.
      </p>

      {hasUnknown && (
        <div className="notice notice-warning" data-testid="transcription-unknown">
          <p>
            <StatusText tone="warning">{UNKNOWN_OUTCOME_TEXT}</StatusText>
          </p>
          <p className="note">
            Це стосується {partsText(counts.unknown)}. Автоматичного повтору немає: рішення за вами.
          </p>
          {resendOffered &&
            (confirming ? (
              <>
                <p data-testid="transcription-confirm-text">
                  {resendQuestion(budgetStop ? budgetUsd : null)}
                </p>
                <div className="actions" role="group" aria-label="Підтвердження повторного надсилання">
                  <button
                    type="button"
                    className="button"
                    data-testid="transcription-confirm-unknown"
                    disabled={busy || !canAct || file.text !== null}
                    aria-describedby={describedBy(fileDescribed)}
                    onClick={() => {
                      onFocusHeading();
                      setConfirming(false);
                      if (budgetStop && budgetUsd !== null) {
                        onResumeWithBudget(budgetUsd, true);
                      } else {
                        onResume(true);
                      }
                    }}
                  >
                    Так, надіслати ще раз
                  </button>
                  <button
                    type="button"
                    className="button"
                    data-testid="transcription-keep-unknown"
                    // The safe answer takes the focus the clicked button had.
                    autoFocus
                    onClick={() => {
                      onFocusHeading();
                      setConfirming(false);
                    }}
                  >
                    Не надсилати
                  </button>
                </div>
              </>
            ) : (
              <div className="actions">
                <button
                  type="button"
                  className="button"
                  data-testid="transcription-retry-unknown"
                  disabled={busy || !canAct || file.text !== null}
                  aria-describedby={describedBy(fileDescribed)}
                  onClick={() => {
                    setConfirming(true);
                  }}
                >
                  Надіслати ще раз
                </button>
              </div>
            ))}
        </div>
      )}

      {file.text !== null && (
        <p id={file.id} className="note" data-testid="transcription-unavailable">
          <span aria-hidden="true">⚠ </span>
          {file.text}
        </p>
      )}
      <ActionError error={error} />
      <div className="actions">
        {/* With parts of unknown outcome the backend refuses a plain resume: only the confirmed one is offered. */}
        {/* A job that stopped on its ceiling would stop on it again: only a higher ceiling goes on (ADR 038, доповнення 2). */}
        {!hasUnknown && !budgetStop && (
          <button
            type="button"
            className="button primary"
            data-testid={stage === 'retry' ? 'transcription-retry-failed' : 'transcription-continue'}
            disabled={busy || !canAct || file.text !== null}
            aria-describedby={describedBy(fileDescribed)}
            onClick={() => {
              onResume(false);
            }}
          >
            {stage === 'retry' ? 'Повторити невдалі' : 'Продовжити'}
          </button>
        )}
        {!hasUnknown && budgetStop && budgetUsd !== null && (
          <button
            type="button"
            className="button primary"
            data-testid="transcription-continue-budget"
            disabled={busy || !canAct || file.text !== null}
            aria-describedby={describedBy(fileDescribed)}
            onClick={() => {
              onResumeWithBudget(budgetUsd, false);
            }}
          >
            {`Продовжити з лімітом ${formatUsd(budgetUsd)}`}
          </button>
        )}
        <button
          type="button"
          className="button"
          data-testid="transcription-start-over"
          disabled={busy || file.text !== null}
          aria-describedby={describedBy(fileDescribed)}
          onClick={() => {
            onRestart();
          }}
        >
          Почати заново (платно)
        </button>
        {errorSettings}
      </div>
    </>
  );
}
