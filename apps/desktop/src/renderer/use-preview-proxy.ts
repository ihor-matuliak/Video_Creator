import { useEffect, useState } from 'react';

import { callApi } from './bridge-calls';

/** How often a preview that is still being prepared is asked again (ADR 021). */
export const PREVIEW_POLL_MS = 1000;

/** Consecutive failed polls after which the preview is reported as failed. */
const MAX_POLL_MISSES = 5;

export type PreviewState =
  | { readonly kind: 'idle' }
  | {
      readonly kind: 'preparing';
      /** Null until the backend has answered `previews.create`. */
      readonly jobId: string | null;
      /** 0-100 when the backend reports it; null means unknown. */
      readonly percent: number | null;
      readonly cancelling: boolean;
    }
  | { readonly kind: 'ready'; readonly previewId: string }
  | { readonly kind: 'failed'; readonly message: string }
  | { readonly kind: 'cancelled' };

const IDLE: PreviewState = { kind: 'idle' };
const STARTING: PreviewState = { kind: 'preparing', jobId: null, percent: null, cancelling: false };

export interface PreviewProxy {
  readonly state: PreviewState;
  /** Asks the backend to stop the job of the preview being prepared. */
  readonly cancel: () => void;
  /** Starts again after a failure or a cancellation. */
  readonly retry: () => void;
}

function percentOf(progress: Readonly<Record<string, unknown>>): number | null {
  const { percent } = progress;
  return typeof percent === 'number' && Number.isFinite(percent)
    ? Math.min(100, Math.max(0, Math.round(percent)))
    : null;
}

/**
 * The preview proxy of one source and audio stream (ADR 021): created when
 * `active`, then asked about once a second, and only while the page is visible.
 * Nothing runs while the original is played directly, so leaving the tab or
 * switching back to the direct player stops the polling.
 */
export function usePreviewProxy(
  sourceId: string,
  audioIndex: number | null,
  active: boolean,
): PreviewProxy {
  const [attempt, setAttempt] = useState(0);
  const key = `${sourceId}|${audioIndex === null ? 'none' : String(audioIndex)}|${String(attempt)}`;
  // The answer is stored with the request it belongs to, so that a stale answer of
  // another stream or attempt is never shown and no state is reset inside an effect.
  const [stored, setStored] = useState<{ readonly key: string; readonly state: PreviewState }>({
    key: '',
    state: STARTING,
  });
  const [cancelRequestedFor, setCancelRequestedFor] = useState<string | null>(null);

  useEffect(() => {
    if (!active) {
      return undefined;
    }
    let stopped = false;
    let timer: number | undefined;
    const put = (state: PreviewState): void => {
      if (!stopped) {
        setStored({ key, state });
      }
    };

    const poll = (previewId: string, jobId: string, misses: number): void => {
      timer = window.setTimeout(() => {
        if (document.visibilityState === 'hidden') {
          poll(previewId, jobId, misses);
          return;
        }
        void (async () => {
          const preview = await callApi('previews.get', { id: previewId });
          if (stopped) {
            return;
          }
          if (!preview.ok) {
            if (preview.error.retryable && misses + 1 < MAX_POLL_MISSES) {
              poll(previewId, jobId, misses + 1);
            } else {
              put({ kind: 'failed', message: preview.error.message });
            }
            return;
          }
          const { status, error } = preview.data;
          if (status === 'ready') {
            put({ kind: 'ready', previewId });
          } else if (status === 'cancelled') {
            put({ kind: 'cancelled' });
          } else if (status === 'failed' || status === 'interrupted') {
            put({
              kind: 'failed',
              message: error?.message ?? 'Підготовку перервано. Спробуйте ще раз',
            });
          } else {
            // queued or running: take the percent from the job, when it says one.
            const job = await callApi('jobs.get', { id: jobId });
            put({
              kind: 'preparing',
              jobId,
              percent: job.ok ? percentOf(job.data.progress) : null,
              cancelling: false,
            });
            poll(previewId, jobId, 0);
          }
        })();
      }, PREVIEW_POLL_MS);
    };

    void callApi('previews.create', { source_id: sourceId, audio_stream_index: audioIndex }).then(
      (created) => {
        if (stopped) {
          return;
        }
        if (!created.ok) {
          put({ kind: 'failed', message: created.error.message });
        } else if (created.data.status === 'ready') {
          put({ kind: 'ready', previewId: created.data.id });
        } else {
          put({ kind: 'preparing', jobId: created.data.job_id, percent: null, cancelling: false });
          poll(created.data.id, created.data.job_id, 0);
        }
      },
    );

    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [active, key, sourceId, audioIndex]);

  let state: PreviewState = IDLE;
  if (active) {
    state = stored.key === key ? stored.state : STARTING;
    if (state.kind === 'preparing' && cancelRequestedFor === key) {
      state = { ...state, cancelling: true };
    }
  }

  const cancel = (): void => {
    if (state.kind !== 'preparing' || state.jobId === null || state.cancelling) {
      return;
    }
    setCancelRequestedFor(key);
    // A refused cancel (the job just finished) is settled by the next poll.
    void callApi('jobs.cancel', { id: state.jobId }).then((result) => {
      if (!result.ok) {
        setCancelRequestedFor(null);
      }
    });
  };

  const retry = (): void => {
    setAttempt((value) => value + 1);
  };

  return { state, cancel, retry };
}
