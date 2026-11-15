import { describe, expect, it } from 'vitest';

import type { ClipDto } from '../shared/api';
import {
  NO_DRAFT,
  UNDO_LIMIT,
  fieldsOf,
  hasChanges,
  isSettled,
  nextClipToReview,
  nextUndoStack,
  planPatch,
  viewOf,
} from './clip-edit-logic';
import { makeClip } from './test-bridge';

const DURATION = 12_000;

function clipAt(id: string, review: string): ClipDto {
  return makeClip({ id, review_status: review });
}

describe('viewOf', () => {
  it('shows the stored clip when nothing was edited', () => {
    expect(viewOf(makeClip(), undefined)).toEqual({
      title: 'Мій фрагмент',
      start: '00:00:04.120',
      end: '00:00:07.880',
      touched: false,
    });
    expect(viewOf(makeClip(), NO_DRAFT)).toEqual(viewOf(makeClip(), undefined));
  });

  it('lays only the edited fields over the stored clip', () => {
    expect(viewOf(makeClip(), { end: '00:00:09.000', touched: true })).toEqual({
      title: 'Мій фрагмент',
      start: '00:00:04.120',
      end: '00:00:09.000',
      touched: true,
    });
  });
});

describe('planPatch', () => {
  it('sends nothing when the fields say what the clip says', () => {
    const plan = planPatch(makeClip(), fieldsOf(makeClip()), DURATION);

    expect(plan).toEqual({ body: {}, titleError: null, intervalError: null });
    expect(hasChanges(plan)).toBe(false);
    expect(isSettled(plan)).toBe(true);
  });

  it('sends a trimmed title that differs', () => {
    const plan = planPatch(makeClip(), { ...fieldsOf(makeClip()), title: '  Нова назва  ' }, DURATION);

    expect(plan.body).toEqual({ title: 'Нова назва' });
  });

  it('treats a title that only differs by spaces as no change', () => {
    const plan = planPatch(makeClip(), { ...fieldsOf(makeClip()), title: ' Мій фрагмент ' }, DURATION);

    expect(isSettled(plan)).toBe(true);
  });

  it('sends both boundaries when one of them changed', () => {
    const plan = planPatch(makeClip(), { ...fieldsOf(makeClip()), end: '00:00:09.000' }, DURATION);

    expect(plan.body).toEqual({ start_ms: 4120, end_ms: 9000 });
  });

  it('reports an empty title and still sends a good boundary', () => {
    const plan = planPatch(makeClip(), { title: '   ', start: '00:00:05.000', end: '00:00:07.880' }, DURATION);

    expect(plan.titleError).toBe('Назва не може бути порожньою');
    expect(plan.body).toEqual({ start_ms: 5000, end_ms: 7880 });
    expect(isSettled(plan)).toBe(false);
  });

  it('reports a bad interval and still sends a good title', () => {
    const plan = planPatch(
      makeClip(),
      { title: 'Інша', start: '00:00:08.000', end: '00:00:07.880' },
      DURATION,
    );

    expect(plan.intervalError).toBe('Кінець має бути пізніше за початок');
    expect(plan.body).toEqual({ title: 'Інша' });
  });

  it('refuses an end after the end of the source', () => {
    const plan = planPatch(makeClip(), { ...fieldsOf(makeClip()), end: '00:00:12.001' }, DURATION);

    expect(plan.intervalError).toContain('00:00:12.000');
    expect(plan.body).toEqual({});
  });
});

describe('nextUndoStack', () => {
  const first = { startMs: 1000, endMs: 2000 };
  const second = { startMs: 1500, endMs: 2500 };
  const third = { startMs: 1500, endMs: 3000 };

  it('remembers the boundaries a change left behind', () => {
    expect(nextUndoStack([], first, second)).toEqual([first]);
    expect(nextUndoStack([first], second, third)).toEqual([first, second]);
  });

  it('removes them when the change goes back to them', () => {
    expect(nextUndoStack([first, second], third, second)).toEqual([first]);
    expect(nextUndoStack([first], second, first)).toEqual([]);
  });

  it('ignores a patch that did not move the boundaries', () => {
    expect(nextUndoStack([first], second, second)).toEqual([first]);
  });

  it('keeps the latest changes only', () => {
    let stack: readonly { startMs: number; endMs: number }[] = [];
    for (let index = 0; index < UNDO_LIMIT + 5; index += 1) {
      stack = nextUndoStack(stack, { startMs: index, endMs: index + 1000 }, { startMs: index + 1, endMs: index + 1001 });
    }

    expect(stack).toHaveLength(UNDO_LIMIT);
    expect(stack.at(-1)).toEqual({ startMs: UNDO_LIMIT + 4, endMs: UNDO_LIMIT + 1004 });
  });
});

describe('nextClipToReview', () => {
  const a = clipAt('a', 'suggested');
  const b = clipAt('b', 'approved');
  const c = clipAt('c', 'suggested');
  const d = clipAt('d', 'rejected');
  const e = clipAt('e', 'suggested');

  it('is the first clip to review when none is active', () => {
    expect(nextClipToReview([b, c, a], null)?.id).toBe('c');
  });

  it('is the next clip after the active one that is still to be reviewed, in list order', () => {
    expect(nextClipToReview([a, b, c, d, e], 'a')?.id).toBe('c');
    expect(nextClipToReview([a, b, c, d, e], 'c')?.id).toBe('e');
  });

  it('wraps around to the start of the list', () => {
    expect(nextClipToReview([a, b, c, d, e], 'e')?.id).toBe('a');
  });

  it('starts after an active clip that is itself reviewed already', () => {
    expect(nextClipToReview([a, b, c, d, e], 'b')?.id).toBe('c');
    expect(nextClipToReview([a, b, c, d, e], 'd')?.id).toBe('e');
  });

  it('is null when no other clip is waiting', () => {
    expect(nextClipToReview([a, b, d], 'a')).toBeNull();
    expect(nextClipToReview([b, d], 'b')).toBeNull();
    expect(nextClipToReview([], null)).toBeNull();
  });

  it('is the first clip to review when the active one is not in the list any more', () => {
    expect(nextClipToReview([b, c], 'gone')?.id).toBe('c');
  });
});
