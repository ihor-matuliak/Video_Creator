import { useCallback, useEffect, useId, useImperativeHandle, useRef, useState } from 'react';
import type { Ref, RefObject, SubmitEvent } from 'react';

import type { ClipDto, SourceDto } from '../shared/api';
import type { ApiErrorInfo } from '../shared/bridge';
import { formatTimecode } from '../shared/timecode';
import { BoundRow } from './BoundRow';
import { callApi } from './bridge-calls';
import { TITLE_MAX } from './clip-edit-logic';
import { HOTKEY_LABELS } from './hotkey-logic';
import { checkInterval, shiftTimecode } from './interval-logic';
import type { PlayerHandle } from './SourcePlayer';

type Bound = 'start' | 'end';

/** What the keyboard shortcuts may ask of the form. */
export interface ManualClipFormHandle {
  /** Puts a position (kept inside the source) into the field of one boundary. */
  readonly setBoundMs: (bound: Bound, ms: number) => void;
}

export interface ManualClipFormProps {
  readonly source: SourceDto;
  readonly player: RefObject<PlayerHandle | null>;
  /** True when the player has a loaded video to read the position from. */
  readonly playerReady: boolean;
  /** Changes whenever the user asks for the form again; moves focus to the title. */
  readonly focusRequest: number;
  readonly onSaved: (clip: ClipDto) => void;
  readonly ref?: Ref<ManualClipFormHandle>;
}

/** A clip without a title is named after its interval instead of being refused. */
function defaultTitle(startMs: number, endMs: number): string {
  return `Фрагмент ${formatTimecode(startMs)}–${formatTimecode(endMs)}`;
}

/** FR-030, FR-031: a manual clip from typed or taken-from-the-player boundaries. */
export function ManualClipForm({
  source,
  player,
  playerReady,
  focusRequest,
  onSaved,
  ref,
}: ManualClipFormProps) {
  const titleRef = useRef<HTMLInputElement>(null);
  const ids = useId();
  const [title, setTitle] = useState('');
  const [startText, setStartText] = useState('');
  const [endText, setEndText] = useState('');
  // The error appears once a boundary was left (docs/03 section 11); the save button
  // follows the text as it is, so saving never waits for a blur.
  const [touched, setTouched] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);
  const [failure, setFailure] = useState<ApiErrorInfo | null>(null);

  useEffect(() => {
    titleRef.current?.focus();
  }, [focusRequest]);

  const check = checkInterval(startText, endText, source.duration_ms);
  const showError = touched && !check.ok;
  const errorId = `${ids}-interval-error`;
  const reasonId = `${ids}-save-reason`;
  // Why «Зберегти фрагмент» is off: the error under the fields once it is shown, a note before.
  // While the clip is being stored the button's own label («Зберігаємо…») says it.
  const saveBlocked = !check.ok && !saving;

  const setBound = useCallback((bound: Bound, text: string): void => {
    if (bound === 'start') {
      setStartText(text);
    } else {
      setEndText(text);
    }
    setSaved(null);
  }, []);

  const durationMs = source.duration_ms;
  const setBoundMs = useCallback(
    (bound: Bound, ms: number): void => {
      setBound(bound, formatTimecode(Math.min(ms, durationMs)));
      setTouched(true);
    },
    [durationMs, setBound],
  );

  useImperativeHandle(ref, () => ({ setBoundMs }), [setBoundMs]);

  const takeFromPlayer = (bound: Bound): void => {
    const ms = player.current?.currentMs() ?? null;
    if (ms !== null) {
      setBoundMs(bound, ms);
    }
  };

  const step = (bound: Bound, deltaMs: number): void => {
    const shifted = shiftTimecode(bound === 'start' ? startText : endText, deltaMs, source.duration_ms);
    if (shifted !== null) {
      setBound(bound, shifted);
      setTouched(true);
    }
  };

  const save = (event: SubmitEvent<HTMLFormElement>): void => {
    event.preventDefault();
    // Parsed from the current text, not from anything remembered at the last blur.
    const interval = checkInterval(startText, endText, source.duration_ms);
    if (saving) {
      return;
    }
    setTouched(true);
    if (!interval.ok) {
      return;
    }
    const trimmed = title.trim();
    setSaving(true);
    setFailure(null);
    setSaved(null);
    void callApi('clips.create', {
      source_id: source.id,
      title: trimmed === '' ? defaultTitle(interval.startMs, interval.endMs) : trimmed,
      start_ms: interval.startMs,
      end_ms: interval.endMs,
      categories: [],
    }).then((result) => {
      setSaving(false);
      if (result.ok) {
        setTitle('');
        setStartText('');
        setEndText('');
        setTouched(false);
        setSaved(result.data.title);
        onSaved(result.data);
      } else {
        setFailure(result.error);
      }
    });
  };

  const playFragment = (): void => {
    const interval = checkInterval(startText, endText, source.duration_ms);
    if (interval.ok) {
      player.current?.playRange(interval.startMs, interval.endMs);
    }
  };

  const boundRow = (bound: Bound, label: string, text: string, testId: string) => (
    <BoundRow
      fieldId={`${ids}-${bound}`}
      label={label}
      testId={testId}
      text={text}
      durationMs={source.duration_ms}
      playerReady={playerReady}
      shortcut={HOTKEY_LABELS[bound === 'start' ? 'start' : 'end']}
      invalid={showError}
      errorId={errorId}
      onChange={(next) => {
        setBound(bound, next);
      }}
      onBlur={() => {
        setTouched(true);
      }}
      onTakeFromPlayer={() => {
        takeFromPlayer(bound);
      }}
      onStep={(deltaMs) => {
        step(bound, deltaMs);
      }}
    />
  );

  return (
    <form className="panel manual-form" aria-label="Новий фрагмент" noValidate onSubmit={save}>
      <h3>Новий фрагмент</h3>
      <label htmlFor={`${ids}-title`}>Назва</label>
      <input
        ref={titleRef}
        id={`${ids}-title`}
        data-testid="clip-title"
        className="field"
        type="text"
        value={title}
        maxLength={TITLE_MAX}
        autoComplete="off"
        placeholder="Фрагмент"
        onChange={(event) => {
          setTitle(event.target.value);
          setSaved(null);
        }}
      />
      {boundRow('start', 'Початок', startText, 'clip-start')}
      {boundRow('end', 'Кінець', endText, 'clip-end')}
      <p className="note">Формат часу: ГГ:ХХ:СС.мс. Кінець не входить у фрагмент.</p>
      {showError && (
        <p id={errorId} role="alert" data-testid="interval-error" className="field-error">
          <span aria-hidden="true">✕</span> {check.message}
        </p>
      )}
      {failure !== null && (
        <p role="alert" data-testid="save-clip-error" className="field-error">
          <span aria-hidden="true">✕</span> Не вдалося зберегти фрагмент. {failure.message}
        </p>
      )}
      {saved !== null && (
        <p role="status" data-testid="clip-saved" className="status status-success">
          <span aria-hidden="true">✓</span> Фрагмент «{saved}» збережено
        </p>
      )}
      <div className="actions">
        <button
          type="submit"
          className="button primary"
          data-testid="save-clip"
          disabled={!check.ok || saving}
          aria-describedby={saveBlocked ? (showError ? errorId : reasonId) : undefined}
        >
          {saving ? 'Зберігаємо…' : 'Зберегти фрагмент'}
        </button>
        <button
          type="button"
          className="button"
          data-testid="preview-clip"
          disabled={!check.ok || !playerReady}
          onClick={playFragment}
        >
          Відтворити фрагмент
        </button>
      </div>
      {saveBlocked && !showError && (
        <p id={reasonId} className="note" data-testid="save-clip-reason">
          <span aria-hidden="true">ⓘ </span>
          Щоб зберегти: {check.message}
        </p>
      )}
      {!playerReady && (
        <p className="note">
          <span aria-hidden="true">ⓘ </span>
          Кнопки «Встановити тут» і «Відтворити фрагмент» стануть доступні, коли відео завантажиться.
          Час можна ввести вручну вже зараз.
        </p>
      )}
    </form>
  );
}
