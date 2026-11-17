import { useEffect, useRef, useState } from 'react';

import type { ApiParams, JobDto, TranscriptionRunDto } from '../shared/api';
import type { ApiErrorInfo, ApiResult } from '../shared/bridge';
import { callApi } from './bridge-calls';
import { notifyTranscriptChanged } from './transcript-events';
import { deriveView, isJobOpen, openStage } from './transcription-logic';
import type { Snapshot, TranscriptionView } from './transcription-logic';
import { useViewActive } from './view-active';

/** How often a run that is going is asked again (ADR 038). */
export const TRANSCRIPTION_POLL_MS = 1000;

/** Consecutive failed polls after which the block says it lost the run. */
const MAX_POLL_MISSES = 5;

interface Failure {
  readonly key: string;
  readonly error: ApiErrorInfo;
}

/**
 * One request to read the block. Each one is a new object, so an answer can say which request it
 * answers: while the newest request is unanswered, the block is reading and sends no paid action.
 */
interface ReadRequest {
  /**
   * The refusal that was on screen when the block came back into view. The read answers it: when
   * that read succeeds, the refusal is cleared (ADR 038, доповнення 2). Null for any other read.
   */
  readonly clears: Failure | null;
}

interface Stored {
  /** The source and track the snapshot belongs to: an answer for another one is never shown. */
  readonly key: string;
  /** The request this snapshot answers. */
  readonly request: ReadRequest;
  readonly snapshot: Snapshot;
}

/**
 * Reads what the block shows: the newest run of the source (the list restores it after a restart
 * or a visit to another tab), its parts and job, the estimate for the chosen track and whether the
 * backend holds a key. Nothing is created here.
 */
async function loadSnapshot(sourceId: string, audioIndex: number): Promise<Snapshot> {
  const [list, estimate, settings] = await Promise.all([
    callApi('transcription.list', { source_id: sourceId }),
    callApi('transcription.estimate', { source_id: sourceId, audio_stream_index: audioIndex }),
    callApi('settings.get', {}),
  ]);
  if (!list.ok) {
    return { ok: false, error: list.error };
  }
  const hasKey = settings.ok ? settings.data.has_api_key : null;
  const budgetUsd = settings.ok ? settings.data.run_budget_usd : null;
  const newest = list.data.items[0];
  if (newest === undefined || openStage(newest.status) === null) {
    return { ok: true, estimate, hasKey, budgetUsd, run: null };
  }
  const detail = await callApi('transcription.get', { id: newest.id });
  if (!detail.ok) {
    return { ok: false, error: detail.error };
  }
  const read = detail.data.job_id === null ? null : await callApi('jobs.get', { id: detail.data.job_id });
  return {
    ok: true,
    estimate,
    hasKey,
    budgetUsd,
    run: { run: detail.data, job: read?.ok === true ? read.data : null },
  };
}

/** The stored snapshot with a fresher run (and job) in it; unchanged when it is no longer about a run. */
function withRun(
  stored: Stored | null,
  key: string,
  run: TranscriptionRunDto,
  job: JobDto | undefined,
): Stored | null {
  if (stored?.key !== key || !stored.snapshot.ok || stored.snapshot.run === null) {
    return stored;
  }
  const kept = stored.snapshot.run;
  return { ...stored, snapshot: { ...stored.snapshot, run: { run, job: job ?? kept.job } } };
}

export interface Transcription {
  readonly view: TranscriptionView;
  /**
   * A start, a resume or a read of the block is on its way, or its effect is not read yet: the buttons
   * that send a paid action stay off meanwhile.
   */
  readonly busy: boolean;
  /** Why the last start, resume or cancel was refused; null when nothing was. */
  readonly error: ApiErrorInfo | null;
  /** Starts a run; `force` confirms a second, paid run over a transcript that already exists. */
  readonly start: (force: boolean) => void;
  readonly cancel: () => void;
  /** Continues the run; `retryUnknown` is the user's confirmed decision to send unanswered parts again. */
  readonly resume: (retryUnknown: boolean) => void;
  /**
   * Continues a run that stopped on its ceiling, with the given one (a decimal string, as in the settings).
   * `retryUnknown` is the user's confirmed resend of unknown parts under that ceiling (ADR 038, доповнення 3).
   */
  readonly resumeWithBudget: (budgetUsd: string, retryUnknown: boolean) => void;
  /** Shows the start summary for a new run in place of the stopped one. Sends nothing. */
  readonly restart: () => void;
  /** Reads everything again. */
  readonly reload: () => void;
}

/**
 * The speech-recognition block of one source and audio track (ADR 038). It reads when it appears,
 * every time the view becomes active again (the settings may have changed the key, the model, the
 * language or the ceiling meanwhile) and every time the source or the track changes. It asks about a
 * running run once a second, and does nothing while the view is hidden. A run is only ever started
 * by `start`, never by reading, and no paid action is sent while the block is reading or while the
 * file is not available (`fileReady` false: missing or changed).
 */
export function useTranscription(sourceId: string, audioIndex: number, fileReady: boolean): Transcription {
  const active = useViewActive();
  const key = `${sourceId}|${String(audioIndex)}`;
  const [stored, setStored] = useState<Stored | null>(null);
  const [request, setRequest] = useState<ReadRequest>({ clears: null });
  // What the last render showed. A change of the view or of the source asks for a read first.
  const [shown, setShown] = useState({ active, key });
  const [cancelRequestedFor, setCancelRequestedFor] = useState<string | null>(null);
  // The source and track whose paid action is on its way. Another one is not held up by it.
  const [sendingKey, setSendingKey] = useState<string | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);
  // The run the user chose to start over: the block shows the summary for a new run instead of it.
  const [restartedFor, setRestartedFor] = useState<string | null>(null);
  // The key on screen; null once the block is gone. An answer that comes after that is not used.
  const onScreen = useRef<string | null>(null);
  // The key of a paid action on its way, read at once by its handler: a second call sends nothing.
  const sending = useRef<string | null>(null);

  if (shown.active !== active || shown.key !== key) {
    setShown({ active, key });
    if (active) {
      // Back on screen, or on another source: the state is read before anything can be sent. A
      // refusal that was on screen before the block went away is settled by that read (ADR 038, доповнення 2).
      setRequest({ clears: shown.active ? null : failure });
    }
  }

  const pending = stored?.key !== key || stored.request !== request;
  const busy = sendingKey === key || pending;
  const view = deriveView(stored?.key === key ? stored.snapshot : null, cancelRequestedFor, restartedFor);
  const watchedRunId = view.kind === 'run' && view.stage === 'active' ? view.run.id : null;

  useEffect(() => {
    onScreen.current = key;
    return () => {
      onScreen.current = null;
    };
  }, [key]);

  useEffect(() => {
    if (!active) {
      return undefined;
    }
    // An object, not a bare boolean: the cleanup flips it while the read below awaits.
    const load = { cancelled: false };
    void loadSnapshot(sourceId, audioIndex).then((snapshot) => {
      if (load.cancelled) {
        return;
      }
      setStored({ key, request, snapshot });
      if (request.clears !== null && deriveView(snapshot, null).kind !== 'failed') {
        const cleared = request.clears;
        setFailure((current) => (current === cleared ? null : current));
      }
    });
    return () => {
      load.cancelled = true;
    };
  }, [active, key, sourceId, audioIndex, request]);

  // The polls are not tied to the reads: a read that follows a terminal state does not restart them.
  useEffect(() => {
    if (!active || watchedRunId === null) {
      return undefined;
    }
    const runId = watchedRunId;
    // A call, not the property: TypeScript keeps a property narrowed to false across an await.
    const watch = { stopped: false };
    const stopped = (): boolean => watch.stopped;
    let timer: number | undefined;

    function schedule(misses: number): void {
      timer = window.setTimeout(() => {
        void poll(misses);
      }, TRANSCRIPTION_POLL_MS);
    }

    /** A read that failed, or could not tell whether the job is still open, counts as a miss. */
    function miss(misses: number, error: ApiErrorInfo): void {
      if (misses + 1 < MAX_POLL_MISSES) {
        schedule(misses + 1);
      } else {
        setStored((current) => (current?.key === key ? { ...current, snapshot: { ok: false, error } } : current));
      }
    }

    async function poll(misses: number): Promise<void> {
      const result = await callApi('transcription.get', { id: runId });
      if (stopped()) {
        return;
      }
      if (!result.ok) {
        miss(misses, result.error);
        return;
      }
      const run = result.data;
      if (openStage(run.status) === 'active') {
        setStored((current) => withRun(current, key, run, undefined));
        schedule(0);
        return;
      }
      // The run says it has stopped, or that it completed. Its job tells whether the work is over: a job
      // still open keeps the block watching, whatever the run says (a resumed job is queued first).
      const read = run.job_id === null ? null : await callApi('jobs.get', { id: run.job_id });
      if (stopped()) {
        return;
      }
      if (read !== null && !read.ok) {
        miss(misses, read.error);
        return;
      }
      if (read?.ok === true && isJobOpen(read.data.state)) {
        const job = read.data;
        setStored((current) => withRun(current, key, run, job));
        schedule(0);
        return;
      }
      // Over: the job has closed, so the transcript may have a new revision and the panels that read it
      // are told (ADR 041 §23(10)). This watch sends no poll after this, so the event comes once per close.
      if (read?.ok === true) {
        notifyTranscriptChanged(sourceId);
      }
      // The block reads its state once more and schedules no poll after this.
      setRequest({ clears: null });
    }

    schedule(0);
    return () => {
      watch.stopped = true;
      window.clearTimeout(timer);
    };
  }, [active, key, sourceId, watchedRunId]);

  /** Reads the state again: the runs, the estimate and the settings. */
  const requestRead = (): void => {
    setRequest({ clears: null });
  };

  /**
   * A refused start or resume, whatever the reason (ADR 038, доповнення 2): the message stays, and
   * settings, estimate and runs are read again before the next paid action is allowed. An ambiguous
   * failure (a timeout) may have been paid for, so nothing is sent again by itself.
   */
  const refuse = (origin: string, error: ApiErrorInfo): void => {
    setFailure({ key: origin, error });
    requestRead();
  };

  /**
   * Sends one paid action for the source and track it was started on. A second call while it is on
   * its way, or while the block is still reading, sends nothing. Its answer is used only while that
   * source and track are on screen: after a switch it is not what the block shows.
   */
  const send = (origin: string, call: () => Promise<ApiResult<unknown>>, accepted: () => void): void => {
    if (!fileReady || busy || sending.current === origin) {
      return;
    }
    sending.current = origin;
    setSendingKey(origin);
    setFailure(null);
    void call().then((result) => {
      if (sending.current === origin) {
        sending.current = null;
      }
      if (onScreen.current === null) {
        return;
      }
      setSendingKey((current) => (current === origin ? null : current));
      if (onScreen.current !== origin) {
        return;
      }
      if (result.ok) {
        accepted();
      } else {
        refuse(origin, result.error);
      }
    });
  };

  const start = (force: boolean): void => {
    const params = { source_id: sourceId, audio_stream_index: audioIndex };
    send(key, () => callApi('transcription.create', force ? { ...params, force: true } : params), requestRead);
  };

  const cancel = (): void => {
    if (view.kind !== 'run' || view.stage !== 'active' || view.cancelling || view.run.job_id === null) {
      return;
    }
    const origin = key;
    const runId = view.run.id;
    const jobId = view.run.job_id;
    setCancelRequestedFor(runId);
    setFailure(null);
    // A refused cancel (the job has just ended) is settled by the next poll; the message stays.
    void callApi('jobs.cancel', { id: jobId }).then((result) => {
      if (!result.ok && onScreen.current === origin) {
        setCancelRequestedFor(null);
        setFailure({ key: origin, error: result.error });
      }
    });
  };

  /** Sends the resume of the stopped run's job; `extra` holds only what this resume changes. */
  const sendResume = (extra: Omit<ApiParams['jobs.resume'], 'id'>): void => {
    if (view.kind !== 'run' || view.stage === 'active' || view.run.job_id === null) {
      return;
    }
    const jobId = view.run.job_id;
    send(key, () => callApi('jobs.resume', { id: jobId, ...extra }), () => {
      setCancelRequestedFor(null);
      requestRead();
    });
  };

  const resume = (retryUnknown: boolean): void => {
    sendResume(retryUnknown ? { retry_unknown: true } : {});
  };

  const resumeWithBudget = (budgetUsd: string, retryUnknown: boolean): void => {
    sendResume(retryUnknown ? { budget_usd: budgetUsd, retry_unknown: true } : { budget_usd: budgetUsd });
  };

  const restart = (): void => {
    if (busy || view.kind !== 'run' || view.stage === 'active') {
      return;
    }
    // Nothing is sent: the summary is shown again with a fresh estimate, and the start stays off until it is read.
    setRestartedFor(view.run.id);
    setFailure(null);
    requestRead();
  };

  return {
    view,
    busy,
    error: failure?.key === key ? failure.error : null,
    start,
    cancel,
    resume,
    resumeWithBudget,
    restart,
    reload: requestRead,
  };
}
