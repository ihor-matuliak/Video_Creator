import { useCallback, useRef } from 'react';

/** Runs `job` once every earlier job of the same clip has finished. */
export type Enqueue = (clipId: string, job: () => Promise<void>) => void;

/**
 * Changes of one clip go to the backend one after another: each carries the revision the
 * previous one produced, so the user's own quick edits and a review never collide (409).
 * A job reads the latest state when it starts, not when it was asked for; a job that finds
 * its clip idle starts at once, within the call that asked for it.
 */
export function useClipQueue(): Enqueue {
  // The last job of each clip that has not finished yet.
  const tails = useRef(new Map<string, Promise<void>>());
  return useCallback<Enqueue>((clipId, job) => {
    const previous = tails.current.get(clipId);
    // Jobs report their failures through state; the second argument only keeps the chain going.
    const run = previous === undefined ? job() : previous.then(job, job);
    const tail = run.finally(() => {
      if (tails.current.get(clipId) === tail) {
        tails.current.delete(clipId);
      }
    });
    tails.current.set(clipId, tail);
  }, []);
}
