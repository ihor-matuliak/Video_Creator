import { useCallback, useEffect, useEffectEvent, useId, useRef, useState } from 'react';

import type { ClipDto, ReviewStatus, SourceDto } from '../shared/api';
import type { ApiErrorInfo } from '../shared/bridge';
import { callApi } from './bridge-calls';
import { ClipEditor } from './ClipEditor';
import { ClipInsight } from './ClipInsight';
import { ClipList } from './ClipList';
import { nextClipToReview } from './clip-edit-logic';
import { DetailsPanel } from './DetailsPanel';
import type { DetailsTabId } from './DetailsPanel';
import { activatesOnSpace, dialogIsOpen, hotkeyOf, isEditableTarget } from './hotkey-logic';
import type { Hotkey } from './hotkey-logic';
import { ManualClipForm } from './ManualClipForm';
import type { ManualClipFormHandle } from './ManualClipForm';
import type { SaveStatus } from './save-status';
import { SourcePlayer } from './SourcePlayer';
import type { PlayerHandle } from './SourcePlayer';
import { Splitter } from './Splitter';
import { TranscriptPanel } from './TranscriptPanel';
import { useClipEditing } from './use-clip-editing';
import type { Bound } from './use-clip-editing';
import { useClipQueue } from './use-clip-queue';
import { useMomentsPanels, useWideLayout } from './use-moments-panels';
import { useViewActive } from './view-active';

/** A guard against a backend that keeps returning a cursor. */
const MAX_PAGES = 100;
/** J and L move the playhead by this much (docs/03 section 8). */
const SEEK_STEP_MS = 1000;

type ListState =
  | { readonly kind: 'loading' }
  | { readonly kind: 'error'; readonly error: ApiErrorInfo }
  | { readonly kind: 'ready'; readonly clips: readonly ClipDto[] };

const LOADING: ListState = { kind: 'loading' };

type Fetched =
  | { readonly ok: true; readonly clips: ClipDto[] }
  | { readonly ok: false; readonly error: ApiErrorInfo };

/** Every page of the clips of one source; null when `run` was cancelled meanwhile. */
async function fetchAllClips(sourceId: string, run: { cancelled: boolean }): Promise<Fetched | null> {
  const clips: ClipDto[] = [];
  let cursor: string | undefined;
  for (let page = 0; page < MAX_PAGES; page += 1) {
    const result = await callApi('clips.list', {
      source_id: sourceId,
      ...(cursor === undefined ? {} : { cursor }),
    });
    if (run.cancelled) {
      return null;
    }
    if (!result.ok) {
      return { ok: false, error: result.error };
    }
    clips.push(...result.data.items);
    if (result.data.next_cursor === null) {
      break;
    }
    cursor = result.data.next_cursor;
  }
  return { ok: true, clips };
}

export interface ClipsTabProps {
  readonly sources: readonly SourceDto[];
  /** The source shown on the Sources tab; the one worked on here unless the user picks another. */
  readonly activeSourceId: string | null;
  readonly onSourceChange: (sourceId: string) => void;
  /** Open with the manual-clip form already shown (the «Створити фрагмент вручну» entry). */
  readonly startWithForm: boolean;
  readonly selectedClipIds: ReadonlySet<string>;
  readonly onSelectedClipIdsChange: (next: ReadonlySet<string>) => void;
  readonly onGoToSources: () => void;
  /** Hears every clip of the list as it is loaded and after each change (for the export bar). */
  readonly onClipsKnown: (clips: readonly ClipDto[]) => void;
  /** A clip whose review action should be focused; null when none. */
  readonly reviewFocusClipId: string | null;
  readonly onReviewFocusHandled: () => void;
  /** Hears how the autosave of the edited clips stands; «saved» again once the workspace is left. */
  readonly onSaveStatusChange: (status: SaveStatus) => void;
}

/** The Моменти tab (docs/03 section 8): player, clip editor and the clips of one source. */
export function ClipsTab({
  sources,
  activeSourceId,
  onSourceChange,
  startWithForm,
  selectedClipIds,
  onSelectedClipIdsChange,
  onGoToSources,
  onClipsKnown,
  reviewFocusClipId,
  onReviewFocusHandled,
  onSaveStatusChange,
}: ClipsTabProps) {
  const source = sources.find((item) => item.id === activeSourceId) ?? sources.at(-1);

  if (source === undefined) {
    return (
      <div className="empty-state" data-testid="clips-no-sources">
        <h3>Спочатку додай відео</h3>
        <p className="lead">Моменти з'являються для відео з вкладки «Джерела»</p>
        <button type="button" className="button primary" onClick={onGoToSources}>
          Перейти до джерел
        </button>
      </div>
    );
  }

  return (
    <div className="moments">
      {sources.length > 1 && (
        <div className="moments-source">
          <label htmlFor="moments-source-select">Джерело</label>
          <select
            id="moments-source-select"
            className="field"
            data-testid="clips-source"
            value={source.id}
            onChange={(event) => {
              onSourceChange(event.target.value);
            }}
          >
            {sources.map((item) => (
              <option key={item.id} value={item.id}>
                {item.display_name}
              </option>
            ))}
          </select>
        </div>
      )}
      <SourceWorkspace
        key={source.id}
        source={source}
        startWithForm={startWithForm}
        selectedClipIds={selectedClipIds}
        onSelectedClipIdsChange={onSelectedClipIdsChange}
        onClipsKnown={onClipsKnown}
        reviewFocusClipId={reviewFocusClipId}
        onReviewFocusHandled={onReviewFocusHandled}
        onSaveStatusChange={onSaveStatusChange}
      />
    </div>
  );
}

interface SourceWorkspaceProps {
  readonly source: SourceDto;
  readonly startWithForm: boolean;
  readonly selectedClipIds: ReadonlySet<string>;
  readonly onSelectedClipIdsChange: (next: ReadonlySet<string>) => void;
  readonly onClipsKnown: (clips: readonly ClipDto[]) => void;
  readonly reviewFocusClipId: string | null;
  readonly onReviewFocusHandled: () => void;
  readonly onSaveStatusChange: (status: SaveStatus) => void;
}

/**
 * The workspace of one source. It exists only while the Моменти tab is open, and the shortcuts
 * work only while the project is on screen (not under «Налаштування»): the listener lives and
 * dies with this component, and ignores keys while the project is hidden.
 */
function SourceWorkspace({
  source,
  startWithForm,
  selectedClipIds,
  onSelectedClipIdsChange,
  onClipsKnown,
  reviewFocusClipId,
  onReviewFocusHandled,
  onSaveStatusChange,
}: SourceWorkspaceProps) {
  const playerRef = useRef<PlayerHandle>(null);
  const formRef = useRef<ManualClipFormHandle>(null);
  const viewActive = useViewActive();
  const [playerReady, setPlayerReady] = useState(false);
  const [formOpen, setFormOpen] = useState(startWithForm);
  const [focusRequest, setFocusRequest] = useState(0);
  const [list, setList] = useState<ListState>(LOADING);
  // The queued changes read the list when they start, long after the render that asked for
  // them, so the list is also kept where a write is seen at once.
  const listRef = useRef<ListState>(LOADING);
  const [attempt, setAttempt] = useState(0);
  const [busy, setBusy] = useState<ReadonlySet<string>>(new Set());
  const [reviewErrors, setReviewErrors] = useState<Readonly<Record<string, string | undefined>>>({});
  const [activeClipId, setActiveClipId] = useState<string | null>(null);
  const [nothingToReview, setNothingToReview] = useState(false);
  // Below 1440 px the right panel is tabs under the player; this one is open.
  const [detailsTab, setDetailsTab] = useState<DetailsTabId>('edit');
  const wide = useWideLayout();
  const { attachGrid, attachList, attachDetails, style: panelStyle, control: panelControl } =
    useMomentsPanels(wide);
  const ids = useId();
  const listId = `${ids}-list`;
  const detailsId = `${ids}-details`;
  // Ends the reads that are still out when this workspace is left.
  const lifetime = useRef({ cancelled: false });
  const enqueue = useClipQueue();

  const writeList = useCallback((next: ListState): void => {
    listRef.current = next;
    setList(next);
  }, []);

  useEffect(() => {
    lifetime.current = { cancelled: false };
    const own = lifetime.current;
    return () => {
      own.cancelled = true;
    };
  }, []);

  useEffect(() => {
    // An object, not a bare boolean: the cleanup flips it while the loop awaits.
    const run = { cancelled: false };
    void fetchAllClips(source.id, run).then((fetched) => {
      if (fetched === null) {
        return;
      }
      writeList(fetched.ok ? { kind: 'ready', clips: fetched.clips } : { kind: 'error', error: fetched.error });
    });
    return () => {
      run.cancelled = true;
    };
  }, [source.id, attempt, writeList]);

  // The export bar needs the review status of every ticked clip, also of another source.
  useEffect(() => {
    if (list.kind === 'ready') {
      onClipsKnown(list.clips);
    }
  }, [list, onClipsKnown]);

  const reload = (): void => {
    writeList(LOADING);
    setAttempt((value) => value + 1);
  };

  /** Reads the clips again and swaps them in without the loading state (the editor stays on screen). */
  const refreshClips = useCallback(async (): Promise<boolean> => {
    const fetched = await fetchAllClips(source.id, lifetime.current);
    if (!fetched?.ok) {
      return false;
    }
    writeList({ kind: 'ready', clips: fetched.clips });
    return true;
  }, [source.id, writeList]);

  const readClip = useCallback((clipId: string): ClipDto | undefined => {
    const current = listRef.current;
    return current.kind === 'ready' ? current.clips.find((clip) => clip.id === clipId) : undefined;
  }, []);

  const replaceClip = useCallback(
    (updated: ClipDto): void => {
      const current = listRef.current;
      if (current.kind === 'ready') {
        writeList({
          kind: 'ready',
          clips: current.clips.map((clip) => (clip.id === updated.id ? updated : clip)),
        });
      }
    },
    [writeList],
  );

  const addClip = (created: ClipDto): void => {
    const current = listRef.current;
    if (current.kind === 'ready') {
      writeList({ kind: 'ready', clips: [...current.clips, created] });
    } else {
      // Saved before the list arrived: ask again so the new clip is certainly in it.
      reload();
    }
  };

  const editing = useClipEditing({
    durationMs: source.duration_ms,
    enqueue,
    readClip,
    onPatched: replaceClip,
    refresh: refreshClips,
  });

  // The project header shows the state of the autosave; the drafts live and die with this workspace.
  const { saveStatus } = editing;
  useEffect(() => {
    onSaveStatusChange(saveStatus);
  }, [saveStatus, onSaveStatusChange]);
  useEffect(() => {
    return () => {
      onSaveStatusChange('saved');
    };
  }, [onSaveStatusChange]);

  const review =(clip: ClipDto, status: ReviewStatus): void => {
    setBusy((current) => new Set(current).add(clip.id));
    setReviewErrors((current) =>
      Object.fromEntries(Object.entries(current).filter(([id]) => id !== clip.id)),
    );
    // After any edit of the same clip that is still being stored, and on the revision it left.
    enqueue(clip.id, async () => {
      const current = readClip(clip.id) ?? clip;
      const result = await callApi('clips.review', {
        id: clip.id,
        expected_revision: current.revision,
        status,
      });
      setBusy((now) => {
        const next = new Set(now);
        next.delete(clip.id);
        return next;
      });
      if (result.ok) {
        replaceClip(result.data);
        return;
      }
      setReviewErrors((now) => ({ ...now, [clip.id]: result.error.message }));
      if (result.error.code === 'STALE_REVISION') {
        // Another change got there first: show what is stored now.
        await refreshClips();
      }
    });
  };

  const toggleSelected = (clipId: string, selected: boolean): void => {
    const next = new Set(selectedClipIds);
    if (selected) {
      next.add(clipId);
    } else {
      next.delete(clipId);
    }
    onSelectedClipIdsChange(next);
  };

  const openForm = (): void => {
    setActiveClipId(null);
    setFormOpen(true);
    setDetailsTab('edit');
    setFocusRequest((value) => value + 1);
  };

  const clips = list.kind === 'ready' ? list.clips : [];
  const activeClip = clips.find((clip) => clip.id === activeClipId);

  /** The clip becomes the one being worked on, and the player goes to its start. */
  const activate = (clip: ClipDto): void => {
    setActiveClipId(clip.id);
    setDetailsTab('edit');
    setNothingToReview(false);
    playerRef.current?.seekTo(clip.start_ms);
  };

  const goToNext = (): void => {
    const next = nextClipToReview(clips, activeClipId);
    if (next === null) {
      setNothingToReview(true);
    } else {
      activate(next);
    }
  };

  /** I and O: the playhead position becomes a boundary of the active clip (or of the new one being typed). */
  const setBoundFromPlayer = (bound: Bound): boolean => {
    const ms = playerRef.current?.currentMs() ?? null;
    if (ms === null) {
      return false;
    }
    if (activeClip !== undefined) {
      editing.setBoundMs(activeClip, bound, ms);
      return true;
    }
    if (formRef.current !== null) {
      formRef.current.setBoundMs(bound, ms);
      return true;
    }
    return false;
  };

  /** A and R: only a clip that is still to be reviewed, like the buttons of its card. */
  const reviewActive = (status: ReviewStatus): boolean => {
    if (activeClip?.review_status !== 'suggested' || busy.has(activeClip.id)) {
      return false;
    }
    review(activeClip, status);
    return true;
  };

  /** True when the key did something (then the browser's own action for it is cancelled). */
  const runHotkey = (hotkey: Hotkey): boolean => {
    const player = playerRef.current;
    switch (hotkey) {
      case 'play':
        player?.togglePlay();
        return true;
      case 'back':
        player?.seekBy(-SEEK_STEP_MS);
        return true;
      case 'forward':
        player?.seekBy(SEEK_STEP_MS);
        return true;
      case 'start':
        return setBoundFromPlayer('start');
      case 'end':
        return setBoundFromPlayer('end');
      case 'approve':
        return reviewActive('approved');
      case 'reject':
        return reviewActive('rejected');
      case 'undo':
        if (activeClip !== undefined && editing.canUndo(activeClip.id)) {
          editing.undo(activeClip);
          return true;
        }
        return false;
    }
  };

  const handleKeyDown = (event: KeyboardEvent): void => {
    // A project under «Налаштування» is still mounted: its shortcuts must not act on it from there.
    if (!viewActive || event.defaultPrevented || event.isComposing) {
      return;
    }
    const hotkey = hotkeyOf(event);
    // Typing, a list or a dialog owns the key; so does a button that Space presses.
    if (hotkey === null || isEditableTarget(event.target) || dialogIsOpen(document)) {
      return;
    }
    if (hotkey === 'play' && activatesOnSpace(event.target)) {
      return;
    }
    // Holding a key must not review clip after clip; only seeking repeats.
    if (event.repeat && hotkey !== 'back' && hotkey !== 'forward') {
      return;
    }
    if (runHotkey(hotkey)) {
      event.preventDefault();
    }
  };

  // An effect event always runs the handler of the latest render: the clip that is active now.
  const onKeyDown = useEffectEvent(handleKeyDown);
  useEffect(() => {
    const listener = (event: KeyboardEvent): void => {
      onKeyDown(event);
    };
    window.addEventListener('keydown', listener);
    return () => {
      window.removeEventListener('keydown', listener);
    };
  }, []);

  const editor =
    activeClip !== undefined ? (
      <>
        <ClipInsight clip={activeClip} />
        <ClipEditor
          key={activeClip.id}
          clip={activeClip}
          view={editing.viewOf(activeClip)}
          saveState={editing.saveStateOf(activeClip.id)}
          canUndo={editing.canUndo(activeClip.id)}
          durationMs={source.duration_ms}
          player={playerRef}
          playerReady={playerReady}
          onTitleChange={(text) => {
            editing.changeTitle(activeClip, text);
          }}
          onBoundChange={(bound, text) => {
            editing.changeBound(activeClip, bound, text);
          }}
          onCommit={() => {
            editing.commit(activeClip);
          }}
          onTakeFromPlayer={(bound) => {
            setBoundFromPlayer(bound);
          }}
          onStep={(bound, deltaMs) => {
            editing.stepBound(activeClip, bound, deltaMs);
          }}
          onUndo={() => {
            editing.undo(activeClip);
          }}
          onResave={() => {
            editing.resave(activeClip);
          }}
          onTakeStored={() => {
            editing.takeStored(activeClip.id);
          }}
        />
      </>
    ) : formOpen ? (
      <ManualClipForm
        ref={formRef}
        source={source}
        player={playerRef}
        playerReady={playerReady}
        focusRequest={focusRequest}
        onSaved={addClip}
      />
    ) : (
      <p className="note" data-testid="editor-empty">
        <span aria-hidden="true">ⓘ </span>
        Виберіть фрагмент у списку або створіть новий вручну, щоб змінити його межі.
      </p>
    );

  const hotkeysHint = (
    <p className="note" data-testid="hotkeys-hint">
      <span aria-hidden="true">ⓘ </span>
      Клавіші: Пробіл — грати/пауза, I і O — початок і кінець тут, A і R — підтвердити і відхилити, J і L — назад і
      вперед на 1 с, Ctrl+Z — скасувати зміну меж. Не діють, поки курсор у полі.
    </p>
  );

  // The transcript of the source the clips belong to (ADR 039): search, «Виправити»; a time in it seeks the player.
  // Keyed by the source: its search and its correction editor start afresh for each source.
  const transcript = (
    <TranscriptPanel
      key={source.id}
      sourceId={source.id}
      onSeek={(ms) => {
        playerRef.current?.seekTo(ms);
      }}
    />
  );

  // One grid in both arrangements: from 1440 px the right panel is the third column, below it
  // sits under the player (styles.css places it); the splitter between player and panel exists
  // only where there is a column to resize.
  return (
    <div className="moments-grid" ref={attachGrid} data-mode={wide ? 'wide' : 'narrow'} style={panelStyle}>
      <section id={listId} ref={attachList} className="moments-list" aria-labelledby="clips-title">
        <div className="moments-list-header">
          <h3 id="clips-title">Фрагменти</h3>
          <button type="button" className="button" data-testid="manual-clip" onClick={openForm}>
            Створити фрагмент вручну
          </button>
        </div>

        {list.kind === 'loading' && (
          <p role="status" data-testid="clips-loading" className="note">
            Завантажуємо фрагменти…
          </p>
        )}

        {list.kind === 'error' && (
          <div role="alert" data-testid="clips-error" className="notice notice-danger">
            <h3>Не вдалося завантажити фрагменти</h3>
            <p>{list.error.message}</p>
            <button type="button" className="button" data-testid="clips-retry" onClick={reload}>
              Спробувати ще раз
            </button>
          </div>
        )}

        {list.kind === 'ready' && clips.length === 0 && (
          <div className="empty-clips" data-testid="empty-candidates">
            <p className="lead">Фрагментів ще немає</p>
            <p className="note">Створи перший вручну: вкажи початок і кінець або візьми їх з плеєра.</p>
          </div>
        )}

        {clips.length > 0 && (
          <ClipList
            clips={clips}
            selected={selectedClipIds}
            busy={busy}
            errors={reviewErrors}
            activeClipId={activeClipId}
            onActivate={activate}
            onToggleSelected={toggleSelected}
            onReview={review}
            focusClipId={reviewFocusClipId}
            onFocusHandled={onReviewFocusHandled}
          />
        )}
      </section>

      <Splitter
        {...panelControl('list')}
        label="Ширина списку фрагментів"
        controls={listId}
        testId="splitter-list"
      />

      <section className="moments-stage" aria-label="Перегляд фрагмента">
        <SourcePlayer source={source} ref={playerRef} onReadyChange={setPlayerReady} />

        {clips.length > 0 && (
          <div className="review-bar">
            <button type="button" className="button" data-testid="next-clip" onClick={goToNext}>
              Наступний
            </button>
            {nothingToReview && (
              <p role="status" data-testid="next-clip-status" className="note">
                Немає інших фрагментів, що чекають на перевірку
              </p>
            )}
          </div>
        )}
      </section>

      {wide && (
        <Splitter
          {...panelControl('details')}
          label="Ширина панелі фрагмента"
          controls={detailsId}
          testId="splitter-details"
        />
      )}

      <DetailsPanel
        id={detailsId}
        ref={attachDetails}
        tabbed={!wide}
        active={detailsTab}
        onChange={setDetailsTab}
        edit={editor}
        keys={hotkeysHint}
        transcript={transcript}
      />
    </div>
  );
}
