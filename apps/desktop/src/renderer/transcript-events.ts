import { useEffect, useRef } from 'react';

/**
 * The renderer's own news that the transcript of a source has a new revision (ADR 041 §23(10)): a
 * subtitle import succeeded, or a recognition job of the source closed. The listeners live in this
 * renderer, so there is no IPC; an event for one source never reaches a listener of another.
 */

type Listener = () => void;

/** The listeners by source id. A source without any has no entry, so the map keeps no old sources. */
const listeners = new Map<string, Set<Listener>>();

/** Tells every listener of the source that its transcript changed. A source without listeners is a no-op. */
export function notifyTranscriptChanged(sourceId: string): void {
  const set = listeners.get(sourceId);
  if (set === undefined) {
    return;
  }
  // A copy, and a check before each call: a listener may be removed while the event is delivered.
  for (const listener of [...set]) {
    if (set.has(listener)) {
      listener();
    }
  }
}

function subscribe(sourceId: string, listener: Listener): () => void {
  let set = listeners.get(sourceId);
  if (set === undefined) {
    set = new Set();
    listeners.set(sourceId, set);
  }
  set.add(listener);
  return () => {
    const current = listeners.get(sourceId);
    current?.delete(listener);
    if (current?.size === 0) {
      listeners.delete(sourceId);
    }
  };
}

/**
 * Calls `onChanged` whenever the transcript of `sourceId` changes, for as long as the component is
 * mounted. The latest `onChanged` is the one called. Following another source moves the listener to it.
 */
export function useTranscriptChanged(sourceId: string, onChanged: () => void): void {
  const latest = useRef(onChanged);
  useEffect(() => {
    latest.current = onChanged;
  });
  useEffect(() => {
    return subscribe(sourceId, () => {
      latest.current();
    });
  }, [sourceId]);
}
