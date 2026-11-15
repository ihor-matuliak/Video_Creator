import { useId } from 'react';
import type { KeyboardEvent, RefObject } from 'react';

import type { ClipDto } from '../shared/api';
import { formatTimecode } from '../shared/timecode';
import { BoundRow } from './BoundRow';
import { TITLE_MAX, planPatch } from './clip-edit-logic';
import type { EditorView } from './clip-edit-logic';
import { HOTKEY_LABELS } from './hotkey-logic';
import { checkInterval } from './interval-logic';
import type { PlayerHandle } from './SourcePlayer';
import { StatusText } from './ui';
import type { Bound, SaveState } from './use-clip-editing';

export interface ClipEditorProps {
  /** The clip as stored. */
  readonly clip: ClipDto;
  /** The texts in the fields: the user's edits laid over the stored values. */
  readonly view: EditorView;
  readonly saveState: SaveState | undefined;
  readonly canUndo: boolean;
  readonly durationMs: number;
  readonly player: RefObject<PlayerHandle | null>;
  /** True when the player has a loaded video to read the position from. */
  readonly playerReady: boolean;
  readonly onTitleChange: (text: string) => void;
  readonly onBoundChange: (bound: Bound, text: string) => void;
  /** A field was left or Enter was pressed. */
  readonly onCommit: () => void;
  readonly onTakeFromPlayer: (bound: Bound) => void;
  readonly onStep: (bound: Bound, deltaMs: number) => void;
  readonly onUndo: () => void;
  readonly onResave: () => void;
  readonly onTakeStored: () => void;
}

/**
 * The fields of the active clip (docs/03 section 8). There is no save button: a change is
 * stored when a field is left, and at once when a boundary is set by a button or a key.
 */
export function ClipEditor({
  clip,
  view,
  saveState,
  canUndo,
  durationMs,
  player,
  playerReady,
  onTitleChange,
  onBoundChange,
  onCommit,
  onTakeFromPlayer,
  onStep,
  onUndo,
  onResave,
  onTakeStored,
}: ClipEditorProps) {
  const ids = useId();
  const plan = planPatch(clip, view, durationMs);
  const intervalError = view.touched ? plan.intervalError : null;
  const titleError = view.touched ? plan.titleError : null;
  const intervalErrorId = `${ids}-interval-error`;
  const titleErrorId = `${ids}-title-error`;

  const playFragment = (): void => {
    const interval = checkInterval(view.start, view.end, durationMs);
    if (interval.ok) {
      player.current?.playRange(interval.startMs, interval.endMs);
    }
  };

  // The form has several text fields and no submit button, so Enter would do nothing by itself.
  const handleKeyDown = (event: KeyboardEvent<HTMLFormElement>): void => {
    if (event.key === 'Enter' && event.target instanceof HTMLInputElement) {
      event.preventDefault();
      onCommit();
    }
  };

  const boundRow = (bound: Bound, label: string, testId: string) => (
    <BoundRow
      fieldId={`${ids}-${bound}`}
      label={label}
      testId={testId}
      text={view[bound]}
      durationMs={durationMs}
      playerReady={playerReady}
      shortcut={HOTKEY_LABELS[bound === 'start' ? 'start' : 'end']}
      invalid={intervalError !== null}
      errorId={intervalErrorId}
      onChange={(text) => {
        onBoundChange(bound, text);
      }}
      onBlur={onCommit}
      onTakeFromPlayer={() => {
        onTakeFromPlayer(bound);
      }}
      onStep={(deltaMs) => {
        onStep(bound, deltaMs);
      }}
    />
  );

  return (
    <form
      className="panel manual-form clip-editor"
      aria-label="Редагування фрагмента"
      data-testid="clip-editor"
      data-clip-id={clip.id}
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        onCommit();
      }}
      onKeyDown={handleKeyDown}
    >
      <h3>Редагування фрагмента</h3>
      <label htmlFor={`${ids}-title`}>Назва</label>
      <input
        id={`${ids}-title`}
        data-testid="clip-title"
        className="field"
        type="text"
        value={view.title}
        maxLength={TITLE_MAX}
        autoComplete="off"
        placeholder="Фрагмент"
        aria-invalid={titleError !== null}
        aria-describedby={titleError === null ? undefined : titleErrorId}
        onChange={(event) => {
          onTitleChange(event.target.value);
        }}
        onBlur={onCommit}
      />
      {titleError !== null && (
        <p id={titleErrorId} role="alert" data-testid="clip-title-error" className="field-error">
          <span aria-hidden="true">✕</span> {titleError}
        </p>
      )}
      {boundRow('start', 'Початок', 'clip-start')}
      {boundRow('end', 'Кінець', 'clip-end')}
      <p className="note">Формат часу: ГГ:ХХ:СС.мс. Кінець не входить у фрагмент. Зміни зберігаються самі.</p>
      {intervalError !== null && (
        <p id={intervalErrorId} role="alert" data-testid="interval-error" className="field-error">
          <span aria-hidden="true">✕</span> {intervalError}
        </p>
      )}

      <div role="status" data-testid="clip-save-status" className="save-status">
        {saveState?.kind === 'saving' && <StatusText tone="neutral">Зберігаємо…</StatusText>}
        {saveState?.kind === 'saved' && <StatusText tone="success">Збережено</StatusText>}
      </div>
      {saveState?.kind === 'error' && (
        <div role="alert" data-testid="clip-save-error" className="notice notice-danger">
          <p>
            <span aria-hidden="true">✕</span> Не вдалося зберегти зміни. {saveState.message}
          </p>
          <p>Ваші значення залишаються в полях.</p>
          <button type="button" className="button" data-testid="clip-resave" onClick={onResave}>
            Спробувати ще раз
          </button>
        </div>
      )}
      {saveState?.kind === 'conflict' && (
        <div role="alert" data-testid="clip-conflict" className="notice notice-warning">
          <p>
            <span aria-hidden="true">⚠</span> Фрагмент змінено в іншому місці. Ваші значення залишаються в полях і
            ще не збережені.
          </p>
          <p>
            Зараз збережено: «{clip.title}», {formatTimecode(clip.start_ms)}–{formatTimecode(clip.end_ms)}.
            {!saveState.refreshed && ' Не вдалося оновити дані фрагмента.'}
          </p>
          <div className="actions">
            <button type="button" className="button primary" data-testid="clip-resave" onClick={onResave}>
              Зберегти мої значення
            </button>
            <button type="button" className="button" data-testid="clip-use-stored" onClick={onTakeStored}>
              Взяти збережені
            </button>
          </div>
        </div>
      )}

      <div className="actions">
        <button
          type="button"
          className="button"
          data-testid="preview-clip"
          disabled={!checkInterval(view.start, view.end, durationMs).ok || !playerReady}
          onClick={playFragment}
        >
          Відтворити фрагмент
        </button>
        <button
          type="button"
          className="button"
          data-testid="undo-bounds"
          aria-keyshortcuts={HOTKEY_LABELS.undo.aria}
          title={`Скасувати останню зміну меж (${HOTKEY_LABELS.undo.title})`}
          disabled={!canUndo}
          onClick={onUndo}
        >
          Скасувати зміну меж
        </button>
      </div>
    </form>
  );
}
