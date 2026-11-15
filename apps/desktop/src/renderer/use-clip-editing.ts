import { useRef, useState } from 'react';

import type { ClipDto } from '../shared/api';
import { formatTimecode } from '../shared/timecode';
import { callApi } from './bridge-calls';
import { NO_DRAFT, boundsOf, hasChanges, isSettled, nextUndoStack, planPatch, viewOf } from './clip-edit-logic';
import type { Bounds, ClipDraft, EditorView } from './clip-edit-logic';
import { shiftTimecode } from './interval-logic';
import { summarizeSaveStates } from './save-status';
import type { SaveStatus } from './save-status';
import type { Enqueue } from './use-clip-queue';

export type Bound = 'start' | 'end';

/** How the last attempt to store the edits of a clip went; nothing before the first edit. */
export type SaveState =
  | { readonly kind: 'saving' }
  | { readonly kind: 'saved' }
  | { readonly kind: 'error'; readonly message: string }
  /** Someone else changed the clip first: the user's values are kept and the stored ones shown. */
  | { readonly kind: 'conflict'; readonly refreshed: boolean };

type ByClip<T> = Readonly<Record<string, T>>;

function withEntry<T>(record: ByClip<T>, clipId: string, value: T): ByClip<T> {
  return { ...record, [clipId]: value };
}

function withoutEntry<T>(record: ByClip<T>, clipId: string): ByClip<T> {
  return Object.fromEntries(Object.entries(record).filter(([id]) => id !== clipId));
}

export interface ClipEditingOptions {
  readonly durationMs: number;
  readonly enqueue: Enqueue;
  /** A clip as the list holds it right now (not as it was last rendered). */
  readonly readClip: (clipId: string) => ClipDto | undefined;
  /** A change was stored: the clip as it is now. */
  readonly onPatched: (clip: ClipDto) => void;
  /** Reads the clips again without disturbing the screen; true when that worked. */
  readonly refresh: () => Promise<boolean>;
}

export interface ClipEditing {
  /** The texts the editor shows: the user's own edits laid over the stored clip. */
  readonly viewOf: (clip: ClipDto) => EditorView;
  readonly saveStateOf: (clipId: string) => SaveState | undefined;
  /** The save state of all the clips edited so far, as one: what the project header shows. */
  readonly saveStatus: SaveStatus;
  readonly canUndo: (clipId: string) => boolean;
  readonly changeTitle: (clip: ClipDto, text: string) => void;
  readonly changeBound: (clip: ClipDto, bound: Bound, text: string) => void;
  /** A field was left (or Enter pressed): store what is valid. */
  readonly commit: (clip: ClipDto) => void;
  /** A boundary taken from the player: stored at once. */
  readonly setBoundMs: (clip: ClipDto, bound: Bound, ms: number) => void;
  readonly stepBound: (clip: ClipDto, bound: Bound, deltaMs: number) => void;
  /** Back to the boundaries before the last change; stored at once. */
  readonly undo: (clip: ClipDto) => void;
  /** Tries again to store the user's values (after an error or a conflict). */
  readonly resave: (clip: ClipDto) => void;
  /** Gives up the user's values for the stored ones; only ever on the user's own request. */
  readonly takeStored: (clipId: string) => void;
}

/**
 * Editing of existing clips with autosave (FR-031, docs/03 section 8). Edits live as drafts
 * until the backend has stored them (`clips.patch` with `expected_revision`); a draft is only
 * dropped once its values are stored or the user asks for the stored ones, so a refused or
 * conflicting save never loses what was typed.
 */
export function useClipEditing({
  durationMs,
  enqueue,
  readClip,
  onPatched,
  refresh,
}: ClipEditingOptions): ClipEditing {
  const [drafts, setDrafts] = useState<ByClip<ClipDraft>>({});
  // The jobs of the queue read the drafts when they start, long after the render that made them.
  const draftsRef = useRef<ByClip<ClipDraft>>({});
  const [saveStates, setSaveStates] = useState<ByClip<SaveState>>({});
  const [undoStacks, setUndoStacks] = useState<ByClip<readonly Bounds[]>>({});

  const writeDrafts = (next: ByClip<ClipDraft>): void => {
    draftsRef.current = next;
    setDrafts(next);
  };

  const setDraft = (clipId: string, draft: ClipDraft): void => {
    writeDrafts(withEntry(draftsRef.current, clipId, draft));
    // A new edit makes "saved" untrue; an error stays until the next attempt answers.
    setSaveStates((current) => (current[clipId]?.kind === 'saved' ? withoutEntry(current, clipId) : current));
  };

  const dropDraft = (clipId: string): void => {
    writeDrafts(withoutEntry(draftsRef.current, clipId));
  };

  const setSaveState = (clipId: string, state: SaveState | null): void => {
    setSaveStates((current) => (state === null ? withoutEntry(current, clipId) : withEntry(current, clipId, state)));
  };

  const currentDraft = (clipId: string): ClipDraft => draftsRef.current[clipId] ?? NO_DRAFT;

  /** Stores what the clip's draft says; one job at a time per clip, each on the latest revision. */
  const save = (clipId: string): void => {
    enqueue(clipId, async () => {
      const clip = readClip(clipId);
      const draft = draftsRef.current[clipId];
      if (clip === undefined || draft === undefined) {
        return;
      }
      const plan = planPatch(clip, viewOf(clip, draft), durationMs);
      if (!hasChanges(plan)) {
        if (isSettled(plan)) {
          dropDraft(clipId);
        }
        return;
      }

      setSaveState(clipId, { kind: 'saving' });
      const result = await callApi('clips.patch', {
        id: clipId,
        expected_revision: clip.revision,
        ...plan.body,
      });

      if (result.ok) {
        const stored = result.data;
        onPatched(stored);
        setUndoStacks((current) =>
          withEntry(current, clipId, nextUndoStack(current[clipId] ?? [], boundsOf(clip), boundsOf(stored))),
        );
        // Typing may have gone on while the request was out: keep what is still unsaved.
        const latest = draftsRef.current[clipId];
        if (latest !== undefined && isSettled(planPatch(stored, viewOf(stored, latest), durationMs))) {
          dropDraft(clipId);
        }
        setSaveState(clipId, { kind: 'saved' });
        return;
      }

      if (result.error.code === 'STALE_REVISION') {
        // The old boundaries on the undo stack no longer describe what is stored.
        setUndoStacks((current) => withoutEntry(current, clipId));
        const refreshed = await refresh();
        const fresh = readClip(clipId);
        const latest = draftsRef.current[clipId];
        if (fresh !== undefined && latest !== undefined && isSettled(planPatch(fresh, viewOf(fresh, latest), durationMs))) {
          // The other change stored exactly what the user typed: nothing is in conflict.
          dropDraft(clipId);
          setSaveState(clipId, null);
          return;
        }
        setSaveState(clipId, { kind: 'conflict', refreshed });
        return;
      }
      setSaveState(clipId, { kind: 'error', message: result.error.message });
    });
  };

  const changeTitle = (clip: ClipDto, text: string): void => {
    setDraft(clip.id, { ...currentDraft(clip.id), title: text });
  };

  const changeBound = (clip: ClipDto, bound: Bound, text: string): void => {
    setDraft(clip.id, { ...currentDraft(clip.id), [bound]: text });
  };

  const commit = (clip: ClipDto): void => {
    const draft = draftsRef.current[clip.id];
    if (draft === undefined) {
      return;
    }
    setDraft(clip.id, { ...draft, touched: true });
    save(clip.id);
  };

  const setBoundMs = (clip: ClipDto, bound: Bound, ms: number): void => {
    setDraft(clip.id, {
      ...currentDraft(clip.id),
      [bound]: formatTimecode(Math.min(Math.max(0, ms), durationMs)),
      touched: true,
    });
    save(clip.id);
  };

  const stepBound = (clip: ClipDto, bound: Bound, deltaMs: number): void => {
    const shifted = shiftTimecode(viewOf(clip, draftsRef.current[clip.id])[bound], deltaMs, durationMs);
    if (shifted !== null) {
      setDraft(clip.id, { ...currentDraft(clip.id), [bound]: shifted, touched: true });
      save(clip.id);
    }
  };

  const canUndo = (clipId: string): boolean =>
    (undoStacks[clipId]?.length ?? 0) > 0 && saveStates[clipId]?.kind !== 'saving';

  const undo = (clip: ClipDto): void => {
    const previous = undoStacks[clip.id]?.at(-1);
    if (previous === undefined || !canUndo(clip.id)) {
      return;
    }
    setDraft(clip.id, {
      ...currentDraft(clip.id),
      start: formatTimecode(previous.startMs),
      end: formatTimecode(previous.endMs),
      touched: true,
    });
    save(clip.id);
  };

  return {
    viewOf: (clip) => viewOf(clip, drafts[clip.id]),
    saveStateOf: (clipId) => saveStates[clipId],
    saveStatus: summarizeSaveStates(Object.values(saveStates)),
    canUndo,
    changeTitle,
    changeBound,
    commit,
    setBoundMs,
    stepBound,
    undo,
    resave: (clip) => {
      const draft = draftsRef.current[clip.id];
      if (draft !== undefined) {
        setDraft(clip.id, { ...draft, touched: true });
      }
      save(clip.id);
    },
    takeStored: (clipId) => {
      dropDraft(clipId);
      setSaveState(clipId, null);
    },
  };
}
