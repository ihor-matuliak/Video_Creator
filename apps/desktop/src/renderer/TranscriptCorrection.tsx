import { useId, useLayoutEffect, useRef } from 'react';
import type { KeyboardEvent, SyntheticEvent } from 'react';

import {
  SEGMENT_TEXT_MAX_LENGTH,
  WORD_TEXT_MAX_LENGTH,
  draftText,
  saveBlockText,
} from './transcript-logic';
import type { CorrectionTarget, CorrectionUnit, SaveBlock } from './transcript-logic';

export interface CorrectionEditorProps {
  readonly unit: CorrectionUnit;
  readonly targets: readonly CorrectionTarget[];
  readonly drafts: Readonly<Record<string, string>>;
  /** Why Save is blocked; null when it may be pressed. */
  readonly block: SaveBlock | null;
  /** A save is in flight: the editor is disabled and cannot be closed. */
  readonly saving: boolean;
  /** The failure of the last save, shown under the fields; the typed text stays. */
  readonly error: string | null;
  /** The last save was refused because its revision is no longer the newest (409): the editor offers a refresh. */
  readonly staleRevision: boolean;
  readonly onDraftChange: (id: string, text: string) => void;
  readonly onSave: () => void;
  readonly onCancel: () => void;
  /** Closes the editor and reads the newest revision again. */
  readonly onRefresh: () => void;
}

/**
 * The «Виправити» editor of one segment (ADR 036, 039): a field per word in a word revision, one
 * text field otherwise. Escape and «Скасувати» close it; both are off while a save is in flight.
 */
export function CorrectionEditor({
  unit,
  targets,
  drafts,
  block,
  saving,
  error,
  staleRevision,
  onDraftChange,
  onSave,
  onCancel,
  onRefresh,
}: CorrectionEditorProps) {
  const ids = useId();
  const reasonId = `${ids}-reason`;
  const form = useRef<HTMLFormElement>(null);
  const wasSaving = useRef(saving);

  // The save disables the field that had the focus, and the browser drops the focus with it. A save that
  // failed gives the focus back to the first field, so the keys typed next are text, not shortcuts.
  // This is a layout effect: it runs in the same commit as the error, so the focus is back before any
  // key can be dispatched. A passive effect runs later, and a key typed in between would reach the body.
  useLayoutEffect(() => {
    if (wasSaving.current && !saving && error !== null) {
      form.current?.querySelector<HTMLElement>('input, textarea')?.focus();
    }
    wasSaving.current = saving;
  }, [saving, error]);

  const onSubmit = (event: SyntheticEvent<HTMLFormElement>): void => {
    event.preventDefault();
    if (block === null && !saving) {
      onSave();
    }
  };

  const onKeyDown = (event: KeyboardEvent<HTMLFormElement>): void => {
    // Escape that ends an IME composition only ends the composition: the editor stays open.
    if (event.key === 'Escape' && !saving && !event.nativeEvent.isComposing) {
      event.preventDefault();
      onCancel();
    }
  };

  return (
    <form
      ref={form}
      className="transcript-correction"
      data-testid="transcript-correction"
      aria-label="Виправлення фрагмента"
      onSubmit={onSubmit}
      onKeyDown={onKeyDown}
    >
      {unit === 'word' ? (
        <div className="transcript-correction-words">
          {targets.map((target, index) => (
            <label key={target.id} className="transcript-correction-word">
              <span className="note">{`Слово ${String(index + 1)}`}</span>
              <input
                type="text"
                className="field"
                maxLength={WORD_TEXT_MAX_LENGTH}
                value={draftText(target, drafts)}
                disabled={saving}
                autoFocus={index === 0}
                onChange={(event) => {
                  onDraftChange(target.id, event.target.value);
                }}
              />
            </label>
          ))}
        </div>
      ) : (
        targets.map((target) => (
          <label key={target.id} className="transcript-correction-text">
            <span className="note">Текст фрагмента</span>
            <textarea
              className="field"
              rows={4}
              maxLength={SEGMENT_TEXT_MAX_LENGTH}
              value={draftText(target, drafts)}
              disabled={saving}
              autoFocus
              onChange={(event) => {
                onDraftChange(target.id, event.target.value);
              }}
            />
          </label>
        ))
      )}

      {error !== null && (
        <p role="alert" data-testid="correction-error" className="field-error">
          <span aria-hidden="true">✕</span> {error}
        </p>
      )}
      {block !== null && (
        <p id={reasonId} className="note" data-testid="correction-reason">
          <span aria-hidden="true">ⓘ </span>
          {saveBlockText(unit, block)}
        </p>
      )}
      <div className="actions">
        <button
          type="submit"
          className="button primary"
          data-testid="correction-save"
          disabled={block !== null || saving}
          aria-describedby={block === null ? undefined : reasonId}
        >
          Зберегти
        </button>
        <button
          type="button"
          className="button"
          data-testid="correction-cancel"
          disabled={saving}
          onClick={onCancel}
        >
          Скасувати
        </button>
        {staleRevision && (
          <button type="button" className="button" data-testid="correction-refresh" onClick={onRefresh}>
            Оновити транскрипт
          </button>
        )}
      </div>
    </form>
  );
}
