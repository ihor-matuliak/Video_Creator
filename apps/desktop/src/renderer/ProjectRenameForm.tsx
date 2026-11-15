import { useId } from 'react';
import type { KeyboardEvent, SubmitEvent } from 'react';

/** What is wrong with the name: a message for the row, and whether the field itself is invalid. */
export interface RenameProblem {
  readonly message: string;
  readonly invalid: boolean;
}

/**
 * The name field of a project row (FR-001): Enter or «Зберегти» saves, Escape or «Скасувати»
 * closes it. The owner keeps the draft, so it also decides what a failed save does.
 */
export function ProjectRenameForm({
  draft,
  problem,
  saving,
  onChange,
  onSubmit,
  onCancel,
}: {
  readonly draft: string;
  readonly problem: RenameProblem | null;
  readonly saving: boolean;
  readonly onChange: (draft: string) => void;
  readonly onSubmit: () => void;
  readonly onCancel: () => void;
}) {
  const fieldId = useId();
  const errorId = useId();

  const handleSubmit = (event: SubmitEvent<HTMLFormElement>): void => {
    event.preventDefault();
    onSubmit();
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLFormElement>): void => {
    // A save that is already on its way cannot be called back.
    if (event.key === 'Escape' && !saving) {
      event.preventDefault();
      onCancel();
    }
  };

  return (
    <form
      className="panel project-rename"
      aria-label="Перейменування проєкту"
      data-testid="rename-form"
      noValidate
      onSubmit={handleSubmit}
      onKeyDown={handleKeyDown}
    >
      <label htmlFor={fieldId}>Нова назва проєкту</label>
      <input
        id={fieldId}
        data-testid="rename-input"
        className="field"
        type="text"
        value={draft}
        autoFocus
        autoComplete="off"
        readOnly={saving}
        aria-invalid={problem?.invalid === true}
        aria-describedby={problem === null ? undefined : errorId}
        onChange={(event) => {
          onChange(event.target.value);
        }}
      />
      {problem !== null && (
        <p id={errorId} role="alert" data-testid="rename-error" className="field-error">
          <span aria-hidden="true">✕</span> {problem.message}
        </p>
      )}
      <div className="actions">
        <button type="submit" className="button" data-testid="rename-save" disabled={saving}>
          {saving ? 'Зберігаємо…' : 'Зберегти'}
        </button>
        <button
          type="button"
          className="button"
          data-testid="rename-cancel"
          disabled={saving}
          onClick={onCancel}
        >
          Скасувати
        </button>
      </div>
    </form>
  );
}
