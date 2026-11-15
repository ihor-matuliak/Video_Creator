import type { ClipDto } from '../shared/api';
import { formatTimecode } from '../shared/timecode';
import { checkInterval } from './interval-logic';

/** The longest title the backend accepts (and the form allows to type). */
export const TITLE_MAX = 160;
/** How many boundary changes of one clip can be undone in a session. */
export const UNDO_LIMIT = 50;

/** The texts in the fields of the clip editor; what the user sees, not what is stored. */
export interface ClipFields {
  readonly title: string;
  readonly start: string;
  readonly end: string;
}

/**
 * What the user has typed and not yet seen saved: only the fields that were edited, so a field
 * that was left alone keeps following the stored clip (also after someone else changed it).
 * `touched`: a field was left, or a boundary was set by a button or a key.
 */
export interface ClipDraft {
  readonly title?: string;
  readonly start?: string;
  readonly end?: string;
  readonly touched: boolean;
}

/** What the fields of the editor show, and whether errors may be shown yet. */
export interface EditorView extends ClipFields {
  readonly touched: boolean;
}

export const NO_DRAFT: ClipDraft = { touched: false };

export interface Bounds {
  readonly startMs: number;
  readonly endMs: number;
}

export function fieldsOf(clip: ClipDto): ClipFields {
  return { title: clip.title, start: formatTimecode(clip.start_ms), end: formatTimecode(clip.end_ms) };
}

/** The stored clip with the user's edits laid over it. */
export function viewOf(clip: ClipDto, draft: ClipDraft | undefined): EditorView {
  const stored = fieldsOf(clip);
  return {
    title: draft?.title ?? stored.title,
    start: draft?.start ?? stored.start,
    end: draft?.end ?? stored.end,
    touched: draft?.touched ?? false,
  };
}

export function boundsOf(clip: ClipDto): Bounds {
  return { startMs: clip.start_ms, endMs: clip.end_ms };
}

function sameBounds(a: Bounds, b: Bounds): boolean {
  return a.startMs === b.startMs && a.endMs === b.endMs;
}

/** The editable fields of `clips.patch`; absent ones are left as they are by the backend. */
export interface PatchBody {
  readonly title?: string;
  readonly start_ms?: number;
  readonly end_ms?: number;
}

export interface PatchPlan {
  /** What differs from the stored clip and is valid; empty when there is nothing to send. */
  readonly body: PatchBody;
  readonly titleError: string | null;
  readonly intervalError: string | null;
}

/**
 * What to send for the fields as typed. Invalid parts are reported and left out, so a good
 * title is still saved next to a boundary that is being typed.
 */
export function planPatch(clip: ClipDto, fields: ClipFields, durationMs: number): PatchPlan {
  let body: PatchBody = {};
  let titleError: string | null = null;
  const title = fields.title.trim();
  if (title === '') {
    titleError = 'Назва не може бути порожньою';
  } else if (title !== clip.title) {
    body = { ...body, title };
  }

  let intervalError: string | null = null;
  const interval = checkInterval(fields.start, fields.end, durationMs);
  if (!interval.ok) {
    intervalError = interval.message;
  } else if (interval.startMs !== clip.start_ms || interval.endMs !== clip.end_ms) {
    body = { ...body, start_ms: interval.startMs, end_ms: interval.endMs };
  }
  return { body, titleError, intervalError };
}

export function hasChanges(plan: PatchPlan): boolean {
  return Object.keys(plan.body).length > 0;
}

/** Nothing left to send and nothing wrong: the fields say what the clip says. */
export function isSettled(plan: PatchPlan): boolean {
  return !hasChanges(plan) && plan.titleError === null && plan.intervalError === null;
}

/**
 * The undo stack of one clip after a patch moved its boundaries from `before` to `after`.
 * Returning to the boundaries on top of the stack is an undo and removes them; any other
 * change keeps the old boundaries as the next thing to go back to.
 */
export function nextUndoStack(
  stack: readonly Bounds[],
  before: Bounds,
  after: Bounds,
): readonly Bounds[] {
  if (sameBounds(before, after)) {
    return stack;
  }
  const top = stack.at(-1);
  if (top !== undefined && sameBounds(top, after)) {
    return stack.slice(0, -1);
  }
  return [...stack, before].slice(-UNDO_LIMIT);
}

/**
 * The clip «Наступний» moves to: the next one after the active clip, in list order, that is
 * still to be reviewed. It wraps around; null when no other clip is waiting for review.
 */
export function nextClipToReview(clips: readonly ClipDto[], activeId: string | null): ClipDto | null {
  const from = activeId === null ? -1 : clips.findIndex((clip) => clip.id === activeId);
  for (let step = 1; step <= clips.length; step += 1) {
    const clip = clips[(from + step) % clips.length];
    if (clip?.review_status === 'suggested' && clip.id !== activeId) {
      return clip;
    }
  }
  return null;
}
