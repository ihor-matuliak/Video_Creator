import { renderHook } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { notifyTranscriptChanged, useTranscriptChanged } from './transcript-events';

const SOURCE_A = 'source-a';
const SOURCE_B = 'source-b';

describe('notifyTranscriptChanged and useTranscriptChanged (ADR 041 §23(10))', () => {
  it('calls the listener of the source that changed, and not the one of another source', () => {
    const heardA = vi.fn();
    const heardB = vi.fn();
    renderHook(() => {
      useTranscriptChanged(SOURCE_A, heardA);
    });
    renderHook(() => {
      useTranscriptChanged(SOURCE_B, heardB);
    });

    notifyTranscriptChanged(SOURCE_A);

    expect(heardA).toHaveBeenCalledTimes(1);
    expect(heardB).not.toHaveBeenCalled();
  });

  it('does nothing for a source that nobody listens to', () => {
    const heard = vi.fn();
    renderHook(() => {
      useTranscriptChanged(SOURCE_A, heard);
    });

    expect(() => {
      notifyTranscriptChanged('no-listeners');
    }).not.toThrow();
    expect(heard).not.toHaveBeenCalled();
  });

  it('stops calling the listener once its component is unmounted', () => {
    const heard = vi.fn();
    const hook = renderHook(() => {
      useTranscriptChanged(SOURCE_A, heard);
    });

    notifyTranscriptChanged(SOURCE_A);
    hook.unmount();
    notifyTranscriptChanged(SOURCE_A);

    expect(heard).toHaveBeenCalledTimes(1);
  });

  it('keeps the other listeners of the same source when one of them is removed', () => {
    const stays = vi.fn();
    const leaves = vi.fn();
    renderHook(() => {
      useTranscriptChanged(SOURCE_A, stays);
    });
    const leaving = renderHook(() => {
      useTranscriptChanged(SOURCE_A, leaves);
    });

    leaving.unmount();
    notifyTranscriptChanged(SOURCE_A);

    expect(stays).toHaveBeenCalledTimes(1);
    expect(leaves).not.toHaveBeenCalled();
  });

  it('does not call a listener that an earlier listener removed during the same event', () => {
    const holder: { later?: { readonly unmount: () => void } } = {};
    renderHook(() => {
      useTranscriptChanged(SOURCE_A, () => {
        holder.later?.unmount();
      });
    });
    const later = vi.fn();
    holder.later = renderHook(() => {
      useTranscriptChanged(SOURCE_A, later);
    });

    notifyTranscriptChanged(SOURCE_A);

    expect(later).not.toHaveBeenCalled();
  });

  it('calls the latest callback, not the one of the first render', () => {
    const first = vi.fn();
    const second = vi.fn();
    const hook = renderHook(
      ({ onChanged }: { readonly onChanged: () => void }) => {
        useTranscriptChanged(SOURCE_A, onChanged);
      },
      { initialProps: { onChanged: first } },
    );

    hook.rerender({ onChanged: second });
    notifyTranscriptChanged(SOURCE_A);

    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
  });

  it('follows the source it is given: the old source no longer reaches it', () => {
    const heard = vi.fn();
    const hook = renderHook(
      ({ sourceId }: { readonly sourceId: string }) => {
        useTranscriptChanged(sourceId, heard);
      },
      { initialProps: { sourceId: SOURCE_A } },
    );

    hook.rerender({ sourceId: SOURCE_B });
    notifyTranscriptChanged(SOURCE_A);
    expect(heard).not.toHaveBeenCalled();

    notifyTranscriptChanged(SOURCE_B);
    expect(heard).toHaveBeenCalledTimes(1);
  });
});
