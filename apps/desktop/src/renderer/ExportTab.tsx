import { useEffect, useState } from 'react';

import type { ClipDto, ExportBatchDto, ExportItemDto, JobDto } from '../shared/api';
import type { ApiErrorInfo } from '../shared/bridge';
import { formatTimecode } from '../shared/timecode';
import { callApi, openExportFolder } from './bridge-calls';
import {
  batchOutcome,
  countItems,
  fileName,
  isBatchFinished,
  runningProgress,
} from './export-logic';
import type { ClipRegistry, ExportRun } from './export-logic';
import { StatusText } from './ui';
import type { Tone } from './ui';

/** How often an open batch is asked again (while the page is visible). */
export const EXPORT_POLL_MS = 1000;

/** Consecutive failed polls (that may succeed on a retry) before the view reports an error. */
const MAX_POLL_MISSES = 5;

interface ItemLabel {
  readonly tone: Tone;
  readonly text: string;
}

const ITEM_LABELS: Readonly<Record<string, ItemLabel>> = {
  queued: { tone: 'neutral', text: 'У черзі' },
  running: { tone: 'neutral', text: 'Експортується' },
  verified: { tone: 'success', text: 'Перевірено' },
  failed: { tone: 'danger', text: 'Не вдалося' },
  cancelled: { tone: 'warning', text: 'Скасовано' },
};

const UNKNOWN_ITEM: ItemLabel = { tone: 'warning', text: 'Невідомо' };

const STAGE_TEXT = { render: 'Створюємо кліп', verify: 'Перевіряємо файл' } as const;

function itemLabel(state: string): ItemLabel {
  return Object.hasOwn(ITEM_LABELS, state) ? (ITEM_LABELS[state] ?? UNKNOWN_ITEM) : UNKNOWN_ITEM;
}

/** The measured duration of a verified file; before that, the length of the clip as exported. */
function itemDuration(item: ExportItemDto, clip: ClipDto | undefined): number | null {
  if (item.duration_ms !== null) {
    return item.duration_ms;
  }
  return clip?.revision === item.clip_revision ? clip.end_ms - clip.start_ms : null;
}

export interface ExportTabProps {
  readonly run: ExportRun;
  /** For the titles and planned lengths of the clips of the batch. */
  readonly clips: ClipRegistry;
  /** «Повторити невдалі» created a new batch: the view moves to it. */
  readonly onRetried: (run: ExportRun) => void;
}

/**
 * The batch view of docs/03 section 9: per-clip states, «Готово N із M» and the actions. It asks
 * the backend once a second while it is open (and the page is visible), and stops when the
 * batch is finished.
 */
export function ExportTab({ run, clips, onRetried }: ExportTabProps) {
  const [batch, setBatch] = useState<ExportBatchDto | null>(null);
  const [job, setJob] = useState<JobDto | null>(null);
  const [loadError, setLoadError] = useState<ApiErrorInfo | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [cancelRequested, setCancelRequested] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [opening, setOpening] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  useEffect(() => {
    const state = { stopped: false, misses: 0 };
    let timer: number | undefined;

    const schedule = (): void => {
      timer = window.setTimeout(() => {
        void poll();
      }, EXPORT_POLL_MS);
    };

    const poll = async (): Promise<void> => {
      if (document.visibilityState === 'hidden') {
        schedule();
        return;
      }
      const [batchResult, jobResult] = await Promise.all([
        callApi('exports.get', { id: run.id }),
        callApi('jobs.get', { id: run.jobId }),
      ]);
      if (state.stopped) {
        return;
      }
      if (jobResult.ok) {
        setJob(jobResult.data);
      }
      if (!batchResult.ok) {
        if (batchResult.error.retryable && state.misses + 1 < MAX_POLL_MISSES) {
          state.misses += 1;
          schedule();
        } else {
          setLoadError(batchResult.error);
        }
        return;
      }
      state.misses = 0;
      setLoadError(null);
      setBatch(batchResult.data);
      if (!isBatchFinished(batchResult.data.status)) {
        schedule();
      }
    };

    void poll();
    return () => {
      state.stopped = true;
      window.clearTimeout(timer);
    };
  }, [run.id, run.jobId, attempt]);

  const reload = (): void => {
    setLoadError(null);
    setAttempt((value) => value + 1);
  };

  const cancel = (): void => {
    if (cancelRequested) {
      return;
    }
    setCancelRequested(true);
    setActionError(null);
    void callApi('jobs.cancel', { id: run.jobId }).then((result) => {
      if (result.ok) {
        setJob(result.data);
      } else {
        setCancelRequested(false);
        setActionError(result.error.message);
      }
    });
  };

  const retry = (): void => {
    if (retrying) {
      return;
    }
    setRetrying(true);
    setActionError(null);
    void callApi('exports.retry', { batch_id: run.id }).then((result) => {
      if (result.ok) {
        onRetried({ id: result.data.id, jobId: result.data.job_id });
      } else {
        setRetrying(false);
        setActionError(result.error.message);
      }
    });
  };

  const openFolder = (): void => {
    if (opening) {
      return;
    }
    setOpening(true);
    setActionError(null);
    void openExportFolder(run.id).then((result) => {
      setOpening(false);
      if (!result.ok) {
        setActionError(result.error.message);
      }
    });
  };

  if (batch === null) {
    return loadError === null ? (
      <p role="status" data-testid="export-loading" className="note export-view">
        Завантажуємо стан експорту…
      </p>
    ) : (
      <div role="alert" data-testid="export-load-error" className="notice notice-danger export-view">
        <h3>Не вдалося отримати стан експорту</h3>
        <p>{loadError.message}</p>
        <button type="button" className="button" data-testid="export-reload" onClick={reload}>
          Спробувати ще раз
        </button>
      </div>
    );
  }

  const counts = countItems(batch.items);
  const outcome = batchOutcome(batch);
  const cancelling = cancelRequested || job?.state === 'cancelling';
  const progress = runningProgress(job);

  return (
    <div className="export-view" data-testid="export-view" data-batch-status={batch.status}>
      <div className="export-header">
        <h3>Експорт</h3>
        <p role="status" className="export-done" data-testid="export-progress">
          Готово {counts.verified} із {counts.total}
        </p>
      </div>

      {outcome === 'running' && (
        <div className="export-outcome">
          <StatusText tone="neutral" testId="export-running">
            {cancelling ? 'Скасовуємо експорт…' : 'Експортуємо вибрані фрагменти…'}
          </StatusText>
          <button
            type="button"
            className="button"
            data-testid="cancel-job"
            disabled={cancelling}
            onClick={cancel}
          >
            Скасувати експорт
          </button>
        </div>
      )}

      {outcome === 'complete' && (
        <p role="status" className="export-outcome" data-testid="export-complete">
          {counts.verified === counts.total ? (
            <StatusText tone="success">Експорт завершено: усі файли перевірено</StatusText>
          ) : (
            <StatusText tone="warning">
              Експорт завершено частково: {counts.failed} з {counts.total} не вдалося
            </StatusText>
          )}
        </p>
      )}

      {outcome === 'cancelled' && (
        <p role="status" className="export-outcome" data-testid="export-cancelled">
          <StatusText tone="warning">
            Експорт скасовано. Готові файли збережено: {counts.verified} із {counts.total}
          </StatusText>
        </p>
      )}

      {outcome === 'failed' && (
        <p role="alert" className="export-outcome" data-testid="export-failed">
          <StatusText tone="danger">Експорт не вдався: жодного файлу не перевірено</StatusText>
        </p>
      )}

      <ul className="export-items" aria-label="Фрагменти експорту">
        {batch.items.map((item) => {
          const clip = clips.get(item.clip_id);
          const label = itemLabel(item.state);
          const duration = itemDuration(item, clip);
          const file = item.final_path === null ? null : fileName(item.final_path);
          const showProgress = item.state === 'running' && progress.stage !== null;
          return (
            <li
              key={item.id}
              className="export-item"
              data-testid="export-item"
              data-clip-id={item.clip_id}
              data-state={item.state}
            >
              <div className="export-item-main">
                <span className="export-item-title">{clip?.title ?? file ?? 'Фрагмент'}</span>
                {file !== null && (
                  <span className="export-item-file" data-testid="export-item-file">
                    {file}
                  </span>
                )}
                <span className="export-item-duration" data-testid="export-item-duration">
                  {duration === null ? '—' : formatTimecode(duration)}
                </span>
              </div>
              <StatusText tone={label.tone} testId="export-item-state">
                {label.text}
              </StatusText>
              {showProgress && (
                <p className="note export-item-progress" data-testid="export-item-progress">
                  {STAGE_TEXT[progress.stage]}
                  {progress.percent === null ? '' : ` · ${String(progress.percent)}%`}
                </p>
              )}
              {item.error !== null && (
                <p className="field-error export-item-error" data-testid="export-item-error">
                  <span aria-hidden="true">✕</span> {item.error.message}
                </p>
              )}
            </li>
          );
        })}
      </ul>

      {loadError !== null && (
        <p role="alert" data-testid="export-load-error" className="field-error">
          <span aria-hidden="true">✕</span> Не вдалося оновити стан: {loadError.message}{' '}
          <button type="button" className="link" data-testid="export-reload" onClick={reload}>
            Спробувати ще раз
          </button>
        </p>
      )}

      {actionError !== null && (
        <p role="alert" data-testid="export-action-error" className="field-error">
          <span aria-hidden="true">✕</span> {actionError}
        </p>
      )}

      <div className="actions">
        {outcome !== 'running' && counts.failed + counts.cancelled > 0 && (
          <button
            type="button"
            className="button primary"
            data-testid="retry-failed"
            disabled={retrying}
            aria-describedby={retrying ? 'export-retrying' : undefined}
            onClick={retry}
          >
            Повторити невдалі
          </button>
        )}
        {retrying && (
          <p id="export-retrying" role="status" className="note" data-testid="export-retrying">
            <span aria-hidden="true">ⓘ </span>
            Запускаємо повторний експорт…
          </p>
        )}
        {counts.verified > 0 && (
          <button
            type="button"
            className="button"
            data-testid="open-folder"
            disabled={opening}
            onClick={openFolder}
          >
            Відкрити папку
          </button>
        )}
      </div>
    </div>
  );
}
