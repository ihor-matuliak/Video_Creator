import { useCallback, useEffect, useRef, useState } from 'react';

import type { TranscriptCoverageSpanDto, TranscriptSegmentDto, TranscriptWordDto } from '../shared/api';
import type { ApiErrorInfo } from '../shared/bridge';
import { callApi } from './bridge-calls';

/** Segments asked for in one window of `transcripts.get` (the backend's largest window, ADR 039). */
const PAGE_SIZE = 1000;
/** A guard against a backend that keeps returning a cursor or an offset: 500 windows cover its 400 000 items. */
const MAX_PAGES = 500;

const TOO_MANY_PAGES: ApiErrorInfo = {
  code: 'TRANSCRIPT_PAGES_EXCEEDED',
  message: 'Сервер повертає забагато частин транскрипту. Спробуйте ще раз',
  retryable: true,
};

/** The newest revision of a source, with all of its segments, words and coverage. */
export interface LoadedTranscript {
  /** The revision's own ID: a correction names it (ADR 036). */
  readonly id: string;
  readonly revision: number;
  /** word or segment: whether the words of the revision carry the timing. */
  readonly timingLevel: string;
  readonly segments: readonly TranscriptSegmentDto[];
  /** Every word of the revision, from all of its windows; empty for a segment-level revision. */
  readonly words: readonly TranscriptWordDto[];
  readonly coverage: readonly TranscriptCoverageSpanDto[];
}

export type TranscriptState =
  | { readonly kind: 'loading' }
  | { readonly kind: 'error'; readonly error: ApiErrorInfo }
  /** The source has no transcript revision at all. */
  | { readonly kind: 'none' }
  | { readonly kind: 'ready'; readonly transcript: LoadedTranscript };

const LOADING: TranscriptState = { kind: 'loading' };

/** Ends the reads that are still out: once `cancelled`, an answer is not used. */
interface Run {
  cancelled: boolean;
}

/** The source on screen and the run of its reads. */
interface Lifetime {
  readonly source: string;
  readonly run: Run;
}

/**
 * The newest revision and all of its segments. The revisions are listed oldest first, so the newest
 * is the last item of the last page. Null when `run` was cancelled meanwhile.
 */
async function loadTranscript(sourceId: string, run: Run): Promise<TranscriptState | null> {
  let newestId: string | null = null;
  let cursor: string | undefined;
  for (let page = 0; page < MAX_PAGES; page += 1) {
    const result = await callApi('transcripts.list', {
      source_id: sourceId,
      ...(cursor === undefined ? {} : { cursor }),
    });
    if (run.cancelled) {
      return null;
    }
    if (!result.ok) {
      return { kind: 'error', error: result.error };
    }
    newestId = result.data.items.at(-1)?.id ?? newestId;
    if (result.data.next_cursor === null) {
      return newestId === null ? { kind: 'none' } : await loadSegments(newestId, run);
    }
    cursor = result.data.next_cursor;
  }
  return { kind: 'error', error: TOO_MANY_PAGES };
}

/** What every window of one revision repeats: its own fields. */
interface Head {
  readonly id: string;
  readonly revision: number;
  readonly timingLevel: string;
  readonly coverage: readonly TranscriptCoverageSpanDto[];
}

/**
 * Every window of one revision, PAGE_SIZE items at a time: the segments and the words of each window
 * are kept together. Null when `run` was cancelled meanwhile.
 */
async function loadSegments(id: string, run: Run): Promise<TranscriptState | null> {
  const segments: TranscriptSegmentDto[] = [];
  const words: TranscriptWordDto[] = [];
  let head: Head | null = null;
  let offset = 0;
  for (let page = 0; page < MAX_PAGES; page += 1) {
    const result = await callApi('transcripts.get', { id, offset, limit: PAGE_SIZE });
    if (run.cancelled) {
      return null;
    }
    if (!result.ok) {
      return { kind: 'error', error: result.error };
    }
    head ??= {
      id: result.data.id,
      revision: result.data.revision,
      timingLevel: result.data.timing_level,
      coverage: result.data.coverage,
    };
    // The segments and the words are windowed on their own; next_offset goes on while either has more.
    segments.push(...result.data.segments);
    words.push(...result.data.words);
    if (result.data.next_offset === null) {
      return { kind: 'ready', transcript: { ...head, segments, words } };
    }
    offset = result.data.next_offset;
  }
  return { kind: 'error', error: TOO_MANY_PAGES };
}

export interface TranscriptLoad {
  readonly state: TranscriptState;
  /** Reads the transcript again (the way out of an error). */
  readonly reload: () => void;
  /**
   * Reads the newest revision again without the loading state: the one on screen stays until the new
   * one arrives. Resolves with the new transcript when it is shown; null when nothing is shown (the
   * read failed, which shows the error state, or it was cancelled).
   */
  readonly refresh: () => Promise<LoadedTranscript | null>;
}

interface Stored {
  /** The source the state belongs to: a state of another source is never shown. */
  readonly key: string;
  /** The read it answers: a reload makes the state loading again until its answer comes. */
  readonly attempt: number;
  readonly state: TranscriptState;
}

/**
 * The transcript of a source for the Моменти panel (ADR 039): the newest revision, read when the
 * source is shown, again by `reload`, and after a correction by `refresh`. An answer that comes after
 * the source changed or the panel is gone is dropped.
 */
export function useTranscript(sourceId: string): TranscriptLoad {
  const [attempt, setAttempt] = useState(0);
  const [stored, setStored] = useState<Stored | null>(null);
  // The source on screen and the run of its reads: a refresh that is still out ends with the source or the panel.
  const lifetime = useRef<Lifetime>({ source: sourceId, run: { cancelled: false } });

  useEffect(() => {
    const own: Run = { cancelled: false };
    lifetime.current = { source: sourceId, run: own };
    return () => {
      own.cancelled = true;
    };
  }, [sourceId]);

  useEffect(() => {
    // An object, not a bare boolean: the cleanup flips it while the reads below await.
    const run: Run = { cancelled: false };
    void loadTranscript(sourceId, run).then((state) => {
      if (state !== null) {
        setStored({ key: sourceId, attempt, state });
      }
    });
    return () => {
      run.cancelled = true;
    };
  }, [sourceId, attempt]);

  const refresh = useCallback(async (): Promise<LoadedTranscript | null> => {
    const live = lifetime.current;
    // Made for another source, before the panel switched to this one, or by a panel that is gone: it reads nothing.
    if (live.source !== sourceId || live.run.cancelled) {
      return null;
    }
    const state = await loadTranscript(sourceId, live.run);
    if (state === null) {
      return null;
    }
    setStored({ key: sourceId, attempt, state });
    return state.kind === 'ready' ? state.transcript : null;
  }, [sourceId, attempt]);

  const current =
    stored !== null && stored.key === sourceId && stored.attempt === attempt ? stored.state : LOADING;
  return {
    state: current,
    reload: () => {
      setAttempt((value) => value + 1);
    },
    refresh,
  };
}
