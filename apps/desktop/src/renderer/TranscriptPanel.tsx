import { Fragment, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { ChangeEvent, ReactNode } from 'react';

import type { TranscriptSegmentDto } from '../shared/api';
import { formatTimecode } from '../shared/timecode';
import { callApi } from './bridge-calls';
import { CorrectionEditor } from './TranscriptCorrection';
import {
  correctionEdits,
  correctionScope,
  correctionUnitOf,
  coverageGaps,
  findMatches,
  gapStateText,
  groupBySegment,
  highlightParts,
  initialDrafts,
  matchCounterText,
  saveBlockOf,
  stepIndex,
} from './transcript-logic';
import type { CorrectionScope, MatchRange } from './transcript-logic';
import { useTranscript } from './use-transcript';
import type { LoadedTranscript } from './use-transcript';

export interface TranscriptPanelProps {
  /** The source whose transcript is shown: its newest revision. */
  readonly sourceId: string;
  /** Moves the player to a time of the source, in milliseconds. */
  readonly onSeek: (ms: number) => void;
}

/**
 * The «Транскрипція» part of the Моменти panel (ADR 039): the segments as plain text, each with a
 * time that seeks the player and a «Виправити» (ADR 036); a local search with a counter and steps;
 * the gaps of the coverage, which seek too. Without a transcript it says where to make one.
 */
export function TranscriptPanel({ sourceId, onSeek }: TranscriptPanelProps) {
  const { state, reload, refresh } = useTranscript(sourceId);

  if (state.kind === 'loading') {
    return (
      <p role="status" data-testid="transcript-loading" className="note">
        Завантажуємо транскрипцію…
      </p>
    );
  }

  if (state.kind === 'error') {
    return (
      <div role="alert" data-testid="transcript-error" className="notice notice-danger">
        <h3>Не вдалося завантажити транскрипцію</h3>
        <p>{state.error.message}</p>
        <button type="button" className="button" data-testid="transcript-retry" onClick={reload}>
          Спробувати ще раз
        </button>
      </div>
    );
  }

  if (state.kind === 'none') {
    return (
      <p className="note" data-testid="transcript-none">
        <span aria-hidden="true">ⓘ </span>
        Транскрипту для цього відео ще немає. Відкрийте вкладку «Джерела», щоб його створити.
      </p>
    );
  }

  return <ReadyTranscript transcript={state.transcript} onSeek={onSeek} onRefresh={refresh} />;
}

/** The text with its matches in <mark>, built from the pieces of the text: never from HTML. */
function highlight(text: string, ranges: readonly MatchRange[], shown: number): ReactNode {
  return highlightParts(text, ranges).map((part, index) =>
    part.matchIndex === null ? (
      <Fragment key={index}>{part.text}</Fragment>
    ) : (
      <mark
        key={index}
        data-match={part.matchIndex}
        aria-current={part.matchIndex === shown ? 'true' : undefined}
      >
        {part.text}
      </mark>
    ),
  );
}

/** The one correction in progress (ADR 036). Its drafts are kept until it is closed or saved. */
interface Editing {
  readonly segmentId: string;
  readonly scope: CorrectionScope;
  readonly drafts: Readonly<Record<string, string>>;
  readonly saving: boolean;
  readonly error: string | null;
  /** The last save was refused as stale (409): the editor offers to read the newest revision. */
  readonly staleRevision: boolean;
}

function ReadyTranscript({
  transcript,
  onSeek,
  onRefresh,
}: {
  readonly transcript: LoadedTranscript;
  readonly onSeek: (ms: number) => void;
  readonly onRefresh: () => Promise<LoadedTranscript | null>;
}) {
  const searchId = useId();
  const gapsId = useId();
  const listRef = useRef<HTMLOListElement>(null);
  const [query, setQuery] = useState('');
  const [current, setCurrent] = useState(0);
  const [editing, setEditing] = useState<Editing | null>(null);
  const [savedRevision, setSavedRevision] = useState<number | null>(null);
  // The segment whose «Виправити» takes the focus once its editor is closed (after a save or a cancel).
  const returnFocusTo = useRef<string | null>(null);

  const unit = correctionUnitOf(transcript.timingLevel);
  const wordsById = useMemo(
    () => new Map(transcript.words.map((word) => [word.id, word] as const)),
    [transcript.words],
  );

  const matches = useMemo(
    () => findMatches(transcript.segments.map((segment) => segment.text), query),
    [transcript.segments, query],
  );
  const bySegment = useMemo(() => groupBySegment(matches), [matches]);
  const gaps = useMemo(() => coverageGaps(transcript.coverage), [transcript.coverage]);
  const total = matches.length;
  const shown = total === 0 ? 0 : Math.min(current, total - 1);

  // The match on show is brought into view whichever control moved to it, and whenever the query changes:
  // a new query can have the same count and index as the one before, and then only the query tells them apart.
  // A saved correction is not a new query: it moves the list the reader scrolled only when it changes the count of matches.
  // Only the list scrolls: its scrollTop moves the match to the middle of it. scrollIntoView would move the page as well.
  useEffect(() => {
    const list = listRef.current;
    if (list === null) {
      return;
    }
    const match = list.querySelector<HTMLElement>(`[data-match="${String(shown)}"]`);
    if (match === null) {
      return;
    }
    const listBox = list.getBoundingClientRect();
    const matchBox = match.getBoundingClientRect();
    const visibleTop = listBox.top + list.clientTop;
    list.scrollTop += matchBox.top - visibleTop - (list.clientHeight - matchBox.height) / 2;
  }, [shown, total, query]);

  // Once the editor is closed, its «Виправити» gets the focus again (it was removed or disabled meanwhile).
  // This is a layout effect, as in TranscriptCorrection: the focus moves in the same commit that closes the
  // editor, so a key dispatched right after the close cannot reach the body before the focus is back.
  useLayoutEffect(() => {
    const segmentId = returnFocusTo.current;
    if (segmentId === null) {
      return;
    }
    returnFocusTo.current = null;
    const buttons = Array.from(
      listRef.current?.querySelectorAll<HTMLButtonElement>('[data-testid="transcript-correct"]') ?? [],
    );
    buttons.find((button) => button.dataset.segmentId === segmentId)?.focus();
  });

  const onQueryChange = (event: ChangeEvent<HTMLInputElement>): void => {
    setQuery(event.target.value);
    // A new search starts again at its first match.
    setCurrent(0);
  };

  const startEditing = (segment: TranscriptSegmentDto): void => {
    const scope = correctionScope(segment, unit, wordsById);
    setSavedRevision(null);
    setEditing({
      segmentId: segment.id,
      scope,
      drafts: initialDrafts(scope.targets),
      saving: false,
      error: null,
      staleRevision: false,
    });
  };

  const changeDraft = (id: string, text: string): void => {
    setEditing((now) => (now === null ? null : { ...now, drafts: { ...now.drafts, [id]: text } }));
  };

  const closeEditing = (): void => {
    if (editing === null || editing.saving) {
      return;
    }
    returnFocusTo.current = editing.segmentId;
    setEditing(null);
  };

  // After a stale save the editor closes, and the newest revision is read again (the one on screen stays
  // until the new one arrives, or the error state replaces it).
  const refreshAfterStale = (): void => {
    closeEditing();
    void onRefresh();
  };

  const saveEditing = (): void => {
    if (editing === null || editing.saving || saveBlockOf(editing.scope, editing.drafts) !== null) {
      return;
    }
    const { segmentId, scope, drafts } = editing;
    const edits = correctionEdits(unit, scope, drafts);
    setEditing({ ...editing, saving: true, error: null, staleRevision: false });
    void callApi('transcripts.correct', { id: transcript.id, edits }).then(async (result) => {
      if (!result.ok) {
        // The typed text stays; the backend's message is shown under the editor.
        setEditing((now) =>
          now === null
            ? null
            : {
                ...now,
                saving: false,
                error: result.error.message,
                staleRevision: result.error.code === 'TRANSCRIPT_REVISION_STALE',
              },
        );
        return;
      }
      // The new revision is read before the editor closes: the old one stays on screen until then.
      const fresh = await onRefresh();
      if (fresh === null) {
        setEditing((now) => (now === null ? null : { ...now, saving: false }));
        return;
      }
      setSavedRevision(fresh.revision);
      returnFocusTo.current = segmentId;
      setEditing(null);
    });
  };

  return (
    <section className="transcript-panel" aria-label="Транскрипція джерела" data-testid="transcript-ready">
      <p className="note" data-testid="transcript-revision">
        {`Ревізія ${String(transcript.revision)} · сегментів: ${String(transcript.segments.length)}`}
      </p>
      {savedRevision !== null && (
        <p role="status" data-testid="correction-saved" className="status status-success">
          <span aria-hidden="true">✓</span> {`Виправлення збережено: ревізія ${String(savedRevision)}`}
        </p>
      )}

      {gaps.length > 0 && (
        <div className="transcript-gaps-block">
          <h4 id={gapsId} className="transcript-subtitle">
            Пропуски покриття
          </h4>
          <p className="note">Ці ділянки відео без розпізнаного тексту. Клік перемотує на їх початок.</p>
          <ul className="transcript-gaps" aria-labelledby={gapsId}>
            {gaps.map((span, index) => (
              <li key={index}>
                <button
                  type="button"
                  className="transcript-time transcript-gap"
                  data-testid="transcript-gap"
                  onClick={() => {
                    onSeek(span.start_ms);
                  }}
                >
                  {`${formatTimecode(span.start_ms)}–${formatTimecode(span.end_ms)} · `}
                  <span className="transcript-gap-state">{gapStateText(span.state)}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="transcript-search">
        <label htmlFor={searchId}>Пошук у тексті</label>
        <input
          id={searchId}
          type="search"
          className="field"
          data-testid="transcript-search"
          value={query}
          onChange={onQueryChange}
        />
        <div className="transcript-search-controls">
          <button
            type="button"
            className="button"
            data-testid="transcript-prev"
            disabled={total === 0}
            onClick={() => {
              setCurrent(stepIndex(shown, total, -1));
            }}
          >
            Попередній
          </button>
          <button
            type="button"
            className="button"
            data-testid="transcript-next"
            disabled={total === 0}
            onClick={() => {
              setCurrent(stepIndex(shown, total, 1));
            }}
          >
            Наступний
          </button>
          {query.trim() !== '' && (
            <span role="status" className="note" data-testid="transcript-count">
              {matchCounterText(shown, total)}
            </span>
          )}
        </div>
      </div>

      {transcript.segments.length === 0 ? (
        <p className="note" data-testid="transcript-empty">
          У цій ревізії немає сегментів.
        </p>
      ) : (
        <ol ref={listRef} className="transcript-segments" data-testid="transcript-segments">
          {transcript.segments.map((segment, row) => (
            <li key={row} className="transcript-segment" data-testid="transcript-segment">
              <button
                type="button"
                className="transcript-time"
                data-testid="transcript-time"
                onClick={() => {
                  onSeek(segment.start_ms);
                }}
              >
                {formatTimecode(segment.start_ms)}
              </button>
              <div className="transcript-body">
                <p className="transcript-text">{highlight(segment.text, bySegment.get(row) ?? [], shown)}</p>
                {editing !== null && editing.segmentId === segment.id ? (
                  <CorrectionEditor
                    unit={unit}
                    targets={editing.scope.targets}
                    drafts={editing.drafts}
                    block={saveBlockOf(editing.scope, editing.drafts)}
                    saving={editing.saving}
                    error={editing.error}
                    staleRevision={editing.staleRevision}
                    onDraftChange={changeDraft}
                    onSave={saveEditing}
                    onCancel={closeEditing}
                    onRefresh={refreshAfterStale}
                  />
                ) : (
                  <button
                    type="button"
                    className="button transcript-correct"
                    data-testid="transcript-correct"
                    data-segment-id={segment.id}
                    disabled={editing !== null}
                    onClick={() => {
                      startEditing(segment);
                    }}
                  >
                    Виправити
                  </button>
                )}
              </div>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
