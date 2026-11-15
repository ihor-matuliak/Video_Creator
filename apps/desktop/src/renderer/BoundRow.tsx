import { shiftTimecode } from './interval-logic';

export const STEP_MS = 1000;

export interface BoundRowProps {
  /** `id` of the text field; the label points at it. */
  readonly fieldId: string;
  /** «Початок» or «Кінець». */
  readonly label: string;
  /** `clip-start` or `clip-end`: the stable test id of the field, the base of its buttons' ids. */
  readonly testId: string;
  readonly text: string;
  readonly durationMs: number;
  readonly playerReady: boolean;
  /** The keyboard shortcut of «Встановити тут» for this boundary, as `title` text and ARIA value. */
  readonly shortcut: { readonly title: string; readonly aria: string };
  readonly invalid: boolean;
  /** `id` of the error text that describes the field while it is invalid. */
  readonly errorId: string | undefined;
  readonly onChange: (text: string) => void;
  readonly onBlur: () => void;
  readonly onTakeFromPlayer: () => void;
  readonly onStep: (deltaMs: number) => void;
}

/** One boundary of a clip: HH:MM:SS.mmm field, «Встановити тут» and ±1 с (docs/03 section 8). */
export function BoundRow({
  fieldId,
  label,
  testId,
  text,
  durationMs,
  playerReady,
  shortcut,
  invalid,
  errorId,
  onChange,
  onBlur,
  onTakeFromPlayer,
  onStep,
}: BoundRowProps) {
  return (
    <div className="bound-row">
      <label htmlFor={fieldId}>{label}</label>
      <div className="bound-controls">
        <input
          id={fieldId}
          data-testid={testId}
          className="field bound-field"
          type="text"
          value={text}
          placeholder="00:00:00.000"
          autoComplete="off"
          spellCheck={false}
          aria-invalid={invalid}
          aria-describedby={invalid ? errorId : undefined}
          onChange={(event) => {
            onChange(event.target.value);
          }}
          onBlur={onBlur}
        />
        <button
          type="button"
          className="button"
          data-testid={`${testId}-here`}
          aria-label={`Встановити тут: ${label.toLowerCase()}`}
          aria-keyshortcuts={shortcut.aria}
          title={`Встановити ${label.toLowerCase()} тут (${shortcut.title})`}
          disabled={!playerReady}
          onClick={onTakeFromPlayer}
        >
          Встановити тут
        </button>
        <button
          type="button"
          className="button"
          data-testid={`${testId}-minus`}
          aria-label={`${label}: мінус 1 с`}
          disabled={shiftTimecode(text, -STEP_MS, durationMs) === null}
          onClick={() => {
            onStep(-STEP_MS);
          }}
        >
          −1 с
        </button>
        <button
          type="button"
          className="button"
          data-testid={`${testId}-plus`}
          aria-label={`${label}: плюс 1 с`}
          disabled={shiftTimecode(text, STEP_MS, durationMs) === null}
          onClick={() => {
            onStep(STEP_MS);
          }}
        >
          +1 с
        </button>
      </div>
    </div>
  );
}
