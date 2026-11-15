import { useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';

import type { ClipDto, SourceDto } from '../shared/api';
import { callApi, chooseExportFolder } from './bridge-calls';
import { audioChoices, hasSound, profileFor } from './export-logic';
import type { AudioChoices, ExportRun, ExportSelection } from './export-logic';
import { StatusText } from './ui';
import { useClearOfStickyBar } from './use-bar-room';

/** What the user is shown after the folder is chosen: frozen when the summary opens. */
interface Summary {
  /** One-time folder token; null once a request used it up (or the backend refused it). */
  readonly token: string | null;
  readonly folderName: string;
  readonly clipIds: readonly string[];
  readonly choices: AudioChoices;
  readonly audio: string;
  readonly picking: boolean;
  readonly submitting: boolean;
  readonly error: string | null;
}

export interface ExportBarProps {
  readonly projectId: string;
  readonly sources: readonly SourceDto[];
  readonly selection: ExportSelection;
  /** The batch was created: the project view moves to the Експорт tab. */
  readonly onCreated: (run: ExportRun) => void;
  /** «Перейти до перевірки»: show the first skipped clip. */
  readonly onReviewSkipped: () => void;
}

interface FolderPick {
  readonly token: string;
  readonly displayName: string;
}

/**
 * The bottom bar of the Моменти tab (docs/03 section 9): the counts, the skipped clips and
 * «Експортувати M». The button opens the native folder picker (a cancel starts nothing), then
 * a summary with the format, audio and quality; only «Почати експорт» creates the batch.
 */
export function ExportBar({
  projectId,
  sources,
  selection,
  onCreated,
  onReviewSkipped,
}: ExportBarProps) {
  const [choosing, setChoosing] = useState(false);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [barError, setBarError] = useState<string | null>(null);
  const bar = useRef<HTMLElement>(null);
  // The bar floats over the bottom of the workspace only while it is compact. The summary is as
  // tall as a dialog: it takes its place after the workspace instead of covering it.
  const docked = summary === null;
  useClearOfStickyBar(bar, docked);

  const approved = selection.approved.length;
  const skipped = selection.skipped;

  const patch = (changes: Partial<Summary>): void => {
    setSummary((current) => (current === null ? current : { ...current, ...changes }));
  };

  /** The native folder picker; null on cancel or when the bridge failed (`onError` hears of it). */
  const pickFolder = async (onError: (message: string) => void): Promise<FolderPick | null> => {
    const picked = await chooseExportFolder();
    if ('canceled' in picked) {
      return null;
    }
    if ('ok' in picked) {
      onError(picked.error.message);
      return null;
    }
    return picked;
  };

  const start = (): void => {
    if (choosing || summary !== null || approved === 0) {
      return;
    }
    // What will be exported is what was approved and ticked right now, in the order of the sources.
    const position = (clip: ClipDto): number => sources.findIndex((source) => source.id === clip.source_id);
    const clips = [...selection.approved].sort((a, b) => position(a) - position(b));
    const involved = sources.filter((source) => clips.some((clip) => clip.source_id === source.id));
    setBarError(null);
    setChoosing(true);
    void pickFolder(setBarError).then((choice) => {
      setChoosing(false);
      if (choice === null) {
        return;
      }
      const choices = audioChoices(involved);
      setSummary({
        token: choice.token,
        folderName: choice.displayName,
        clipIds: clips.map((clip) => clip.id),
        choices,
        audio: choices.defaultValue,
        picking: false,
        submitting: false,
        error: null,
      });
    });
  };

  const rechoose = (): void => {
    if (summary === null || summary.picking || summary.submitting) {
      return;
    }
    patch({ picking: true, error: null });
    void pickFolder((message) => {
      patch({ error: message });
    }).then((choice) => {
      patch(
        choice === null
          ? { picking: false }
          : { picking: false, token: choice.token, folderName: choice.displayName, error: null },
      );
    });
  };

  const confirm = (): void => {
    if (summary === null) {
      return;
    }
    const { token, clipIds, audio, submitting } = summary;
    if (token === null || submitting) {
      return;
    }
    patch({ submitting: true, error: null });
    void callApi('exports.create', {
      project_id: projectId,
      clip_ids: clipIds,
      folderToken: token,
      profile: profileFor(audio),
    }).then((result) => {
      if (result.ok) {
        setSummary(null);
        onCreated({ id: result.data.id, jobId: result.data.job_id });
      } else {
        // The folder token is single-use even when the backend refuses the batch.
        patch({ submitting: false, token: null, error: result.error.message });
      }
    });
  };

  // Every reason the button is off: nothing to export, a folder being chosen, a summary waiting.
  let hint: string | null = null;
  if (choosing) {
    hint = 'Обираємо теку для експорту…';
  } else if (summary !== null) {
    hint = 'Параметри експорту відкрито нижче: підтвердьте їх або скасуйте';
  } else if (selection.selected === 0) {
    hint = 'Виберіть фрагменти для експорту';
  } else if (approved === 0) {
    hint = 'Жоден із вибраних фрагментів ще не підтверджено. Підтвердьте їх, щоб експортувати';
  }

  return (
    <section
      ref={bar}
      className="export-bar"
      aria-label="Експорт вибраних фрагментів"
      data-testid="export-bar"
      data-docked={docked}
    >
      <div className="export-bar-row">
        <p className="export-counts" data-testid="export-counts">
          Вибрано {selection.selected} · підтверджено {approved}
        </p>
        <button
          type="button"
          className="button primary"
          data-testid="export-selected"
          disabled={approved === 0 || choosing || summary !== null}
          aria-describedby={hint === null ? undefined : 'export-hint'}
          onClick={start}
        >
          Експортувати {approved}
        </button>
      </div>

      {hint !== null && (
        <p id="export-hint" className="note" data-testid="export-hint">
          <span aria-hidden="true">ⓘ </span>
          {hint}
        </p>
      )}

      {skipped.length > 0 && (
        <div className="export-skipped" data-testid="export-skipped">
          <StatusText tone="warning">
            Пропущено {skipped.length}: не підтверджені, тож не потраплять в експорт
          </StatusText>
          <ul className="plain-list export-skipped-list" aria-label="Пропущені фрагменти">
            {skipped.map((clip) => (
              <li key={clip.id} data-testid="export-skipped-item" data-clip-id={clip.id}>
                {clip.title ?? 'Фрагмент'}
              </li>
            ))}
          </ul>
          <button type="button" className="button" data-testid="review-skipped" onClick={onReviewSkipped}>
            Перейти до перевірки
          </button>
        </div>
      )}

      {barError !== null && (
        <p role="alert" data-testid="export-error" className="field-error">
          <span aria-hidden="true">✕</span> {barError}
        </p>
      )}

      {summary !== null && (
        <ExportSummary
          summary={summary}
          onAudio={(audio) => {
            patch({ audio });
          }}
          onConfirm={confirm}
          onRechoose={rechoose}
          onCancel={() => {
            setSummary(null);
          }}
        />
      )}
    </section>
  );
}

interface ExportSummaryProps {
  readonly summary: Summary;
  readonly onAudio: (value: string) => void;
  readonly onConfirm: () => void;
  readonly onRechoose: () => void;
  readonly onCancel: () => void;
}

function ExportSummary({ summary, onAudio, onConfirm, onRechoose, onCancel }: ExportSummaryProps) {
  const busy = summary.submitting || summary.picking;
  const handleKeyDown = (event: KeyboardEvent<HTMLElement>): void => {
    if (event.key === 'Escape' && !summary.submitting) {
      onCancel();
    }
  };

  return (
    <section
      className="panel export-summary"
      role="dialog"
      aria-labelledby="export-summary-title"
      data-testid="export-summary"
      onKeyDown={handleKeyDown}
    >
      <h3 id="export-summary-title">Перевірте параметри експорту</h3>
      <dl className="facts">
        <div>
          <dt>Тека</dt>
          <dd data-testid="export-folder">{summary.folderName}</dd>
        </div>
        <div>
          <dt>Фрагментів</dt>
          <dd data-testid="export-clip-count">{summary.clipIds.length}</dd>
        </div>
        <div>
          <dt>Формат</dt>
          <dd data-testid="export-format">
            {hasSound(summary.audio) ? 'MP4 · H.264 · AAC' : 'MP4 · H.264 · без звуку'}
          </dd>
        </div>
        <div>
          <dt>
            <label htmlFor="export-audio">Звук у готових кліпах</label>
          </dt>
          <dd>
            <select
              id="export-audio"
              className="field export-audio"
              data-testid="export-audio"
              value={summary.audio}
              disabled={busy}
              autoFocus
              onChange={(event) => {
                onAudio(event.target.value);
              }}
            >
              {summary.choices.options.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </dd>
        </div>
        <div>
          <dt>Субтитри SRT</dt>
          <dd>
            <label className="inline-field">
              <input
                type="checkbox"
                data-testid="export-srt"
                defaultChecked={false}
                disabled
                aria-describedby="export-srt-note"
              />
              Окремий файл .srt до кожного кліпу
            </label>
            <p id="export-srt-note" className="note">
              Недоступно: субтитри з'являться разом із транскрипцією.
            </p>
          </dd>
        </div>
      </dl>

      <details className="export-quality" data-testid="export-quality-details">
        <summary>Якість: Висока</summary>
        <label htmlFor="export-quality">Якість</label>
        <select
          id="export-quality"
          className="field export-quality-select"
          data-testid="export-quality"
          defaultValue="high"
          disabled={busy}
        >
          <option value="high">Висока</option>
        </select>
        <p className="note">
          H.264, CRF 18, preset medium. Це якісне перекодування оригіналу, не lossless.
        </p>
      </details>

      {summary.error !== null && (
        <p role="alert" data-testid="export-summary-error" className="field-error">
          <span aria-hidden="true">✕</span> {summary.error}
          {summary.token === null && ' Оберіть теку ще раз.'}
        </p>
      )}

      {busy && (
        <p id="export-busy" role="status" className="note" data-testid="export-busy">
          <span aria-hidden="true">ⓘ </span>
          {summary.submitting ? 'Запускаємо експорт…' : 'Обираємо теку для експорту…'}
        </p>
      )}

      <div className="actions">
        {summary.token === null ? (
          <button
            type="button"
            className="button primary"
            data-testid="export-rechoose"
            disabled={busy}
            aria-describedby={busy ? 'export-busy' : undefined}
            onClick={onRechoose}
          >
            Обрати теку ще раз
          </button>
        ) : (
          <button
            type="button"
            className="button primary"
            data-testid="confirm-export"
            disabled={busy}
            aria-describedby={busy ? 'export-busy' : undefined}
            onClick={onConfirm}
          >
            {summary.submitting ? 'Запускаємо…' : 'Почати експорт'}
          </button>
        )}
        <button
          type="button"
          className="button"
          data-testid="export-summary-cancel"
          disabled={summary.submitting}
          onClick={onCancel}
        >
          Скасувати
        </button>
      </div>
    </section>
  );
}
