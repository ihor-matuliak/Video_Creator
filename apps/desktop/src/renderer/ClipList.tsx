import { useEffect, useRef } from 'react';

import type { ClipDto, ReviewStatus } from '../shared/api';
import { formatTimecode } from '../shared/timecode';
import { HOTKEY_LABELS } from './hotkey-logic';
import { StatusText } from './ui';
import type { Tone } from './ui';

interface ReviewLabel {
  readonly tone: Tone;
  readonly text: string;
}

const REVIEW_LABELS: Readonly<Record<string, ReviewLabel>> = {
  suggested: { tone: 'neutral', text: 'На розгляді' },
  approved: { tone: 'success', text: 'Підтверджено' },
  rejected: { tone: 'danger', text: 'Відхилено' },
};

const UNKNOWN_REVIEW: ReviewLabel = { tone: 'warning', text: 'Невідомо' };

function reviewLabel(status: string): ReviewLabel {
  return Object.hasOwn(REVIEW_LABELS, status) ? (REVIEW_LABELS[status] ?? UNKNOWN_REVIEW) : UNKNOWN_REVIEW;
}

/** What a click on the card itself does not mean: these have their own action. */
const INTERACTIVE = 'button, input, label, a, select, textarea';

/** The review keys act on the active clip only, so only its buttons say which key they are. */
function shortcutProps(name: string, key: 'approve' | 'reject') {
  return {
    'aria-keyshortcuts': HOTKEY_LABELS[key].aria,
    title: `${name} (${HOTKEY_LABELS[key].title})`,
  };
}

export interface ClipListProps {
  readonly clips: readonly ClipDto[];
  /** Ids ticked for export: a separate decision from the review status. */
  readonly selected: ReadonlySet<string>;
  /** Ids whose review request is in flight. */
  readonly busy: ReadonlySet<string>;
  /** A refused review, by clip id. */
  readonly errors: Readonly<Record<string, string | undefined>>;
  /** The clip being worked on: the player shows it, the editor edits it, the review keys act on it. */
  readonly activeClipId?: string | null;
  /** A card was clicked (or its title pressed): make that clip the active one. */
  readonly onActivate: (clip: ClipDto) => void;
  readonly onToggleSelected: (clipId: string, selected: boolean) => void;
  readonly onReview: (clip: ClipDto, status: ReviewStatus) => void;
  /** A clip whose review action should get the keyboard focus (the export bar's «Перейти до перевірки»). */
  readonly focusClipId?: string | null;
  /** Called once the focus request has been served, whether or not the clip was in the list. */
  readonly onFocusHandled?: () => void;
}

/** The candidate cards of docs/03 section 8; the selection checkbox and the review buttons differ. */
export function ClipList({
  clips,
  selected,
  busy,
  errors,
  activeClipId = null,
  onActivate,
  onToggleSelected,
  onReview,
  focusClipId = null,
  onFocusHandled,
}: ClipListProps) {
  const listRef = useRef<HTMLUListElement>(null);

  useEffect(() => {
    if (focusClipId === null) {
      return;
    }
    const cards = listRef.current?.querySelectorAll<HTMLElement>('li[data-clip-id]') ?? [];
    const card = Array.from(cards).find((element) => element.dataset.clipId === focusClipId);
    // The first button of the actions row is the review action: approve, or back to review.
    card?.querySelector<HTMLElement>('.clip-actions button')?.focus();
    onFocusHandled?.();
  }, [focusClipId, onFocusHandled]);

  return (
    <ul className="clip-list" aria-label="Фрагменти" ref={listRef}>
      {clips.map((clip) => {
        const label = reviewLabel(clip.review_status);
        const working = busy.has(clip.id);
        const busyId = `clip-busy-${clip.id}`;
        const error = errors[clip.id];
        const active = clip.id === activeClipId;
        return (
          <li
            key={clip.id}
            className="clip-card"
            data-testid="clip-card"
            data-clip-id={clip.id}
            data-active={active}
            aria-current={active ? 'true' : undefined}
            onClick={(event) => {
              // Anywhere on the card, except on its own controls (they do their own thing).
              if (!(event.target instanceof Element) || event.target.closest(INTERACTIVE) === null) {
                onActivate(clip);
              }
            }}
          >
            <label className="clip-select">
              <input
                type="checkbox"
                data-testid="select-clip"
                data-clip-id={clip.id}
                aria-label={`Вибрати для експорту: ${clip.title}`}
                checked={selected.has(clip.id)}
                onChange={(event) => {
                  onToggleSelected(clip.id, event.target.checked);
                }}
              />
            </label>
            <div className="clip-body">
              <h4 className="clip-title">
                <button
                  type="button"
                  className="clip-open"
                  data-testid="open-clip"
                  title="Відкрити фрагмент: перейти до його початку і змінити межі"
                  onClick={() => {
                    onActivate(clip);
                  }}
                >
                  <span className="clip-title-text">{clip.title}</span>
                </button>
              </h4>
              <p className="clip-times">
                {formatTimecode(clip.end_ms - clip.start_ms)} · {formatTimecode(clip.start_ms)}–
                {formatTimecode(clip.end_ms)}
              </p>
              <StatusText tone={label.tone} testId="clip-review-status">
                {label.text}
              </StatusText>
              <div className="clip-actions">
                {clip.review_status === 'suggested' && (
                  <>
                    <button
                      type="button"
                      className="button primary"
                      data-testid="approve-clip"
                      {...(active ? shortcutProps('Підтвердити', 'approve') : {})}
                      disabled={working}
                      aria-describedby={working ? busyId : undefined}
                      onClick={() => {
                        onReview(clip, 'approved');
                      }}
                    >
                      Підтвердити
                    </button>
                    <button
                      type="button"
                      className="button"
                      data-testid="reject-clip"
                      {...(active ? shortcutProps('Відхилити', 'reject') : {})}
                      disabled={working}
                      aria-describedby={working ? busyId : undefined}
                      onClick={() => {
                        onReview(clip, 'rejected');
                      }}
                    >
                      Відхилити
                    </button>
                  </>
                )}
                {(clip.review_status === 'approved' || clip.review_status === 'rejected') && (
                  <button
                    type="button"
                    className="button"
                    data-testid="reopen-clip"
                    disabled={working}
                    aria-describedby={working ? busyId : undefined}
                    onClick={() => {
                      onReview(clip, 'suggested');
                    }}
                  >
                    Повернути до розгляду
                  </button>
                )}
              </div>
              {working && (
                <p id={busyId} role="status" className="note" data-testid="clip-busy">
                  <span aria-hidden="true">ⓘ </span>
                  Зберігаємо рішення…
                </p>
              )}
              {error !== undefined && (
                <p role="alert" data-testid="clip-error" className="field-error">
                  <span aria-hidden="true">✕</span> {error}
                </p>
              )}
            </div>
          </li>
        );
      })}
    </ul>
  );
}
